"""Per-range competition lifecycle: what the management panel shows and drives.

A range's workspace carries everything: the estate document's identity records the scoring
engine's address, its secrets section the `scoring` automation account's password, and its
run section the hold/ended markers. The engine's own API is the live truth for the rest —
status is polled as `scoring` (never `admin`: Quotient allows ONE session per account and
the operator's browser session must survive a panel refresh).
"""

from pathlib import Path

import requests
import urllib3

from dashboard import estate

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_TIMEOUT = 5

# The deploy action and the range model's live run states. The states are imported at call
# time: ranges itself imports this module for its per-range summaries, so the dependency
# stays one-way at module load.
# A resume_deploy is a deploy too: it builds the same range from the phase it reached.
_DEPLOY_ACTIONS = ("deploy", "resume_deploy")


def deploying(rng):
    """Whether a deploy still owns this range and is building its engine.

    The engine boots and answers `/api/engine` before create-competition has finished: the
    later phases seed teams, bootstrap the firewall and bring the portal up. A competition
    panel observing only the engine therefore offered "Start the competition" against a
    half-built range. A verify/redeploy also makes the range read `building`, but those run
    against an engine that already exists, so only a deploy holds the controls back.
    """
    from dashboard.ranges import IN_FLIGHT

    runs = rng.get("runs")
    if runs is None:             # a caller with only a range view (no run records)
        if "deploying" in rng:
            return bool(rng["deploying"])
        return rng.get("state") == "building"
    return any(run.get("action") in _DEPLOY_ACTIONS and run.get("state") in IN_FLIGHT
               for run in runs)


def lifecycle_state(engine, state):
    """What the engine's /api/engine says → the panel's one-word state."""
    if state.get("ended"):
        return "ended"
    if not engine:
        return "unknown"
    if not engine.get("competition_started"):
        return "ready"          # deployed, teams seeded, clock not started
    return "running" if engine.get("running") else "paused"


def _engine_ip(ws):
    # estate.engine_setup.engine_ctx's own read: the document's identity.engine_mgmt_ip
    # ("" on a DHCP engine, which has no stable address to record).
    return estate.load_flat(ws).get("engine_mgmt_ip") or None


def _login(session, base_url, password):
    r = session.post(f"{base_url}/api/login",
                     json={"username": "scoring", "password": password}, timeout=_TIMEOUT)
    r.raise_for_status()
    return r


def engine_status(ws, state):
    """GET /api/engine as the scoring account, or None when the engine cannot answer."""
    ip = _engine_ip(ws)
    if not ip:
        return None
    base_url = f"http://{ip}"
    try:
        # /api/engine is admin-only, and the login's cookie is what carries that role;
        # a second bare request is anonymous and always 401.
        session = requests.Session()
        _login(session, base_url, state.get("scoring_password") or "")
        r = session.get(f"{base_url}/api/engine", timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        # A 200 that is not the engine payload (a proxy page, a list, an older schema) is not
        # an observation: treating it as one offered Start on `{"status": "ok"}` and crashed
        # the panel on a JSON list.
        if not isinstance(data, dict) or "competition_started" not in data:
            return None
        return data
    except (requests.RequestException, ValueError):
        return None


def credentials(state, engine_ip):
    """The operator-facing logins + admin-page links, from the workspace's own state."""
    base = f"http://{engine_ip}" if engine_ip else ""
    out = {}
    if state.get("admin_password"):
        out["admin"] = {"user": "admin", "password": state["admin_password"]}
    if state.get("inject_password"):
        out["inject"] = {"user": "inject", "password": state["inject_password"]}
    links = {}
    if base:
        links = {"scoreboard": base, "admin": f"{base}/admin",
                 "announcements": f"{base}/announcements", "injects": f"{base}/injects"}
    portal = state.get("portal_url") or ""
    if portal and state.get("portal_up"):
        links["portal"] = portal
    return {"creds": out, "links": links}


def status_for_range(store, rng):
    """The management panel's payload for one range view (ranges.range_view shape).

    A workspace that records no VM of its own has no engine to poll and no engine to link
    to: its recorded address is where the engine used to be, so both are withheld rather
    than answered with a five-second timeout and a dead scoreboard link.
    """
    ws = Path(rng["key"])
    state = estate.load_flat(ws)
    recorded = bool(rng.get("has_recorded_vms"))
    building = deploying(rng)
    # An engine answer from a range that is still being built is not a readiness signal, and
    # the poll would burn its five-second timeout on a box that is mid-boot. The panel shows
    # `deploying` instead; the first poll after the deploy ends answers for real.
    engine = engine_status(ws, state) if recorded and not building else None
    # Links are built from this address: a recorded address behind a dead engine produced
    # clickable Scoreboard/Admin URLs on a card that said "engine unreachable". Null it when
    # the poll failed; the credentials stay (recorded recovery secrets).
    ip = _engine_ip(ws) if recorded and engine is not None else None
    return {
        "range": rng["key"],
        "comp_id": rng["comp_id"],
        "deploy_run_id": rng["deploy_run_id"],
        "range_state": rng["state"],
        "deploying": building,
        "tearing_down": bool(rng.get("tearing_down")),
        "settled": bool(rng["settled"]) if "settled" in rng else rng["state"] != "building",
        "deploy_state": rng.get("deploy_state"),
        "deploy_failed": bool(rng.get("deploy_failed")),
        "state": "deploying" if building else lifecycle_state(engine, state),
        "reachable": engine is not None,
        "engine": engine or {},
        "hold_start": bool(state.get("hold_start")),
        "seeded": bool(state.get("seeded")),
        "ended": bool(state.get("ended")),
        **credentials(state, ip),
    }
