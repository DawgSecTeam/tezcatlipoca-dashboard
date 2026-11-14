"""SQLAlchemy engine/session factory for the scoring DB."""

import os
from pathlib import Path

# from sqlmodel import SQLModel, create_engine
from sqlalchemy import create_engine, event

from ._pragma import apply_sqlite_pragmas
from .models import score_meta

DB_PATH = Path(__file__).parent.parent.parent / "db" / "database.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)  # Create ../db/ if it doesn't exist

# Allow DATABASE_URL to be overridden via environment variable (used by the test suite
# to point at a separate test DB without touching db/database.db).
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DB_PATH}")

# Ensure the directory exists for any custom DB path too
_custom_db_path = Path(DATABASE_URL.replace("sqlite:///", ""))
_custom_db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    echo=False,  # SQL echo off: per-query logging across ~32 test-worker backends pegged host CPU
    connect_args={"timeout": 30, "check_same_thread": False},
    # docs/PLAN_soak_backend_perf.md §3.3: raise the pool so concurrent readers
    # (metrics/status/details) don't queue on the default 5-conn pool. WAL SQLite
    # handles many concurrent readers; the writer stays serialized through
    # write_actor regardless of pool size. Proven 2026-08-30: under Phase-5
    # mixed stress /admin/status stretched to 28-30s while the event loop was
    # idle — the wait was on a free DB connection, not on CPU.
    pool_size=32,
    max_overflow=32,
    pool_pre_ping=False,
)

event.listen(engine, "connect", apply_sqlite_pragmas)

# Runtime single-writer guard (diagnostic/defensive). No-op unless
# C3R_WRITE_GUARD=log|strict — then it flags any write to this engine off the
# write-actor thread (armed after startup by the app lifespan). See write_guard.
from .write_guard import install_write_guard  # noqa: E402

install_write_guard(engine)


def create_db_and_tables() -> None:
    """Create every scoring-engine table that doesn't already exist."""
    score_meta.create_all(engine)
