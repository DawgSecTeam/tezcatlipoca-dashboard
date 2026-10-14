"""Run execution: engine worktrees, driver processes, and stage-event tracking.

One thread per run. The engine stays standalone: a run gets a disposable worktree plus a
workspace comp dir, and the driver is the same CLI a human would type. Stage progress is
observed, never parsed out of the driver's stdout: .deploy_state.json last_phase for
deploys, the scrim harness's run.json phases otherwise."""

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

from dashboard import config, nodes, ranges, workspaces

PHASE_NAMES = {
    1: "cleanup", 2: "engine_template", 3: "prepare_engine", 4: "golden_set",
    5: "firewall_bootstrap", 6: "repair_sweep", 7: "domains_and_final", 8: "seed",
}
FAIL_FAST_ACTIONS = ("deploy", "plan", "scrim")
# Actions whose driver reports no engine phase: they get ONE stage event, not the range's
# stale deploy phases (a 6-second teardown used to re-emit phase 1/8..8/8 in one second).
SINGLE_STEP_STAGES = {"verify": "verify", "destroy": "teardown", "redeploy": "redeploy"}

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
                     "--blue-watchdog", "--skip-deploy", "--resume-event"):
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

    def submit(self, comp_id, action, engine_ref="main", params=None):
        params = dict(params or {})
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
        if action in ("destroy", "verify", "redeploy"):
            # These act on the workspace that HOLDS the range: ownership (which VMs may be
            # touched), the deploy state and the terraform workdir all live in the workspace a
            # PRIOR run built, and a freshly seeded one has none — the teardown refused
            # ("carries no run id, so ownership cannot be proven"), verify died inside
            # `terraform output` (misreporting a missing workdir as a missing binary), and the
            # queue could never clean up or verify what it deployed (2026-10-14). Resolve it at
            # submit so a hopeless run is refused before a run record exists.
            target = self._range_workspace(comp_id, params, action)
            if target is None:
                raise RunRefused(
                    f"nothing to {action} for {comp_id}: no run workspace with a "
                    f".deploy_state.json run id exists under {self.runs_dir} (a range deployed "
                    f"by hand is {action}d from its own comp dir)")
            params["_destroy_comp_dir"] = str(target)
        record = self.store.create_run(run_id, comp_id, action, engine_ref, params,
                                       run_dir, run_dir / "run.log")
        self._emit(run_id, "lifecycle", data={"state": "queued", "action": action})
        threading.Thread(target=self._run, args=(record, comp_src, engine_ref),
                         daemon=True).start()
        return record

    def _range_workspace(self, comp_id, params, action):
        """The workspace holding this competition's range: an explicit `comp_dir`, else the
        newest NON-retired run workspace OF THIS COMPETITION that carries a deploy run id
        (the store is the ledger of which run dirs belong to which competition — matching by
        workspace name would let one competition's run reach another's range).

        A retired candidate is skipped so a re-run teardown cannot be queued against a range
        that is already gone; teardown is idempotent, so a range whose teardown merely FAILED
        stays resolvable and retryable."""
        explicit = (params or {}).get("comp_dir")
        if explicit:
            return Path(explicit)
        by_key = {r["key"]: r for r in ranges.build_ranges(self.store)}
        retired = []
        for run in self.store.list_runs(comp_id=comp_id):        # newest first
            ws = Path(run["run_dir"]) / "comp"
            state = ws / ".deploy_state.json"
            if not ((ws / "Compfile").exists() and state.exists()):
                continue
            try:
                if not json.loads(state.read_text()).get("run_id"):
                    continue
            except (OSError, ValueError):
                continue
            rng = by_key.get(str(ws.resolve()))
            if rng and rng["state"] == "retired":
                retired.append(rng)
                continue
            return ws
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

    def _driver_env(self):
        """The environment a driver child gets.

        PATH carries the user-space tool dirs (opencode, terraform, sshpass). The ssh key path is
        forced ABSOLUTE to this service's own key file: the engine resolves a relative
        `TF_VAR_ssh_private_key_path` against the CURRENT DIRECTORY — which for a run is the
        throwaway worktree — and writes that resolved path into terraform.tfvars.json, i.e. into
        the state. The worktree is deleted when the run ends, so a relative value left the range
        un-destroyable: `terraform destroy` died parsing the config with
        "no file exists at <worktree>/proxmox" (2026-10-14, run-3aaf37f1, four attempts).
        """
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PATH": config.driver_path()}
        # Catalog tokens ride the child env: the engine reads them by token_env name, so
        # no secret is written into the worktree, the run record or the comp workspace.
        env.update(nodes.token_env(nodes.load()))
        key = config.engine_proxmox_file()
        if key and Path(key).is_file():
            key = Path(key).resolve()
            env["TF_VAR_ssh_private_key_path"] = str(key)
            # bad-auto reads `<deploy.ssh_key>.pub` beside the private key, and a run host may
            # hold only the private half — the assume-breach red01 deploy then died on
            # FileNotFoundError AFTER the range itself was built (2026-10-14). The public half
            # is in the engine env; materialize it so the two halves always travel together.
            pub = key.with_name(key.name + ".pub")
            if not pub.exists() and env.get("TF_VAR_ssh_public_key"):
                pub.write_text(env["TF_VAR_ssh_public_key"].strip() + "\n")
        return env

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
        run_id, run_dir = record["id"], Path(record["run_dir"])
        try:
            destroy_dir = record["params"].get("_destroy_comp_dir")
            ws = (Path(destroy_dir) if destroy_dir
                  else workspaces.seed_workspace(comp_src, run_dir / "comp"))
            worktree = run_dir / "engine"
            engine_used = self._provision_worktree(worktree, engine_ref, run_dir)
            nodes_file = self._run_nodes_file(record["params"], run_dir)
            cmd = build_command(record["action"], record["comp_id"], ws, record["params"],
                                nodes_file)
            child_env = self._driver_env()
            log = open(record["log_path"], "w")
            log.write(f"$ engine {engine_used['ref']} @ {engine_used['commit'][:12]}\n")
            log.write("$ " + " ".join(cmd) + "\n")
            log.flush()
            self.store.update_run(run_id, state="running", started_at=time.time())
            self._emit(run_id, "lifecycle", data={"state": "running", "cmd": cmd})
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
            shutil.rmtree(run_dir / "engine", ignore_errors=True)
            (run_dir / "bad-auto").unlink(missing_ok=True)
            subprocess.run(["git", "-C", str(self.engine_repo), "worktree", "prune"],
                           capture_output=True)

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

    def _cancel_requested(self, run_id):
        _, events = self.store.get_events(run_id, 0)
        return any(e.get("state") == "cancelling" for e in events if e["kind"] == "lifecycle")

    # ---- worktree provisioning ----

    def _resolve_ref(self, engine_ref):
        """The ref a run's worktree is actually cut at.

        `engine_ref: main` used to mean the engine clone's LOCAL main, which nothing in this
        service ever updates (ensure_repos only clones when missing, and only the competitions
        clone gets pulled) — so every run executed whatever main was at clone time. Measured
        2026-10-14: the clone sat 9 commits behind origin/main and three runs ran the stale
        engine while reporting `engine_ref: main`. Prefer the freshly fetched remote-tracking
        branch; SHAs and tags pass through unchanged (an unknown name still fails at `worktree
        add` with git's own message).
        """
        if not engine_ref:
            return engine_ref
        probe = subprocess.run(
            ["git", "-C", str(self.engine_repo), "rev-parse", "--verify", "--quiet",
             f"refs/remotes/origin/{engine_ref}"], capture_output=True)
        return f"origin/{engine_ref}" if probe.returncode == 0 else engine_ref

    def _provision_worktree(self, worktree, engine_ref, run_dir):
        subprocess.run(["git", "-C", str(self.engine_repo), "fetch", "origin", "--quiet"],
                       capture_output=True)
        resolved = self._resolve_ref(engine_ref)
        proc = subprocess.run(["git", "-C", str(self.engine_repo), "worktree", "add",
                               "--detach", str(worktree), resolved],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise RunRefused(f"cannot cut engine worktree at {engine_ref!r}: "
                             f"{proc.stderr.strip()[-200:]}")
        head = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        env_file = config.engine_env_file()
        if env_file:
            shutil.copy2(env_file, worktree / ".env")
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
            # .deploy_state.json here replayed the range's stale deploy phases under the
            # teardown's own run id.
            self._stage(run_id, SINGLE_STEP_STAGES[action], 1, 1)
            proc.wait()
            return
        last_phase, seen, since = 0, set(), time.time()
        while proc.poll() is None:
            if action in ("deploy", "plan"):
                last_phase = self._emit_deploy_phases(run_id, ws, last_phase)
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

    def _stage(self, run_id, stage, index=None, total=None):
        self.store.update_run(run_id, stage=stage)
        self._emit(run_id, "stage", stage=stage,
                   data={"stage": stage, "index": index, "total": total})

    @staticmethod
    def _deploy_phase(ws):
        try:
            return int(json.loads((Path(ws) / ".deploy_state.json").read_text()).get("last_phase") or 0)
        except (OSError, ValueError):
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
