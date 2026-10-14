from dashboard.store import Store


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
