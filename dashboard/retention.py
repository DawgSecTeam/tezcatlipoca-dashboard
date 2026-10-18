"""Sanctioned retention: move a RETIRED range's run dir under `retired-runs/<date>/`.

The ops host grew an ad-hoc `retired-runs/<date>/` tree while something outside both repos
deleted four run dirs' `comp/` — the estate document, the run id (the ownership proof) and the
evidence tree live there, so a deleted workspace strands its range. This module is the
deletion-free replacement: a run may only be archived once its range is retired (the same
predicate `/api/ranges/retire` records), and the whole run dir — `comp/` and its evidence
included — is MOVED, never deleted.

Usage (dashboard data comes from TEZ_DASHBOARD_DATA, else `<repo>/data`):

    python3 -m dashboard.retention run-abc123 [--data-dir DIR] [--date YYYY-MM-DD]

The four run dirs already under `retired-runs/2026-10-15/` are an ops step, not this code's.
"""

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path

from dashboard import config, ranges
from dashboard.runner import RunRefused, artifacts_root
from dashboard.store import Store

RETIRED_DIR = "retired-runs"


class RetentionRefused(RunRefused):
    """The run may not be archived: not retired, already archived, missing, or still active."""


def archive_run(store, run_id, data_dir=None, day=None):
    """Move `run_id`'s run dir to `<data>/retired-runs/<date>/<name>`, after the retire check.

    Returns a JSON-able record of the move. The run dir is never touched while its range is
    not retired, so an active range's `comp/` (the only place its ownership/teardown journal
    lives) cannot be moved out from under it.
    """
    run = store.get_run(run_id)
    if not run:
        raise RetentionRefused(f"no such run: {run_id}")
    key = ranges.range_key(run)
    rng = next((r for r in ranges.build_ranges(store) if r["key"] == key), None)
    if rng is None:
        raise RetentionRefused(f"run {run_id} does not belong to a known range")
    if rng["state"] != "retired":
        # Same gate as POST /api/ranges/retire: retirement is a recorded fact, never assumed
        # from a missing workspace or a failed deploy.
        raise RetentionRefused(
            f"the range at {rng['key']} is {rng['state']}, not retired — retire it first "
            f"(POST /api/ranges/retire, or a successful full destroy)")
    src = Path(run["run_dir"])
    data = Path(data_dir) if data_dir else config.data_dir()
    retired_root = data / RETIRED_DIR
    if retired_root.resolve() in src.resolve().parents:
        raise RetentionRefused(f"run {run_id} is already archived at {src}")
    if not src.is_dir():
        raise RetentionRefused(f"run {run_id} has no run dir at {src} — nothing to archive")
    busy = [r["id"] for r in store.list_runs()
            if Path(r["run_dir"]) == src and r["state"] in ranges.IN_FLIGHT]
    if busy:
        raise RetentionRefused(
            f"run dir {src} is held by an in-flight run ({', '.join(busy)}) — wait for it")
    dest = retired_root / (day or date.today().isoformat()) / src.name
    if dest.exists():
        raise RetentionRefused(f"refusing to overwrite the existing archive {dest}")
    # Evidence is archived by the MOVE itself: `artifacts_root` (comp/.automated-tests) is
    # inside the run dir and travels with it. Nothing is copied out or deleted.
    evidence = artifacts_root(run)
    files = sum(1 for path in evidence.rglob("*") if path.is_file()) if evidence.is_dir() else 0
    archived_evidence = (dest / evidence.relative_to(src) if src in evidence.parents
                         else evidence)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    return {
        "run_id": run_id,
        "range": rng["key"],
        "from": str(src),
        "to": str(dest),
        "evidence": str(archived_evidence) if archived_evidence.exists() else None,
        "evidence_files": files,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m dashboard.retention",
        description="Retire-gated retention: move a retired run's dir to retired-runs/<date>/.")
    parser.add_argument("run_id", help="the run to archive (its range must already be retired)")
    parser.add_argument("--data-dir", help="dashboard data dir (default TEZ_DASHBOARD_DATA)")
    parser.add_argument("--date", help="archive date folder, YYYY-MM-DD (default today)")
    args = parser.parse_args(argv)
    data = Path(args.data_dir) if args.data_dir else config.data_dir()
    store = Store(data / "dashboard.sqlite3")
    try:
        result = archive_run(store, args.run_id, data_dir=data, day=args.date)
    except RetentionRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
