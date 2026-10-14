"""The range model: one range per workspace, liveness from the workspace's own state."""

import json

from dashboard import config, ranges
from dashboard.runner import RunManager, RunRefused


class FakeStore:
    def __init__(self, runs):
        self.runs = sorted(runs, key=lambda r: r["created_at"], reverse=True)

    def list_runs(self, state=None, comp_id=None):
        return [r for r in self.runs if not comp_id or r["comp_id"] == comp_id]


def _rec(rid, action, comp="c", state="succeeded", rc=0, created=0.0, run_dir=None, params=None):
    return {"id": rid, "comp_id": comp, "action": action, "state": state, "returncode": rc,
            "created_at": created, "ended_at": created + 1, "params": params or {},
            "run_dir": str(run_dir), "stage": "", "log_path": None, "pid": None, "error": None}


def _workspace(tmp_path, rid, vms=False):
    ws = tmp_path / rid / "comp"
    ws.mkdir(parents=True)
    (ws / "Compfile").write_text("name c\n")
    (ws / ".deploy_state.json").write_text(json.dumps({"run_id": rid}))
    if vms:
        (ws / "terraform").mkdir()
        (ws / "terraform" / "terraform.tfstate").write_text(json.dumps({"resources": [
            {"type": "proxmox_virtual_environment_vm", "name": "team_box",
             "instances": [{"index_key": "team1-web01", "attributes": {"vm_id": 1332}}]},
        ]}))
    return ws


def _range_of(*runs):
    built = ranges.build_ranges(FakeStore(list(runs)))
    assert len(built) == 1, [r["state"] for r in built]
    return built[0]


def test_state_building(tmp_path):
    ws = _workspace(tmp_path, "run-b")
    rng = _range_of(_rec("run-b", "deploy", state="running", rc=None, created=1,
                         run_dir=tmp_path / "run-b"))
    assert rng["state"] == "building" and rng["live"] is True and rng["key"] == str(ws.resolve())


def test_state_live_from_the_workspaces_vms(tmp_path):
    ws = _workspace(tmp_path, "run-l", vms=True)
    rng = _range_of(_rec("run-l", "deploy", created=1, run_dir=tmp_path / "run-l"))
    assert (rng["state"], rng["live"]) == ("live", True)
    assert ranges.has_vms(str(ws)) is True


def test_failed_build_with_vms_still_up_is_live(tmp_path):
    """run-df29374d ended rc=1 while its range stayed deployed; the board must show it."""
    _workspace(tmp_path, "run-f", vms=True)
    rng = _range_of(_rec("run-f", "scrim", state="failed", rc=1, created=1,
                         run_dir=tmp_path / "run-f"))
    assert (rng["state"], rng["live"]) == ("live", True)


def test_state_retired(tmp_path):
    _workspace(tmp_path, "run-r")
    rng = _range_of(
        _rec("run-r", "deploy", created=1, run_dir=tmp_path / "run-r"),
        _rec("run-down", "destroy", created=2, run_dir=tmp_path / "run-down",
             params={"_destroy_comp_dir": str(tmp_path / "run-r" / "comp")}))
    assert (rng["state"], rng["live"]) == ("retired", False)
    assert rng["retired_by"] == "run-down" and rng["retired_at"] == 3.0


def test_state_teardown_failed_is_on_the_board(tmp_path):
    """Every destroy failed, so terraform's resources are gone but red01 may not be."""
    _workspace(tmp_path, "run-t")
    rng = _range_of(
        _rec("run-t", "deploy", state="failed", rc=1, created=1, run_dir=tmp_path / "run-t"),
        _rec("run-down", "destroy", state="failed", rc=1, created=2,
             run_dir=tmp_path / "run-down",
             params={"_destroy_comp_dir": str(tmp_path / "run-t" / "comp")}))
    assert (rng["state"], rng["live"]) == ("teardown_failed", True)


def test_state_failed_when_nothing_came_up(tmp_path):
    _workspace(tmp_path, "run-x")
    rng = _range_of(_rec("run-x", "deploy", state="failed", rc=1, created=1,
                         run_dir=tmp_path / "run-x"))
    assert (rng["state"], rng["live"]) == ("failed", False)
    assert rng["retired_by"] is None and rng["retired_at"] is None


