"""Sanctioned retention: a retired run dir is MOVED, never deleted, and only after retire."""

import json
import shutil
from pathlib import Path

import pytest

from dashboard import ranges, retention
from dashboard.retention import RetentionRefused, archive_run
from dashboard.store import Store


def _run(tmp_path, run_id="run-a"):
    data = tmp_path / "data"
    run_dir = data / "runs" / run_id
    ws = run_dir / "comp"
    ws.mkdir(parents=True)
    (ws / "Compfile").write_text("name c\n")
    (ws / "estate-state.json").write_text(json.dumps({
        "identity": {"comp_name": "c", "run_id": run_id}, "resources": []}))
    evidence = ws / ".automated-tests" / "run-one"
    evidence.mkdir(parents=True)
    (evidence / "run.json").write_text(json.dumps({"phases": []}))
    store = Store(data / "dashboard.sqlite3")
    store.create_run(run_id, "c", "deploy", "main", {}, run_dir, run_dir / "run.log")
    store.update_run(run_id, state="succeeded", returncode=0)
    return data, store, run_dir, ws, evidence


def test_a_non_retired_workspace_is_refused_and_left_intact(tmp_path):
    """The ops host's disappearing run dirs: retention must never move (let alone delete) a
    workspace whose range still exists."""
    data, store, run_dir, ws, evidence = _run(tmp_path)
    with pytest.raises(RetentionRefused, match="not retired"):
        archive_run(store, "run-a", data_dir=data, day="2026-10-18")
    assert run_dir.is_dir() and (ws / "estate-state.json").is_file()
    assert evidence.is_dir()
    assert not (data / "retired-runs").exists()


def test_a_retired_workspace_moves_without_deleting_comp(tmp_path):
    data, store, run_dir, ws, evidence = _run(tmp_path)
    store.retire_range(str(ws), "c", "test retire")
    assert ranges.build_ranges(store)[0]["state"] == "retired"

    moved = archive_run(store, "run-a", data_dir=data, day="2026-10-18")

    dest = data / "retired-runs" / "2026-10-18" / "run-a"
    assert moved["to"] == str(dest) and moved["evidence_files"] == 1
    assert not run_dir.exists()                       # the run dir MOVED...
    assert (dest / "comp" / "estate-state.json").is_file()   # ...with comp/ intact
    assert (dest / "comp" / "Compfile").is_file()
    assert (dest / "comp" / ".automated-tests" / "run-one" / "run.json").is_file()
    assert Path(moved["evidence"]).is_dir()
    # A retired range whose dir was archived is not a lost workspace; state does not change.
    rng = ranges.build_ranges(store)[0]
    assert rng["state"] == "retired" and rng["workspace_missing"] is False


def test_archiving_a_retired_run_never_calls_rmtree(tmp_path, monkeypatch):
    """The move must preserve comp/; a deletion call is a bug, not a shortcut."""
    data, store, run_dir, ws, evidence = _run(tmp_path)
    store.retire_range(str(ws), "c", "test retire")

    def forbidden(*args, **kwargs):
        raise AssertionError("retention must never rmtree a workspace")

    monkeypatch.setattr(retention.shutil, "rmtree", forbidden)
    archive_run(store, "run-a", data_dir=data, day="2026-10-18")
    assert (data / "retired-runs" / "2026-10-18" / "run-a" / "comp").is_dir()


def test_the_cli_refuses_a_non_retired_run_then_archives_it_after_retire(tmp_path):
    data, store, run_dir, ws, evidence = _run(tmp_path)
    argv = ["run-a", "--data-dir", str(data), "--date", "2026-10-18"]
    assert retention.main(argv) == 2
    assert run_dir.is_dir()
    store.retire_range(str(ws), "c", "test retire")
    assert retention.main(argv) == 0
    assert (data / "retired-runs" / "2026-10-18" / "run-a" / "comp"
            / "estate-state.json").is_file()


def test_the_archive_refuses_a_missing_run_dir_instead_of_inventing_one(tmp_path):
    data, store, run_dir, ws, evidence = _run(tmp_path)
    store.retire_range(str(ws), "c", "test retire")
    shutil.rmtree(run_dir)
    with pytest.raises(RetentionRefused, match="no run dir"):
        archive_run(store, "run-a", data_dir=data, day="2026-10-18")
