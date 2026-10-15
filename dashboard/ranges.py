"""The range model: one range per run workspace, and the one retire predicate.

A range is a WORKSPACE, not a competition: two concurrent deploys of one competition are
two ranges (distinct run dirs), and grouping them by comp_id would let one teardown retire
the other. `params._destroy_comp_dir` is the workspace a verify/destroy/redeploy run acted
on; a deploy/scrim run's own workspace is `<run_dir>/comp` (see runner._range_workspace).
"""

import json
from pathlib import Path

IN_FLIGHT = ("queued", "running", "pausing", "paused")


def range_key(run):
    """The workspace path that identifies a run's range."""
    ws = (run.get("params") or {}).get("_destroy_comp_dir") or (Path(run["run_dir"]) / "comp")
    return str(Path(ws).resolve())


def has_vms(key):
    """Whether the workspace's terraform state still lists a VM instance.

    The range's INFRASTRUCTURE is the liveness authority, not the build run's terminal
    state: run-df29374d ended rc=1 while its VMs stayed up, and a board keyed on build
    success hid exactly the range that needed attention (2026-10-15).
    """
    try:
        state = json.loads((Path(key) / "terraform" / "terraform.tfstate").read_text())
    except (OSError, ValueError):
        return False
    return any(
        len(resource.get("instances") or [])
        for resource in state.get("resources") or []
        if resource.get("type") == "proxmox_virtual_environment_vm"
    )


def retired_by(runs):
    """The newest destroy of these runs that ended rc=0, or None.

    Teardown is idempotent and re-runnable, so a failed or cancelled destroy retires
    nothing — it is the signal that the range must stay on the board.
    """
    return next((r for r in runs if r["action"] == "destroy" and r["returncode"] == 0), None)


def range_state(key, runs):
    if any(r["state"] in IN_FLIGHT for r in runs):
        return "building"
    if has_vms(key):
        return "live"
    if retired_by(runs):
        return "retired"
    if any(r["action"] == "destroy" for r in runs):
        # Every destroy ended rc!=0: an incomplete teardown may have left non-terraform
        # resources (red01) behind, so the board must show it rather than call it failed.
        return "teardown_failed"
    return "failed"


def range_view(key, runs):
    origin = next((r for r in runs if r["action"] in ("deploy", "scrim")), None)
    retiring = retired_by(runs)
    state = range_state(key, runs)
    return {
        "key": key,
        "comp_id": runs[0]["comp_id"],
        "state": state,
        "live": state in ("building", "live", "teardown_failed"),
        "retired_by": retiring["id"] if retiring else None,
        "retired_at": retiring["ended_at"] if retiring else None,
        "deploy_run_id": origin["id"] if origin else None,
        # The RANGE's own deploy/scrim run: a destroy's params.teams is a form default,
        # which is how a 1-team range showed "2 teams" on the board.
        "teams": (origin.get("params") or {}).get("teams") if origin else None,
        "runs": runs,
    }


def build_ranges(store):
    """Every range, newest run first."""
    grouped = {}
    for run in store.list_runs():  # newest first
        grouped.setdefault(range_key(run), []).append(run)
    return sorted((range_view(key, runs) for key, runs in grouped.items()),
                  key=lambda r: r["runs"][0]["created_at"], reverse=True)
