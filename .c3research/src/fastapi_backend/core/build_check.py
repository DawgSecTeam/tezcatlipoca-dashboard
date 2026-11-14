"""CHECK step — verify every required artifact is built and current.

docs/PLAN_build_check_deploy_name_pools.md §5A.4. Second of the three operator
steps (BUILD → CHECK → DEPLOY). Mirrors ``core/scoring_preflight.run_scoring_preflight``
but keyed by ``ioc_name``: per-IOC ``{reason}`` on ``progress_json`` + a summary on
``metadata_json``. A completed CHECK with failures makes the Operations "Deploy
All IOCs" button warn-and-confirm (§5A.6).

Per-IOC reasons:
  - ``present``        — the required binary exists on disk and is current
  - ``missing``        — needs a BUILD (no binary at all)
  - ``stale``          — a compiled shim whose source drifted OR whose baked
                         defines ≠ the referenced artifact's recorded value
                         (must recompile before deploy)
  - ``not-applicable`` — script / technique-fixed / inject: nothing to pre-build

``passed`` = present + not-applicable. Implant filename is NOT name-matched
(SliverCheck detects the session, not the name).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fastapi_backend.c2 import build_manifest, shim_builder
from fastapi_backend.core.build_all import (
    _find_reusable_build,
    _resolve_coupled_defines,
    spec_wants_payload,
)
from fastapi_backend.core.deploy_all import _get_ioc_loader, _load_inventory
from fastapi_backend.core.task_tracker import (
    start_task,
    update_box_progress,
    update_task_metadata,
)
from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import OperatorTask
from iocs.base import DefaultCredCheck, SliverCheck

logger = logging.getLogger(__name__)

REASON_PRESENT = "present"
REASON_MISSING = "missing"
REASON_STALE = "stale"
REASON_NA = "not-applicable"


def _classify(ioc: Any, boxes_by_os: dict[str, list[str]]) -> str:
    # Implant IOC → a build must exist on disk (name not scored → no name-match).
    if isinstance(ioc.check, SliverCheck) and not getattr(ioc, "sliver_inject", False):
        if ioc.implant_spec is None or not boxes_by_os.get(ioc.os):
            return REASON_NA
        # Pivot-set members are NOT built here — BUILD-ALL skips them with the
        # same reason ("pivot set — built at deploy", core/build_all.py), because
        # the set compiles as a coordinated 3-implant group on the DEPLOY path:
        # the members are built against the gateway's tcppivot listener, which
        # does not exist until the gateway is deployed and calls back.
        #
        # Without this, CHECK demanded a standalone build that BUILD deliberately
        # never creates, so every pivot IOC reported "missing" permanently and a
        # clean rebuild could never verify. Observed after a full Build All on
        # 2026-08-08: firewall_implant_devd_kqueue and linux_implant_udev both
        # 'failed/missing' while windows_implant_inject (sliver_inject, so it took
        # a different branch) reported not-applicable — two members of ONE set
        # classified differently, which is what made the inconsistency visible.
        if getattr(ioc.implant_spec, "pivot_set_name", None):
            return REASON_NA
        return REASON_PRESENT if _find_reusable_build(ioc) is not None else REASON_MISSING

    # Coupled shim → recompiled-cache (if pooled) or committed payload (if not).
    if shim_builder.is_recompilable(ioc.name):
        defines = _resolve_coupled_defines(ioc)
        wants_payload = spec_wants_payload(ioc.name)
        if defines:
            # A payload-baking shim is only 'present' if a payload was actually
            # baked — a manifest entry with no SHELLCODE_B64 digest means BUILD
            # never produced one (§7.4).
            if wants_payload and "SHELLCODE_B64" not in defines:
                return REASON_MISSING
            art = shim_builder.expected_artifact(ioc.name, defines)
            if art is None or not Path(art).exists():
                return REASON_MISSING
            return REASON_PRESENT if build_manifest.defines_match(art, defines) else REASON_STALE
        if wants_payload:
            # No manifest entry at all → nothing baked. The committed template is
            # compiled with an EMPTY payload, so treating its mere existence as
            # 'present' is exactly what let empty droppers ship silently (§7.4).
            return REASON_MISSING
        committed = shim_builder.committed_payload(ioc.name)
        if committed is None:
            return REASON_NA
        return REASON_PRESENT if committed.exists() else REASON_MISSING

    return REASON_NA


def has_running_build_check() -> bool:
    """True if a build_check task is already pending/running."""
    with Session(engine) as db:
        row = db.execute(
            select(OperatorTask.id).where(
                OperatorTask.task_type == "build_check",
                OperatorTask.status.in_(["pending", "running"]),
            ).limit(1)
        ).first()
    return row is not None


async def run_build_check(task_id: int, *, team_nums: list[int] | None = None) -> None:
    """Classify every in-scope IOC's build state; write per-IOC reason + summary."""
    start_task(task_id)

    loader = _get_ioc_loader()
    iocs_list = [
        ioc for ioc in loader.ioc_definitions.values()
        if not isinstance(ioc.check, DefaultCredCheck)
    ]
    boxes_by_os = _load_inventory(team_nums)

    by_reason: dict[str, int] = {}
    for ioc in iocs_list:
        try:
            reason = _classify(ioc, boxes_by_os)
        except Exception:
            logger.exception("build_check: IOC %s raised", ioc.name)
            reason = REASON_MISSING
        by_reason[reason] = by_reason.get(reason, 0) + 1
        status = "done" if reason in (REASON_PRESENT, REASON_NA) else "failed"
        update_box_progress(task_id, ioc.name, status,
                            details=json.dumps({"reason": reason}))

    total = len(iocs_list)
    passed = by_reason.get(REASON_PRESENT, 0) + by_reason.get(REASON_NA, 0)
    failed = total - passed
    update_task_metadata(task_id, {"total": total, "passed": passed,
                                   "failed": failed, "by_reason": by_reason})
    from fastapi_backend.core.task_tracker import finish_task  # cycle-break
    finish_task(task_id, "failed" if failed else "done")
    logger.info(
        "build_check complete (task=%s): passed=%d failed=%d of %d — %s",
        task_id, passed, failed, total, by_reason,
    )
