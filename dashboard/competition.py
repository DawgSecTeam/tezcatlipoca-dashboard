"""Per-range competition lifecycle: what the management panel shows and drives.

A range's workspace carries everything: the estate document's identity records the scoring
engine's address, its secrets section the `scoring` automation account's password, and its
run section the hold/ended markers (a pre-v0.3 workspace has the legacy `.deploy_state.json`
plus terraform state instead — `dashboard.estate` reads both). The engine's own API is the
live truth for the rest — status is polled as `scoring` (never `admin`: Quotient allows ONE
session per account and the operator's browser session must survive a panel refresh).
"""

import json
from pathlib import Path

import requests
import urllib3

from dashboard import estate

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


def _tfstate_engine_ip(ws):
    """A pre-v0.3 workspace's engine address, from terraform's agent_context output."""
    try:
        raw = json.loads((Path(ws) / "terraform" / "terraform.tfstate").read_text())
        return json.loads(raw["outputs"]["agent_context"]["value"])["scoring_engine_ip"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _engine_ip(ws):
    # estate.engine_setup.engine_ctx's own read: the document's identity.engine_mgmt_ip
    # ("" on a DHCP engine, which has no stable address to record).
    legacy = _tfstate_engine_ip(ws)
    # A workspace folded into a document by estate.state.migrate has no resources list and
    # an engine IP taken from the ambient env at migration time; terraform's recorded output
    # is still the range's own answer, so it wins there.
    if legacy and not estate.has_inventory(ws):
        return legacy
    return estate.load_flat(ws).get("engine_mgmt_ip") or legacy


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
    state = estate.load_flat(ws)
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
