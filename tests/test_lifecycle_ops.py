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


def test_resume_deploy_continues_the_workspace_at_the_phase_that_failed(tmp_path):
    # A failed deploy used to be destroy-only: `deploy` seeds a fresh workspace, so the
    # plumbed --from-phase had no estate document to read. The resume acts on the workspace
    # holding the range and enters at the phase AFTER the last one the document completed.
    import json
    (tmp_path / "estate-state.json").write_text(json.dumps(
        {"schema_version": 1, "identity": {"comp_name": "c", "run_id": "run-abc12345"},
         "run": {"last_phase": 3, "hold_start": True}}))
    cmd = build_command("resume_deploy", "c", str(tmp_path), {})
    assert cmd[1] == "create-competition.py"
    assert cmd[cmd.index("--from-phase") + 1] == "4"
    assert "--hold-start" in cmd          # the range was meant to stay held
    assert "--teams" not in cmd           # the document already knows the roster
    explicit = build_command("resume_deploy", "c", str(tmp_path), {"from_phase": 6})
    assert explicit[explicit.index("--from-phase") + 1] == "6"


def test_a_resume_of_a_preflight_failure_can_be_given_the_team_count(tmp_path):
    # Nothing is recorded before preflight passes, so a resume at phase 1 has no roster; the
    # driver prompts for it, which a stdin-less runner cannot answer (EOFError, live
    # run-279a2cf7). The count rides the command instead.
    import json
    (tmp_path / "estate-state.json").write_text(json.dumps(
        {"schema_version": 1, "identity": {"comp_name": "c", "run_id": "run-abc12345"},
         "run": {"last_phase": 0, "hold_start": True}}))
    cmd = build_command("resume_deploy", "c", str(tmp_path), {"teams": 1})
    assert cmd[cmd.index("--from-phase") + 1] == "1"
    assert cmd[cmd.index("--teams") + 1] == "1"


def test_phase_label_follows_the_drivers_start_banner(tmp_path):
    # last_phase moves only when a phase COMPLETES, so the longest phase (red01/Mythic)
    # reported the one before it for its whole duration.
    from dashboard.runner import RunManager
    log = tmp_path / "run.log"
    log.write_text("[1/8] cleanup...\nnoise\n[7/8] final\n")
    assert RunManager._phase_from_log(log) == 7
    assert RunManager._phase_from_log(tmp_path / "missing.log") == 0


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
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"comp_name": "comp-a", "run_id": "run-deploy"},
         "resources": [{"kind": "engine", "node": "pve", "name": "scoring-engine",
                        "vmid": 900, "bridge": None, "tags": ["tezcatlipoca"], "at": "t"}]}))
    store.create_run("run-deploy", "comp-a", "deploy", "main", {"teams": 1}, run_dir,
                     run_dir / "run.log")
    # The workspace exists because its deploy finished; a deploy still in flight is the
    # subject of its own test (and now correctly holds the lifecycle actions back).
    store.update_run("run-deploy", state="succeeded", returncode=0)
    return mgr, store


def test_lifecycle_actions_resolve_the_range_workspace(tmp_path, monkeypatch):
    mgr, _ = _manager(tmp_path, monkeypatch)
    run = mgr.submit("comp-a", "start", "main", {})
    assert run["params"]["_destroy_comp_dir"].endswith("run-deploy/comp")
    team = mgr.submit("comp-a", "delete_team", "main", {"team": "team2"})
    assert team["action"] == "delete_team"


def test_lifecycle_actions_resolve_a_document_workspace(tmp_path, monkeypatch):
    """identity.run_id in the document is the ownership proof a lifecycle action resolves."""
    mgr, store = _manager(tmp_path, monkeypatch)
    ws = tmp_path / "data" / "runs" / "run-deploy" / "comp"
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"comp_name": "comp-a", "run_id": "run-deploy"},
         "resources": [{"kind": "engine", "node": "pve", "name": "scoring-engine",
                        "vmid": 900, "bridge": None, "tags": ["tezcatlipoca"], "at": "t"}]}))
    run = mgr.submit("comp-a", "start", "estate-v0.3.0", {})
    assert run["params"]["_destroy_comp_dir"].endswith("run-deploy/comp")
    assert run["engine_ref"] == "estate-v0.3.0"


