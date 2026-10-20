"""The board's cached engine scoring: served stale-while-revalidating, never blocking."""

import threading
import time

from dashboard import scoring_cache


def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def setup_method():
    scoring_cache.reset()


def teardown_method():
    scoring_cache.reset()


def test_none_until_the_first_poll_lands_then_cached_within_ttl(monkeypatch):
    polls = []
    landed = threading.Event()

    def fake_poll(ws):
        polls.append(ws)
        entry = {"state": "running", "round": 7, "reachable": True, "at": time.time()}
        landed.set()
        return entry

    monkeypatch.setattr(scoring_cache, "_poll", fake_poll)
    rng = {"key": "/w/a", "has_recorded_vms": True}
    # The first request cannot know what the engine would say: it serves nothing
    # rather than block, and the background poll fills the cache.
    assert scoring_cache.snapshot(rng) is None
    assert landed.wait(5)
    assert _wait_until(lambda: scoring_cache.snapshot(rng) == {"state": "running", "round": 7, "reachable": True})
    # Fresh for the whole TTL: no further poll is started.
    before = len(polls)
    assert scoring_cache.snapshot(rng)["round"] == 7
    assert len(polls) == before


def test_serves_the_last_known_value_while_a_refresh_is_in_flight(monkeypatch):
    monkeypatch.setattr(scoring_cache, "TTL_SECONDS", 0.0)  # every read is stale
    polls = []
    first = threading.Event()
    second_started = threading.Event()
    second_may_finish = threading.Event()

    def fake_poll(ws):
        polls.append(ws)
        if len(polls) == 1:
            first.set()
            return {"state": "running", "round": 1, "reachable": True, "at": time.time()}
        second_started.set()
        second_may_finish.wait(5)
        return {"state": "running", "round": 2, "reachable": True, "at": time.time()}

    monkeypatch.setattr(scoring_cache, "_poll", fake_poll)
    rng = {"key": "/w/stale", "has_recorded_vms": True}
    scoring_cache.snapshot(rng)
    assert first.wait(5)
    assert _wait_until(lambda: scoring_cache.snapshot(rng) is not None)
    # The entry is now always stale, so a refresh starts — but the read still
    # answers immediately with the last-known value.
    assert scoring_cache.snapshot(rng)["round"] == 1
    assert second_started.wait(5)
    # And only one refresh per workspace is in flight, however often it is read.
    in_flight_reads = len(polls)
    scoring_cache.snapshot(rng)
    scoring_cache.snapshot(rng)
    assert len(polls) == in_flight_reads
    second_may_finish.set()
    assert _wait_until(lambda: scoring_cache.snapshot(rng)["round"] == 2)


def test_a_range_without_recorded_vms_has_nothing_to_poll(monkeypatch):
    def no_poll(ws):
        raise AssertionError("no recorded VMs means no engine to ask")

    monkeypatch.setattr(scoring_cache, "_poll", no_poll)
    assert scoring_cache.snapshot({"key": "/w/none", "has_recorded_vms": False}) is None


def test_prune_forgets_workspaces_the_board_no_longer_shows():
    scoring_cache._cache["/w/gone"] = {"state": "running", "round": 1, "reachable": True, "at": time.time()}
    scoring_cache._cache["/w/here"] = {"state": "paused", "round": 2, "reachable": True, "at": time.time()}
    scoring_cache.prune({"/w/here"})
    assert set(scoring_cache._cache) == {"/w/here"}


def test_the_gate_caps_concurrent_engine_polls(monkeypatch):
    monkeypatch.setattr(scoring_cache, "TTL_SECONDS", 0.0)
    started = []
    may_finish = threading.Event()
    lock = threading.Lock()

    def fake_poll(ws):
        with lock:
            started.append(ws)
        may_finish.wait(5)
        return {"state": "unknown", "round": 0, "reachable": False, "at": time.time()}

    monkeypatch.setattr(scoring_cache, "_poll", fake_poll)
    ranges = [{"key": f"/w/{i}", "has_recorded_vms": True} for i in range(scoring_cache._MAX_REFRESHES + 2)]
    for rng in ranges:
        scoring_cache.snapshot(rng)
    assert _wait_until(lambda: len(started) == scoring_cache._MAX_REFRESHES)
    time.sleep(0.1)
    assert len(started) == scoring_cache._MAX_REFRESHES  # the gate held the rest back
    may_finish.set()
    assert _wait_until(lambda: not scoring_cache._inflight)  # slots are free again
    for rng in ranges:  # a fresh read claims the freed slots
        scoring_cache.snapshot(rng)
    assert _wait_until(lambda: set(started) >= {rng["key"] for rng in ranges})


def test_last_round_reads_the_record_and_the_legacy_number():
    assert scoring_cache._round({"last_round": {"ID": 5, "StartTime": "x", "Checks": None, "SLAs": None}}) == 5
    assert scoring_cache._round({"last_round": 4}) == 4
    assert scoring_cache._round({}) == 0
    assert scoring_cache._round({"last_round": {"ID": 0}}) == 0
