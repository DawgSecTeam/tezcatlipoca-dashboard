"""A failed run must explain itself in the journal and in its own log.

These paths run on a background thread, so an exception there never reaches uvicorn's
access log and used to leave only a bare `state=failed` row. Pinned here: startup orphan
reconciliation and a worker crash both journal the traceback/reason, append it to the run's
log (the first place an operator looks), and carry it on the terminal event.
"""

import logging
from contextlib import contextmanager
from pathlib import Path

from dashboard.events import EventBus
from dashboard.runner import RunManager
from dashboard.store import Store


@contextmanager
def _capture(caplog):
    """The dashboard logger does not propagate (a plain root handler would duplicate every
    line), so attach pytest's capture handler directly for these assertions."""
    logger = logging.getLogger("dashboard.runner")
    logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


def _manager(tmp_path, monkeypatch):
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path))
    store = Store(tmp_path / "dashboard.sqlite3")
    manager = RunManager(store, EventBus())
    manager.runs_dir.mkdir(parents=True, exist_ok=True)
    return store, manager


def _seed(store, tmp_path, run_id="run-orphan", state="running"):
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log = run_dir / "run.log"
    log.write_text("driver output\n")
    store.create_run(run_id, "comp-a", "scrim", "main", {}, run_dir, log)
    store.update_run(run_id, state=state)
    return run_dir, log


def test_reconcile_orphans_logs_the_reason_on_the_event_and_the_run_log(
        tmp_path, monkeypatch, caplog):
    store, manager = _manager(tmp_path, monkeypatch)
    _, log = _seed(store, tmp_path)
    with _capture(caplog):
        assert manager.reconcile_orphans() == ["run-orphan"]
    run = store.get_run("run-orphan")
    assert run["state"] == "failed" and run["error"].startswith("orphaned:")
    text = log.read_text()
    assert "[dashboard] failed: orphaned: no worker after service restart" in text
    _, events = store.get_events("run-orphan", 0)
    terminal = events[-1]
    assert terminal["state"] == "failed" and terminal["orphaned"] is True
    assert terminal["error"] == run["error"]
    assert "closed orphaned run run-orphan" in caplog.text


def test_worker_crash_is_journaled_and_appended_to_the_run_log(
        tmp_path, monkeypatch, caplog):
    store, manager = _manager(tmp_path, monkeypatch)
    run_dir, log = _seed(store, tmp_path, run_id="run-crash", state="queued")
    record = store.get_run("run-crash")

    def explode(*_args, **_kwargs):
        raise RuntimeError("boom while seeding")

    monkeypatch.setattr(manager, "_workspace", explode)
    with _capture(caplog):
        manager._run(record, tmp_path / "comp", "main")

    run = store.get_run("run-crash")
    assert run["state"] == "failed"
    assert "RuntimeError: boom while seeding" in (run["error"] or "")
    text = log.read_text()
    assert "[dashboard] failed: RuntimeError: boom while seeding" in text
    _, events = store.get_events("run-crash", 0)
    assert events[-1]["error"] == run["error"]
    assert "run run-crash (comp-a/scrim) crashed" in caplog.text
    assert "RuntimeError: boom while seeding" in caplog.text


def test_the_worktree_is_released_even_when_the_worker_crashes(tmp_path, monkeypatch):
    store, manager = _manager(tmp_path, monkeypatch)
    run_dir, _ = _seed(store, tmp_path, run_id="run-clean", state="queued")
    worktree = run_dir / "engine"
    (worktree / "sub").mkdir(parents=True)
    (worktree / "sub" / "file").write_text("x")
    record = store.get_run("run-clean")
    monkeypatch.setattr(manager, "_workspace", lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("no workspace")))
    manager._run(record, tmp_path / "comp", "main")
    assert not worktree.exists()
    assert (run_dir / "comp").parent == run_dir  # the range workspace itself is never deleted


def test_shutdown_grace_is_bounded_and_invalid_values_fall_back(monkeypatch):
    from dashboard import config

    monkeypatch.delenv("TEZ_DASHBOARD_SHUTDOWN_GRACE", raising=False)
    assert config.shutdown_grace_seconds() == 10
    monkeypatch.setenv("TEZ_DASHBOARD_SHUTDOWN_GRACE", "2")
    assert config.shutdown_grace_seconds() == 2
    monkeypatch.setenv("TEZ_DASHBOARD_SHUTDOWN_GRACE", "not-a-number")
    assert config.shutdown_grace_seconds() == 10