def test_a_deploy_mints_the_range_ops_token(tmp_path, monkeypatch):
    mgr, store = _manager(tmp_path, monkeypatch)
    run = mgr.submit("comp-a", "deploy", "main", {"teams": 1})
    token = store.token_for_range(str(Path(run["run_dir"]) / "comp"))
    assert token and store.range_for_token(token)["comp_id"] == "comp-a"


@pytest.mark.parametrize("action,params", [
    ("start", {}), ("freeze", {}), ("resume", {}), ("end", {}),
    ("verify", {}), ("redeploy", {}), ("delete_team", {"team": "team2"}),
])
def test_range_actions_are_refused_while_a_deploy_is_still_building(
        tmp_path, monkeypatch, action, params):
    """The engine answers /api/engine before create-competition finishes. Every action that
    reads or rewrites the range — not just Start — must wait for the build, and destroy is
    the one deliberate exception (it cancels the deploy)."""
    mgr, store = _manager(tmp_path, monkeypatch)
    store.update_run("run-deploy", state="running")
    with pytest.raises(RunRefused, match="is still deploying"):
        mgr.submit("comp-a", action, "main", params)


def test_a_non_destroy_action_does_not_cancel_the_live_deploy(tmp_path, monkeypatch):
    """Only destroy tears a range down under its deploy; every other action must leave it
    alone. The unconditional cleanup made a start submitted during a deploy kill the deploy."""
    mgr, _ = _manager(tmp_path, monkeypatch)
    cancelled = []
    monkeypatch.setattr(RunManager, "_cancel_live_runs_for_comp_dir",
                        lambda self, comp_dir: cancelled.append(comp_dir))
    mgr.submit("comp-a", "verify", "main", {})
    assert cancelled == []


def _add_range_run(store, tmp_path, run_id, action, params):
    run_dir = tmp_path / "data" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    store.create_run(run_id, "comp-a", action, "main", params, run_dir, None)
    store.update_run(run_id, state="running")
    return run_dir


def test_a_destroy_cancels_the_resolved_deploy_workspace_even_without_comp_dir(tmp_path, monkeypatch):
    """The API/MCP/run page may leave comp_dir out. The resolved target, not the optional
    param, is what the owning deploy has to be matched against."""
    mgr, store = _manager(tmp_path, monkeypatch)
    store.update_run("run-deploy", state="running")
    cancelled = []
    monkeypatch.setattr(RunManager, "_cancel_live_runs_for_comp_dir",
                        lambda self, comp_dir: cancelled.append(comp_dir))
    mgr.submit("comp-a", "destroy", "main", {})
    ws = (tmp_path / "data" / "runs" / "run-deploy" / "comp").resolve()
    assert [Path(p).resolve() for p in cancelled] == [ws]


def test_nothing_else_runs_on_a_range_that_is_being_torn_down(tmp_path, monkeypatch):
    mgr, store = _manager(tmp_path, monkeypatch)
    ws = str((tmp_path / "data" / "runs" / "run-deploy" / "comp").resolve())
    _add_range_run(store, tmp_path, "run-down", "destroy", {"comp_dir": ws})
    with pytest.raises(RunRefused, match="tearing this range down"):
        mgr.submit("comp-a", "start", "main", {})
    # A second destroy is a deliberate retry and stays allowed.
    monkeypatch.setattr(RunManager, "_cancel_live_runs_for_comp_dir", lambda self, d: None)
    assert mgr.submit("comp-a", "destroy", "main", {})["action"] == "destroy"


