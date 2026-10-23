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

    def list_range_retires(self):
        return {}


def _rec(rid, action, comp="c", state="succeeded", rc=0, created=0.0, run_dir=None, params=None):
    return {"id": rid, "comp_id": comp, "action": action, "state": state, "returncode": rc,
            "created_at": created, "ended_at": created + 1, "params": params or {},
            "run_dir": str(run_dir), "stage": "", "log_path": None, "pid": None, "error": None}


def _workspace(tmp_path, rid, vms=False):
    """A run workspace: the estate document is the range's whole state."""
    ws = tmp_path / rid / "comp"
    ws.mkdir(parents=True)
    (ws / "Compfile").write_text("name c\n")
    resources = ([{"kind": "engine", "node": "pve", "name": "quotient-engine", "vmid": 1000,
                   "bridge": None, "tags": ["tezcatlipoca"], "at": "t"}]
                 if vms else [])
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"comp_name": "c", "run_id": rid, "engine_mgmt_ip": "10.0.0.9"},
         "resources": resources}))
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
    """Every destroy failed, so the estate's resources are gone but red01 may not be."""
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
    """The retired older range is skipped; the workspace that records VMs resolves."""
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


def test_a_stub_document_does_not_shadow_the_live_range(tmp_path):
    """The refused run-791e0f7b left a seeded document with a run id but no inventory; ranked
    by run-dir recency it won over run-ab512e1b's live range and every range action died on
    `no engine management IP recorded` (2026-10-15)."""
    live = _workspace(tmp_path, "run-live", vms=True)
    _workspace(tmp_path, "run-stub")                      # identity + empty inventory
    mgr = RunManager(FakeStore([
        _rec("run-stub", "deploy", state="failed", rc=1, created=2,
             run_dir=tmp_path / "run-stub"),
        _rec("run-live", "deploy", created=1, run_dir=tmp_path / "run-live")]), None)
    assert mgr._range_workspace("c", {}, "start") == live


def test_an_ambiguous_resolver_refuses_naming_both(tmp_path):
    """Two workspaces that both record a deployed range: picking one would act on the other
    range's VMs, so the resolver names both instead of guessing."""
    a = _workspace(tmp_path, "run-a", vms=True)
    b = _workspace(tmp_path, "run-b", vms=True)
    mgr = RunManager(FakeStore([
        _rec("run-b", "deploy", created=2, run_dir=tmp_path / "run-b"),
        _rec("run-a", "deploy", created=1, run_dir=tmp_path / "run-a")]), None)
    with pytest.raises(RunRefused) as refused:
        mgr._range_workspace("c", {}, "destroy")
    assert "run-b" in str(refused.value) and "run-a" in str(refused.value)
    assert str(a) in str(refused.value) and str(b) in str(refused.value)
    assert mgr._range_workspace("c", {"comp_dir": str(b)}, "destroy") == b


def test_an_orphan_reclaim_retires_the_range(tmp_path):
    """`--orphan-comp/--orphan-run` destroys the VMs by tag and deletes the workspace, so no
    destroy run ever reaches the store; without a retire record the stale document keeps
    reading `live` (crimson-relay range-b289904cf2d5ccdf)."""
    from dashboard.store import Store

    ws = _workspace(tmp_path, "run-o", vms=True)
    run_dir = ws.parent
    store = Store(tmp_path / "db.sqlite3")
    store.create_run("run-o", "c", "deploy", "main", {}, run_dir, run_dir / "run.log")
    store.update_run("run-o", state="succeeded", returncode=0)
    before = ranges.build_ranges(store)[0]
    assert (before["state"], before["has_recorded_vms"]) == ("live", True)

    store.retire_range(ws, "c", "destroy-competition.py --orphan-run run-o")
    rng = ranges.build_ranges(store)[0]
    assert (rng["state"], rng["live"], rng["has_recorded_vms"]) == ("retired", False, False)
    assert rng["retired_by"] == "destroy-competition.py --orphan-run run-o"
    assert rng["retired_at"] is not None


