"""The range model: one range per workspace, liveness from the workspace's own state."""

import hashlib
import json

import pytest

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


def _doc_workspace(tmp_path, rid, vms=True):
    """A v0.3.0 workspace: the estate document, no terraform state at all."""
    ws = tmp_path / rid / "comp"
    ws.mkdir(parents=True)
    (ws / "Compfile").write_text("name c\n")
    resources = ([{"kind": "engine", "node": "pve", "name": "quotient-engine", "vmid": 1000,
                   "bridge": None, "tags": ["tezcatlipoca"], "at": "t"}]
                 if vms else [])
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"comp_name": "c", "run_id": rid}, "resources": resources}))
    return ws


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


# ── v0.3.0: the estate document is the liveness authority ───────────────────────────────

def test_state_live_from_the_estate_documents_vms(tmp_path):
    ws = _doc_workspace(tmp_path, "run-doc", vms=True)
    rng = _range_of(_rec("run-doc", "deploy", created=1, run_dir=tmp_path / "run-doc"))
    assert (rng["state"], rng["live"]) == ("live", True)
    assert ranges.has_vms(str(ws)) is True


def test_a_document_without_vms_is_not_live(tmp_path):
    """v0.3.0's destroy forgets each resource as the VM goes, so an empty list IS 'gone'."""
    ws = _doc_workspace(tmp_path, "run-doc", vms=False)
    rng = _range_of(_rec("run-doc", "deploy", created=1, run_dir=tmp_path / "run-doc"))
    assert (rng["state"], rng["live"]) == ("failed", False)
    assert ranges.has_vms(str(ws)) is False


def test_a_document_with_only_bridges_is_not_live(tmp_path):
    """Bridges carry no vmid: they are networks, not the range's VMs."""
    ws = tmp_path / "run-b" / "comp"
    ws.mkdir(parents=True)
    (ws / "estate-state.json").write_text(json.dumps({
        "identity": {"run_id": "run-b"},
        "resources": [{"kind": "bridge", "node": "pve", "name": "vmbr113", "vmid": None,
                       "bridge": None, "tags": [], "at": "t"}]}))
    assert ranges.has_vms(str(ws)) is False


def test_a_retired_document_retires(tmp_path):
    _doc_workspace(tmp_path, "run-doc", vms=False)
    rng = _range_of(
        _rec("run-doc", "deploy", created=1, run_dir=tmp_path / "run-doc"),
        _rec("run-down", "destroy", created=2, run_dir=tmp_path / "run-down",
             params={"_destroy_comp_dir": str(tmp_path / "run-doc" / "comp")}))
    assert (rng["state"], rng["live"]) == ("retired", False)


def test_a_failed_teardown_that_still_records_a_vm_stays_live(tmp_path):
    """A refusal leaves its resource recorded, so the board must keep showing the range."""
    _doc_workspace(tmp_path, "run-doc", vms=True)
    rng = _range_of(
        _rec("run-doc", "deploy", state="failed", rc=1, created=1, run_dir=tmp_path / "run-doc"),
        _rec("run-down", "destroy", state="failed", rc=1, created=2,
             run_dir=tmp_path / "run-down",
             params={"_destroy_comp_dir": str(tmp_path / "run-doc" / "comp")}))
    assert (rng["state"], rng["live"]) == ("live", True)


def test_a_migrated_workspace_reads_liveness_from_its_terraform_state(tmp_path):
    """A document with an empty resources list (estate.state.migrate builds none) must not
    hide a live pre-v0.3 range: terraform's state is still its VM inventory."""
    ws = tmp_path / "run-m" / "comp"
    ws.mkdir(parents=True)
    (ws / "Compfile").write_text("name c\n")
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"comp_name": "c", "run_id": "run-m"}, "resources": []}))
    (ws / "terraform").mkdir()
    (ws / "terraform" / "terraform.tfstate").write_text(json.dumps({"resources": [
        {"type": "proxmox_virtual_environment_vm", "name": "team_box",
         "instances": [{"index_key": "team1-web01", "attributes": {"vm_id": 1332}}]}]}))
    assert ranges.has_vms(str(ws)) is True
    rng = _range_of(_rec("run-m", "deploy", created=1, run_dir=tmp_path / "run-m"))
    assert (rng["state"], rng["live"]) == ("live", True)
    # and once the v0.3.0 estate's teardown has emptied it, nothing stands in for the VMs
    (ws / "terraform" / "terraform.tfstate").write_text(json.dumps({"resources": []}))
    assert ranges.has_vms(str(ws)) is False


