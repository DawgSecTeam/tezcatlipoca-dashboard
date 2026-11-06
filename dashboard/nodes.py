"""The target catalog: the deploy backends a run may be placed on, as dynamic config.

Two platforms live in one catalog: Proxmox hosts (`platform: proxmox`, the historical
shape — endpoint/node/datastore/token) and GCP targets (`platform: gcp`, project/zone
plus the service-account key, server-side). Tokens and keys live here (0600) and are
injected into a run's process env; the per-run lists handed to the engine carry only
the token_env NAMES, so no secret lands in a worktree, a run record or a node list.

A GCP run needs no nodes.json at all (one zone per range is the engine's gcp model),
so the gcp credential rides as a per-run file the runner materializes, pointed at by
TEZ_GCP_CREDENTIALS_PATH.
"""

import json
import os
import re

from dashboard import config

PROXMOX = "proxmox"
GCP = "gcp"
KNOWN = (PROXMOX, GCP)

FIELDS = ("name", "platform", "endpoint", "node", "datastore", "engine_base_vmid",
          "engine_mgmt_ip", "engine_mgmt_gw", "jump_mgmt_ip", "ssh_host", "jump_template",
          "weight", "max_teams", "project", "zone")
REQUIRED = {"proxmox": ("name", "endpoint", "node", "datastore"),
            "gcp": ("name", "project", "zone")}
_TOKEN_ENV = re.compile(r"[^A-Za-z0-9]+")


def catalog_path():
    return config.data_dir() / "nodes.json"


def token_env_for(name):
    return "TEZ_NODE_TOKEN_" + _TOKEN_ENV.sub("_", name).upper()


def load():
    try:
        data = json.loads(catalog_path().read_text())
    except FileNotFoundError:
        return {"balancing": {}, "nodes": []}
    except ValueError as e:
        raise ValueError(f"node catalog is malformed: {e}") from e
    return {"balancing": data.get("balancing") or {}, "nodes": data.get("nodes") or []}


def save(nodes, balancing):
    # The UI round-trips a masked catalog: an entry with no token keeps the stored one.
    old = {n["name"]: {"token": n.get("token"), "credentials": n.get("credentials")}
           for n in load()["nodes"]}
    clean, seen = [], set()
    for raw in nodes:
        platform = str(raw.get("platform") or PROXMOX)
        if platform not in KNOWN:
            raise ValueError(f"node {raw.get('name') or '?'} has unknown platform {platform!r} "
                             f"(known: {', '.join(KNOWN)})")
        node = {k: v for k, v in raw.items() if k in FIELDS or k in ("token", "credentials")}
        node["platform"] = platform
        for key in REQUIRED[platform]:
            if not str(node.get(key) or "").strip():
                raise ValueError(f"node {node.get('name') or '?'} is missing '{key}'")
        if node["name"] in seen:
            raise ValueError(f"duplicate node name {node['name']!r}")
        seen.add(node["name"])
        secret_key = "credentials" if platform == GCP else "token"
        if not node.get(secret_key):
            node[secret_key] = (old.get(node["name"]) or {}).get(secret_key) or ""
        clean.append(node)
    path = catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"balancing": balancing or {}, "nodes": clean}, indent=2))
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return {"balancing": balancing or {}, "nodes": clean}


def view(catalog):
    out = []
    for n in catalog["nodes"]:
        row = {k: n.get(k) for k in FIELDS}
        row["platform"] = n.get("platform") or PROXMOX
        row["has_token"] = bool(n.get("token"))
        row["has_credentials"] = bool(n.get("credentials"))
        out.append(row)
    return {"balancing": catalog["balancing"], "nodes": out}


def token_env(catalog):
    # Every catalog token goes into every run's env, so a later verify/destroy resolves
    # its placement's token_env without the list being re-sent.
    return {token_env_for(n["name"]): n["token"] for n in catalog["nodes"]
            if (n.get("platform") or PROXMOX) == PROXMOX and n.get("token")}


def gcp_entry(catalog=None):
    """The catalog's gcp target (one per estate today; the name-select comes later)."""
    catalog = catalog if catalog is not None else load()
    for n in catalog.get("nodes") or []:
        if (n.get("platform") or PROXMOX) == GCP:
            return n
    return None


def run_list(catalog, names):
    # The selected subset, in the engine's nodes.json schema — token_env names only.
    by_name = {n["name"]: n for n in catalog["nodes"]}
    missing = [x for x in names if x not in by_name]
    if missing:
        raise ValueError(f"unknown node(s): {', '.join(missing)}")
    gcp = [x for x in names if (by_name[x].get("platform") or PROXMOX) == GCP]
    if gcp:
        # the engine's nodes.json is the Proxmox placement schema: a gcp run gets no
        # node list at all (one zone per range), so a mixed selection can't be honored.
        raise ValueError(f"gcp target(s) take no node list: {', '.join(gcp)}")
    no_token = [x for x in names if not by_name[x].get("token")]
    if no_token:
        raise ValueError(f"node(s) without an API token: {', '.join(no_token)}")
    return {"balancing": catalog["balancing"],
            "nodes": [{**{k: by_name[x].get(k) for k in FIELDS},
                       "platform": PROXMOX,
                       "token_env": token_env_for(x)} for x in names]}