def test_the_retire_endpoint_records_an_orphan_reclaim(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from dashboard.app import create_app
    from dashboard.store import Store

    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    ws = _workspace(tmp_path, "run-o", vms=True)
    run_dir = ws.parent
    store = Store(tmp_path / "data" / "dashboard.sqlite3")
    store.create_run("run-o", "c", "deploy", "main", {}, run_dir, run_dir / "run.log")
    store.update_run("run-o", state="succeeded", returncode=0)

    http = TestClient(create_app())
    body = {"range": str(ws), "by": "destroy-competition.py --orphan-run run-o"}
    assert http.post("/api/ranges/retire", json=body).status_code == 200
    row = http.get("/api/ranges").json()[0]
    assert row["state"] == "retired" and row["has_recorded_vms"] is False
    assert row["retired_by"] == body["by"]
    # `--orphan-run` names the estate document's run id, not the workspace path.
    doc = json.loads((ws / "estate-state.json").read_text())
    doc["identity"]["run_id"] = "run-engineid"
    (ws / "estate-state.json").write_text(json.dumps(doc))
    assert http.post("/api/ranges/retire",
                     json={"run": "run-engineid", "by": "orphan"}).json()["retired_by"] == "orphan"
    # A typo must not retire some other range.
    assert http.post("/api/ranges/retire",
                     json={"range": str(tmp_path / "nope")}).status_code == 404
    assert http.post("/api/ranges/retire", json={"run": "run-nope"}).status_code == 404


def test_ranges_route(tmp_path, monkeypatch):
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(RunManager, "ensure_repos", lambda self: None)
    from fastapi.testclient import TestClient
    from dashboard.app import create_app
    assert TestClient(create_app()).get("/api/ranges").json() == []


# ── the estate document is the liveness authority ───────────────────────────────────────

def test_state_live_from_the_estate_documents_vms(tmp_path):
    ws = _workspace(tmp_path, "run-doc", vms=True)
    rng = _range_of(_rec("run-doc", "deploy", created=1, run_dir=tmp_path / "run-doc"))
    assert (rng["state"], rng["live"]) == ("live", True)
    assert ranges.has_vms(str(ws)) is True


def test_a_document_without_vms_is_not_live(tmp_path):
    """The destroy forgets each resource as the VM goes, so an empty list IS 'gone'."""
    ws = _workspace(tmp_path, "run-doc", vms=False)
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
    _workspace(tmp_path, "run-doc", vms=False)
    rng = _range_of(
        _rec("run-doc", "deploy", created=1, run_dir=tmp_path / "run-doc"),
        _rec("run-down", "destroy", created=2, run_dir=tmp_path / "run-down",
             params={"_destroy_comp_dir": str(tmp_path / "run-doc" / "comp")}))
    assert (rng["state"], rng["live"]) == ("retired", False)


def test_a_failed_teardown_that_still_records_a_vm_stays_live(tmp_path):
    """A refusal leaves its resource recorded, so the board must keep showing the range."""
    _workspace(tmp_path, "run-doc", vms=True)
    rng = _range_of(
        _rec("run-doc", "deploy", state="failed", rc=1, created=1, run_dir=tmp_path / "run-doc"),
        _rec("run-down", "destroy", state="failed", rc=1, created=2,
             run_dir=tmp_path / "run-down",
             params={"_destroy_comp_dir": str(tmp_path / "run-doc" / "comp")}))
    assert (rng["state"], rng["live"]) == ("live", True)


def test_a_teardown_that_empties_the_document_and_fails_is_teardown_failed(tmp_path):
    """The resources are gone but the destroy ended non-zero: red01 may still be out there."""
    _workspace(tmp_path, "run-doc", vms=False)
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
    ws = _workspace(tmp_path, "run-origin")
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
    other = _workspace(tmp_path, "run-other", vms=False)
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
    from dashboard import scoring_cache

    def canned_snapshot(rng):
        # Live scoring reaches the board through the cached background poll; this
        # test pins the request path, so serve a canned entry with no threads.
        assert rng["has_recorded_vms"], "only ranges with recorded VMs are polled"
        return {"state": "running", "round": 3, "reachable": True}

    monkeypatch.setattr(scoring_cache, "snapshot", canned_snapshot)
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
    assert summary["teams"] == 1  # the card reports the RANGE deploy team count
    assert summary["nodes"] == ["pve", "sat", "templates"]
    assert summary["engine_ip"] == "10.0.0.42" and summary["engine_ref"] == "origin-ref"
    assert summary["pending_requests"] == 1
    assert summary["latest_run"]["id"] == "run-check"
    assert summary["latest_destroy"]["id"] == "run-down"
    assert [run["id"] for run in summary["active_runs"]] == ["run-check"]
    assert summary["degradations"] == [{"step": "repair", "detail": "web01 lagged"}]
    assert summary["deployed_at"] == 1
    assert summary["scoring"] == {"state": "running", "round": 3, "reachable": True}
    other_row = next(row for row in board if row["key"] == str(other))
    assert other_row["scoring"] is None and not other_row["live"]
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


def _kept_template_workspace(tmp_path, rid):
    """The document a `--keep-templates` teardown leaves behind: templates only."""
    ws = tmp_path / rid / "comp"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "Compfile").write_text("name c\n")
    (ws / "estate-state.json").write_text(json.dumps({
        "identity": {"comp_name": "c", "run_id": rid},
        "resources": [
            {"kind": "engine_template", "node": "pve", "name": "engine-template", "vmid": 2241},
            {"kind": "golden", "node": "pve", "name": "golden-web01", "vmid": 2251},
        ]}))
    return ws