def test_a_second_lifecycle_action_waits_for_the_first(tmp_path, monkeypatch):
    mgr, store = _manager(tmp_path, monkeypatch)
    ws = str((tmp_path / "data" / "runs" / "run-deploy" / "comp").resolve())
    _add_range_run(store, tmp_path, "run-live", "start", {"comp_dir": ws})
    with pytest.raises(RunRefused, match="already running the competition"):
        mgr.submit("comp-a", "freeze", "main", {})


@pytest.mark.parametrize("action", ["redeploy", "verify", "delete_team"])
def test_start_waits_for_a_recovery_operation_on_the_range(tmp_path, monkeypatch, action):
    """Starting the clock while boxes are reset (or a verify is reading them) scores a range
    that is still moving; pause/resume/end stay available because stopping is always safe."""
    mgr, store = _manager(tmp_path, monkeypatch)
    ws = str((tmp_path / "data" / "runs" / "run-deploy" / "comp").resolve())
    params = {"comp_dir": ws, **({"team": "team2"} if action == "delete_team" else {})}
    _add_range_run(store, tmp_path, "run-op", action, params)
    with pytest.raises(RunRefused, match="competition cannot start"):
        mgr.submit("comp-a", "start", "main", {})
    # Freezing/ending is a stop, not a start: it must not be held behind the recovery.
    monkeypatch.setattr(RunManager, "_run", lambda self, *a, **k: None)
    assert mgr.submit("comp-a", "end", "main", {})["action"] == "end"


def test_a_deploy_cannot_be_pointed_at_an_existing_workspace(tmp_path, monkeypatch):
    """comp_dir is for actions on a range; a deploy seeds a new workspace under its run dir."""
    mgr, _ = _manager(tmp_path, monkeypatch)
    ws = str((tmp_path / "data" / "runs" / "run-deploy" / "comp").resolve())
    with pytest.raises(RunRefused, match="builds a new workspace"):
        mgr.submit("comp-a", "deploy", "main", {"comp_dir": ws})


def test_a_lifecycle_action_needs_a_deployed_workspace(tmp_path, monkeypatch):
    """A Compfile-only stub is not a range: only destroy may clean it up."""
    mgr, store = _manager(tmp_path, monkeypatch)
    stub = tmp_path / "stub" / "comp"
    stub.mkdir(parents=True)
    (stub / "Compfile").write_text("name comp-a\n")
    store.create_run("run-stub", "comp-a", "deploy", "main", {}, stub.parent, None)
    with pytest.raises(RunRefused, match="no estate run id"):
        mgr.submit("comp-a", "start", "main", {"comp_dir": str(stub)})


def test_start_is_refused_while_a_scrim_drives_the_range(tmp_path, monkeypatch):
    mgr, store = _manager(tmp_path, monkeypatch)
    ws = str((tmp_path / "data" / "runs" / "run-deploy" / "comp").resolve())
    _add_range_run(store, tmp_path, "run-scrim", "scrim", {"comp_dir": ws})
    with pytest.raises(RunRefused, match="scrim harness"):
        mgr.submit("comp-a", "start", "main", {})


def test_lifecycle_on_a_failed_range_is_refused(tmp_path, monkeypatch):
    mgr, store = _manager(tmp_path, monkeypatch)
    broken = tmp_path / "data" / "runs" / "run-broken"
    ws = broken / "comp"
    ws.mkdir(parents=True)
    (ws / "Compfile").write_text("name comp-a\n")
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"comp_name": "comp-a", "run_id": "run-broken"}, "resources": []}))
    store.create_run("run-broken", "comp-a", "deploy", "main", {}, broken, None)
    store.update_run("run-broken", state="failed", returncode=1)
    with pytest.raises(RunRefused, match="is failed"):
        mgr.submit("comp-a", "start", "main", {"comp_dir": str(ws)})


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


