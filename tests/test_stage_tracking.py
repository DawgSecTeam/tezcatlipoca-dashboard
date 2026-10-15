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


@pytest.mark.parametrize("state_name", ["estate-state.json", ".deploy_state.json"])
@pytest.mark.parametrize("action,stage", [("verify", "verify"), ("destroy", "teardown"),
                                          ("redeploy", "redeploy")])
def test_single_step_actions_emit_one_stage(tmp_path, monkeypatch, action, stage, state_name):
    ws = tmp_path / "comp"
    ws.mkdir()
    # A single-step action must not poll the range's stale deploy phases, in EITHER state
    # shape: v0.3.0's document, or the legacy file a pre-v0.3 range still carries.
    (ws / state_name).write_text(json.dumps(
        {"last_phase": 8} if state_name == ".deploy_state.json"
        else {"identity": {"run_id": "run-old"}, "run": {"last_phase": 8}}))
    mgr, store = _manager(tmp_path, monkeypatch)
    proc = FakeProc()
    mgr._track_stages("run-1", ws, proc, action)
    assert proc.waited
    assert [e["stage"] for e in store.events] == [stage]
    assert store.events[0]["index"] == 1 and store.events[0]["total"] == 1
    assert store.stages == [stage]
    assert not any(e["stage"].startswith("phase ") for e in store.events)


class PhaseProc:
    """Raises the workspace's last_phase to `upto`, then exits.

    Writes the v0.3.0 document (run.last_phase); `legacy=True` writes the pre-v0.3 flat
    file, which the reader must still follow.
    """

    def __init__(self, ws, upto, legacy=False):
        self.ws, self.upto, self.n, self.legacy = ws, upto, 0, legacy

    def poll(self):
        self.n += 1
        phase = min(self.n, self.upto)
        if self.legacy:
            (self.ws / ".deploy_state.json").write_text(json.dumps({"last_phase": phase}))
        else:
            (self.ws / "estate-state.json").write_text(
                json.dumps({"identity": {"run_id": "run-1"}, "run": {"last_phase": phase}}))
        return None if self.n <= self.upto else 0

    def wait(self):
        return 0


@pytest.mark.parametrize("legacy", [False, True])
def test_deploy_emits_phases_with_index_and_total(tmp_path, monkeypatch, legacy):
    ws = tmp_path / "comp"
    ws.mkdir()
    name = ".deploy_state.json" if legacy else "estate-state.json"
    (ws / name).write_text(json.dumps({"last_phase": 0}) if legacy
                          else json.dumps({"identity": {"run_id": "run-1"},
                                           "run": {"last_phase": 0}}))
    monkeypatch.setattr("dashboard.runner.time.sleep", lambda _seconds: None)
    mgr, store = _manager(tmp_path, monkeypatch)
    mgr._track_stages("run-1", ws, PhaseProc(ws, 3, legacy=legacy), "deploy")
    assert [e["stage"] for e in store.events] == [
        "phase 1/8 cleanup", "phase 2/8 engine_template", "phase 3/8 prepare_engine"]
    assert [(e["index"], e["total"]) for e in store.events] == [(1, 8), (2, 8), (3, 8)]


def test_scrim_uses_the_harness_phases_in_order(tmp_path, monkeypatch):
    ws = tmp_path / "comp"
    ws.mkdir()
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"run_id": "run-old"}, "run": {"last_phase": 8}}))
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
