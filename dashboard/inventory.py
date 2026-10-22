"""The range's deployed infrastructure, projected for the run page's Teams/Infra tabs.

A range's state is the competition's monolithic estate document (`estate-state.json`): every
VM and bridge the deploy created is a record in its `resources` list, its
identity/templates/run sections carry the deploy facts, and `teams.json` (engine-written)
carries the team roster. This projects that document through `dashboard.estate` — the
service-side mirror of the engine's `estate.state`.

The document holds every generated password (its `secrets` section), so everything here reads
named fields and never returns a whole file. A missing workspace, state, document or config
degrades to empty sections instead of a 500.
"""

import json
from pathlib import Path

import yaml
from fastapi import HTTPException

from dashboard import comps_api, estate, ranges

# The document's resource kind for a non-VM record.
_BRIDGE_KIND = "bridge"


def build_inventory(store, run, rng=None):
    """Project local inventory; an already-resolved range avoids rebuilding the ledger."""
    key = rng["key"] if rng is not None else ranges.range_key(run)
    ws = Path(key)
    if rng is None:
        rng = next((r for r in ranges.build_ranges(store) if r["key"] == key), None)
    # The flat view is the deploy facts (last_phase, endpoint, seeded, …), from the document.
    state = estate.load_flat(ws)
    document = estate.document(ws)
    boxes = _snapshot_boxes(ws, document, run["comp_id"])
    resources = estate.resources(ws)
    identifiers = _team_identifiers(ws, state)
    teams = _teams_from_resources(resources, identifiers, _targets(state), boxes)
    networks = _networks_from_resources(resources, identifiers, state)
    machines = _machines_from_resources(resources, state, teams, ws, document)
    return {
        # Mirrors /api/ranges minus its run history: the page already holds the run it is
        # showing, and a range's whole history must not ride in on every inventory read.
        "range": {k: v for k, v in (rng or {"key": key}).items() if k != "runs"},
        # A workspace that vanished is a lost range, not an empty deploy: surface it here too
        # so the run page and the board read the same fact.
        "workspace_missing": bool((rng or {}).get("workspace_missing", not ws.is_dir())),
        "deploy": {
            "last_phase": state.get("last_phase"),
            "endpoint": state.get("deployed_endpoint"),
            "seeded": state.get("seeded"),
            "multi_node": state.get("multi_node"),
            "degradations": state.get("degradations") or [],
        },
        "teams": teams,
        "networks": networks,
        "machines": machines,
        "teardown": _teardown(document),
        "counts": {"teams": len(teams), "boxes": sum(len(t["boxes"]) for t in teams),
                   "networks": len(networks)},
    }


def _json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _snapshot_boxes(ws, document, comp_id):
    """Frozen deploy roster, then workspace snapshot, never today's edited definition first."""
    roster = document.get("roster")
    boxes = roster.get("boxes") if isinstance(roster, dict) else None
    if isinstance(boxes, list):
        return [box for box in boxes if isinstance(box, dict)]
    boxes = _json(ws / "boxes.json", None)
    if isinstance(boxes, list):
        return [box for box in boxes if isinstance(box, dict)]
    return _comp_boxes(comp_id)


def _comp_boxes(comp_id):
    """Fallback when the range has no snapshot of its box definitions."""
    try:
        boxes = comps_api.read_json(comps_api.comp_dir(comp_id) / "boxes.json", [])
        return [box for box in boxes if isinstance(box, dict)] if isinstance(boxes, list) else []
    except (HTTPException, OSError, ValueError):
        return []


# ── v0.3.0: the estate document ─────────────────────────────────────────────────────────

def _team_identifiers(ws, state):
    """team key -> subnet identifier.

    The document's own roster — `secrets.teams`, which every deploy writes (`ctx.state
    ["teams"]` is absorbed on the first checkpoint) — or the engine-written `teams.json`,
    which a team removal rewrites. Both carry the team's password too, so only `identifier`
    is ever read. With neither (a document from a partial deploy) there is no key to hang a
    subnet on, and the teams list stays empty rather than inventing one.
    """
    for source in (state.get("teams"), _json(ws / "teams.json", {})):
        if not isinstance(source, dict):
            continue
        found = {str(key): str(entry["identifier"])
                 for key, entry in source.items()
                 if isinstance(entry, dict) and entry.get("identifier") not in (None, "")}
        if found:
            return found
    return {}


def _targets(state):
    """vm_name -> the deploy's frozen target row, for a box's assigned address.

    The document's `roster.targets`, written by common/targets.persist_targets at deploy. A
    partial deploy has none, so the caller falls back to the engine's own IP rule.
    """
    rows = state.get("targets")
    return rows if isinstance(rows, dict) else {}


