"""Pause requests hold a deploy at a phase boundary, and only there."""

import json
from pathlib import Path

import pytest

from dashboard.runner import RunManager, RunRefused


class FakeStore:
    def __init__(self, run):
        self.run = run
        self.events = []

    def get_run(self, run_id):
        return self.run if run_id == self.run["id"] else None

    def update_run(self, run_id, **fields):
        self.run.update(fields)

    def add_event(self, run_id, kind, stage="", data=None):
        event = {"seq": len(self.events) + 1, "kind": kind, "stage": stage, **(data or {})}
        self.events.append(event)
        return event


class FakeBus:
    def publish(self, run_id, event):
        pass


def _manager(tmp_path, run):
    store = FakeStore(run)
    mgr = RunManager(store, FakeBus())
    mgr.engine_repo = tmp_path / "engine"
    return mgr, store


def _run(tmp_path, action="deploy", state="running", engine_ref="main"):
    run_dir = tmp_path / "runs" / "run-1"
    (run_dir / "comp").mkdir(parents=True, exist_ok=True)
    return {"id": "run-1", "comp_id": "mini", "action": action, "state": state,
            "engine_ref": engine_ref, "run_dir": str(run_dir), "params": {},
            "stage": "", "returncode": None, "error": None}


def _cut_worktree(run, hook=True):
    """What provisioning leaves behind: the worktree, hook module present or not."""
    lib = Path(run["run_dir"]) / "engine" / "deploy_lib"
    lib.mkdir(parents=True, exist_ok=True)
    if hook:
        (lib / "pause.py").write_text("")


def test_pause_writes_a_per_run_request_and_marks_the_run_pausing(tmp_path):
    run = _run(tmp_path)
    _cut_worktree(run)
    mgr, store = _manager(tmp_path, run)
    result = mgr.pause("run-1")
    assert (Path(run["run_dir"]) / "pause").exists()
    assert store.run["state"] == "pausing"
    assert [e["state"] for e in store.events] == ["pausing"]
    assert store.events[0]["stage"] == "pause requested"
    assert result["run"]["state"] == "pausing" and "phase in progress" in result["detail"]


def test_pause_clicked_twice_is_one_request(tmp_path):
    run = _run(tmp_path)
    _cut_worktree(run)
    mgr, store = _manager(tmp_path, run)
    mgr.pause("run-1")
    mgr.pause("run-1")
    assert [e["state"] for e in store.events] == ["pausing"]


def test_pause_refuses_an_action_without_boundaries(tmp_path):
    run = _run(tmp_path, action="verify")
    _cut_worktree(run)
    mgr, _ = _manager(tmp_path, run)
    with pytest.raises(RunRefused, match="no phase boundary"):
        mgr.pause("run-1")
    assert not (Path(run["run_dir"]) / "pause").exists()


def test_pause_refuses_a_cut_worktree_without_the_hook(tmp_path):
    run = _run(tmp_path)
    _cut_worktree(run, hook=False)
    mgr, _ = _manager(tmp_path, run)
    with pytest.raises(RunRefused, match="predates the boundary hook"):
        mgr.pause("run-1")
    assert not (Path(run["run_dir"]) / "pause").exists()


def test_pause_of_a_queued_run_fetches_before_it_probes_the_ref(tmp_path, monkeypatch):
    # The clone's last fetch is stale exactly when the click lands mid-provisioning, so the
    # ref is only trustworthy after the same fetch provisioning would do.
    run = _run(tmp_path, state="queued")
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return type("P", (), {"returncode": 0})()

    monkeypatch.setattr("dashboard.runner.subprocess.run", fake_run)
    mgr, _ = _manager(tmp_path, run)
    mgr.pause("run-1")
    assert Path(run["run_dir"], "pause").exists()
    assert seen[0][-3:] == ["fetch", "origin", "--quiet"]
    assert "cat-file" in seen[-1] and seen[-1][-1].endswith(":deploy_lib/pause.py")


def test_pause_of_a_queued_run_is_refused_when_the_ref_has_no_hook(tmp_path, monkeypatch):
    run = _run(tmp_path, state="queued")
    monkeypatch.setattr("dashboard.runner.subprocess.run", lambda cmd, **kw: type(
        "P", (), {"returncode": 0 if "cat-file" not in cmd else 128})())
    mgr, _ = _manager(tmp_path, run)
    with pytest.raises(RunRefused, match="predates the boundary hook"):
        mgr.pause("run-1")


def test_pause_refuses_a_deploy_past_its_last_phase(tmp_path):
    run = _run(tmp_path)
    _cut_worktree(run)
    state = Path(run["run_dir"]) / "comp" / "estate-state.json"
    state.write_text(json.dumps({"identity": {"run_id": "run-1"},
                                 "run": {"last_phase": 8}}))
    mgr, _ = _manager(tmp_path, run)
    with pytest.raises(RunRefused, match="no boundary left"):
        mgr.pause("run-1")


def test_resume_withdraws_a_pending_request(tmp_path):
    run = _run(tmp_path)
    _cut_worktree(run)
    mgr, store = _manager(tmp_path, run)
    mgr.pause("run-1")
    mgr.resume("run-1")
    assert not (Path(run["run_dir"]) / "pause").exists()
    assert store.run["state"] == "running"
    assert [e["state"] for e in store.events] == ["pausing", "running"]


def test_resume_of_a_held_run_leaves_the_announcement_to_the_tracker(tmp_path):
    run = _run(tmp_path, state="paused")
    request = Path(run["run_dir"]) / "pause"
    request.touch()
    mgr, store = _manager(tmp_path, run)
    mgr.resume("run-1")
    assert not request.exists()
    # Still "paused" until the driver clears its marker and the tracker sees it: two writers
    # for one transition would flip the chip twice.
    assert store.run["state"] == "paused"
    assert [e["state"] for e in store.events] == ["resume_requested"]


def test_resume_refuses_a_run_that_is_not_held(tmp_path):
    run = _run(tmp_path)
    mgr, _ = _manager(tmp_path, run)
    with pytest.raises(RunRefused, match="nothing to resume"):
        mgr.resume("run-1")


def test_the_tracker_mirrors_the_hold_and_the_resume(tmp_path):
    run = _run(tmp_path)
    mgr, store = _manager(tmp_path, run)
    ws = Path(run["run_dir"]) / "comp"
    state = ws / "estate-state.json"

    def write(last_phase, held=None):
        body = {"identity": {"run_id": "run-1"}, "run": {"last_phase": last_phase}}
        if held is not None:
            body["run"]["paused_after_phase"] = held
        state.write_text(json.dumps(body))

    write(3, 3)
    assert mgr._reflect_pause("run-1", ws, None) == 3
    assert store.run["state"] == "paused" and store.run["stage"] == "paused after phase 3/8"
    assert [e["state"] for e in store.events] == ["paused"]
    # The same marker seen again is not a second transition.
    assert mgr._reflect_pause("run-1", ws, 3) == 3
    write(3)
    assert mgr._reflect_pause("run-1", ws, 3) is None
    assert store.run["state"] == "running"
    assert [e["state"] for e in store.events] == ["paused", "running"]