def test_failed_destroy_does_not_retire(tmp_path):
    _workspace(tmp_path, "run-d", vms=True)
    rng = _range_of(
        _rec("run-d", "deploy", created=1, run_dir=tmp_path / "run-d"),
        _rec("run-down", "destroy", state="failed", rc=1, created=2,
             run_dir=tmp_path / "run-down",
             params={"_destroy_comp_dir": str(tmp_path / "run-d" / "comp")}))
    assert rng["state"] == "live" and rng["retired_by"] is None


def test_newest_successful_destroy_names_the_retirer(tmp_path):
    _workspace(tmp_path, "run-n")
    rng = _range_of(
        _rec("run-n", "deploy", created=1, run_dir=tmp_path / "run-n"),
        _rec("run-failed", "destroy", state="failed", rc=1, created=2,
             run_dir=tmp_path / "run-f1",
             params={"_destroy_comp_dir": str(tmp_path / "run-n" / "comp")}),
        _rec("run-down", "destroy", created=3, run_dir=tmp_path / "run-f2",
             params={"_destroy_comp_dir": str(tmp_path / "run-n" / "comp")}))
    assert rng["retired_by"] == "run-down" and rng["state"] == "retired"


def test_concurrent_deploys_of_one_comp_stay_two_ranges(tmp_path):
    _workspace(tmp_path, "run-a", vms=True)
    _workspace(tmp_path, "run-b", vms=True)
    built = ranges.build_ranges(FakeStore([
        _rec("run-a", "deploy", created=1, run_dir=tmp_path / "run-a"),
        _rec("run-b", "deploy", created=2, run_dir=tmp_path / "run-b")]))
    assert [r["deploy_run_id"] for r in built] == ["run-b", "run-a"]
    assert all(r["state"] == "live" for r in built)


def test_teams_come_from_the_deploy_run_not_a_later_destroy(tmp_path):
    _workspace(tmp_path, "run-1")
    rng = _range_of(
        _rec("run-1", "deploy", created=1, run_dir=tmp_path / "run-1", params={"teams": 1}),
        _rec("run-down", "destroy", created=2, run_dir=tmp_path / "run-d",
             params={"teams": 2, "_destroy_comp_dir": str(tmp_path / "run-1" / "comp")}))
    assert rng["teams"] == 1


def test_retired_candidate_refused_naming_the_retiring_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    ws = _workspace(tmp_path, "run-old")
    mgr = RunManager(FakeStore([
        _rec("run-old", "deploy", created=1, run_dir=tmp_path / "run-old"),
        _rec("run-down", "destroy", created=2, run_dir=tmp_path / "run-down",
             params={"_destroy_comp_dir": str(ws)})]), None)
    try:
        mgr._range_workspace("c", {}, "destroy")
        raise AssertionError("a retired range must refuse")
    except RunRefused as e:
        assert "the range in run-old was torn down by run-down" in str(e)
        assert "nothing to destroy" in str(e)


def test_newest_non_retired_candidate_wins(tmp_path):
    """Two live ranges of one competition both resolve; a destroy targets the newest."""
    _workspace(tmp_path, "run-old")
    live = _workspace(tmp_path, "run-new", vms=True)
    mgr = RunManager(FakeStore([
        _rec("run-old", "deploy", created=1, run_dir=tmp_path / "run-old"),
        _rec("run-down", "destroy", created=2, run_dir=tmp_path / "run-down",
             params={"_destroy_comp_dir": str(tmp_path / "run-old" / "comp")}),
        _rec("run-new", "deploy", created=3, run_dir=tmp_path / "run-new")]), None)
    assert mgr._range_workspace("c", {}, "destroy") == live


def test_teardown_failed_candidate_stays_resolvable(tmp_path):
    """A failed teardown is exactly the range an operator retries, so it must not be refused."""
    ws = _workspace(tmp_path, "run-t")
    mgr = RunManager(FakeStore([
        _rec("run-t", "deploy", state="failed", rc=1, created=1, run_dir=tmp_path / "run-t"),
        _rec("run-down", "destroy", state="failed", rc=1, created=2,
             run_dir=tmp_path / "run-d", params={"_destroy_comp_dir": str(ws)})]), None)
    assert mgr._range_workspace("c", {}, "destroy") == ws


def test_ranges_route(tmp_path, monkeypatch):
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(RunManager, "ensure_repos", lambda self: None)
    from fastapi.testclient import TestClient
    from dashboard.app import create_app
    assert TestClient(create_app()).get("/api/ranges").json() == []