def test_kept_templates_are_not_a_live_range(tmp_path):
    """Four ranges boarded `live` after a clean destroy that kept their templates."""
    ws = _kept_template_workspace(tmp_path, "run-kt")
    rng = _range_of(_rec("run-kt", "deploy", created=1, run_dir=tmp_path / "run-kt"),
                    _rec("run-kd", "destroy", created=2, run_dir=tmp_path / "run-kt"))
    assert (rng["state"], rng["live"]) == ("retired", False)
    assert ranges.has_vms(str(ws)) is False


def test_kept_templates_without_a_destroy_are_not_live(tmp_path):
    _kept_template_workspace(tmp_path, "run-kt2")
    rng = _range_of(_rec("run-kt2", "deploy", created=1, run_dir=tmp_path / "run-kt2"))
    assert (rng["state"], rng["live"]) == ("failed", False)


def _retired_kept_store(tmp_path, *rids):
    runs = []
    for n, rid in enumerate(rids, start=1):
        ws = _kept_template_workspace(tmp_path, rid)
        runs.append(_rec(rid, "deploy", created=n * 2, run_dir=tmp_path / rid))
        runs.append(_rec(f"{rid}-down", "destroy", created=n * 2 + 1,
                         run_dir=tmp_path / f"{rid}-down",
                         params={"_destroy_comp_dir": str(ws)}))
    return FakeStore(runs)


def test_a_retired_range_with_kept_templates_resolves_for_a_full_destroy(tmp_path):
    """`--teams-only` retires the range and keeps the engine template + goldens; a full
    teardown is the only path that can reclaim them, so destroy must reach that workspace."""
    ws = _kept_template_workspace(tmp_path, "run-kt")
    mgr = RunManager(_retired_kept_store(tmp_path, "run-kt"), None)
    assert mgr._range_workspace("c", {}, "destroy") == ws

    # The board sees the kept rows as reclaimable WITHOUT calling the retired range live.
    rng = _range_of(*mgr.store.runs)
    assert (rng["state"], rng["live"]) == ("retired", False)
    assert rng["reclaimable_templates"] is True
    assert [row["kind"] for row in rng["kept_templates"]] == ["engine_template", "golden"]
    assert {row["vmid"] for row in rng["kept_templates"]} == {2241, 2251}


def test_a_retired_range_with_an_empty_inventory_still_refuses_a_destroy(tmp_path):
    """Only a KEPT template inventory is reclaimable; an emptied range has nothing to destroy."""
    ws = _workspace(tmp_path, "run-empty")
    mgr = RunManager(FakeStore([
        _rec("run-empty", "deploy", created=1, run_dir=tmp_path / "run-empty"),
        _rec("run-empty-down", "destroy", created=2, run_dir=tmp_path / "run-empty-down",
             params={"_destroy_comp_dir": str(ws)})]), None)
    with pytest.raises(RunRefused, match="nothing to destroy"):
        mgr._range_workspace("c", {}, "destroy")


def test_a_retired_kept_range_still_refuses_verify(tmp_path):
    """The reclaim exception is destroy-only: verify has nothing to verify in a retired range."""
    _kept_template_workspace(tmp_path, "run-ktv")
    mgr = RunManager(_retired_kept_store(tmp_path, "run-ktv"), None)
    with pytest.raises(RunRefused, match="nothing to verify"):
        mgr._range_workspace("c", {}, "verify")


def test_two_retired_kept_ranges_refuse_naming_both(tmp_path):
    """Two retired workspaces both keep templates: picking one would act on the other's VMs,
    exactly like the live-ambiguity refusal."""
    a = _kept_template_workspace(tmp_path, "run-ka")
    b = _kept_template_workspace(tmp_path, "run-kb")
    mgr = RunManager(_retired_kept_store(tmp_path, "run-ka", "run-kb"), None)
    with pytest.raises(RunRefused) as refused:
        mgr._range_workspace("c", {}, "destroy")
    assert "kept templates" in str(refused.value)
    assert "run-ka" in str(refused.value) and "run-kb" in str(refused.value)
    assert str(a) in str(refused.value) and str(b) in str(refused.value)
    # an explicit comp_dir disambiguates the same way it does for live ranges
    assert mgr._range_workspace("c", {"comp_dir": str(b)}, "destroy") == b


