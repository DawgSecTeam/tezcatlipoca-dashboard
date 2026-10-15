"""The range's deployed infrastructure, projected for the run page's Teams/Infra tabs.

v0.3.0 replaced terraform's state with the competition's monolithic estate document
(`estate-state.json`): every VM and bridge the deploy created is a record in its
`resources` list, its identity/templates/run sections carry the deploy facts
`.deploy_state.json` used to, and `teams.json` (engine-written) carries the team roster.
This projects that document through `dashboard.estate` — the service-side mirror of the
engine's `estate.state` — and falls back to the legacy `.deploy_state.json` +
`terraform/terraform.tfstate` pair for a pre-v0.3 workspace, so those ranges still display.

Both shapes hold every generated password (the document's `secrets` section and
`.deploy_state.json` alike), so everything here reads named fields and never returns a
whole file. A missing workspace, state, document or config degrades to empty sections
instead of a 500.
"""

import json
from pathlib import Path

import yaml
from fastapi import HTTPException

from dashboard import comps_api, estate, ranges

# Legacy terraform resource types (pre-v0.3 workspaces only).
_VM_TYPE = "proxmox_virtual_environment_vm"
_BRIDGE_TYPE = "proxmox_network_linux_bridge"

# The document's resource kind for a non-VM record.
_BRIDGE_KIND = "bridge"


