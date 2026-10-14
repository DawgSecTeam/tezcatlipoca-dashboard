"""The node catalog: the Proxmox hosts a run may be placed on, as dynamic config.

Replaces node selection from the engine env file. Tokens live here (server-side, 0600) and
are injected into a run's process env; the per-run node list handed to the engine carries
only the token_env names, so no secret lands in a worktree or a run record."""

import json
import os
import re

from dashboard import config

FIELDS = ("name", "endpoint", "node", "datastore", "engine_base_vmid", "engine_mgmt_ip",
          "engine_mgmt_gw", "jump_mgmt_ip", "ssh_host", "jump_template", "weight", "max_teams")
REQUIRED = ("name", "endpoint", "node", "datastore")
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
    old = {n["name"]: n.get("token") for n in load()["nodes"]}
    clean, seen = [], set()
    for raw in nodes:
        node = {k: v for k, v in raw.items() if k in FIELDS or k == "token"}
        for key in REQUIRED:
            if not str(node.get(key) or "").strip():
                raise ValueError(f"node {node.get('name') or '?'} is missing '{key}'")
        if node["name"] in seen:
            raise ValueError(f"duplicate node name {node['name']!r}")
        seen.add(node["name"])
        if not node.get("token"):
            node["token"] = old.get(node["name"]) or ""
        clean.append(node)
    path = catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"balancing": balancing or {}, "nodes": clean}, indent=2))
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return {"balancing": balancing or {}, "nodes": clean}


def view(catalog):
    return {"balancing": catalog["balancing"],
            "nodes": [{**{k: n.get(k) for k in FIELDS}, "has_token": bool(n.get("token"))}
                      for n in catalog["nodes"]]}


def token_env(catalog):
    # Every catalog token goes into every run's env, so a later verify/destroy resolves
    # its placement's token_env without the list being re-sent.
    return {token_env_for(n["name"]): n["token"] for n in catalog["nodes"] if n.get("token")}


def run_list(catalog, names):
    # The selected subset, in the engine's nodes.json schema — token_env names only.
    by_name = {n["name"]: n for n in catalog["nodes"]}
    missing = [x for x in names if x not in by_name]
    if missing:
        raise ValueError(f"unknown node(s): {', '.join(missing)}")
    no_token = [x for x in names if not by_name[x].get("token")]
    if no_token:
        raise ValueError(f"node(s) without an API token: {', '.join(no_token)}")
    return {"balancing": catalog["balancing"],
            "nodes": [{**{k: by_name[x].get(k) for k in FIELDS}, "token_env": token_env_for(x)}
                      for x in names]}
