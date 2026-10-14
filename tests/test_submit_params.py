"""Submit params precedence: an explicit params entry beats the body/tool's own defaults."""

import json

import pytest
from fastapi.testclient import TestClient

from dashboard.runner import RunManager, build_command
from dashboard.store import Store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    captured = {}

    def fake_submit(self, comp_id, action, engine_ref, params):
        captured.update(params)
        return {"id": "run-x", "params": params}

    monkeypatch.setattr(RunManager, "submit", fake_submit)
    from dashboard.app import create_app
    return TestClient(create_app()), captured


def test_explicit_params_win_over_the_body_defaults(client):
    http, captured = client
    r = http.post("/api/runs", json={
        "comp_id": "comp-a", "action": "redeploy", "teams": 1,
        "params": {"teams": "2", "boxes": "web01", "mode": "reset"}})
    assert r.status_code == 200, r.text
    assert captured["teams"] == "2" and captured["boxes"] == "web01"
    assert captured["mode"] == "reset"
    # the body's other convenience fields still arrive as defaults
    assert captured["nodes"] == [] and captured["engine_node"] == ""


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
    return mgr


def test_redeploy_forwards_its_team_selector_verbatim(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, monkeypatch)
    rec = mgr.submit("comp-a", "redeploy", "main",
                     {"teams": "team2", "boxes": "web01", "mode": "reset"})
    assert rec["params"]["teams"] == "team2"
    cmd = build_command("redeploy", "comp-a", rec["params"]["_destroy_comp_dir"], rec["params"])
    assert cmd[cmd.index("--teams") + 1] == "team2"
    assert cmd[cmd.index("--boxes") + 1] == "web01"
    assert cmd[cmd.index("--mode") + 1] == "reset"


def test_redeploy_command_carries_a_numeric_selector(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, monkeypatch)
    rec = mgr.submit("comp-a", "redeploy", "main", {"teams": "2", "boxes": "web01"})
    assert rec["params"]["teams"] == "2"
    cmd = build_command("redeploy", "comp-a", rec["params"]["_destroy_comp_dir"], rec["params"])
    assert cmd[cmd.index("--teams") + 1] == "2"


def test_deploy_still_coerces_the_team_count(tmp_path, monkeypatch):
    mgr = _manager(tmp_path, monkeypatch)
    rec = mgr.submit("comp-a", "deploy", "main", {"teams": "3"})
    assert rec["params"]["teams"] == 3