def test_a_teardown_that_empties_the_document_and_fails_is_teardown_failed(tmp_path):
    """The resources are gone but the destroy ended non-zero: red01 may still be out there."""
    _doc_workspace(tmp_path, "run-doc", vms=False)
    rng = _range_of(
        _rec("run-doc", "deploy", created=1, run_dir=tmp_path / "run-doc"),
        _rec("run-down", "destroy", state="failed", rc=1, created=2,
             run_dir=tmp_path / "run-down",
             params={"_destroy_comp_dir": str(tmp_path / "run-doc" / "comp")}))
    assert (rng["state"], rng["live"]) == ("teardown_failed", True)


def test_range_id_is_opaque_stable_and_uses_the_resolved_workspace(tmp_path):
    ws = _workspace(tmp_path, "run-origin")
    alias = tmp_path / "alias"
    alias.symlink_to(ws, target_is_directory=True)
    expected = "range-" + hashlib.sha256(str(ws.resolve()).encode()).hexdigest()[:16]
    assert ranges.range_id(alias) == ranges.range_id(ws) == expected
    deploy = _rec("run-origin", "deploy", created=1, run_dir=ws.parent)
    followup = _rec("run-check", "verify", created=2, run_dir=tmp_path / "run-check",
                    params={"_destroy_comp_dir": str(alias)})
    assert _range_of(deploy, followup)["id"] == expected
    assert ranges.range_id(tmp_path / "other" / "comp") != expected


def test_explicit_scrim_joins_range_without_replacing_origin(tmp_path):
    ws = _workspace(tmp_path, "run-origin", vms=True)
    rng = _range_of(
        _rec("run-origin", "deploy", created=1, run_dir=ws.parent, params={"teams": 1}),
        _rec("run-scrim", "scrim", created=2, run_dir=tmp_path / "run-scrim",
             params={"comp_dir": str(ws), "skip_deploy": True, "teams": 2}))
    assert rng["deploy_run_id"] == "run-origin" and rng["teams"] == 1


@pytest.mark.parametrize("action", ["verify", "destroy", "redeploy", "start", "freeze",
                                    "resume", "end", "delete_team", "scrim"])
def test_explicit_scope_rejects_other_comp_unknown_and_retired(tmp_path, action):
    ws = _workspace(tmp_path, "run-owned")
    origin = _rec("run-owned", "deploy", created=1, run_dir=ws.parent)
    mgr = RunManager(FakeStore([origin]), None)
    assert mgr._range_workspace("c", {"comp_dir": str(ws)}, action) == ws
    with pytest.raises(RunRefused, match="does not belong"):
        mgr._range_workspace("other-comp", {"comp_dir": str(ws)}, action)
    with pytest.raises(RunRefused, match="does not belong"):
        mgr._range_workspace("c", {"comp_dir": str(tmp_path / "untracked")}, action)
    mgr.store = FakeStore([origin, _rec("run-down", "destroy", created=2,
        run_dir=tmp_path / "run-down", params={"_destroy_comp_dir": str(ws)})])
    with pytest.raises(RunRefused, match="was torn down by run-down"):
        mgr._range_workspace("c", {"comp_dir": str(ws)}, action)


def test_explicit_scope_can_retry_a_failed_teardown(tmp_path):
    ws = _workspace(tmp_path, "run-owned")
    mgr = RunManager(FakeStore([
        _rec("run-owned", "deploy", created=1, run_dir=ws.parent),
        _rec("run-down", "destroy", state="failed", rc=1, created=2,
             run_dir=tmp_path / "run-down", params={"_destroy_comp_dir": str(ws)})]), None)
    assert mgr._range_workspace("c", {"comp_dir": str(ws)}, "destroy") == ws


