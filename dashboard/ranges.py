"""The range model: one range per run workspace, and the one retire predicate.

A range is a WORKSPACE, not a competition: two concurrent deploys of one competition are
two ranges (distinct run dirs), and grouping them by comp_id would let one teardown retire
the other. `params._destroy_comp_dir` is the workspace a verify/destroy/redeploy run acted
on; a deploy/scrim run's own workspace is `<run_dir>/comp` (see runner._range_workspace).
"""

import hashlib
from pathlib import Path

from dashboard import estate

IN_FLIGHT = ("queued", "running", "pausing", "paused")


def range_key(run):
    """The workspace path that identifies a run's range."""
    params = run.get("params") or {}
    ws = (params.get("_destroy_comp_dir") or params.get("comp_dir")
          or (Path(run["run_dir"]) / "comp"))
    return str(Path(ws).resolve())


def range_id(key):
    """A stable opaque URL identity; workspace aliases resolve to the same range."""
    return "range-" + hashlib.sha256(str(Path(key).resolve()).encode()).hexdigest()[:16]


def has_vms(key):
    """Whether the workspace still records VM infrastructure of its own.

    The range's INFRASTRUCTURE is the liveness authority, not the build run's terminal
    state: run-df29374d ended rc=1 while its VMs stayed up, and a board keyed on build
    success hid exactly the range that needed attention (2026-10-15).

    The estate document's `resources` list IS the inventory the teardown walks, and a destroy
    forgets each entry only once the VM is gone — so "still a vmid recorded" means "still a VM
    out there".

    Kept templates are not infrastructure: a teardown that keeps this competition's goldens
    and engine template leaves exactly those rows behind, and counting them made every
    `--teams-only`/`--keep-templates` range read live after a clean destroy (2026-10-15).
    """
    return any(row.get("kind") not in estate.KEPT_KINDS for row in estate.vms(Path(key)))


def retired_by(runs):
    """The newest run of these that ended rc=0 having torn the range down, or None.

    Teardown is idempotent and re-runnable, so a failed or cancelled run retires
    nothing — it is the signal that the range must stay on the board.

    A `scrim` retires too: its teardown stage runs `destroy-competition.py` itself, so a
    scrim that exited 0 and left no resource behind has retired the range. Without it a
    clean 30-minute scrim plus teardown read `failed` (2026-10-15, run-952b279e). A scrim
    that kept the range cannot be mislabelled by this: `has_vms` is evaluated first, and a
    kept range still carries its VMs, so it never reaches this predicate.
    """
    return next((r for r in runs
                 if r["action"] in ("destroy", "scrim") and r["returncode"] == 0), None)


def range_state(key, runs):
    if any(r["state"] in IN_FLIGHT for r in runs):
        return "building"
    if has_vms(key):
        return "live"
    if retired_by(runs):
        return "retired"
    if any(r["action"] == "destroy" for r in runs):
        # Every destroy ended rc!=0: an incomplete teardown may have left resources the
        # estate never owned (red01 and its LLM key), so the board must show it rather than
        # call it failed.
        return "teardown_failed"
    return "failed"