def _teams_from_resources(resources, identifiers, targets, boxes):
    """The recorded team boxes, grouped into the teams the roster names.

    A team box's recorded name is the deploy's vm_name (`team1-web01`, `113-web01` for
    every other team — the engine's positional naming), so the prefix before the first '-'
    is the team key for team1 and the subnet identifier otherwise.
    """
    by_identifier = {v: k for k, v in identifiers.items()}
    last_octet = {b["name"]: b.get("last_octet") for b in boxes if b.get("name")}
    teams = {k: {"key": k, "identifier": v, "subnet": f"192.168.{v}.0/24",
                 "engine_ip": f"192.168.{v}.1", "boxes": []}
             for k, v in identifiers.items() if v}
    for row in resources:
        if row.get("kind") != "team_box":
            continue
        prefix, _, box_name = str(row.get("name") or "").partition("-")
        team_key = prefix if prefix in teams else by_identifier.get(prefix)
        if team_key not in teams or not box_name:
            continue
        target = targets.get(row.get("name")) or {}
        ip = target.get("ip") or _fallback_ip(teams[team_key]["identifier"],
                                             last_octet.get(box_name))
        teams[team_key]["boxes"].append({
            "name": box_name,
            "vmid": row.get("vmid"),
            "ip": ip,
            "node": row.get("node") or target.get("node"),
            # The document records ownership (vmid/tags), not power state: an absent value
            # renders as "unknown", never as "stopped".
            "started": None,
            "tags": row.get("tags") or [],
        })
    return [teams[k] for k in sorted(teams, key=_team_order)]


def _networks_from_resources(resources, identifiers, state):
    by_identifier = {v: k for k, v in identifiers.items()}
    engine_node = state.get("node") or ""
    out = []
    for row in resources:
        if row.get("kind") != _BRIDGE_KIND:
            continue
        name = str(row.get("bridge") or row.get("name") or "")
        if not name:
            continue
        node = row.get("node") or None
        identifier = name[4:].lstrip("W") if name.startswith("vmbr") else ""
        out.append({
            "name": name,
            "node": node,
            "kind": ("red" if name.startswith("vmbrR")
                     else "transit" if name.startswith("vmbrW")
                     else "satellite" if engine_node and node != engine_node
                     else "team"),
            "teams": [by_identifier[identifier]] if identifier in by_identifier else [],
            # A bridge record carries no comment: the engine hands it to the Proxmox API
            # (where it belongs), not to the state document.
            "comment": None,
        })
    return out


def _machines_from_resources(resources, state, teams, ws, document):
    machines = {}
    engine = next((r for r in resources if r.get("kind") == "engine"), None)
    if engine:
        machines["scoring_engine"] = {
            "name": engine.get("name"), "vmid": engine.get("vmid"),
            "node": engine.get("node"),
            # estate.engine_setup.engine_ctx's own read: identity.engine_mgmt_ip, "" on a
            # DHCP engine (which has no stable address to record here).
            "mgmt_ip": state.get("engine_mgmt_ip") or None,
            "team_ips": [t["engine_ip"] for t in teams],
        }
    red01 = _red01(ws, state, document)
    if red01:
        machines["red01"] = red01
    template = next((r for r in resources if r.get("kind") == "engine_template"), None)
    vmid = state.get("engine_template_vmid") or (template or {}).get("vmid")
    if vmid:
        entry = {"vmid": vmid}
        if template and template.get("node"):
            entry["node"] = template["node"]
        machines["engine_template"] = entry
    goldens = state.get("golden_template_ids")
    if not goldens:
        # The document's golden records are named `golden-<box>`; the flat map is richer
        # (it survives the resource being forgotten) so it wins when present.
        goldens = {str(r["name"])[len("golden-"):]: r.get("vmid")
                   for r in resources
                   if r.get("kind") == "golden" and r.get("vmid")
                   and str(r.get("name") or "").startswith("golden-")}
    if goldens:
        machines["goldens"] = ([{"name": name, "vmid": vmid} for name, vmid in goldens.items()]
                               if isinstance(goldens, dict)
                               else [{"vmid": vmid} for vmid in goldens])
    return machines


# ── shared ──────────────────────────────────────────────────────────────────────────────

def _team_order(key):
    return int(key[4:]) if key.startswith("team") and key[4:].isdigit() else 0


def _fallback_ip(identifier, last_octet):
    # common/targets.py's own rule (192.168.<team identifier>.<box last_octet>).
    return f"192.168.{identifier}.{last_octet}" if identifier and last_octet else None


def _red01(ws, state, document):
    """red01's identity: the document's `red` section (current engine), else the comp's
    bad-auto config, else the legacy assume-breach record."""
    red = document.get("red") or {}
    if isinstance(red, dict) and (red.get("vmid") or red.get("ip")):
        return {"vmid": red.get("vmid"), "ip": red.get("ip"), "node": red.get("node")}
    cfg = _yaml(ws / ".badauto-config.yaml")
    deploy = cfg.get("deploy") or {}
    breach = state.get("assume_breach") or {}
    vmid = deploy.get("red_vmid") or breach.get("vmid")
    ip = deploy.get("red_ip") or breach.get("red_ip")
    return {"vmid": vmid, "ip": ip} if vmid or ip else None


def _yaml(path):
    try:
        text = path.read_text()
    except OSError:
        return {}
    try:
        return json.loads(text) or {}
    except ValueError:
        pass
    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError:  # a hand-edited config must not 500 the run page
        return {}


def _teardown(document):
    """The teardown section's run id and step NAMES — the difference between a retired range
    and one whose red01 (and the LLM key on it) is still out there.

    teardown/engine.py journals a step only once it actually completed, and never forgets
    one, so `done` is the live record of what this range's destroy has processed.
    """
    section = document.get("teardown") if isinstance(document.get("teardown"), dict) else {}
    done = section.get("done") or {}
    return {"run_id": section.get("run_id"), "done": list(done), "red01_done": "red01" in done}
