"""SQLite run store: run records + the append-only event log per run.

Durable across service restarts: a run's state/outcome and its stage events survive,
so a reconnecting client recovers via the event cursor instead of polling."""

import json
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, comp_id TEXT NOT NULL, action TEXT NOT NULL,
    engine_ref TEXT, params TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '',
    run_dir TEXT, log_path TEXT, pid INTEGER,
    returncode INTEGER, error TEXT,
    created_at REAL NOT NULL, started_at REAL, ended_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    run_id TEXT NOT NULL, seq INTEGER NOT NULL, ts REAL NOT NULL,
    kind TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, seq)
);
"""


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def create_run(self, run_id, comp_id, action, engine_ref, params, run_dir, log_path):
        with self._lock:
            self._db.execute(
                "INSERT INTO runs (id, comp_id, action, engine_ref, params, state, run_dir, "
                "log_path, created_at) VALUES (?,?,?,?,?, 'queued', ?,?,?)",
                (run_id, comp_id, action, engine_ref, json.dumps(params), str(run_dir),
                 str(log_path), time.time()))
            self._db.commit()
        return self.get_run(run_id)

    def update_run(self, run_id, **fields):
        allowed = {"state", "stage", "pid", "returncode", "error", "started_at", "ended_at"}
        sets = ", ".join(f"{k} = ?" for k in fields if k in allowed)
        vals = [v for k, v in fields.items() if k in allowed]
        with self._lock:
            self._db.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*vals, run_id))
            self._db.commit()

    def get_run(self, run_id):
        row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._view(row) if row else None

    def list_runs(self, state=None, comp_id=None):
        sql, vals = "SELECT * FROM runs WHERE 1=1", []
        if state:
            sql += " AND state = ?"
            vals.append(state)
        if comp_id:
            sql += " AND comp_id = ?"
            vals.append(comp_id)
        sql += " ORDER BY created_at DESC"
        return [self._view(r) for r in self._db.execute(sql, vals).fetchall()]

    def running_run_for_comp(self, comp_id):
        row = self._db.execute(
            "SELECT * FROM runs WHERE comp_id = ? AND state IN ('queued','running') "
            "ORDER BY created_at DESC", (comp_id,)).fetchone()
        return self._view(row) if row else None

    def add_event(self, run_id, kind, stage="", data=None):
        with self._lock:
            seq = self._db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?",
                (run_id,)).fetchone()[0]
            self._db.execute(
                "INSERT INTO events (run_id, seq, ts, kind, stage, data) VALUES (?,?,?,?,?,?)",
                (run_id, seq, time.time(), kind, stage, json.dumps(data or {})))
            self._db.commit()
        return seq

    def get_events(self, run_id, since=0):
        rows = self._db.execute(
            "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq",
            (run_id, since)).fetchall()
        events = [{"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], "stage": r["stage"],
                   **json.loads(r["data"])} for r in rows]
        nxt = events[-1]["seq"] if events else since
        return nxt, events

    @staticmethod
    def _view(row):
        v = dict(row)
        v["params"] = json.loads(v["params"])
        return v
