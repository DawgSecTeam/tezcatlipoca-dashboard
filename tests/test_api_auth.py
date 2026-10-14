import os

import pytest
from fastapi.testclient import TestClient

from dashboard import config
from dashboard.runner import RunManager


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("TEZ_DASHBOARD_TOKENS", "zcode=sekrit,hn=other")
    monkeypatch.setattr(RunManager, "ensure_repos", lambda self: None)
    monkeypatch.setattr(RunManager, "refresh_comps", lambda self: None)
    from dashboard.app import create_app
    return TestClient(create_app())


def test_health_is_open(client):
    assert client.get("/api/health").status_code == 200


def test_api_requires_bearer(client):
    assert client.get("/api/runs").status_code == 401
    ok = client.get("/api/runs", headers={"Authorization": "Bearer sekrit"})
    assert ok.status_code == 200 and ok.json() == []


def test_submit_rejects_unknown_comp(client):
    r = client.post("/api/runs", headers={"Authorization": "Bearer other"},
                    json={"comp_id": "ghost", "action": "deploy"})
    assert r.status_code == 409 and "no such competition" in r.json()["detail"]


def test_run_404(client):
    assert client.get("/api/runs/run-nope",
                      headers={"Authorization": "Bearer sekrit"}).status_code == 404


def test_tokens_parse(monkeypatch):
    monkeypatch.setenv("TEZ_DASHBOARD_TOKENS", "a=x, b = y ")
    assert config.tokens() == {"x": "a", "y": "b"}
    assert os.environ["TEZ_DASHBOARD_TOKENS"]