def build_inventory(store, run, rng=None):
    """Project local inventory; an already-resolved range avoids rebuilding the ledger."""
    key = rng["key"] if rng is not None else ranges.range_key(run)
    ws = Path(key)
    if rng is None:
        rng = next((r for r in ranges.build_ranges(store) if r["key"] == key), None)
    # The flat view is the deploy facts (last_phase, endpoint, seeded, …); load_flat reads
    # the v0.3.0 document with the legacy file as its read-only fallback.
    state = estate.load_flat(ws)
    document = estate.document(ws)
    boxes = _snapshot_boxes(ws, document, run["comp_id"])
    resources = estate.resources(ws)
    # A document always wins for the flat deploy facts; the INVENTORY is different. A
    # workspace folded into a document by `estate.state.migrate` has a document and an empty
    # resources list, with terraform's state still the only VM inventory it owns — so fall
    # back there (and for a workspace with no document at all).
    tfstate = {} if resources else _json(ws / "terraform" / "terraform.tfstate", {})
    if resources or not tfstate:
        identifiers = _team_identifiers(ws, state)
        teams = _teams_from_resources(resources, identifiers, _targets(ws, state), boxes)
        networks = _networks_from_resources(resources, identifiers, state)
        machines = _machines_from_resources(resources, state, teams, ws)
    else:
        teams = _teams(tfstate, boxes)
        networks = _networks(tfstate)
        machines = _machines(tfstate, state, ws, teams)
    return {
        # Mirrors /api/ranges minus its run history: the page already holds the run it is
        # showing, and a range's whole history must not ride in on every inventory read.
        "range": {k: v for k, v in (rng or {"key": key}).items() if k != "runs"},
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
        "teardown": _teardown(ws, document),
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
    """Legacy fallback when the range has no snapshot of its box definitions."""
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


def _targets(ws, state):
    """vm_name -> the deploy's frozen target row, for a box's assigned address.

    The document's `roster.targets` (written when a legacy range is folded in) or the
    engine's own `targets.json`. Neither exists for a partial deploy, so the caller falls
    back to the engine's own IP rule.
    """
    rows = state.get("targets")
    if not isinstance(rows, dict) or not rows:
        raw = _json(ws / "targets.json", {})
        rows = raw.get("targets") if isinstance(raw, dict) else {}
    return rows if isinstance(rows, dict) else {}


def _teams_from_resources(resources, identifiers, targets, boxes):
    """The recorded team boxes, grouped into the teams the roster names.

    A team box's recorded name is the deploy's vm_name (`team1-web01`, `113-web01` for
    every other team — the engine's positional naming), so the prefix before the first '-'
    is the team key for team1 and the subnet identifier otherwise. That is the same rule
    the legacy path reads off terraform's index_key.
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
            "kind": ("transit" if name.startswith("vmbrW")
                     else "satellite" if engine_node and node != engine_node
                     else "team"),
            "teams": [by_identifier[identifier]] if identifier in by_identifier else [],
            # A bridge record carries no comment: the engine hands it to the Proxmox API
            # (where it belongs), not to the state document.
            "comment": None,
        })
    return out


def _machines_from_resources(resources, state, teams, ws):
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
    red01 = _red01(ws, state)
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


# ── pre-v0.3: terraform's state ─────────────────────────────────────────────────────────

def _instances(tfstate, name):
    """(index_key, attributes) for every instance of a resource, satellites included."""
    for resource in tfstate.get("resources") or []:
        rname = str(resource.get("name") or "")
        if resource.get("type") != _VM_TYPE or not (
                rname == name or rname.startswith(name + "_")):
            continue
        for inst in resource.get("instances") or []:
            yield inst.get("index_key"), inst.get("attributes") or {}


def _identifiers(tfstate):
    """team key -> subnet identifier, read off the team bridges.

    The bridges are the only per-range record of a team's 192.168.<id> subnet that carries
    no secret (the deploy state's `teams` map does, and teams.json is off limits).
    """
    found = {}
    # Primary bridges first, then the satellites (sorted, so state order cannot decide):
    # only a satellite-hosted team needs the satellite's bridge to name its subnet.
    resources = sorted(
        (r for r in tfstate.get("resources") or []
         if r.get("type") == _BRIDGE_TYPE
         and str(r.get("name") or "").startswith("team_bridge")),
        key=lambda r: str(r.get("name")))
    for resource in resources:
        for inst in resource.get("instances") or []:
            name = (inst.get("attributes") or {}).get("name") or ""
            if inst.get("index_key") and name.startswith("vmbr") and name[4:].isdigit():
                found.setdefault(str(inst["index_key"]), name[4:])
    return found


def _teams(tfstate, boxes):
    identifiers = _identifiers(tfstate)
    by_identifier = {v: k for k, v in identifiers.items()}
    last_octet = {b["name"]: b.get("last_octet") for b in boxes if b.get("name")}
    teams = {k: {"key": k, "identifier": v, "subnet": f"192.168.{v}.0/24",
                 "engine_ip": f"192.168.{v}.1", "boxes": []}
             for k, v in identifiers.items() if v}
    for index_key, attrs in _instances(tfstate, "team_box"):
        prefix, _, box_name = str(index_key or "").partition("-")
        team_key = prefix if prefix in teams else by_identifier.get(prefix)
        if team_key not in teams or not box_name:
            continue
        team = teams[team_key]
        team["boxes"].append({
            "name": box_name,
            "vmid": attrs.get("vm_id"),
            "ip": _initialization_ip(attrs) or _fallback_ip(team["identifier"],
                                                            last_octet.get(box_name)),
            "node": attrs.get("node_name"),
            "started": attrs.get("started"),
            "tags": attrs.get("tags") or [],
        })
    return [teams[k] for k in sorted(teams, key=_team_order)]


def _networks(tfstate):
    out = []
    for resource in tfstate.get("resources") or []:
        name = str(resource.get("name") or "")
        if resource.get("type") != _BRIDGE_TYPE:
            continue
        for inst in resource.get("instances") or []:
            attrs = inst.get("attributes") or {}
            out.append({
                "name": attrs.get("name"),
                "node": attrs.get("node_name"),
                "kind": ("transit" if name.startswith("transit_bridge")
                         else "satellite" if "_sat" in name else "team"),
                "teams": [str(inst["index_key"])] if inst.get("index_key") else [],
                "comment": attrs.get("comment"),
            })
    return out


def _machines(tfstate, state, ws, teams):
    machines = {}
    engine = next(_instances(tfstate, "scoring_engine"), None)
    if engine:
        attrs = engine[1]
        machines["scoring_engine"] = {
            "name": attrs.get("name"), "vmid": attrs.get("vm_id"),
            "node": attrs.get("node_name"), "mgmt_ip": _initialization_ip(attrs),
            "team_ips": [t["engine_ip"] for t in teams],
        }
    red01 = _red01(ws, state)
    if red01:
        machines["red01"] = red01
    if state.get("engine_template_vmid"):
        machines["engine_template"] = {"vmid": state["engine_template_vmid"]}
    goldens = state.get("golden_template_ids")
    if goldens:
        machines["goldens"] = ([{"name": name, "vmid": vmid} for name, vmid in goldens.items()]
                               if isinstance(goldens, dict)
                               else [{"vmid": vmid} for vmid in goldens])
    return machines


# ── shared ──────────────────────────────────────────────────────────────────────────────

def _team_order(key):
    return int(key[4:]) if key.startswith("team") and key[4:].isdigit() else 0


def _initialization_ip(attrs):
    """The cloud-init address, which Windows/unmanaged boxes (and the default-mode engine
    with no mgmt IP) do not have."""
    try:
        return attrs["initialization"][0]["ip_config"][0]["ipv4"][0]["address"].split("/")[0]
    except (KeyError, IndexError, TypeError):
        return None


def _fallback_ip(identifier, last_octet):
    # common/targets.py's own rule (192.168.<team identifier>.<box last_octet>).
    return f"192.168.{identifier}.{last_octet}" if identifier and last_octet else None


def _red01(ws, state):
    """red01's identity: the comp's bad-auto config (what `badauto destroy` acts on), else
    the deploy state's assume-breach record (vmid/ip only)."""
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


def _teardown(ws, document):
    """The teardown journal's run id and step NAMES — the difference between a retired
    range and one whose red01 (and the LLM key on it) is still out there.

    The journal file is the engine's own live record; a range folded into the document
    carries the same fields in its `teardown` section, so read that when the file is gone.
    """
    journal = _json(ws / ".teardown-journal.json", {})
    if not isinstance(journal, dict) or not journal:
        journal = document.get("teardown") if isinstance(document.get("teardown"), dict) else {}
    done = journal.get("done") or {}
    return {"run_id": journal.get("run_id"), "done": list(done), "red01_done": "red01" in done}
