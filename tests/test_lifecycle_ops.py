"""Competition lifecycle + reset requests: the management panel's backend.

Offline. Pinned: the deploy/start split flag plumbing, the lifecycle action kinds
(command shapes + workspace resolution + refusals), the ops-token ingest contract
(the portal's converging sync), approve/deny, the approved-run → done/failed hook,
and the lifecycle state mapping the panel renders."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard import competition
from dashboard.runner import RunManager, RunRefused, build_command
from dashboard.store import Store


# ---- command shapes ----

def test_deploy_holds_start_by_default_and_the_checkbox_buys_the_old_way_back():
    held = build_command("deploy", "c", "/ws", {"teams": 2})
    assert "--hold-start" in held
    oneshot = build_command("deploy", "c", "/ws", {"teams": 2, "start_after_deploy": True})
    assert "--hold-start" not in oneshot


def test_lifecycle_action_kinds_map_to_the_engine_clis():
    assert build_command("start", "c", "/ws", {})[1:4] == ["competition-ops.py", "start", "--comp-dir"]
    assert build_command("freeze", "c", "/ws", {})[2] == "freeze"
    assert build_command("resume", "c", "/ws", {})[2] == "resume"
    end = build_command("end", "c", "/ws", {})
    assert end[1] == "competition-ops.py" and end[2] == "end"
    team = build_command("delete_team", "c", "/ws", {"team": "team2"})
    assert team[1] == "destroy-competition.py"
    assert team[team.index("--team") + 1] == "team2" and "--yes" in team
    with pytest.raises(RunRefused):
        build_command("delete_team", "c", "/ws", {})


# ---- submit: workspace resolution + refusals ----

class _Bus:
    def publish(self, run_id, event):
        pass


def _manager(tmp_path, monkeypatch):
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(RunManager, "ensure_repos", lambda self: None)
    monkeypatch.setattr(RunManager, "refresh_comps", lambda self: None)
    monkeypatch.setattr(RunManager, "_run", lambda self, *a, **k: None)
    store = Store(tmp_path / "db.sqlite3")
    mgr = RunManager(store, _Bus())
    mgr.comps_repo = tmp_path / "competitions"
    (mgr.comps_repo / "comp-a").mkdir(parents=True)
    (mgr.comps_repo / "comp-a" / "Compfile").write_text("name comp-a\n")
    run_dir = tmp_path / "data" / "runs" / "run-deploy"
    ws = run_dir / "comp"
    ws.mkdir(parents=True)
    (ws / "Compfile").write_text("name comp-a\n")
    (ws / ".deploy_state.json").write_text(json.dumps({"run_id": "run-deploy"}))
    store.create_run("run-deploy", "comp-a", "deploy", "main", {"teams": 1}, run_dir,
                     run_dir / "run.log")
    return mgr, store


def test_lifecycle_actions_resolve_the_range_workspace(tmp_path, monkeypatch):
    mgr, _ = _manager(tmp_path, monkeypatch)
    run = mgr.submit("comp-a", "start", "main", {})
    assert run["params"]["_destroy_comp_dir"].endswith("run-deploy/comp")
    team = mgr.submit("comp-a", "delete_team", "main", {"team": "team2"})
    assert team["action"] == "delete_team"


def test_lifecycle_actions_resolve_a_document_workspace(tmp_path, monkeypatch):
    """A v0.3.0 range has no .deploy_state.json: identity.run_id in the document is the proof."""
    mgr, store = _manager(tmp_path, monkeypatch)
    ws = tmp_path / "data" / "runs" / "run-deploy" / "comp"
    (ws / ".deploy_state.json").unlink()
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"comp_name": "comp-a", "run_id": "run-deploy"}, "resources": []}))
    run = mgr.submit("comp-a", "start", "estate-v0.3.0", {})
    assert run["params"]["_destroy_comp_dir"].endswith("run-deploy/comp")
    assert run["engine_ref"] == "estate-v0.3.0"


def test_a_deploy_mints_the_range_ops_token(tmp_path, monkeypatch):
    mgr, store = _manager(tmp_path, monkeypatch)
    run = mgr.submit("comp-a", "deploy", "main", {"teams": 1})
    token = store.token_for_range(str(Path(run["run_dir"]) / "comp"))
    assert token and store.range_for_token(token)["comp_id"] == "comp-a"


# ---- the ops ingest contract ----

@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(RunManager, "ensure_repos", lambda self: None)
    monkeypatch.setattr(RunManager, "refresh_comps", lambda self: None)
    monkeypatch.setattr(RunManager, "_run", lambda self, *a, **k: None)
    monkeypatch.setattr(RunManager, "submit",
                        lambda self, comp_id, action, ref, params:
                        {"id": "run-reset", "params": params})
    from dashboard.app import create_app
    return TestClient(create_app()), tmp_path


def _push(http, token, rows):
    return http.post("/api/ops/requests", json={"requests": rows},
                     headers={"Authorization": f"Bearer {token}"})


def test_ops_ingest_authenticates_with_the_range_token(api):
    http, tmp_path = api
    assert http.post("/api/ops/requests", json={"requests": []}).status_code == 401
    assert _push(http, "not-a-token", []).status_code == 401
    # a deployed range's token (minted by a deploy submit in real life)
    store = Store(tmp_path / "data" / "dashboard.sqlite3")
    token = store.mint_range_token("/data/runs/run-deploy/comp", "comp-a")
    r = _push(http, token, [{"id": 1, "team": "team1", "box": "web01", "at": "t", "by": "ada",
                             "status": "pending"}])
    assert r.status_code == 200
    assert r.json()["statuses"] == {"1": "pending"}


def test_ops_sync_converges_on_the_dashboard_status(api):
    http, tmp_path = api
    store = Store(tmp_path / "data" / "dashboard.sqlite3")
    token = store.mint_range_token("/data/runs/run-deploy/comp", "comp-a")
    rows = [{"id": 1, "team": "team1", "box": "web01", "status": "pending"}]
    assert _push(http, token, rows).json()["statuses"] == {"1": "pending"}
    req = [r for r in store.list_requests() if r["portal_id"] == "1"][0]
    store.set_request_status(req["id"], "approved")
    # the portal re-pushes its local `pending`; the dashboard's word wins
    assert _push(http, token, rows).json()["statuses"] == {"1": "approved"}


def test_approve_submits_the_reset_against_the_range_and_links_the_run(api):
    http, tmp_path = api
    store = Store(tmp_path / "data" / "dashboard.sqlite3")
    token = store.mint_range_token("/data/runs/run-deploy/comp", "comp-a")
    _push(http, token, [{"id": 1, "team": "team1", "box": "web01", "status": "pending"}])
    req = [r for r in store.list_requests() if r["portal_id"] == "1"][0]
    r = http.post(f"/api/requests/{req['id']}/approve")
    assert r.status_code == 200
    assert r.json()["status"] == "approved" and r.json()["approved_run"] == "run-reset"
    resolved = http.post(f"/api/requests/{req['id']}/approve")
    assert resolved.status_code == 409


def test_deny_and_the_api_surface_needs_the_bearer(api, monkeypatch):
    http, tmp_path = api
    monkeypatch.setenv("TEZ_DASHBOARD_TOKENS", "ui=tok")
    store = Store(tmp_path / "data" / "dashboard.sqlite3")
    token = store.mint_range_token("/data/runs/run-deploy/comp", "comp-a")
    _push(http, token, [{"id": 1, "team": "team1", "box": "web01", "status": "pending"}])
    req = store.list_requests()[0]
    assert http.get("/api/requests").status_code == 401
    assert http.get("/api/requests", headers={"Authorization": "Bearer tok"}).status_code == 200
    r = http.post(f"/api/requests/{req['id']}/deny", headers={"Authorization": "Bearer tok"})
    assert r.json()["status"] == "denied"


# ---- the approved run's outcome resolves the request ----

def test_finish_resolves_the_approved_request_once(tmp_path, monkeypatch):
    mgr, store = _manager(tmp_path, monkeypatch)
    emitted = []

    def fake_emit(run_id, kind, stage="", data=None):
        emitted.append((kind, data))

    monkeypatch.setattr(mgr, "_emit", fake_emit)
    store.mint_range_token("/data/runs/run-deploy/comp", "comp-a")
    store.upsert_requests("/data/runs/run-deploy/comp", "comp-a",
                          [{"id": 1, "team": "team1", "box": "web01", "status": "pending"}])
    rid = store.list_requests()[0]["id"]
    store.set_request_status(rid, "approved", approved_run="run-x")
    mgr._finish("run-x", 0)
    assert store.get_request(rid)["status"] == "done"
    assert ("request", {"request": rid, "status": "done", "team": "team1",
                        "box": "web01"}) in emitted
    # only `approved` resolves: the done request is never re-resolved by another finish
    mgr._finish("run-x", 1)
    assert store.get_request(rid)["status"] == "done"


# ---- lifecycle state mapping ----

def _ws(tmp_path, state):
    ws = tmp_path / "ws"
    (ws / "terraform").mkdir(parents=True)
    (ws / ".deploy_state.json").write_text(json.dumps(state))
    (ws / "terraform" / "terraform.tfstate").write_text(json.dumps(
        {"outputs": {"agent_context": {"value": json.dumps(
            {"scoring_engine_ip": "10.0.0.9"})}}}))
    return {"key": str(ws), "comp_id": "comp-a", "deploy_run_id": "run-d",
            "state": "live"}


def test_lifecycle_states_from_the_engine_api(tmp_path, monkeypatch):
    def fake_engine(ws, state):
        return state.get("_engine")

    monkeypatch.setattr(competition, "engine_status", fake_engine)
    from dashboard.competition import lifecycle_state
    assert lifecycle_state(None, {}) == "unknown"
    assert lifecycle_state({"competition_started": False}, {}) == "ready"
    assert lifecycle_state({"competition_started": True, "running": True}, {}) == "running"
    assert lifecycle_state({"competition_started": True, "running": False}, {}) == "paused"
    assert lifecycle_state({"competition_started": True, "running": True},
                           {"ended": True}) == "ended"


def test_status_payload_carries_links_and_creds(tmp_path, monkeypatch):
    monkeypatch.setattr(competition, "engine_status",
                        lambda ws, state: {"competition_started": False, "running": False})
    rng = _ws(tmp_path, {"scoring_password": "s", "admin_password": "a", "hold_start": True})
    view = competition.status_for_range(None, rng)
    assert view["state"] == "ready" and view["hold_start"] and view["reachable"]
    assert view["links"]["admin"].endswith("/admin")
    assert view["creds"]["admin"] == {"user": "admin", "password": "a"}


def test_an_unreachable_engine_reads_the_state_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(competition, "engine_status", lambda ws, state: None)
    rng = _ws(tmp_path, {"ended": True})
    view = competition.status_for_range(None, rng)
    assert view["state"] == "ended" and not view["reachable"]


def test_the_engine_ip_comes_from_the_documents_identity(tmp_path):
    """estate.engine_setup.engine_ctx's own read — terraform's output is gone in v0.3.0."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"comp_name": "comp-a", "run_id": "run-1",
                      "engine_mgmt_ip": "10.0.0.42"}, "resources": []}))
    assert competition._engine_ip(ws) == "10.0.0.42"
    # a DHCP engine records "" — the panel must see no address, not an empty one
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"comp_name": "comp-a", "run_id": "run-1", "engine_mgmt_ip": ""},
         "resources": []}))
    assert competition._engine_ip(ws) is None


