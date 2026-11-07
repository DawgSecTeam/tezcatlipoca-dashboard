"""The range model: one range per run workspace, and the one retire predicate.

A range is a WORKSPACE, not a competition: two concurrent deploys of one competition are
two ranges (distinct run dirs), and grouping them by comp_id would let one teardown retire
the other. `params._destroy_comp_dir` is the workspace a verify/destroy/redeploy run acted
on; a deploy/scrim run's own workspace is `<run_dir>/comp` (see runner._range_workspace).
"""

import hashlib
import re
from pathlib import Path

from dashboard import comps_api, estate

IN_FLIGHT = ("queued", "running", "pausing", "paused")

# `phase 4/8 golden_set`-style stamps: the runner's stage string for a deploy run. The
# estate document records the same fact as `last_phase`; whichever is further wins.
_PHASE_IN_STAGE = re.compile(r"phase\s+(\d+)\s*/\s*\d+", re.IGNORECASE)

# The actions that cannot build infrastructure. `plan` renders a definition and `verify` inspects
# an existing range; a workspace whose ONLY runs are these never held a range, so it must not get a
# board card that later reports `failed` and sends the operator hunting VMs that never existed
# (2026-11-06 audit). Everything else — deploy, scrim, destroy, lifecycle — is a range.
NON_PROVISIONING_ACTIONS = ("plan", "verify")


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


def reclaimable_templates(key, state, kept=None):
    """Whether a FULL teardown may still act on a retired range's kept templates.

    `--teams-only` retires a range but keeps this competition's engine template and goldens
    recorded for its next run. A full destroy is the only path that can destroy them, so the
    resolver must let it reach exactly such a workspace — while it has no live VMs left. Every
    other action, and a retired range with an empty inventory, still refuses.
    """
    kept = estate.kept_vms(Path(key)) if kept is None else kept
    return state == "retired" and bool(kept) and not has_vms(key)


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


def range_state(key, runs, recorded=None, retired=False, origin_failed=False):
    if any(r["state"] in IN_FLIGHT for r in runs):
        return "building"
    if recorded is None:
        recorded = has_vms(key)
    if recorded:
        # A deploy that FAILED and left its engine VM + bridge behind is not `live`: those
        # VMs exist BECAUSE the build died, and calling the range live hid the one action
        # that clears them (2026-11-06: run-90f48b3a failed at phase 3, its range reported
        # live, and every later deploy refused at preflight on vmid 1420 / bridge vmbr121).
        # Report `failed` with the leftover inventory still attached; a range that really
        # completed (the estate document says seeded or last_phase 8) keeps `live`.
        return "live" if not origin_failed else "failed"
    if retired:
        return "retired"
    if any(r["action"] == "destroy" for r in runs):
        # Every destroy ended rc!=0: an incomplete teardown may have left resources the
        # estate never owned (red01 and its LLM key), so the board must show it rather than
        # call it failed.
        return "teardown_failed"
    return "failed"


def _phase_reached(origin, key, flat=None):
    """The furthest deploy phase this range's OWN records show (1..8), or None.

    Two local sources agree on one number: the origin run's stage stamp (`phase 5/8
    firewall_bootstrap`) and the estate document's `last_phase`. Verify and Reset act on
    boxes that phase 4 onward creates — a deploy that died at phase 2 has nothing to
    verify, and offering the action produced a run of meaningless check failures
    (2026-11-06 audit). None means neither record says: an old workspace or a range with
    no deploy run, where the caller must fall back rather than pretend.
    """
    phases = []
    match = _PHASE_IN_STAGE.search((origin or {}).get("stage") or "")
    if match:
        phases.append(int(match.group(1)))
    last = (flat if flat is not None else estate.load_flat(key)).get("last_phase")
    if isinstance(last, (int, float)) and not isinstance(last, bool):
        phases.append(int(last))
    return max(phases) if phases else None


