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
from pathlib import Path

from dashboard import config, workspaces

PHASE_NAMES = {
    1: "cleanup", 2: "engine_template", 3: "prepare_engine", 4: "golden_set",
    5: "firewall_bootstrap", 6: "repair_sweep", 7: "domains_and_final", 8: "seed",
}
FAIL_FAST_ACTIONS = ("deploy", "plan", "scrim")


class RunRefused(Exception):
    pass


def build_command(action, comp_id, ws, params):
    # The drivers an operator would type, with the workspace as the comp dir.
    py, ws = sys.executable, str(ws)
    extra = [str(a) for a in (params.get("extra_args") or [])]
    if action in ("deploy", "plan"):
        cmd = [py, "create-competition.py", "--comp-dir", ws, "--competition", comp_id,
               "--teams", str(params.get("teams") or 2), "--yes"]
        for flag, key in (("--scoring-vmid", "scoring_vmid"), ("--engine-node", "engine_node"),
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
        if params.get("teams"):
            params["teams"] = int(params["teams"])
        self.ensure_repos()
        self.refresh_comps()
        comp_src = self.comps_repo / comp_id
        if not (comp_src / "Compfile").exists():
            raise RunRefused(f"no such competition: {comp_id}")
        if self.store.running_run_for_comp(comp_id):
            raise RunRefused(f"a run is already active for {comp_id} — refusing to overlap")
        run_id = "run-" + uuid.uuid4().hex[:8]
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        record = self.store.create_run(run_id, comp_id, action, engine_ref, params,
                                       run_dir, run_dir / "run.log")
        self._emit(run_id, "lifecycle", data={"state": "queued", "action": action})
        threading.Thread(target=self._run, args=(record, comp_src, engine_ref),
                         daemon=True).start()
        return record

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
            ws = workspaces.seed_workspace(comp_src, run_dir / "comp")
            worktree = run_dir / "engine"
            self._provision_worktree(worktree, engine_ref, run_dir)
            cmd = build_command(record["action"], record["comp_id"], ws, record["params"])
            log = open(record["log_path"], "w")
            log.write("$ " + " ".join(cmd) + "\n")
            log.flush()
            self.store.update_run(run_id, state="running", started_at=time.time())
            self._emit(run_id, "lifecycle", data={"state": "running", "cmd": cmd})
            proc = subprocess.Popen(cmd, cwd=str(worktree), stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True,
                                    env={**os.environ, "PYTHONUNBUFFERED": "1"})
            with self._lock:
                self._procs[run_id] = proc
            self.store.update_run(run_id, pid=proc.pid)
            self._track_stages(run_id, ws, proc)
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

    def _provision_worktree(self, worktree, engine_ref, run_dir):
        subprocess.run(["git", "-C", str(self.engine_repo), "fetch", "origin", "--quiet"],
                       capture_output=True)
        proc = subprocess.run(["git", "-C", str(self.engine_repo), "worktree", "add",
                               "--detach", str(worktree), engine_ref],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise RunRefused(f"cannot cut engine worktree at {engine_ref!r}: "
                             f"{proc.stderr.strip()[-200:]}")
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

    # ---- stage tracking ----

    def _track_stages(self, run_id, ws, proc):
        last_phase, seen_phases = 0, set()
        while proc.poll() is None:
            phase = self._deploy_phase(ws)
            if phase and phase > last_phase:
                for n in range(last_phase + 1, phase + 1):
                    self._stage(run_id, f"phase {n}/8 {PHASE_NAMES.get(n, '')}".strip())
                last_phase = phase
            for p in self._scrim_phases(ws) - seen_phases:
                self._stage(run_id, str(p))
                seen_phases.add(p)
            time.sleep(2)
        phase = self._deploy_phase(ws)
        if phase and phase > last_phase:
            for n in range(last_phase + 1, phase + 1):
                self._stage(run_id, f"phase {n}/8 {PHASE_NAMES.get(n, '')}".strip())

    def _stage(self, run_id, stage):
        self.store.update_run(run_id, stage=stage)
        self._emit(run_id, "stage", stage=stage, data={"stage": stage})

    @staticmethod
    def _deploy_phase(ws):
        try:
            return int(json.loads((Path(ws) / ".deploy_state.json").read_text()).get("last_phase") or 0)
        except (OSError, ValueError):
            return 0

    @staticmethod
    def _scrim_phases(ws):
        found = set()
        for manifest in Path(ws).glob(".automated-tests/*/run.json"):
            try:
                for entry in json.loads(manifest.read_text()).get("phases") or []:
                    found.add(entry.get("phase"))
            except (OSError, ValueError):
                pass
        found.discard(None)
        return found
