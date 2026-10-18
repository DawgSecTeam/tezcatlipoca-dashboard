"""Run execution: engine worktrees, driver processes, and stage-event tracking.

One thread per run. The engine stays standalone: a run gets a disposable worktree plus a
workspace comp dir, and the driver is the same CLI a human would type. Stage progress is
observed, never parsed out of the driver's stdout: the estate document's `last_phase` for
deploys (via `dashboard.estate`, which mirrors the engine's `estate.state.load_flat`), the
scrim harness's run.json phases otherwise."""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import importlib.util
from pathlib import Path

from dashboard import config, estate, nodes, ranges, workspaces

PHASE_NAMES = {
    1: "cleanup", 2: "engine_template", 3: "prepare_engine", 4: "golden_set",
    5: "firewall_bootstrap", 6: "repair_sweep", 7: "domains_and_final", 8: "seed",
}
FAIL_FAST_ACTIONS = ("deploy", "plan", "scrim")
# The one action with boundaries to hold at: verify/destroy/redeploy report a single stage,
# and a parked scrim would freeze its harness while red, blue and the clock keep running.
PAUSABLE_ACTIONS = ("deploy",)
# Actions whose driver reports no engine phase: they get ONE stage event, not the range's
# stale deploy phases (a 6-second teardown used to re-emit phase 1/8..8/8 in one second).
SINGLE_STEP_STAGES = {"verify": "verify", "destroy": "teardown", "redeploy": "redeploy",
                      "start": "competition start", "freeze": "competition freeze",
                      "resume": "competition resume", "end": "competition end",
                      "delete_team": "team removal"}
# Actions that act on the workspace an earlier run built (ownership and the estate document
# — which carries the run id, the secrets and the VM inventory — live there) instead of
# seeding their own.
RANGE_ACTIONS = ("verify", "destroy", "redeploy", "start", "freeze", "resume", "end",
                 "delete_team")

# Third-party packages the engine drivers import in-process. The service's own interpreter IS
# the drivers' interpreter, so a venv that predates a dependency fails deep inside a deploy
# (ModuleNotFoundError: toml, at phase 2, after minutes of work) instead of at submit —
# checked here so a stale venv costs a refusal, not a run.
DRIVER_DEPS = ("toml", "requests", "urllib3", "yaml", "dotenv", "PIL", "cryptography",
               "websocket", "paramiko", "mysql.connector")


def artifacts_root(run):
    """Where a run's test folders live: the workspace it acted on.

    A destroy run acts on a PRIOR run's workspace (see _destroy_target), so its own run dir
    holds no evidence — resolving through here keeps `get_artifacts` honest for both.
    """
    params = run.get("params") if isinstance(run.get("params"), dict) else {}
    ws = params.get("_destroy_comp_dir") or (Path(run["run_dir"]) / "comp")
    return Path(ws) / ".automated-tests"


class RunRefused(Exception):
    pass


def missing_driver_deps(spec=importlib.util.find_spec):
    """Third-party modules this interpreter cannot import but the drivers need."""
    missing = []
    for name in DRIVER_DEPS:
        try:
            if spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    return missing


