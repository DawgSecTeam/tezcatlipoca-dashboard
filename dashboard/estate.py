"""The competition's estate state document, read from a run workspace.

The v0.3.0 engine dropped terraform AND `.deploy_state.json`: a range's state is now the
monolithic `estate-state.json` (identity / secrets / roster / templates / run / resources /
teardown). This module is the service-side mirror of the engine's `estate.state` — the
dashboard reads workspaces directly and never imports a per-run engine worktree, so it
cannot call `estate.state.load_flat` itself; it must agree with it instead:

  * `load_flat` / `read_flat` flatten the document's sections in `estate.state`'s own order
    (`identity`, `secrets`, `roster`, `templates`, `run`) so every flat key the dashboard
    already knew (`last_phase`, `run_id`, `seeded`, `engine_mgmt_ip`, `teams`, …) keeps
    resolving;
  * a workspace with no document still reads its legacy `.deploy_state.json` — read-only,
    so displaying a pre-v0.3 range never migrates it as a side effect (`read_flat` returns
    None when the comp dir carries no state at all, `load_flat` returns `{}`);
  * `document()` / `resources()` expose the parts the flat view cannot carry: the VM and
    bridge inventory (`resources`) and the teardown section (`teardown`).

Only the fields a projection needs are read by callers; `document()` exists for the two
parts the flat view cannot carry (the resource list and the teardown section) and must
never be handed to a response, because the document holds every generated password.
"""

import json
from pathlib import Path

STATE_NAME = "estate-state.json"
LEGACY_NAME = ".deploy_state.json"

# estate/state.py's _SECTIONS: the sections flatten() folds into the flat view, in order.
_SECTIONS = ("identity", "secrets", "roster", "templates", "run")

# Kinds a teardown deliberately leaves behind: `--teams-only` / `--keep-templates` keep this
# competition's golden + engine templates for its next run, so a range whose only remaining
# rows are these is torn down, not live.
KEPT_KINDS = ("engine_template", "golden")


def state_path(comp_dir):
    """Path of the competition's estate document."""
    return Path(comp_dir) / STATE_NAME


def legacy_path(comp_dir):
    """Path of the pre-v0.3 deploy state file."""
    return Path(comp_dir) / LEGACY_NAME


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
    """The raw estate document, or {} for a workspace that predates it.

    Not for projection into an API payload: it carries the secrets section verbatim.
    """
    data = _read_json(state_path(comp_dir))
    return data if isinstance(data, dict) else {}


def read_flat(comp_dir):
    """The competition's flat state view, or None when it has no state at all.

    A range deployed before the estate document fell back to its legacy
    `.deploy_state.json` — read-only, exactly like `estate.state.read_flat`. The same fallback
    covers a document that exists WITHOUT an identity: a folded record (the teardown journal,
    a template hash) can be written before the deploy records its state, and letting that
    partial document win would lose the run id every destroy path requires.
    """
    comp_dir = Path(comp_dir)
    legacy_file = legacy_path(comp_dir)
    if state_path(comp_dir).exists():
        flat = flatten(_read_json(state_path(comp_dir)))
        if flat.get("run_id") or not legacy_file.exists():
            return flat
        # The document's own keys win; the legacy file only supplies what a partial document
        # has not recorded yet (its identity, above all).
        legacy = _read_json(legacy_file)
        if not isinstance(legacy, dict):
            return flat
        legacy.update(flat)
        return legacy
    if legacy_file.exists():
        legacy = _read_json(legacy_file)
        return legacy if isinstance(legacy, dict) else None
    return None


def load_flat(comp_dir):
    """`read_flat`, with {} instead of None when the workspace has no state yet."""
    return read_flat(comp_dir) or {}


def has_state(comp_dir):
    """Whether the comp dir carries a v0.3.0 document or a legacy deploy state."""
    comp_dir = Path(comp_dir)
    return state_path(comp_dir).exists() or legacy_path(comp_dir).exists()


def resources(comp_dir):
    """The document's recorded resources (VMs and bridges); [] for a legacy workspace.

    Rows look like `{kind, node, name, vmid, bridge, tags, at}`: `vmid` set for a VM
    (engine, team_box, golden, engine_template, jump), `bridge`/`name` for a bridge. The
    list is the estate's ownership inventory, which is what makes it the teardown path's
    authority — and the dashboard's replacement for terraform's `resources` array.
    """
    entries = document(comp_dir).get("resources")
    if not isinstance(entries, list):
        return []
    return [row for row in entries if isinstance(row, dict)]


def vms(comp_dir):
    """The recorded VM resources (anything the document gave a vmid)."""
    return [row for row in resources(comp_dir) if row.get("vmid") is not None]


def has_inventory(comp_dir):
    """Whether the document carries a resource inventory of its own.

    `estate.state.EstateState.migrate` folds a pre-v0.3 range's identity, secrets and roster
    into a document WITHOUT building its `resources` list, so a workspace can have both a
    document and a terraform state, with the state still the only VM inventory it has. Every
    reader that needs that inventory must fall back in exactly that case; an empty
    `resources` list is the signal. (A v0.3.0 range whose teardown emptied the list has no
    terraform state at all, so it still reads as gone.)
    """
    return bool(resources(comp_dir))


def teardown(comp_dir):
    """The document's teardown section, {} when absent."""
    section = document(comp_dir).get("teardown")
    return section if isinstance(section, dict) else {}
