from dashboard.store import Store


def test_an_existing_data_dir_gains_the_engine_head_column(tmp_path):
    """A live data dir predates `engine_head`; CREATE TABLE IF NOT EXISTS would leave the old
    table, so a record could not state what it ran."""
    import sqlite3

    db = tmp_path / "db.sqlite3"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, comp_id TEXT, action TEXT, "
                "engine_ref TEXT, params TEXT, state TEXT, stage TEXT, run_dir TEXT, "
                "log_path TEXT, pid INTEGER, returncode INTEGER, error TEXT, created_at REAL, "
                "started_at REAL, ended_at REAL)")
    con.execute("INSERT INTO runs (id, state, params, created_at) "
                "VALUES ('run-old', 'failed', '{}', 1)")
    con.commit()
    con.close()

    store = Store(db)
    assert store.get_run("run-old")["engine_head"] is None
    store.update_run("run-old", engine_head="d" * 40)
    assert store.get_run("run-old")["engine_head"] == "d" * 40


def test_run_record_roundtrip(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    run = store.create_run("run-1", "comp-a", "deploy", "main", {"teams": 2},
                           tmp_path / "r", tmp_path / "r" / "run.log")
    assert run["state"] == "queued" and run["params"] == {"teams": 2}
    store.update_run("run-1", state="running", pid=42, stage="phase 2/8 engine_template")
    run = store.get_run("run-1")
    assert run["state"] == "running" and run["pid"] == 42
    store.update_run("run-1", state="succeeded", returncode=0)
    assert [r["id"] for r in store.list_runs(state="succeeded")] == ["run-1"]


def test_event_cursor(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.create_run("run-1", "comp-a", "deploy", "main", {}, tmp_path / "r", None)
    store.add_event("run-1", "stage", stage="phase 1/8 cleanup")
    store.add_event("run-1", "lifecycle", data={"state": "running"})
    nxt, events = store.get_events("run-1", 0)
    assert nxt == 2 and [e["kind"] for e in events] == ["stage", "lifecycle"]
    nxt2, again = store.get_events("run-1", nxt)
    assert nxt2 == nxt and again == []


def test_an_absent_log_path_is_null_not_the_string_none(tmp_path):
    """`str(None)` recorded the literal "None"; a later append then created a file by that
    name in the service's cwd instead of skipping a logless run."""
    store = Store(tmp_path / "db.sqlite3")
    run = store.create_run("run-nolog", "comp-a", "verify", "main", {}, tmp_path / "r", None)
    assert run["log_path"] is None
    assert store.get_run("run-nolog")["log_path"] is None