def build_command(action, comp_id, ws, params, nodes_file=None):
    # The drivers an operator would type, with the workspace as the comp dir.
    py, ws = sys.executable, str(ws)
    extra = [str(a) for a in (params.get("extra_args") or [])]
    if action in ("deploy", "plan"):
        cmd = [py, "create-competition.py", "--comp-dir", ws, "--competition", comp_id,
               "--teams", str(params.get("teams") or 2), "--yes"]
        if nodes_file:
            cmd += ["--nodes", str(nodes_file)]
        for flag, key in (("--scoring-vmid", "scoring_vmid"),
                          ("--team-identifiers", "team_identifiers"),
                          ("--engine-mgmt-ip", "engine_mgmt_ip"),
                          ("--engine-node", "engine_node"),
                          ("--team-node", "team_node"), ("--from-phase", "from_phase")):
            if params.get(key) is not None:
                cmd += [flag, str(params[key])]
        if action == "plan":
            cmd.append("--plan-only")
        # Deploy and start are separate steps: the default ends the deploy with the
        # competition NOT started (engine paused, clock unstarted, injects deferred);
        # start_after_deploy buys the old one-shot behavior back.
        if not params.get("start_after_deploy"):
            cmd.append("--hold-start")
    elif action in ("start", "freeze", "resume", "end"):
        cmd = [py, "competition-ops.py", action, "--comp-dir", ws]
    elif action == "delete_team":
        if not params.get("team"):
            raise RunRefused("delete_team needs params.team (the team's key, e.g. team2)")
        cmd = [py, "destroy-competition.py", "--comp-dir", ws,
               "--team", str(params["team"]), "--yes"]
    elif action == "verify":
        cmd = [py, "verify-competition.py", ws]
    elif action == "destroy":
        cmd = [py, "destroy-competition.py", "--comp-dir", ws, "--yes"]
        for flag in ("--teams-only", "--keep-red", "--end-of-competition"):
            if params.get(flag.lstrip("-").replace("-", "_")):
                cmd.append(flag)
    elif action == "redeploy":
        cmd = [py, "redeploy-competition.py", "--comp-dir", ws, "--yes",
               "--mode", str(params.get("mode") or "reset")]
        if params.get("boxes"):
            cmd += ["--boxes", str(params["boxes"])]
        if params.get("teams"):
            cmd += ["--teams", str(params["teams"])]
    elif action == "scrim":
        cmd = [py, "run-agent-scrim.py", "--competition", comp_id, "--comp-dir", ws,
               "--teams", str(params.get("teams") or 2),
               "--duration-min", str(params.get("duration_min") or 90)]
        if nodes_file:
            cmd += ["--nodes", str(nodes_file)]
        for flag in ("--new", "--from-template", "--red-vmid", "--red-ip", "--red-model",
                     "--blue-model", "--llm-base-url", "--blue-base-url", "--keep-range",
                     "--blue-watchdog", "--skip-deploy", "--resume-event",
                     "--scoring-vmid", "--team-identifiers", "--engine-mgmt-ip"):
            val = params.get(flag.lstrip("-").replace("-", "_"))
            if val is True:
                cmd.append(flag)
            elif val not in (None, False):
                cmd += [flag, str(val)]
    else:
        raise RunRefused(f"unknown action {action!r}")
    return cmd + extra


