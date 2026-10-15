"""Per-range competition lifecycle: what the management panel shows and drives.

A range's workspace (.deploy_state.json + terraform state) carries everything: the
scoring engine's address, the `scoring` automation account's password, and the
hold/ended markers. The engine's own API is the live truth for the rest — status is
polled as `scoring` (never `admin`: Quotient allows ONE session per account and the
operator's browser session must survive a panel refresh).
"""

import json
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_TIMEOUT = 5


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
    # The same read run-schedule does, without spawning terraform: the workspace's
    # tfstate carries the deploy's agent_context output.
    try:
        raw = json.loads((Path(ws) / "terraform" / "terraform.tfstate").read_text())
        return json.loads(raw["outputs"]["agent_context"]["value"])["scoring_engine_ip"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _login(base_url, password):
    r = requests.post(f"{base_url}/api/login",
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
        _login(base_url, state.get("scoring_password") or "")
        r = requests.get(f"{base_url}/api/engine", timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
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
    """The management panel's payload for one range view (ranges.range_view shape)."""
    ws = Path(rng["key"])
    state = {}
    try:
        state = json.loads((ws / ".deploy_state.json").read_text())
    except (OSError, ValueError):
        pass
    engine = engine_status(ws, state)
    ip = _engine_ip(ws)
    return {
        "range": rng["key"],
        "comp_id": rng["comp_id"],
        "deploy_run_id": rng["deploy_run_id"],
        "range_state": rng["state"],
        "state": lifecycle_state(engine, state),
        "reachable": engine is not None,
        "engine": engine or {},
        "hold_start": bool(state.get("hold_start")),
        "seeded": bool(state.get("seeded")),
        "ended": bool(state.get("ended")),
        **credentials(state, ip),
    }