def range_view(key, runs):
    origin = next((r for r in runs if r["action"] in ("deploy", "scrim")
                   and str((Path(r["run_dir"]) / "comp").resolve()) == key), None)
    retiring = retired_by(runs)
    state = range_state(key, runs)
    return {
        "id": range_id(key),
        "key": key,
        "comp_id": runs[0]["comp_id"],
        "state": state,
        "live": state in ("building", "live", "teardown_failed"),
        "retired_by": retiring["id"] if retiring else None,
        "retired_at": retiring["ended_at"] if retiring else None,
        "deploy_run_id": origin["id"] if origin else None,
        "engine_ref": origin.get("engine_ref") if origin else None,
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


_RUN_FIELDS = ("id", "action", "state", "stage", "returncode", "created_at", "started_at",
               "ended_at")
_PARAM_FIELDS = ("teams", "team", "boxes", "mode", "comp_dir", "_destroy_comp_dir", "nodes",
                 "engine_node", "team_node", "engine_mgmt_ip", "team_identifiers",
                 "scoring_vmid", "start_after_deploy")


def run_view(run, detail=False):
    """Named public fields only: arbitrary params, driver errors and extra_args may be secret."""
    if run is None:
        return None
    view = {field: run.get(field) for field in _RUN_FIELDS}
    if detail:
        view.update({field: run.get(field) for field in ("comp_id", "engine_ref", "run_dir",
                                                        "log_path", "pid")})
        params = run.get("params") or {}
        view["params"] = {field: params[field] for field in _PARAM_FIELDS if field in params}
    return view


def requests_for_range(rng, requests):
    """Requests belong to a workspace, not every concurrent range of the competition."""
    return [req for req in requests
            if req.get("comp_id") == rng["comp_id"]
            and str(Path(req["range_key"]).resolve()) == rng["key"]]


def origin_run(rng):
    return next((run for run in rng["runs"] if run["id"] == rng["deploy_run_id"]), None)


def range_summary(rng, inventory, requests):
    """Cheap local facts only: no engine polling, credentials or full run histories."""
    from dashboard.competition import _engine_ip, credentials

    machines = inventory["machines"]
    nodes = {row.get("node") for row in estate.resources(rng["key"])}
    nodes.update(row.get("node") for row in inventory["networks"])
    nodes.update(box.get("node") for team in inventory["teams"] for box in team["boxes"])
    for machine in machines.values():
        for row in machine if isinstance(machine, list) else [machine]:
            nodes.add(row.get("node"))
    origin = origin_run(rng)
    summary = {field: rng[field] for field in ("id", "key", "comp_id", "state", "live",
                                               "deploy_run_id")}
    engine_ip = (machines.get("scoring_engine") or {}).get("mgmt_ip") or _engine_ip(rng["key"])
    state = estate.load_flat(rng["key"])
    summary.update({
        "has_recorded_vms": has_vms(rng["key"]),
        "counts": inventory["counts"],
        "nodes": sorted(node for node in nodes if node),
        "engine_ip": engine_ip,
        "deployed_at": origin.get("created_at") if origin else None,
        # Recorded workspace flags, not an engine observation: the board never polls.
        "event": {flag: bool(state.get(flag)) for flag in ("seeded", "hold_start", "ended")},
        "links": credentials(state, engine_ip)["links"],
        "degradations": inventory["deploy"]["degradations"],
        "pending_requests": sum(req["status"] == "pending" for req in requests),
        "active_runs": [run_view(run) for run in rng["runs"] if run["state"] in IN_FLIGHT],
        "latest_run": run_view(rng["runs"][0]) if rng["runs"] else None,
        "latest_destroy": run_view(next((run for run in rng["runs"]
                                          if run["action"] == "destroy"), None)),
        "engine_ref": origin.get("engine_ref") if origin else None,
    })
    try:
        for line in (Path(rng["key"]) / "Compfile").read_text().splitlines():
            key, _, value = line.strip().partition(" ")
            if key == "name" and value.strip():
                summary["name"] = value.strip()
                break
    except OSError:
        pass
    return summary


def build_range_board(store):
    # Resolve the ledger once. The inventory projector must not call build_ranges per row.
    from dashboard.inventory import build_inventory

    requests = store.list_requests()
    return [range_summary(rng, build_inventory(store, rng["runs"][0], rng=rng),
                          requests_for_range(rng, requests)) for rng in build_ranges(store)]


def range_detail(store, rng):
    from dashboard.inventory import build_inventory

    inventory = build_inventory(store, rng["runs"][0], rng=rng)
    requests = requests_for_range(rng, store.list_requests())
    return {**rng, **range_summary(rng, inventory, requests),
            "runs": [run_view(run, detail=True) for run in rng["runs"]],
            "inventory": inventory, "requests": requests}