def test_an_explicit_retired_kept_workspace_resolves_for_destroy_only(tmp_path):
    ws = _kept_template_workspace(tmp_path, "run-kx")
    mgr = RunManager(_retired_kept_store(tmp_path, "run-kx"), None)
    assert mgr._range_workspace("c", {"comp_dir": str(ws)}, "destroy") == ws
    with pytest.raises(RunRefused, match="nothing to verify"):
        mgr._range_workspace("c", {"comp_dir": str(ws)}, "verify")
    # an explicit retired workspace with no kept inventory keeps refusing too
    empty = _workspace(tmp_path, "run-ex")
    empty_mgr = RunManager(FakeStore([
        _rec("run-ex", "deploy", created=1, run_dir=tmp_path / "run-ex"),
        _rec("run-ex-down", "destroy", created=2, run_dir=tmp_path / "run-ex-down",
             params={"_destroy_comp_dir": str(empty)})]), None)
    with pytest.raises(RunRefused, match="nothing to destroy"):
        empty_mgr._range_workspace("c", {"comp_dir": str(empty)}, "destroy")


def test_a_full_destroy_for_a_frozen_comp_still_forwards_end_of_competition():
    """A frozen range is destroyed with the flag that freezes scoring first; reclaiming a
    retired range must not drop it."""
    from dashboard.runner import build_command
    cmd = build_command("destroy", "c", "/ws", {"end_of_competition": True})
    assert "--end-of-competition" in cmd
    assert "--end-of-competition" not in build_command("destroy", "c", "/ws", {})


def test_a_scrim_that_tore_its_own_range_down_retires_it(tmp_path):
    """run-952b279e: a 30-minute scrim ran destroy-competition in its teardown stage and
    the range read `failed`, because only an action literally named destroy retired."""
    _workspace(tmp_path, "run-s", vms=False)
    rng = _range_of(_rec("run-s", "scrim", created=2, run_dir=tmp_path / "run-s"),
                    _rec("run-d", "deploy", created=1, run_dir=tmp_path / "run-s"))
    assert (rng["state"], rng["live"]) == ("retired", False)
    assert rng["retired_by"] == "run-s"


def test_a_scrim_that_kept_its_range_stays_live(tmp_path):
    _workspace(tmp_path, "run-sk", vms=True)
    rng = _range_of(_rec("run-sk", "scrim", created=1, run_dir=tmp_path / "run-sk"))
    assert (rng["state"], rng["live"]) == ("live", True)


def test_reconcile_orphans_closes_a_run_no_worker_holds(tmp_path):
    """run-31072cb0: the worker died before its try block, leaving the row queued forever."""
    from dashboard.events import EventBus
    from dashboard.store import Store

    store = Store(tmp_path / "db.sqlite3")
    store.create_run("run-ghost", "c", "deploy", "main", {}, tmp_path / "g", None)
    store.create_run("run-live", "c", "deploy", "main", {}, tmp_path / "l", None)
    mgr = RunManager(store, EventBus())
    mgr._procs["run-live"] = object()  # spawned this lifetime — not an orphan

    assert set(mgr.reconcile_orphans()) == {"run-ghost"}
    ghost = store.get_run("run-ghost")
    assert ghost["state"] == "failed" and "orphaned" in ghost["error"]
    assert store.get_run("run-live")["state"] == "queued"
    assert mgr.reconcile_orphans() == []


def test_startup_reconciles_a_run_whose_worker_is_gone(tmp_path, monkeypatch):
    """Entering the app's lifespan is what closes the orphan — construction must not."""
    from fastapi.testclient import TestClient
    from dashboard.app import create_app
    from dashboard.store import Store

    data = tmp_path / "data"
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(data))
    store = Store(data / "dashboard.sqlite3")
    store.create_run("run-stuck", "c", "deploy", "main", {}, tmp_path / "s", None)
    assert store.get_run("run-stuck")["state"] == "queued"  # create_app() alone leaves it

    with TestClient(create_app()):
        pass
    row = store.get_run("run-stuck")
    assert row["state"] == "failed" and "orphaned" in row["error"]


# ---- what a range summary publishes about a range that is not all there ----

