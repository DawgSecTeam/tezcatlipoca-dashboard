"""Cached engine scoring status for the ranges board.

The board should show live scoring state per range, but one engine observation is
a login plus GET /api/engine over the management network (and a five-second
timeout on a dead one), while the board is polled every 10s by every open tab.
So the board never waits on an engine: each workspace's status is served from a
process-local cache, and a stale entry is refreshed by a background thread while
the last-known value is served immediately. A range whose engine just came up
shows its scoring state one TTL later; a board with a dead engine stays instant.

No credentials ever enter this cache — the public block is state/round/reachable
only, exactly what the board card renders.
"""

import logging
import threading
import time
from pathlib import Path

from dashboard import competition, estate

log = logging.getLogger(__name__)

TTL_SECONDS = 15.0
_MAX_REFRESHES = 4

_lock = threading.Lock()
_cache = {}        # workspace path -> entry {state, round, reachable, at}
_inflight = set()  # workspace paths with a refresh thread running
_gate = threading.BoundedSemaphore(_MAX_REFRESHES)


def _round(engine):
    # /api/engine answers last_round as a round record ({ID, StartTime, Checks,
    # SLAs}); anything else (older engines stamped a bare number) still reads.
    last = (engine or {}).get("last_round")
    if isinstance(last, dict):
        return last.get("ID") or 0
    if isinstance(last, (int, float)):
        return int(last)
    return 0


def _poll(ws):
    state = estate.load_flat(ws)
    engine = competition.engine_status(ws, state)
    return {
        "state": competition.lifecycle_state(engine, state),
        "round": _round(engine),
        "reachable": engine is not None,
        "at": time.time(),
    }


def _launch(ws):
    def work():
        try:
            entry = _poll(ws)
            with _lock:
                # A range that lost its VMs while this thread ran is pruned by
                # then; writing anyway leaves one orphan entry until the next
                # prune, which is harmless.
                _cache[ws] = entry
        except Exception:  # keep the last-known entry; the next TTL retries
            log.warning("scoring poll failed for %s", ws, exc_info=True)
        finally:
            # The permit goes back before the in-flight marker clears, so "not in
            # flight" also means "its slot is free again".
            _gate.release()
            with _lock:
                _inflight.discard(ws)

    try:
        threading.Thread(target=work, name=f"scoring-cache:{Path(ws).name}",
                         daemon=True).start()
    except Exception:  # a poll that never starts must not consume a slot forever
        log.exception("cannot start scoring poll for %s", ws)
        _gate.release()
        with _lock:
            _inflight.discard(ws)


def snapshot(rng):
    """The board's scoring block for one range: cached, never blocking.

    None means nothing to show — either the range records no VMs of its own (no
    engine to poll, the same rule competition.status_for_range applies) or the
    first poll is still in flight.
    """
    if not rng.get("has_recorded_vms"):
        return None
    ws = rng["key"]
    with _lock:
        entry = _cache.get(ws)
        fresh = entry is not None and time.time() - entry["at"] < TTL_SECONDS
        # The gate, not the thread count, caps concurrent engine polls; a range
        # that cannot get a slot retries on the next board request.
        start = not fresh and ws not in _inflight and _gate.acquire(blocking=False)
        if start:
            _inflight.add(ws)
    if start:
        _launch(ws)
    if entry is None:
        return None
    return {"state": entry["state"], "round": entry["round"], "reachable": entry["reachable"]}


def prune(keep):
    """Drop cache entries for workspaces the board no longer shows as live."""
    with _lock:
        for ws in [ws for ws in _cache if ws not in keep and ws not in _inflight]:
            del _cache[ws]


def reset():
    """Test seam: forget everything, including in-flight markers."""
    with _lock:
        _cache.clear()
        _inflight.clear()