def test_a_migrated_workspace_prefers_terraforms_recorded_engine_ip(tmp_path):
    """migrate takes the engine IP from the ambient env, which may be a default; the
    range's own terraform output is the answer until v0.3.0 records resources."""
    ws = tmp_path / "ws"
    (ws / "terraform").mkdir(parents=True)
    (ws / "terraform" / "terraform.tfstate").write_text(json.dumps(
        {"outputs": {"agent_context": {"value": json.dumps(
            {"scoring_engine_ip": "10.0.0.9"})}}}))
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"comp_name": "comp-a", "run_id": "run-1",
                      "engine_mgmt_ip": "10.0.0.250"}, "resources": []}))
    assert competition._engine_ip(ws) == "10.0.0.9"


def test_status_for_range_reads_the_document(tmp_path, monkeypatch):
    monkeypatch.setattr(competition, "engine_status", lambda ws, state: None)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "estate-state.json").write_text(json.dumps({
        "identity": {"comp_name": "comp-a", "run_id": "run-1", "engine_mgmt_ip": "10.0.0.42"},
        "secrets": {"admin_password": "a", "scoring_password": "s"},
        "run": {"hold_start": True, "ended": True, "seeded": True}}))
    rng = {"key": str(ws), "comp_id": "comp-a", "deploy_run_id": "run-d", "state": "live"}
    view = competition.status_for_range(None, rng)
    assert view["state"] == "ended" and not view["reachable"]
    assert view["hold_start"] and view["seeded"] and view["ended"]
    assert view["creds"]["admin"] == {"user": "admin", "password": "a"}