def test_a_request_is_claimed_once_and_cannot_be_denied_after_approval(api):
    """Two tabs approving one request used to submit two redeploys; the claim is atomic."""
    http, tmp_path = api
    store = Store(tmp_path / "data" / "dashboard.sqlite3")
    token = store.mint_range_token("/data/runs/run-deploy/comp", "comp-a")
    _push(http, token, [{"id": 1, "team": "team1", "box": "web01", "status": "pending"}])
    req = [r for r in store.list_requests() if r["portal_id"] == "1"][0]
    assert store.claim_request(req["id"]) is True
    assert store.claim_request(req["id"]) is False
    assert http.post(f"/api/requests/{req['id']}/deny").status_code == 409


def test_a_denied_request_cannot_be_approved_later(api):
    http, tmp_path = api
    store = Store(tmp_path / "data" / "dashboard.sqlite3")
    token = store.mint_range_token("/data/runs/run-deploy/comp", "comp-a")
    _push(http, token, [{"id": 1, "team": "team1", "box": "web01", "status": "pending"}])
    req = [r for r in store.list_requests() if r["portal_id"] == "1"][0]
    assert http.post(f"/api/requests/{req['id']}/deny").status_code == 200
    assert http.post(f"/api/requests/{req['id']}/approve").status_code == 409


def test_an_unexpected_approval_error_does_not_strand_the_request(api, monkeypatch):
    http, tmp_path = api
    store = Store(tmp_path / "data" / "dashboard.sqlite3")
    token = store.mint_range_token("/data/runs/run-deploy/comp", "comp-a")
    _push(http, token, [{"id": 1, "team": "team1", "box": "web01", "status": "pending"}])
    req = [r for r in store.list_requests() if r["portal_id"] == "1"][0]

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(RunManager, "submit", boom)
    with pytest.raises(RuntimeError, match="boom"):
        http.post(f"/api/requests/{req['id']}/approve")
    # The claim is rolled back, so the operator can retry instead of seeing a permanent 409.
    assert store.get_request(req["id"])["status"] == "pending"