_EMPTY_INVENTORY = {"machines": {}, "networks": [], "teams": [], "counts": {},
                    "deploy": {"degradations": []}}


def test_the_board_summary_carries_kept_and_reclaimable_template_info(tmp_path):
    """The board can offer the reclaim (and see the kept rows) without the range reading
    live: the state stays `retired`."""
    ws = _kept_template_workspace(tmp_path, "run-ktb")
    rng = _range_of(
        _rec("run-ktb", "deploy", created=1, run_dir=tmp_path / "run-ktb"),
        _rec("run-ktb-down", "destroy", created=2, run_dir=tmp_path / "run-ktb-down",
             params={"_destroy_comp_dir": str(ws)}))
    summary = ranges.range_summary(rng, _EMPTY_INVENTORY, [])
    assert summary["state"] == "retired" and summary["live"] is False
    assert summary["reclaimable_templates"] is True
    assert [row["kind"] for row in summary["kept_templates"]] == ["engine_template", "golden"]
    assert summary["workspace_missing"] is False


def test_a_non_retired_range_whose_workspace_is_gone_reports_it(tmp_path):
    """A run row whose workspace vanished must not silently become an empty/absent range: the
    board (and inventory) carry workspace_missing, and the state is not reinterpreted."""
    from dashboard.inventory import build_inventory
    from dashboard.store import Store

    store = Store(tmp_path / "db.sqlite3")
    run_dir = tmp_path / "runs" / "run-gone"
    store.create_run("run-gone", "c", "deploy", "main", {}, run_dir, run_dir / "run.log")
    store.update_run("run-gone", state="succeeded", returncode=0)
    rng = ranges.build_ranges(store)[0]
    assert rng["state"] == "failed" and rng["live"] is False
    assert rng["workspace_missing"] is True
    inventory = build_inventory(store, store.get_run("run-gone"))
    assert inventory["workspace_missing"] is True
    assert inventory["range"]["workspace_missing"] is True


def test_a_range_without_recorded_vms_publishes_no_engine_link(tmp_path):
    """The document keeps the engine's address after its VM is gone; the board turned that
    into a Scoreboard link on a range with nothing behind it."""
    _workspace(tmp_path, "run-gone", vms=False)
    rng = _range_of(_rec("run-gone", "destroy", created=1, run_dir=tmp_path / "run-gone"))
    summary = ranges.range_summary(rng, _EMPTY_INVENTORY, [])
    assert summary["has_recorded_vms"] is False
    assert summary["links"] == {} and summary["engine_ip"] == "10.0.0.9"


def test_a_live_range_still_publishes_its_engine_link(tmp_path):
    _workspace(tmp_path, "run-up", vms=True)
    rng = _range_of(_rec("run-up", "deploy", created=1, run_dir=tmp_path / "run-up"))
    summary = ranges.range_summary(rng, _EMPTY_INVENTORY, [])
    assert summary["has_recorded_vms"] is True
    assert summary["links"]["scoreboard"] == "http://10.0.0.9"


def test_the_range_and_run_views_publish_the_resolved_engine_head(tmp_path):
    """`engine_ref` is what the caller asked for; `engine_head` is the commit that ran."""
    ws = _workspace(tmp_path, "run-h", vms=True)
    run = _rec("run-h", "deploy", created=1, run_dir=ws.parent)
    run["engine_ref"], run["engine_head"] = "main", "a" * 40
    rng = _range_of(run)
    assert rng["engine_ref"] == "main" and rng["engine_head"] == "a" * 40
    assert ranges.run_view(run)["engine_head"] == "a" * 40
    assert ranges.run_view(run, detail=True)["engine_head"] == "a" * 40
    assert ranges.range_summary(rng, _EMPTY_INVENTORY, [])["engine_head"] == "a" * 40


def test_comp_defined_follows_the_definitions_checkout(tmp_path, monkeypatch):
    """A range outlives the definition it was deployed from; the range page links to the
    competition editor, which 404s once that folder is gone."""
    from dashboard import comps_api
    monkeypatch.setattr(comps_api, "COMPS", tmp_path / "competitions")
    _workspace(tmp_path, "run-def", vms=True)
    rng = _range_of(_rec("run-def", "deploy", created=1, run_dir=tmp_path / "run-def"))
    assert ranges.range_summary(rng, _EMPTY_INVENTORY, [])["comp_defined"] is False
    (tmp_path / "competitions" / "c").mkdir(parents=True)
    assert ranges.range_summary(rng, _EMPTY_INVENTORY, [])["comp_defined"] is True
