"""SQLite run store: run records + the append-only event log per run.

Durable across service restarts: a run's state/outcome and its stage events survive,
so a reconnecting client recovers via the event cursor instead of polling."""

import json
import secrets
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
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    range_key TEXT NOT NULL, comp_id TEXT NOT NULL, portal_id TEXT NOT NULL,
    team TEXT NOT NULL, box TEXT NOT NULL,
    requested_at TEXT, requested_by TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    approved_run TEXT,
    created_ts REAL NOT NULL,
    UNIQUE (range_key, portal_id)
);
CREATE TABLE IF NOT EXISTS range_tokens (
    token TEXT PRIMARY KEY, range_key TEXT NOT NULL, comp_id TEXT NOT NULL,
    created_ts REAL NOT NULL
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
        created = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO runs (id, comp_id, action, engine_ref, params, state, run_dir, "
                "log_path, created_at) VALUES (?,?,?,?,?, 'queued', ?,?,?)",
                (run_id, comp_id, action, engine_ref, json.dumps(params), str(run_dir),
                 str(log_path), created))
            self._db.commit()
        # The inserted record, not a re-read: a transient read failure here handed the
        # caller None, which then died unpacking in the worker and left the queued row
        # behind forever with no process (2026-10-15 run-31072cb0).
        return {"id": run_id, "comp_id": comp_id, "action": action, "engine_ref": engine_ref,
                "params": params, "state": "queued", "stage": "", "run_dir": str(run_dir),
                "log_path": str(log_path), "pid": None, "returncode": None, "error": None,
                "created_at": created, "started_at": None, "ended_at": None}

    def update_run(self, run_id, **fields):
        allowed = {"state", "stage", "pid", "returncode", "error", "started_at", "ended_at"}
        sets = ", ".join(f"{k} = ?" for k in fields if k in allowed)
        vals = [v for k, v in fields.items() if k in allowed]
        with self._lock:
            self._db.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*vals, run_id))
            self._db.commit()

    def get_run(self, run_id):
        # Reads take the same lock as writes: the connection is shared across the FastAPI
        # threadpool, and sqlite3 connections are not thread-safe even with
        # check_same_thread=False — concurrent drawer fetches could reset each other's
        # statements and made get_run transiently return nothing ("no such run" on a run
        # that plainly exists).
        with self._lock:
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
        with self._lock:
            rows = self._db.execute(sql, vals).fetchall()
        return [self._view(r) for r in rows]

    def add_event(self, run_id, kind, stage="", data=None):
        """Append one event and return it in the SAME shape `get_events` yields.

        The returned dict is what the bus pushes (runner._emit), so a pushed event and a
        cursor replay can never drift into two shapes."""
        ts = time.time()
        with self._lock:
            seq = self._db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?",
                (run_id,)).fetchone()[0]
            self._db.execute(
                "INSERT INTO events (run_id, seq, ts, kind, stage, data) VALUES (?,?,?,?,?,?)",
                (run_id, seq, ts, kind, stage, json.dumps(data or {})))
            self._db.commit()
        return {"seq": seq, "ts": ts, "kind": kind, "stage": stage, **(data or {})}

    def get_events(self, run_id, since=0):
        with self._lock:
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

    # ---- reset requests (the portal's push; the dashboard is the status authority) ----

    def upsert_requests(self, range_key, comp_id, rows):
        """Insert unknown requests, then answer with THIS store's status per portal id.

        The portal pushes its whole list every sync; the response's statuses are
        authoritative and it converges toward them, so an approve/deny/reset-run outcome
        reaches the team without any dashboard → portal path. New rows always start
        `pending` regardless of what the portal claims (only the dashboard resolves)."""
        statuses = {}
        with self._lock:
            for row in rows:
                portal_id = str(row.get("id") or "")
                if not portal_id:
                    continue
                cur = self._db.execute(
                    "SELECT id, status FROM requests WHERE range_key = ? AND portal_id = ?",
                    (range_key, portal_id)).fetchone()
                if cur:
                    statuses[portal_id] = cur["status"]
                    continue
                self._db.execute(
                    "INSERT INTO requests (range_key, comp_id, portal_id, team, box, "
                    "requested_at, requested_by, status, created_ts) "
                    "VALUES (?,?,?,?,?,?,?,'pending',?)",
                    (range_key, comp_id, portal_id, str(row.get("team") or ""),
                     str(row.get("box") or ""), str(row.get("at") or ""),
                     str(row.get("by") or ""), time.time()))
                statuses[portal_id] = "pending"
            self._db.commit()
        return statuses

    def list_requests(self):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM requests ORDER BY created_ts DESC").fetchall()
        return [dict(r) for r in rows]

    def set_request_status(self, req_id, status, approved_run=None):
        with self._lock:
            self._db.execute(
                "UPDATE requests SET status = ?, approved_run = ? WHERE id = ?",
                (status, approved_run, req_id))
            self._db.commit()
        return self.get_request(req_id)

    def get_request(self, req_id):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM requests WHERE id = ?", (req_id,)).fetchone()
        return dict(row) if row else None

    def requests_for_run(self, run_id):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM requests WHERE approved_run = ?", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- per-range ops tokens (what a deployed portal presents at /api/ops/*) ----

    def mint_range_token(self, range_key, comp_id):
        token = secrets.token_urlsafe(24)
        with self._lock:
            # One token per WORKSPACE: the primary key is the token, so INSERT OR REPLACE
            # only replaced a row when the token collided, and a re-deploy into the same
            # workspace accumulated rows that token_for_range then picked arbitrarily.
            self._db.execute("DELETE FROM range_tokens WHERE range_key = ?", (range_key,))
            self._db.execute(
                "INSERT OR REPLACE INTO range_tokens (token, range_key, comp_id, created_ts) "
                "VALUES (?,?,?,?)", (token, range_key, comp_id, time.time()))
            self._db.commit()
        return token

    def range_for_token(self, token):
        if not token:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM range_tokens WHERE token = ?", (token,)).fetchone()
        return dict(row) if row else None

    def token_for_range(self, range_key):
        with self._lock:
            row = self._db.execute(
                "SELECT token FROM range_tokens WHERE range_key = ?",
                (range_key,)).fetchone()
        return row["token"] if row else None