def range_view(key, runs, reclaim=None):
    origin = next((r for r in runs if r["action"] in ("deploy", "scrim")
                   and str((Path(r["run_dir"]) / "comp").resolve()) == key), None)
    retiring = retired_by(runs)
    # The range's own records of how far its deploy got, read once: `last_phase` feeds
    # deploy_phase below, `seeded` says the build effectively finished even if the run row
    # came back non-zero (an operator completed it by hand, a late phase-only crash).
    flat = estate.load_flat(key)
    # A failed deploy only poisons a range when it never went on to complete: the estate
    # document is the completion authority, not the exit code alone.
    completed = bool(flat.get("seeded")) or (
        isinstance(flat.get("last_phase"), (int, float))
        and not isinstance(flat.get("last_phase"), bool) and flat["last_phase"] >= 8)
    origin_failed = bool(origin and origin.get("state") in ("failed", "cancelled")
                         and not completed)
    # A range reclaimed by `destroy-competition.py --orphan-comp/--orphan-run` is retired in
    # the store: its workspace document still lists the VMs a teardown never got to forget, so
    # the recorded inventory must not win (the board kept a phantom `live` for crimson-relay
    # range-b289904cf2d5ccdf while a Proxmox sweep found zero tagged VMs). A failed deploy, in
    # contrast, still OWNS its leftovers: destroy must be able to reach that workspace.
    live_vms = has_vms(key)
    recorded = live_vms and reclaim is None
    state = range_state(key, runs, recorded, retired=bool(retiring or reclaim),
                        origin_failed=origin_failed)
    kept = estate.kept_vms(Path(key))
    return {
        "id": range_id(key),
        "key": key,
        "comp_id": runs[0]["comp_id"],
        "state": state,
        # `live` means "this range still has a claim on the estate": a failed deploy holding
        # leftover VMs stays on the board (its leftovers collide with every later deploy),
        # while a failed range that owns nothing drops off.
        "live": state in ("building", "live", "teardown_failed") or (state == "failed" and recorded),
        # A deploy still owns this range: its engine may answer /api/engine but the range is
        # not ready to drive. The board, the range page and the run page all gate their
        # competition controls on this one recorded fact instead of guessing from `building`
        # (which also covers verify/redeploy/delete_team, none of which means "not ready").
        "deploying": any(run.get("action") == "deploy" and run.get("state") in IN_FLIGHT
                         for run in runs),
        # Its mirror: a destroy removes the VMs every other range action needs, so the
        # surfaces disable them from this fact instead of re-deriving it from run lists.
        "tearing_down": any(run.get("action") == "destroy" and run.get("state") in IN_FLIGHT
                            for run in runs),
        # Any operation other than a scrim means the range is moving: the engine may answer,
        # but the workspace is mid-write. One fact for the bar, the headers, Teams, the bell
        # and the competition panel to gate on. A scrim is excluded because it deploys and
        # runs the competition itself — its range is exactly where scoring controls belong.
        "settled": not any(run.get("state") in IN_FLIGHT and run.get("action") != "scrim"
                           for run in runs),
        "deploy_state": origin.get("state") if origin else None,
        "deploy_failed": bool(origin and origin.get("state") in ("failed", "cancelled")),
        # How far the deploy actually got (see _phase_reached): the page gates Verify/Reset
        # on this so a deploy that died before the boxes existed cannot offer actions that
        # only fail against them.
        "deploy_phase": _phase_reached(origin, key, flat=flat),
        # The recorded infrastructure is what the range's engine address and its recovery
        # actions hang on, so every view of a range carries it.
        "has_recorded_vms": recorded,
        # `--teams-only` leaves these behind ON PURPOSE; they are not infrastructure, but a
        # full teardown is still allowed to reclaim them (reclaimable_templates below).
        "kept_templates": [{"kind": row.get("kind"), "name": row.get("name"),
                            "node": row.get("node"), "vmid": row.get("vmid")}
                           for row in kept],
        "reclaimable_templates": reclaimable_templates(key, state, kept=kept),
        # A non-retired range whose workspace vanished from disk. Reported, never inferred
        # into a different state: the board must show the loss instead of dropping the range.
        "workspace_missing": state != "retired" and not Path(key).is_dir(),
        "retired_by": retiring["id"] if retiring else (reclaim or {}).get("by"),
        "retired_at": (retiring["ended_at"] if retiring else (reclaim or {}).get("created_ts")),
        "deploy_run_id": origin["id"] if origin else None,
        "engine_ref": origin.get("engine_ref") if origin else None,
        # The commit the deploy actually cut, not the ref it asked for: a stale fetch used to
        # execute an older revision while the record said `main` (known-issues: provenance).
        "engine_head": origin.get("engine_head") if origin else None,
        # The RANGE's own deploy/scrim run: a destroy's params.teams is a form default,
        # which is how a 1-team range showed "2 teams" on the board.
        "teams": (origin.get("params") or {}).get("teams") if origin else None,
        "runs": runs,
    }


