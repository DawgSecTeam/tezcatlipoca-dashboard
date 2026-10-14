"""The range's deployed infrastructure, projected for the run page's Teams/Infra tabs.

The workspace holds two files with every generated password (`.deploy_state.json` and
`teams.json`), so everything here reads named fields and never returns a whole file. A
missing workspace, tfstate or config degrades to empty sections instead of a 500.
"""

import json
from pathlib import Path

import yaml
from fastapi import HTTPException

from dashboard import comps_api, ranges

# .deploy_state.json also holds admin_password, box_password, box_creds, domain_creds,
# inject_password, scoring_password, *_password and teams — never read those.
_VM_TYPE = "proxmox_virtual_environment_vm"
_BRIDGE_TYPE = "proxmox_network_linux_bridge"


def build_inventory(store, run):
    """Everything the run page knows about the range a run belongs to."""
    key = ranges.range_key(run)
    ws = Path(key)
    rng = next((r for r in ranges.build_ranges(store) if r["key"] == key), None)
    state = _json(ws / ".deploy_state.json", {})
    tfstate = _json(ws / "terraform" / "terraform.tfstate", {})
    teams = _teams(tfstate, _comp_boxes(run["comp_id"]))
    networks = _networks(tfstate)
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
        "machines": _machines(tfstate, state, ws, teams),
        "teardown": _teardown(ws),
        "counts": {"teams": len(teams), "boxes": sum(len(t["boxes"]) for t in teams),
                   "networks": len(networks)},
    }


def _json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _comp_boxes(comp_id):
    """The competition's box definitions, via the comps checkout's own reader."""
    try:
        return comps_api.read_json(comps_api.comp_dir(comp_id) / "boxes.json", [])
    except HTTPException:
        return []


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


def _teardown(ws):
    """The teardown journal's run id and step NAMES — the difference between a retired
    range and one whose red01 (and the LLM key on it) is still out there."""
    journal = _json(ws / ".teardown-journal.json", {})
    done = journal.get("done") or {}
    return {"run_id": journal.get("run_id"), "done": list(done), "red01_done": "red01" in done}
