"""Stage tracking: one step for the single-step actions, engine phases only where real."""

import json
import os
import time

import pytest

from dashboard import config
from dashboard.runner import RunManager


class FakeStore:
    def __init__(self):
        self.events = []
        self.stages = []

    def update_run(self, run_id, **fields):
        self.stages.append(fields.get("stage"))

    def add_event(self, run_id, kind, stage="", data=None):
        event = {"kind": kind, "stage": stage, **(data or {})}
        self.events.append(event)
        return event


class FakeBus:
    def publish(self, run_id, event):
        pass


class FakeProc:
    """poll() returns the given codes, then 0; a destroy must only ever wait()."""

    def __init__(self, codes=()):
        self.codes = list(codes)
        self.waited = False

    def poll(self):
        assert self.codes, "a single-step action must not enter the poll loop"
        return self.codes.pop(0)

    def wait(self):
        self.waited = True
        return 0


def _manager(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    store = FakeStore()
    return RunManager(store, FakeBus()), store


@pytest.mark.parametrize("action,stage", [("verify", "verify"), ("destroy", "teardown"),
                                          ("redeploy", "redeploy")])
def test_single_step_actions_emit_one_stage(tmp_path, monkeypatch, action, stage):
    ws = tmp_path / "comp"
    ws.mkdir()
    (ws / ".deploy_state.json").write_text(json.dumps({"last_phase": 8}))
    mgr, store = _manager(tmp_path, monkeypatch)
    proc = FakeProc()
    mgr._track_stages("run-1", ws, proc, action)
    assert proc.waited
    assert [e["stage"] for e in store.events] == [stage]
    assert store.events[0]["index"] == 1 and store.events[0]["total"] == 1
    assert store.stages == [stage]
    assert not any(e["stage"].startswith("phase ") for e in store.events)


class PhaseProc:
    """Raises .deploy_state.json's last_phase to `upto`, then exits."""

    def __init__(self, ws, upto):
        self.ws, self.upto, self.n = ws, upto, 0

    def poll(self):
        self.n += 1
        (self.ws / ".deploy_state.json").write_text(
            json.dumps({"last_phase": min(self.n, self.upto)}))
        return None if self.n <= self.upto else 0

    def wait(self):
        return 0


def test_deploy_emits_phases_with_index_and_total(tmp_path, monkeypatch):
    ws = tmp_path / "comp"
    ws.mkdir()
    (ws / ".deploy_state.json").write_text(json.dumps({"last_phase": 0}))
    monkeypatch.setattr("dashboard.runner.time.sleep", lambda _seconds: None)
    mgr, store = _manager(tmp_path, monkeypatch)
    mgr._track_stages("run-1", ws, PhaseProc(ws, 3), "deploy")
    assert [e["stage"] for e in store.events] == [
        "phase 1/8 cleanup", "phase 2/8 engine_template", "phase 3/8 prepare_engine"]
    assert [(e["index"], e["total"]) for e in store.events] == [(1, 8), (2, 8), (3, 8)]


def test_scrim_uses_the_harness_phases_in_order(tmp_path, monkeypatch):
    ws = tmp_path / "comp"
    ws.mkdir()
    (ws / ".deploy_state.json").write_text(json.dumps({"last_phase": 8}))
    monkeypatch.setattr("dashboard.runner.time.sleep", lambda _seconds: None)

    class ScrimProc:
        def __init__(self):
            self.n = 0

        def poll(self):
            self.n += 1
            if self.n == 1:
                manifest = ws / ".automated-tests" / "scrim-1" / "run.json"
                manifest.parent.mkdir(parents=True)
                manifest.write_text(json.dumps({"phases": [{"phase": "recon"}, {"phase": "attack"}]}))
                return None
            return 0

        def wait(self):
            return 0

    mgr, store = _manager(tmp_path, monkeypatch)
    mgr._track_stages("run-1", ws, ScrimProc(), "scrim")
    assert [e["stage"] for e in store.events] == ["recon", "attack"]
    assert not any(e["stage"].startswith("phase ") for e in store.events)


def test_scrim_phases_are_ordered_and_ignore_stale_manifests(tmp_path):
    root = tmp_path / "comp" / ".automated-tests"
    old = root / "run-old" / "run.json"
    new = root / "run-new" / "run.json"
    for path in (old, new):
        path.parent.mkdir(parents=True)
    old.write_text(json.dumps({"phases": [{"phase": "z"}, {"phase": "a"}]}))
    new.write_text(json.dumps({"phases": [{"phase": "p"}, {"phase": "q"}, {"phase": "p"}]}))
    os.utime(old, (1000, 1000))
    assert RunManager._scrim_phases(tmp_path / "comp", time.time() - 60) == ["p", "q"]
