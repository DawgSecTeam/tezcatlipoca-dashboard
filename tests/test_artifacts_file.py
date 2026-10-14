"""The artifact file route serves a single collected file; anything outside the evidence
tree (traversal, missing) refuses with 404."""

import pytest
from fastapi.testclient import TestClient

from dashboard import config
from dashboard.runner import RunManager
from dashboard.store import Store


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("TEZ_DASHBOARD_TOKENS", raising=False)
    monkeypatch.setattr(RunManager, "ensure_repos", lambda self: None)
    monkeypatch.setattr(RunManager, "refresh_comps", lambda self: None)
    from dashboard.app import create_app
    return TestClient(create_app())


def _seed_run(files):
    """A run record whose workspace carries collected evidence under .automated-tests."""
    run_dir = config.data_dir() / "runs" / "run-1"
    root = run_dir / "comp" / ".automated-tests"
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    run_dir.mkdir(parents=True, exist_ok=True)
    store = Store(config.data_dir() / "dashboard.sqlite3")
    store.create_run("run-1", "mini", "scrim", "main", {}, run_dir, run_dir / "run.log")


def test_lists_and_serves_a_collected_file(client):
    _seed_run({"full-scrim-20261014/REPORT.md": "# report"})
    r = client.get("/api/runs/run-1/artifacts")
    assert r.status_code == 200
    assert r.json() == [{"path": "full-scrim-20261014/REPORT.md", "bytes": 8}]
    f = client.get("/api/runs/run-1/artifacts/file",
                   params={"path": "full-scrim-20261014/REPORT.md"})
    assert f.status_code == 200 and f.text == "# report"


def test_artifact_file_refuses_traversal_and_missing(client):
    _seed_run({"REPORT.md": "x"})
    (config.data_dir() / "secret.txt").write_text("sekret")
    for path in ("../secret.txt", "no-such.md", ""):
        r = client.get("/api/runs/run-1/artifacts/file", params={"path": path})
        assert r.status_code == 404, path


def test_artifact_file_404_for_unknown_run(client):
    r = client.get("/api/runs/run-nope/artifacts/file", params={"path": "x"})
    assert r.status_code == 404
