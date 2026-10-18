"""The competition's estate state document, read from a run workspace.

A range's state is the engine's monolithic `estate-state.json` (identity / secrets / roster /
templates / run / resources / teardown). This module is the service-side mirror of the
engine's `estate.state` — the dashboard reads workspaces directly and never imports a per-run
engine worktree, so it cannot call `estate.state.load_flat` itself; it must agree with it
instead:

  * `load_flat` / `read_flat` flatten the document's sections in `estate.state`'s own order
    (`identity`, `secrets`, `roster`, `templates`, `run`) so every flat key the dashboard
    already knew (`last_phase`, `run_id`, `seeded`, `engine_mgmt_ip`, `teams`, …) keeps
    resolving;
  * `read_flat` returns None when the comp dir carries no document, while `load_flat`
    returns `{}`;
  * `document()` / `resources()` expose the parts the flat view cannot carry: the VM and
    bridge inventory (`resources`) and the teardown section (`teardown`).

Only the fields a projection needs are read by callers; `document()` exists for the two
parts the flat view cannot carry (the resource list and the teardown section) and must
never be handed to a response, because the document holds every generated password.
"""

import json
from pathlib import Path

STATE_NAME = "estate-state.json"

# estate/state.py's _SECTIONS: the sections flatten() folds into the flat view, in order.
_SECTIONS = ("identity", "secrets", "roster", "templates", "run")

# Kinds a teardown deliberately leaves behind: `--teams-only` / `--keep-templates` keep this
# competition's golden + engine templates for its next run, so a range whose only remaining
# rows are these is torn down, not live.
KEPT_KINDS = ("engine_template", "golden")


def state_path(comp_dir):
    """Path of the competition's estate document."""
    return Path(comp_dir) / STATE_NAME


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def flatten(document):
    """The flat state view `estate.state.EstateState.flatten` builds from a document.

    A section that is not a mapping is skipped rather than crashing the read: a torn or
    hand-edited document must degrade a page, not 500 it.
    """
    flat = {}
    if not isinstance(document, dict):
        return flat
    for section in _SECTIONS:
        value = document.get(section)
        if isinstance(value, dict):
            flat.update(value)
    return flat


def document(comp_dir):
    """The raw estate document, or {} when the workspace has none.

    Not for projection into an API payload: it carries the secrets section verbatim.
    """
    data = _read_json(state_path(comp_dir))
    return data if isinstance(data, dict) else {}


def read_flat(comp_dir):
    """The competition's flat state view, or None when it has no document."""
    path = state_path(comp_dir)
    if not path.exists():
        return None
    return flatten(_read_json(path))


def load_flat(comp_dir):
    """`read_flat`, with {} instead of None when the workspace has no state yet."""
    return read_flat(comp_dir) or {}


def has_state(comp_dir):
    """Whether the comp dir carries the estate document."""
    return state_path(comp_dir).exists()


def resources(comp_dir):
    """The document's recorded resources (VMs and bridges); [] when it has none.

    Rows look like `{kind, node, name, vmid, bridge, tags, at}`: `vmid` set for a VM
    (engine, team_box, golden, engine_template, jump), `bridge`/`name` for a bridge. The
    list is the estate's ownership inventory, which is what makes it the teardown path's
    authority.
    """
    entries = document(comp_dir).get("resources")
    if not isinstance(entries, list):
        return []
    return [row for row in entries if isinstance(row, dict)]


def vms(comp_dir):
    """The recorded VM resources (anything the document gave a vmid)."""
    return [row for row in resources(comp_dir) if row.get("vmid") is not None]


def kept_vms(comp_dir):
    """The recorded VMs a teardown deliberately keeps: this competition's engine template
    and goldens (`--teams-only` / `--keep-templates`).

    These are not live infrastructure (`ranges.has_vms` excludes them), but they are real VMs
    with ownership tags, and a later FULL teardown is the only path that destroys them."""
    return [row for row in vms(comp_dir) if row.get("kind") in KEPT_KINDS]


def has_inventory(comp_dir):
    """Whether the document carries a resource inventory of its own.

    An empty `resources` list is the signal: a deploy that has built nothing, or a teardown
    that has emptied the list, records no VM inventory.
    """
    return bool(resources(comp_dir))


def teardown(comp_dir):
    """The document's teardown section, {} when absent."""
    section = document(comp_dir).get("teardown")
    return section if isinstance(section, dict) else {}