def test_board_and_detail_are_local_non_secret_and_workspace_scoped(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from dashboard import competition
    from dashboard.app import create_app
    from dashboard.store import Store

    data = tmp_path / "data"
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(data))
    store = Store(data / "dashboard.sqlite3")
    ws = _doc_workspace(tmp_path, "run-origin")
    (ws / "Compfile").write_text("name Original range\nadmin_password RANGE-SECRET\n")
    (ws / "estate-state.json").write_text(json.dumps({
        "identity": {"run_id": "run-origin", "engine_mgmt_ip": "10.0.0.42"},
        "secrets": {"admin_password": "RANGE-SECRET",
                    "teams": {"team1": {"identifier": "113", "password": "RANGE-SECRET"}}},
        "roster": {"boxes": [{"name": "web01", "last_octet": 4}]},
        "run": {"degradations": [{"step": "repair", "detail": "web01 lagged"}]},
        "resources": [
            {"kind": "engine", "name": "engine", "node": "pve", "vmid": 1000},
            {"kind": "bridge", "name": "vmbr113", "node": "pve"},
            {"kind": "team_box", "name": "team1-web01", "node": "sat", "vmid": 1001},
            {"kind": "golden", "name": "golden-web01", "node": "templates", "vmid": 1002}]}))
    other = _doc_workspace(tmp_path, "run-other", vms=False)
    for rid, action, workspace, params, state, rc, created, ref in [
        ("run-origin", "deploy", ws, {"teams": 1, "password": "RANGE-SECRET",
         "extra_args": ["--password", "RANGE-SECRET"]}, "failed", 1, 1, "origin-ref"),
        ("run-down", "destroy", ws, {"_destroy_comp_dir": str(ws)}, "failed", 1, 2, "new-ref"),
        ("run-check", "verify", ws, {"_destroy_comp_dir": str(ws)}, "paused", None, 3, "new-ref"),
        ("run-other", "deploy", other, {"teams": 2}, "succeeded", 0, 4, "other-ref"),
    ]:
        run_dir = workspace.parent if action == "deploy" else tmp_path / rid
        store.create_run(rid, "c", action, ref, params, run_dir, run_dir / "run.log")
        store.update_run(rid, state=state, returncode=rc, error="RANGE-SECRET")
        # Explicit ordering makes this test independent of wall-clock resolution.
        with store._lock:
            store._db.execute("UPDATE runs SET created_at = ? WHERE id = ?", (created, rid))
            store._db.commit()
    for workspace in (ws, other):
        store.upsert_requests(str(workspace), "c", [
            {"id": 1, "team": "team1", "box": "web01"},
            {"id": 2, "team": "team2", "box": "web01"}])
    own_req = next(req for req in store.list_requests()
                   if req["range_key"] == str(ws) and req["portal_id"] == "2")
    store.set_request_status(own_req["id"], "denied")
    token = store.mint_range_token(str(ws), "c")

    def no_poll(*args, **kwargs):
        raise AssertionError("range endpoints must not poll the engine or expose credentials")

    monkeypatch.setattr(competition, "engine_status", no_poll)
    monkeypatch.setattr(competition, "status_for_range", no_poll)
    http = TestClient(create_app())
    original = ranges.build_ranges
    calls = []

    def counted(store):
        calls.append(1)
        return original(store)

    monkeypatch.setattr(ranges, "build_ranges", counted)
    response = http.get("/api/ranges/board")
    assert response.status_code == 200 and len(calls) == 1
    board = response.json()
    assert len(board) == 2 and board[0]["id"] != board[1]["id"]
    summary = next(row for row in board if row["key"] == str(ws))
    assert summary["name"] == "Original range"
    assert summary["state"] == "building" and summary["live"] and summary["has_recorded_vms"]
    assert summary["counts"] == {"teams": 1, "boxes": 1, "networks": 1}
    assert summary["nodes"] == ["pve", "sat", "templates"]
    assert summary["engine_ip"] == "10.0.0.42" and summary["engine_ref"] == "origin-ref"
    assert summary["pending_requests"] == 1
    assert summary["latest_run"]["id"] == "run-check"
    assert summary["latest_destroy"]["id"] == "run-down"
    assert [run["id"] for run in summary["active_runs"]] == ["run-check"]
    assert summary["degradations"] == [{"step": "repair", "detail": "web01 lagged"}]
    assert summary["deployed_at"] == 1
    assert set(summary["event"]) == {"seeded", "hold_start", "ended"}
    assert summary["links"]["scoreboard"] == "http://10.0.0.42"
    assert not {"runs", "inventory", "requests", "params", "creds"}.intersection(summary)
    detail_response = http.get(f"/api/ranges/{summary['id']}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["id"] == summary["id"] and detail["teams"] == 1
    assert detail["name"] == "Original range" and detail["has_recorded_vms"]
    assert detail["engine_ref"] == detail["inventory"]["range"]["engine_ref"] == "origin-ref"
    assert [run["id"] for run in detail["runs"]] == ["run-check", "run-down", "run-origin"]
    assert detail["runs"][-1]["params"] == {"teams": 1}
    assert len(detail["requests"]) == 2
    assert all(req["range_key"] == str(ws) for req in detail["requests"])
    assert detail["inventory"]["range"]["id"] == summary["id"]
    assert "competition" not in detail and "creds" not in detail
    for payload in (board, detail):
        blob = json.dumps(payload)
        assert "RANGE-SECRET" not in blob and token not in blob
        assert "extra_args" not in blob and "admin_password" not in blob
    assert http.get("/api/ranges/range-missing").status_code == 404
    compatible = http.get("/api/ranges").json()
    assert {row["id"] for row in compatible} == {row["id"] for row in board}
    assert all("runs" in row for row in compatible)