def test_retirement_revokes_the_portal_token_and_closes_requests(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    token = store.mint_range_token("/data/ws", "comp-a")
    store.upsert_requests("/data/ws", "comp-a", [{"id": 1, "team": "team1", "box": "web01"}])
    store.retire_range("/data/ws", "comp-a", "test retire")
    assert store.range_for_token(token) is None
    assert store.list_requests()[0]["status"] == "failed"


def test_reconcile_requests_resolves_orphaned_approvals(tmp_path, monkeypatch):
    """A restart (or a crash before linking) left approved requests with no writer."""
    mgr, store = _manager(tmp_path, monkeypatch)
    ws = str(tmp_path / "data" / "runs" / "run-deploy" / "comp")
    store.upsert_requests(ws, "comp-a", [
        {"id": 1, "team": "team1", "box": "web01"},
        {"id": 2, "team": "team1", "box": "web02"},
    ])
    first, second = store.list_requests()
    store.claim_request(first["id"])
    store.set_request_status(first["id"], "approved", approved_run="run-deploy")
    store.claim_request(second["id"])          # approved, never linked to a run
    assert sorted(mgr.reconcile_requests()) == sorted([first["id"], second["id"]])
    assert store.get_request(first["id"])["status"] == "done"
    assert store.get_request(second["id"])["status"] == "failed"


def test_reconcile_requests_leaves_a_running_reset_alone(tmp_path, monkeypatch):
    mgr, store = _manager(tmp_path, monkeypatch)
    ws = str(tmp_path / "data" / "runs" / "run-deploy" / "comp")
    store.upsert_requests(ws, "comp-a", [{"id": 1, "team": "team1", "box": "web01"}])
    req = store.list_requests()[0]
    store.claim_request(req["id"])
    store.set_request_status(req["id"], "approved", approved_run="run-deploy")
    store.update_run("run-deploy", state="running", returncode=None)
    assert mgr.reconcile_requests() == []
    assert store.get_request(req["id"])["status"] == "approved"


def test_approval_preserves_requested_team_workspace_and_origin_ref(api, monkeypatch):
    http, tmp_path = api
    store = Store(tmp_path / "data" / "dashboard.sqlite3")
    captured = {}

    def capture(self, comp_id, action, ref, params):
        captured.update(comp_id=comp_id, action=action, ref=ref, params=params)
        return {"id": "run-reset", "params": params}

    monkeypatch.setattr(RunManager, "submit", capture)
    original = tmp_path / "runs" / "run-original"
    workspace = str(original / "comp")
    store.create_run("run-original", "comp-a", "deploy", "original-sha", {"teams": 3},
                     original, original / "run.log")
    # A newer range of the same competition must not supply the ref or reset target.
    newer = tmp_path / "runs" / "run-newer"
    store.create_run("run-newer", "comp-a", "deploy", "different-sha", {"teams": 2},
                     newer, newer / "run.log")
    followup = tmp_path / "runs" / "run-verify"
    store.create_run("run-verify", "comp-a", "verify", "followup-sha",
                     {"_destroy_comp_dir": workspace}, followup, followup / "run.log")
    token = store.mint_range_token(workspace, "comp-a")
    _push(http, token, [{"id": 1, "team": "team3", "box": "dc01"}])
    req = store.list_requests()[0]
    assert http.post(f"/api/requests/{req['id']}/approve").status_code == 200
    assert captured == {"comp_id": "comp-a", "action": "redeploy", "ref": "original-sha",
                        "params": {"mode": "reset", "boxes": "dc01", "teams": "team3",
                                   "comp_dir": workspace}}
    cmd = build_command("redeploy", "comp-a", workspace, captured["params"])
    assert cmd[cmd.index("--teams") + 1] == "team3"
    assert cmd[cmd.index("--boxes") + 1] == "dc01"


def test_empty_reset_team_cannot_expand_to_a_whole_range(api):
    http, tmp_path = api
    store = Store(tmp_path / "data" / "dashboard.sqlite3")
    token = store.mint_range_token("/data/runs/run-deploy/comp", "comp-a")
    _push(http, token, [{"id": 1, "team": "", "box": "web01"}])
    req = store.list_requests()[0]
    assert http.post(f"/api/requests/{req['id']}/approve").status_code == 409
    assert store.get_request(req["id"])["status"] == "pending"


def test_submit_explicit_target_cannot_cross_competitions_or_internal_override(tmp_path, monkeypatch):
    mgr, store = _manager(tmp_path, monkeypatch)
    other = tmp_path / "runs" / "run-other"
    store.create_run("run-other", "other-comp", "deploy", "main", {}, other, other / "run.log")
    for action in ("destroy", "redeploy", "start", "scrim"):
        with pytest.raises(RunRefused, match="does not belong"):
            mgr.submit("comp-a", action, "main", {"comp_dir": str(other / "comp")})
    # An external caller cannot inject the manager-owned target to bypass resolution.
    run = mgr.submit("comp-a", "start", "main", {"_destroy_comp_dir": str(other / "comp")})
    assert run["params"]["_destroy_comp_dir"].endswith("run-deploy/comp")


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


def test_a_successful_destroy_revokes_the_portal_token_and_closes_its_requests(tmp_path, monkeypatch):
    mgr, store = _manager(tmp_path, monkeypatch)
    ws = str((tmp_path / "data" / "runs" / "run-deploy" / "comp").resolve())
    token = store.mint_range_token(ws, "comp-a")
    store.upsert_requests(ws, "comp-a", [{"id": 1, "team": "team1", "box": "web01"}])
    run_dir = tmp_path / "data" / "runs" / "run-down"
    store.create_run("run-down", "comp-a", "destroy", "main",
                     {"_destroy_comp_dir": ws}, run_dir, None)
    mgr._finish("run-down", 0)
    assert store.token_for_range(ws) is None
    assert store.list_requests()[0]["status"] == "failed"


# ---- lifecycle state mapping ----

def _ws(tmp_path, state):
    """A range workspace that still records its own VM — what a live range looks like."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "estate-state.json").write_text(json.dumps(
        {"identity": {"comp_name": "comp-a", "run_id": "run-d", "engine_mgmt_ip": "10.0.0.9"},
         "run": state}))
    return {"key": str(ws), "comp_id": "comp-a", "deploy_run_id": "run-d",
            "state": "live", "has_recorded_vms": True}


def test_engine_status_polls_with_the_login_cookie(tmp_path, monkeypatch):
    """Quotient's /api/engine is admin-only: the login cookie is the authorization."""
    seen = {}

    class Response:
        def __init__(self, payload=None): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    class Session:
        def __init__(self): self.cookies = {}
        def post(self, url, json, timeout):
            self.cookies["quotient"] = "session"
            return Response()
        def get(self, url, timeout):
            seen["cookie"] = self.cookies.get("quotient")
            return Response({"competition_started": False})

    monkeypatch.setattr(competition.requests, "Session", Session)
    ws = _ws(tmp_path, {"scoring_password": "s"})["key"]
    assert competition.engine_status(ws, {"scoring_password": "s"}) == {
        "competition_started": False}
    assert seen["cookie"] == "session"


def test_a_200_that_is_not_the_engine_payload_is_not_an_observation(tmp_path, monkeypatch):
    """A proxy page or an older schema must not read as "ready" (or 500 the panel)."""
    payload = ["not", "an", "engine"]

    class Response:
        def raise_for_status(self): pass
        def json(self): return payload

    class Session:
        def post(self, *a, **k): return Response()
        def get(self, *a, **k): return Response()

    monkeypatch.setattr(competition.requests, "Session", Session)
    ws = _ws(tmp_path, {"scoring_password": "s"})["key"]
    assert competition.engine_status(ws, {"scoring_password": "s"}) is None
    payload = {"status": "ok"}
    assert competition.engine_status(ws, {"scoring_password": "s"}) is None


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
    # The address is recorded, but a dead engine must not produce clickable admin links.
    assert view["links"] == {}


def test_a_range_with_no_recorded_vm_is_not_polled_and_links_nothing(tmp_path, monkeypatch):
    """A torn-down range records the engine's old address; asking it or linking it lies."""
    monkeypatch.setattr(competition, "engine_status",
                        lambda ws, state: pytest.fail("polled an engine with no VM behind it"))
    rng = _ws(tmp_path, {"scoring_password": "s", "admin_password": "a"})
    rng["has_recorded_vms"] = False
    view = competition.status_for_range(None, rng)
    assert view["state"] == "unknown" and not view["reachable"] and view["links"] == {}
    assert view["creds"]["admin"] == {"user": "admin", "password": "a"}


def test_a_deploy_in_flight_holds_the_panel_at_deploying(tmp_path, monkeypatch):
    """The engine answers before the range is finished: start must wait for the deploy."""
    monkeypatch.setattr(competition, "engine_status",
                        lambda ws, state: pytest.fail("polled an engine mid-deploy"))
    rng = _ws(tmp_path, {"scoring_password": "s", "admin_password": "a"})
    rng["state"] = "building"
    rng["runs"] = [{"id": "run-d", "action": "deploy", "state": "running"}]
    view = competition.status_for_range(None, rng)
    assert view["deploying"] is True and view["state"] == "deploying" and not view["reachable"]
    # Credentials stay recorded: a client can use them once the deploy ends.
    assert view["creds"]["admin"] == {"user": "admin", "password": "a"}


def test_a_verify_does_not_hide_a_live_competition(tmp_path, monkeypatch):
    """Only a deploy means "not ready"; verify/redeploy run against an existing engine."""
    monkeypatch.setattr(competition, "engine_status",
                        lambda ws, state: {"competition_started": False})
    rng = _ws(tmp_path, {"scoring_password": "s"})
    rng["runs"] = [{"id": "run-v", "action": "verify", "state": "running"}]
    view = competition.status_for_range(None, rng)
    assert view["deploying"] is False and view["state"] == "ready" and view["reachable"]


def test_the_engine_ip_comes_from_the_documents_identity(tmp_path):
    """estate.engine_setup.engine_ctx's own read: the document's identity is the only source."""
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