class RunManager:
    def __init__(self, store, bus):
        self.store, self.bus = store, bus
        self.data = config.data_dir()
        self.runs_dir = self.data / "runs"
        self.engine_repo = self.data / "engine"
        self.comps_repo = self.data / "competitions"
        self._procs = {}  # run_id -> Popen
        self._lock = threading.Lock()

    # ---- startup reconciliation ----

    def reconcile_orphans(self):
        """Fail every non-terminal run this lifetime holds no process for.

        A restart loses every Popen, and `cancel` refuses a run it never spawned, so a row
        left `queued`/`running` by a dead worker is unmanageable forever — and a `queued`
        row is what makes its range board as `building` (2026-10-15 run-31072cb0, stuck six
        hours; its worker died unpacking a None record before the try block).
        """
        with self._lock:
            live = set(self._procs)
        orphans = [r["id"] for r in self.store.list_runs()
                   if r["state"] in ranges.IN_FLIGHT and r["id"] not in live]
        for run_id in orphans:
            self.store.update_run(run_id, state="failed", ended_at=time.time(),
                                  error="orphaned: no worker after service restart")
            self._emit(run_id, "state", data={"state": "failed", "orphaned": True})
        return orphans

    # ---- repo checkouts ----

    def ensure_repos(self):
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        if not (self.engine_repo / ".git").exists():
            subprocess.run(["git", "clone", config.engine_repo_url(), str(self.engine_repo)],
                           check=True, capture_output=True)
        if not (self.comps_repo / ".git").exists():
            subprocess.run(["git", "clone", config.competitions_repo_url(), str(self.comps_repo)],
                           check=True, capture_output=True)

    def refresh_comps(self):
        subprocess.run(["git", "-C", str(self.comps_repo), "pull", "--ff-only"],
                       capture_output=True)

    # ---- lifecycle ----

    def submit(self, comp_id, action, engine_ref=None, params=None):
        params = dict(params or {})
        # Internal workspace targeting is manager-owned, not a caller-supplied override.
        params.pop("_destroy_comp_dir", None)
        # No ref named -> the service's pinned default (config.default_engine_ref): v0.3.0
        # is the estate release, and a worktree cut at an older revision reads a state
        # shape this service no longer projects.
        engine_ref = (engine_ref or "").strip() or config.default_engine_ref()
        # Only deploy/plan/scrim take a team COUNT; a redeploy's `teams` is a selector
        # ("team2", "2" or the subnet identifier "102"), so int() there would 500.
        if params.get("teams") and action in ("deploy", "plan", "scrim"):
            params["teams"] = int(params["teams"])
        self.ensure_repos()
        self.refresh_comps()
        comp_src = self.comps_repo / comp_id
        if not (comp_src / "Compfile").exists():
            raise RunRefused(f"no such competition: {comp_id}")
        selected = [str(x) for x in (params.get("nodes") or [])]
        if selected:
            try:
                nodes.run_list(nodes.load(), selected)
            except ValueError as e:
                raise RunRefused(f"node list refused: {e}") from e
        missing = missing_driver_deps()
        if missing:
            raise RunRefused(
                f"the service interpreter ({sys.executable}) cannot import "
                f"{', '.join(missing)} — the engine drivers run under it, so a run would die "
                "inside the deploy. Install the service's requirements in its venv "
                "(.venv/bin/pip install -r requirements.txt) and submit again.")
        run_id = "run-" + uuid.uuid4().hex[:8]
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        # A scrim normally DEPLOYS its own range, so it seeds its own workspace — but
        # --skip-deploy/--resume-event run against the range an earlier deploy built, whose
        # secrets (teams.json) and ownership live in THAT workspace: seeding a fresh one made
        # the harness die immediately on a missing teams.json (live 2026-10-15, run-63fb7e14).
        resume_scope = action == "scrim" and (params.get("skip_deploy")
                                              or params.get("resume_event"))
        if action in RANGE_ACTIONS or resume_scope or params.get("comp_dir"):
            # These act on the workspace that HOLDS the range: ownership (which VMs may be
            # touched) and the estate state document (run id, secrets, VM inventory) live in
            # the workspace a PRIOR run built, and a freshly seeded one has none — the
            # teardown refused ("carries no run id, so ownership cannot be proven"), the
            # verify had nothing to read, and the queue could never clean up or verify what
            # it deployed (2026-10-14). Resolve it at submit so a hopeless run is refused
            # before a run record exists.
            target = self._range_workspace(comp_id, params, action)
            if target is None:
                raise RunRefused(
                    f"nothing to {action} for {comp_id}: no run workspace with an estate "
                    f"run id exists under {self.runs_dir} (a range deployed by hand is "
                    f"{action}d from its own comp dir)")
            params["_destroy_comp_dir"] = str(target)
            if params.get("comp_dir"):
                params["comp_dir"] = str(target)
        record = self.store.create_run(run_id, comp_id, action, engine_ref, params,
                                       run_dir, run_dir / "run.log")
        if action == "deploy":
            # A deployed portal needs a channel back to THIS dashboard (the reset-request
            # sync): a per-range token minted once per range, keyed on the workspace the
            # deploy is about to build. The URL rides the service env (TEZ_PUBLIC_URL) —
            # unset it and portals deploy without the channel.
            self.store.mint_range_token(str(run_dir / "comp"), comp_id)
        self._emit(run_id, "lifecycle", data={"state": "queued", "action": action})
        threading.Thread(target=self._run, args=(record, comp_src, engine_ref),
                         daemon=True).start()
        return record

    def _range_workspace(self, comp_id, params, action):
        """The workspace holding this competition's range: an explicit `comp_dir`, else the
        workspace that RECORDS a deployed range of this competition (the store is the ledger of
        which run dirs belong to which competition — matching by workspace name would let one
        competition's run reach another's range).

        Candidates rank by whether their document records an inventory (a range that exists),
        never by run-dir recency alone: a run that died before building one leaves a seeded
        document carrying a run id, and letting that stub win made every later range action
        target a workspace with nothing in it (live 2026-10-15, the refused run-791e0f7b stubbed
        over run-ab512e1b). Two candidates at the winning rank mean the workspace that owns the
        range is genuinely ambiguous, and picking one would act on the wrong VMs, so that is
        refused naming both.

        A retired candidate is skipped so a re-run teardown cannot be queued against a range
        that is already gone; teardown is idempotent, so a range whose teardown merely FAILED
        stays resolvable and retryable. The one exception is a full destroy against a retired
        range that still records only kept templates: `--teams-only` retires the range and
        leaves this competition's engine template + goldens for its next run, and a full
        teardown is the only path that reclaims them (2026-10-15). That exception is
        destroy-only; everything else still refuses.
        """
        explicit = (params or {}).get("comp_dir")
        by_key = {r["key"]: r for r in ranges.build_ranges(self.store)}
        if explicit:
            ws = Path(explicit).resolve()
            rng = by_key.get(str(ws))
            # Check every record, not just the group's newest comp_id: an old malformed
            # cross-comp follow-up must not grant access to the original range.
            if rng is None or any(run["comp_id"] != comp_id for run in rng["runs"]):
                raise RunRefused(f"workspace {ws} does not belong to competition {comp_id}")
            if rng["state"] == "retired":
                if action == "destroy" and ranges.reclaimable_templates(rng["key"],
                                                                        rng["state"]):
                    return ws
                raise RunRefused(
                    f"the range in {rng['deploy_run_id']} was torn down by {rng['retired_by']} — "
                    f"nothing to {action} (a fresh deploy builds a new range)")
            return ws
        retired, reclaimable, candidates = [], [], []
        for run in self.store.list_runs(comp_id=comp_id):        # newest first
            ws = Path(run["run_dir"]) / "comp"
            if not (ws / "Compfile").exists():
                continue
            if not estate.load_flat(ws).get("run_id"):
                continue
            rng = by_key.get(str(ws.resolve()))
            if rng and rng["state"] == "retired":
                retired.append(rng)
                if action == "destroy" and ranges.reclaimable_templates(rng["key"],
                                                                        rng["state"]):
                    reclaimable.append((run["id"], ws))
                continue
            candidates.append((run["id"], ws))
        built = [(rid, ws) for rid, ws in candidates if estate.has_inventory(ws)]
        pool = built or candidates
        if len(pool) > 1:
            (a_id, a_ws), (b_id, b_ws) = pool[0], pool[1]
            raise RunRefused(
                f"{len(pool)} workspaces record a range for {comp_id} ({a_id} at {a_ws}, "
                f"{b_id} at {b_ws}) — name the one to {action} with comp_dir")
        if pool:
            return pool[0][1]
        if len(reclaimable) > 1:
            named = ", ".join(f"{rid} at {ws}" for rid, ws in reclaimable)
            raise RunRefused(
                f"{len(reclaimable)} retired workspaces still record kept templates for "
                f"{comp_id} ({named}) — name the one to destroy with comp_dir")
        if reclaimable:
            return reclaimable[0][1]
        if retired:
            rng = retired[0]
            raise RunRefused(
                f"the range in {rng['deploy_run_id']} was torn down by {rng['retired_by']} — "
                f"nothing to {action} (a fresh deploy builds a new range)")
        return None

    def cancel(self, run_id):
        with self._lock:
            proc = self._procs.get(run_id)
        if proc is None or proc.poll() is not None:
            raise RunRefused(f"run {run_id} is not running")
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        self._emit(run_id, "lifecycle", data={"state": "cancelling"})
        return self.store.get_run(run_id)

    def pause(self, run_id):
        """Ask this run's driver to hold at its next phase boundary (deploy_lib/pause.py).

        The request is a file in the run's OWN dir, never the shared workspace: a later run in
        the same comp dir must not inherit it."""
        run = self.store.get_run(run_id)
        if not run:
            raise RunRefused(f"no such run: {run_id}")
        if run["action"] not in PAUSABLE_ACTIONS:
            raise RunRefused(f"a {run['action']} run has no phase boundary to hold at")
        if run["state"] not in ("queued", "running", "pausing"):
            raise RunRefused(f"run {run_id} is {run['state']} — nothing to hold")
        run_dir = Path(run["run_dir"])
        if not self._pause_hook(run, run_dir):
            raise RunRefused(
                f"engine ref {run['engine_ref']} predates the boundary hook "
                f"(deploy_lib/pause.py) — the driver would ignore the request")
        if self._deploy_phase(run_dir / "comp") >= 8:
            raise RunRefused("the deploy is past its last phase — no boundary left to hold at")
        request = run_dir / "pause"
        if not request.exists():        # a second click is the same request, not a second event
            request.touch()
            self.store.update_run(run_id, state="pausing")
            self._emit(run_id, "lifecycle", stage="pause requested",
                       data={"state": "pausing"})
        return {"run": self.store.get_run(run_id),
                "detail": "holding at the end of the phase in progress"}

    def resume(self, run_id):
        """Withdraw the request: a pending one never lands, a landed one lets the driver go."""
        run = self.store.get_run(run_id)
        if not run:
            raise RunRefused(f"no such run: {run_id}")
        if run["state"] not in ("pausing", "paused"):
            raise RunRefused(f"run {run_id} is {run['state']} — nothing to resume")
        (Path(run["run_dir"]) / "pause").unlink(missing_ok=True)
        if run["state"] == "pausing":
            # The driver never reached a boundary, so no tracker will announce this one.
            self.store.update_run(run_id, state="running")
            self._emit(run_id, "lifecycle", data={"state": "running", "resumed": True})
        else:
            # A parked driver clears its marker within the second; the stage tracker sees that
            # and announces the resume, so one transition keeps one writer.
            self._emit(run_id, "lifecycle", data={"state": "resume_requested"})
        return {"run": self.store.get_run(run_id), "detail": "resuming"}

    def _pause_hook(self, run, run_dir):
        """Whether the engine this run executes carries the boundary hook.

        A cut worktree answers for itself. Before that the ref has to be fetched first: the
        clone's last fetch is stale exactly when a click lands mid-provisioning, and a refusal
        on a stale fetch would reject a pause the fresh worktree would honor."""
        if (run_dir / "engine").is_dir():
            return (run_dir / "engine" / "deploy_lib" / "pause.py").exists()
        subprocess.run(["git", "-C", str(self.engine_repo), "fetch", "origin", "--quiet"],
                       capture_output=True)
        probe = subprocess.run(
            ["git", "-C", str(self.engine_repo), "cat-file", "-e",
             f"{self._resolve_ref(run['engine_ref'])}:deploy_lib/pause.py"], capture_output=True)
        return probe.returncode == 0

    def _driver_env(self):
        """The environment a driver child gets.

        PATH carries the user-space tool dirs (opencode, sshpass). The ssh key path is forced
        ABSOLUTE to this service's own key file and exported as `TEZ_SSH_PRIVATE_KEY_PATH`:
        `estate/config.py` resolves a relative key path against the CURRENT DIRECTORY — which
        for a run is the throwaway worktree — and the estate document records the result. The
        worktree is deleted when the run ends, so a relative value leaves a range whose key
        cannot be found at teardown (2026-10-14, run-3aaf37f1, four attempts).
        """
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PATH": config.driver_path()}
        # Catalog tokens ride the child env: the engine reads them by token_env name, so
        # no secret is written into the worktree, the run record or the comp workspace.
        env.update(nodes.token_env(nodes.load()))
        key = config.engine_proxmox_file()
        if key and Path(key).is_file():
            key = Path(key).resolve()
            env["TEZ_SSH_PRIVATE_KEY_PATH"] = str(key)
            # bad-auto reads `<deploy.ssh_key>.pub` beside the private key, and a run host may
            # hold only the private half — the assume-breach red01 deploy then died on
            # FileNotFoundError AFTER the range itself was built (2026-10-14). The public half
            # is in the engine env; materialize it so the two halves always travel together.
            pub = key.with_name(key.name + ".pub")
            public = env.get("TEZ_SSH_PUBLIC_KEY")
            if not pub.exists() and public:
                pub.write_text(public.strip() + "\n")
        return env

    @staticmethod
    def _workspace(record, comp_src):
        """The workspace a run acts in.

        An explicit `comp_dir` wins for ANY action: a scrim of an already-deployed range must
        act on the workspace that holds it (`--skip-deploy`), not a fresh seed, and the range
        actions get theirs resolved by `_range_workspace` into `_destroy_comp_dir`.
        """
        explicit = (record["params"].get("comp_dir")
                    or record["params"].get("_destroy_comp_dir"))
        return (Path(explicit) if explicit
                else workspaces.seed_workspace(comp_src, Path(record["run_dir"]) / "comp"))

    def _run_nodes_file(self, params, run_dir):
        """The run's selected nodes, in the engine's nodes.json schema.

        Only the selection is written (a token_env name per node, never a token) — the
        values ride the child env from _driver_env()."""
        selected = [str(x) for x in (params.get("nodes") or [])]
        if not selected:
            return None
        path = run_dir / "nodes.json"
        path.write_text(json.dumps(nodes.run_list(nodes.load(), selected), indent=2))
        return path

    def _emit(self, run_id, kind, stage="", data=None):
        """Record an event AND push it to the bus — one path, so a subscriber sees exactly
        what the cursor replays.

        Recording without publishing is why `watch_run` subscribers and the SSE stream sat
        silent on a live run while the store filled up: `EventBus.publish` had no caller."""
        event = self.store.add_event(run_id, kind, stage=stage, data=data)
        self.bus.publish(run_id, event)
        return event

    def _run(self, record, comp_src, engine_ref):
        try:
            run_id, run_dir = record["id"], Path(record["run_dir"])
            ws = self._workspace(record, comp_src)
            worktree = run_dir / "engine"
            engine_used = self._provision_worktree(worktree, engine_ref, run_dir)
            # What actually runs, in the record: `engine_ref` is what was ASKED for (a branch
            # name), `engine_head` the commit that ref resolved to at the cut.
            self.store.update_run(run_id, engine_head=engine_used["commit"])
            nodes_file = self._run_nodes_file(record["params"], run_dir)
            cmd = build_command(record["action"], record["comp_id"], ws, record["params"],
                                nodes_file)
            child_env = self._driver_env()
            # The run's own control file (deploy_lib/pause.py reads it): a per-run path, so a
            # request cannot outlive its run into a shared workspace.
            child_env["TEZ_PAUSE_FILE"] = str(run_dir / "pause")
            # The portal ops channel (portal_ops → portal .env): the URL the deployed portal
            # pushes reset requests to, and this range's own token.
            if config.public_url():
                child_env["TEZ_OPS_URL"] = config.public_url()
                token = self.store.token_for_range(str(ws.resolve()))
                if token:
                    child_env["TEZ_OPS_TOKEN"] = token
            log = open(record["log_path"], "w")
            log.write(f"$ engine {engine_used['ref']} @ {engine_used['commit'][:12]}\n")
            log.write("$ " + " ".join(cmd) + "\n")
            log.flush()
            # Keep a request made while the run was still provisioning: writing "running" over
            # it would make the operator's click look lost.
            state = "pausing" if (run_dir / "pause").exists() else "running"
            self.store.update_run(run_id, state=state, started_at=time.time())
            # The cut is announced here: carry the resolved commit so a subscriber need not
            # trust `engine_ref` alone for what this run executes.
            self._emit(run_id, "lifecycle", data={"state": state, "cmd": cmd,
                                                  "engine_ref": engine_ref,
                                                  "engine_head": engine_used["commit"]})
            proc = subprocess.Popen(cmd, cwd=str(worktree), stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True,
                                    env=child_env)
            with self._lock:
                self._procs[run_id] = proc
            self.store.update_run(run_id, pid=proc.pid)
            self._track_stages(run_id, ws, proc, record["action"])
            rc = proc.wait()
            log.close()
            self._finish(run_id, rc, cancelling=rc < 0)
        except Exception as e:  # a crashed run must end as a failed run, not a ghost
            self._finish(run_id, None, error=f"{type(e).__name__}: {e}")
        finally:
            # Never discard the range's workspace while its range exists: the estate document
            # (run id, ownership, teardown journal) is the sanctioned way to finish a failed
            # teardown (known-issues: a deleted per-run workspace strands its range).
            workspaces.release(run_dir, self._range_retired(record))
            (run_dir / "bad-auto").unlink(missing_ok=True)
            (run_dir / "pause").unlink(missing_ok=True)
            subprocess.run(["git", "-C", str(self.engine_repo), "worktree", "prune"],
                           capture_output=True)

    def _range_retired(self, record):
        """Whether the range this run acted on is retired, so its workspace may be released."""
        key = ranges.range_key(record)
        rng = next((r for r in ranges.build_ranges(self.store) if r["key"] == key), None)
        return bool(rng and rng["state"] == "retired")

    def _finish(self, run_id, rc, error=None, cancelling=False):
        if error is not None:
            state = "failed"
        elif rc == 0:
            state = "succeeded"
        elif cancelling or self._cancel_requested(run_id):
            state = "cancelled"
        else:
            state = "failed"
        self.store.update_run(run_id, state=state, returncode=rc, error=error,
                              ended_at=time.time())
        self._emit(run_id, "lifecycle",
                   data={"state": state, "returncode": rc, "error": error})
        # An approved reset request rides the redeploy run that resolves it: a clean exit
        # is `done`, anything else is `failed` (the team's chip frees the box either way —
        # done/denied/failed all allow a new request). Only `approved` resolves, so the
        # resolution is one-shot even if the same run id were finished twice.
        for req in self.store.requests_for_run(run_id):
            if req["status"] != "approved":
                continue
            outcome = "done" if state == "succeeded" else "failed"
            self.store.set_request_status(req["id"], outcome)
            self._emit(run_id, "request", data={"request": req["id"],
                                                "status": outcome, "team": req["team"],
                                                "box": req["box"]})

    def _cancel_requested(self, run_id):
        _, events = self.store.get_events(run_id, 0)
        return any(e.get("state") == "cancelling" for e in events if e["kind"] == "lifecycle")

    # ---- worktree provisioning ----

    def _resolve_ref(self, engine_ref):
        """The ref a run's worktree is actually cut at.

        A branch name used to mean the engine clone's LOCAL branch, which nothing in this
        service ever updates (ensure_repos only clones when missing, and only the competitions
        clone gets pulled) — so every run executed whatever that branch was at clone time.
        Measured 2026-10-14: the clone sat 9 commits behind origin/main and three runs ran the
        stale engine while reporting `engine_ref: main`. Prefer the freshly fetched
        remote-tracking branch; SHAs and tags pass through unchanged (an unknown name still
        fails at `worktree add` with git's own message).
        """
        if not engine_ref:
            return engine_ref
        probe = subprocess.run(
            ["git", "-C", str(self.engine_repo), "rev-parse", "--verify", "--quiet",
             f"refs/remotes/origin/{engine_ref}"], capture_output=True)
        return f"origin/{engine_ref}" if probe.returncode == 0 else engine_ref

    def _provision_worktree(self, worktree, engine_ref, run_dir):
        # A discarded fetch return code is how a whole round executed `2358606` while its
        # records said `main`: `_resolve_ref` then resolved origin/main from whatever cached
        # remote-tracking ref was on disk, and the run executed an older revision than the one
        # the UI reported. A run that cannot guarantee which code it will execute must not
        # start, so a failed fetch refuses (naming the ref and git's error) before any worktree.
        fetch = subprocess.run(["git", "-C", str(self.engine_repo), "fetch", "origin", "--quiet"],
                               capture_output=True, text=True)
        if fetch.returncode != 0:
            raise RunRefused(f"cannot fetch engine ref {engine_ref!r} from origin: "
                             f"{fetch.stderr.strip()[-200:]}")
        resolved = self._resolve_ref(engine_ref)
        proc = subprocess.run(["git", "-C", str(self.engine_repo), "worktree", "add",
                               "--detach", str(worktree), resolved],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise RunRefused(f"cannot cut engine worktree at {engine_ref!r}: "
                             f"{proc.stderr.strip()[-200:]}")
        head = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        # Per-run configuration. A worktree is a fresh checkout and both config surfaces are
        # gitignored, so the run carries its own: `.env` (canonical TEZ_* names, which
        # estate/config.py resolves through its _SPEC) and, when the operator has one, the
        # native `tezcatlipoca.toml` profile. Without at least one of them a driver starts
        # with no endpoint, node or API token.
        env_file = config.engine_env_file()
        if env_file:
            shutil.copy2(env_file, worktree / ".env")
        profile = config.engine_config_file()
        if profile:
            shutil.copy2(profile, worktree / "tezcatlipoca.toml")
        proxmox = config.engine_proxmox_file()
        if proxmox:
            shutil.copy2(proxmox, worktree / "proxmox")
            os.chmod(worktree / "proxmox", 0o600)
        badauto = config.badauto_path()
        if badauto:
            (run_dir / "bad-auto").symlink_to(badauto)
        vendor = worktree / "vendor"
        vendor.mkdir(exist_ok=True)
        nakon = config.nakon_path()
        if nakon:
            slot = vendor / "nakon"
            if slot.is_symlink() or slot.is_file():
                slot.unlink()
            elif slot.exists():
                shutil.rmtree(slot, ignore_errors=True)  # empty submodule slot from the worktree
            slot.symlink_to(nakon)
        else:
            subprocess.run(["git", "-C", str(worktree), "submodule", "update", "--init",
                            "vendor/nakon"], capture_output=True)
        return {"ref": resolved, "commit": head}

    # ---- stage tracking ----

    def _track_stages(self, run_id, ws, proc, action):
        if action in SINGLE_STEP_STAGES:
            # No engine phase signal for these: one stage at start, then just wait. Polling
            # the workspace's state here replayed the range's stale deploy phases under the
            # teardown's own run id.
            self._stage(run_id, SINGLE_STEP_STAGES[action], 1, 1)
            proc.wait()
            return
        last_phase, seen, since, held = 0, set(), time.time(), None
        while proc.poll() is None:
            if action in ("deploy", "plan"):
                last_phase = self._emit_deploy_phases(run_id, ws, last_phase)
                held = self._reflect_pause(run_id, ws, held)
            else:  # scrim: the harness's phases only, and only this run's manifests
                for phase in self._scrim_phases(ws, since):
                    if phase not in seen:
                        seen.add(phase)
                        self._stage(run_id, str(phase))
            time.sleep(2)
        if action in ("deploy", "plan"):
            self._emit_deploy_phases(run_id, ws, last_phase)

    def _emit_deploy_phases(self, run_id, ws, last_phase):
        phase = self._deploy_phase(ws)
        for n in range(last_phase + 1, phase + 1):
            self._stage(run_id, f"phase {n}/8 {PHASE_NAMES.get(n, '')}".strip(), n, 8)
        return max(last_phase, phase)

    def _reflect_pause(self, run_id, ws, held):
        """Mirror the driver's boundary hold into the run's state and event stream.

        The driver writes the held phase to the estate document and never to stdout, so this
        poll is the observer's only signal — and the marker clearing again is the resume."""
        now = self._deploy_held(ws)
        if now == held:
            return held
        if now is None:
            self.store.update_run(run_id, state="running")
            self._emit(run_id, "lifecycle", data={"state": "running", "resumed": True})
        else:
            self.store.update_run(run_id, state="paused", stage=f"paused after phase {now}/8")
            self._emit(run_id, "lifecycle", stage=f"paused after phase {now}/8",
                       data={"state": "paused", "after_phase": now})
        return now

    @staticmethod
    def _deploy_held(ws):
        # estate.state.load_flat's flat view (the document's run section).
        try:
            return estate.load_flat(ws).get("paused_after_phase")
        except Exception:      # a tracker poll must never kill the run it observes
            return None

    def _stage(self, run_id, stage, index=None, total=None):
        self.store.update_run(run_id, stage=stage)
        self._emit(run_id, "stage", stage=stage,
                   data={"stage": stage, "index": index, "total": total})

    @staticmethod
    def _deploy_phase(ws):
        # The estate document's run.last_phase.
        try:
            return int(estate.load_flat(ws).get("last_phase") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _scrim_phases(ws, since):
        """The harness's phases in the order each manifest lists them, from the manifests
        written since `since`.

        The workspace is shared across runs, so a `--skip-deploy` scrim that read every
        manifest replayed a previous run's phases (and a set delivered them unordered)."""
        found = []
        for manifest in sorted(Path(ws).glob(".automated-tests/*/run.json")):
            try:
                if manifest.stat().st_mtime < since:
                    continue
                entries = json.loads(manifest.read_text()).get("phases") or []
            except (OSError, ValueError):
                continue
            for entry in entries:
                phase = entry.get("phase")
                if phase is not None and phase not in found:
                    found.append(phase)
        return found
