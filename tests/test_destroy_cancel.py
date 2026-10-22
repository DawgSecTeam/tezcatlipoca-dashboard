"""A destroy tears the range down under the deploy that owns it. The deploy then hangs on a
dead box and keeps the engine lock (live: run-084bf18b was refused while the destroyed run's
create-competition still held engine-10.0.0.150-2406.lock)."""

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dashboard import runner as runner_mod


class _Store:
    def __init__(self, runs):
        self._runs = runs

    def get_run(self, run_id):
        return self._runs.get(run_id)

    def update_run(self, *a, **k):
        pass


class _Runner(runner_mod.RunManager):
    def __init__(self, store):  # no real db/bus
        self.store = store
        self._procs = {}
        self._lock = threading.Lock()
        self.emitted = []

    def _emit(self, run_id, kind, data=None):
        self.emitted.append((run_id, kind, data))


def test_a_destroy_cancels_the_live_run_on_its_workspace(tmp_path):
    comp_dir = str(tmp_path / "comp")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)
    store = _Store({"run-live": {"id": "run-live", "state": "running",
                                 "params": {"comp_dir": comp_dir}}})
    runner = _Runner(store)
    runner._procs["run-live"] = proc
    runner._cancel_live_runs_for_comp_dir(comp_dir)
    assert runner.emitted and runner.emitted[0][0] == "run-live"
    proc.wait(timeout=10)
    assert proc.poll() is not None


def test_a_destroy_leaves_another_workspace_alone(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)
    store = _Store({"run-other": {"id": "run-other", "state": "running",
                                  "params": {"comp_dir": str(tmp_path / "other")}}})
    runner = _Runner(store)
    runner._procs["run-other"] = proc
    runner._cancel_live_runs_for_comp_dir(str(tmp_path / "comp"))
    assert runner.emitted == []
    assert proc.poll() is None
    os.killpg(proc.pid, signal.SIGTERM)