def build_ranges(store):
    """Every range, newest run first.

    A range is a workspace that a provisioning run built in. `plan` and `verify` runs get their
    own per-run workspace but never create infrastructure, so they are not ranges: one dry run
    used to leave a permanent card behind that later reported `failed` and told the operator to go
    hunt Proxmox for VMs that never existed (2026-11-06 audit). A workspace that DOES carry a
    deploy/scrim keeps every run against it, plans included.
    """
    reclaimed = store.list_range_retires()
    grouped = {}
    for run in store.list_runs():  # newest first
        grouped.setdefault(range_key(run), []).append(run)
    out = []
    for key, runs in grouped.items():
        # A workspace whose only runs are inspection actions (plan/verify) never held
        # infrastructure. Anything else — a deploy, a destroy, a lifecycle action, an in-flight
        # run — is a range, including a failed one the board must show.
        inspection_only = all(r["action"] in NON_PROVISIONING_ACTIONS for r in runs)
        if inspection_only and not any(r["state"] in IN_FLIGHT for r in runs):
            continue
        out.append(range_view(key, runs, reclaimed.get(key)))
    return sorted(out, key=lambda r: r["runs"][0]["created_at"], reverse=True)


_RUN_FIELDS = ("id", "action", "state", "stage", "returncode", "created_at", "started_at",
               "ended_at", "engine_head")
_PARAM_FIELDS = ("teams", "team", "boxes", "mode", "comp_dir", "_destroy_comp_dir", "nodes",
                 "engine_node", "team_node", "engine_mgmt_ip", "team_identifiers",
                 "scoring_vmid", "start_after_deploy", "auto_reclaim")


def run_view(run, detail=False):
    """Named public fields only: arbitrary params, driver errors and extra_args may be secret.

    The raw `error` is deliberately withheld: it can quote a command line or a URL carrying a
    token. The run page's `/api/runs/{id}` is the authenticated detail surface that shows it."""
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
                                               "deploying", "tearing_down", "settled",
                                               "deploy_state", "deploy_failed",
                                               "deploy_phase",
                                               "deploy_run_id", "has_recorded_vms",
                                               "kept_templates", "reclaimable_templates",
                                               "workspace_missing", "teams")}
    engine_ip = (machines.get("scoring_engine") or {}).get("mgmt_ip") or _engine_ip(rng["key"])
    state = estate.load_flat(rng["key"])
    summary.update({
        "counts": inventory["counts"],
        "nodes": sorted(node for node in nodes if node),
        "engine_ip": engine_ip,
        "deployed_at": origin.get("created_at") if origin else None,
        # Recorded workspace flags, not an engine observation: scoring state reaches
        # the board only through scoring_cache's cached background poll.
        "event": {flag: bool(state.get(flag)) for flag in ("seeded", "hold_start", "ended")},
        # An address recorded in a workspace whose VMs are gone is not a reachable engine:
        # publishing it as a link is how a torn-down range kept offering a scoreboard.
        "links": credentials(state, engine_ip if rng["has_recorded_vms"] else None)["links"],
        # A range outlives its competition's definition: the UI must not link to a page
        # that only 404s.
        "comp_defined": (comps_api.COMPS / rng["comp_id"]).is_dir(),
        "degradations": inventory["deploy"]["degradations"],
        "pending_requests": sum(req["status"] == "pending" for req in requests),
        "active_runs": [run_view(run) for run in rng["runs"] if run["state"] in IN_FLIGHT],
        "latest_run": run_view(rng["runs"][0]) if rng["runs"] else None,
        "latest_destroy": run_view(next((run for run in rng["runs"]
                                          if run["action"] == "destroy"), None)),
        "engine_ref": origin.get("engine_ref") if origin else None,
        "engine_head": origin.get("engine_head") if origin else None,
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
    from dashboard import scoring_cache

    requests = store.list_requests()
    ranges = build_ranges(store)
    live_keys = set()
    board = []
    for rng in ranges:
        summary = range_summary(rng, build_inventory(store, rng["runs"][0], rng=rng),
                                requests_for_range(rng, requests))
        # Live scoring is a cached background observation (scoring_cache): the board
        # request itself stays local and instant, and a range without recorded VMs
        # has no engine to ask, so it carries None rather than "unreachable".
        summary["scoring"] = scoring_cache.snapshot(rng) if rng["live"] else None
        if rng["live"]:
            live_keys.add(rng["key"])
        board.append(summary)
    scoring_cache.prune(live_keys)
    return board


def range_detail(store, rng):
    from dashboard.inventory import build_inventory

    inventory = build_inventory(store, rng["runs"][0], rng=rng)
    requests = requests_for_range(rng, store.list_requests())
    return {**rng, **range_summary(rng, inventory, requests),
            "runs": [run_view(run, detail=True) for run in rng["runs"]],
            "inventory": inventory, "requests": requests}
