"""Check manager for Red Team Scoring system.

Handles IOC check execution and scheduling logic.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from fastapi_backend.core.config import config
from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import (
    BlueTeams,
    Boxes,
    CheckInstance,
    Credentials,
    DeployedIOCs,
)
from fastapi_backend.database.write_actor import write_actor
from fastapi_backend.netexec.task import BoxIOCTask, IOCTask
from fastapi_backend.utils.asyncio_tasks import fire_and_forget
from fastapi_backend.utils.nxc_routing import _get_best_cred, is_domain_cred
from iocs.base import DefaultCredCheck, SliverCheck

if TYPE_CHECKING:
    from fastapi_backend.core.competition_state import CompetitionState  # lazy-load
    from fastapi_backend.core.inventory_manager import InventoryManager  # lazy-load
    from fastapi_backend.core.ioc_definition import IOCDefinitionLoader  # lazy-load
    from fastapi_backend.core.orchestrator import CompetitionOrchestrator  # lazy-load
    from fastapi_backend.netexec.executor import NXCCheckExecutor  # lazy-load


class CheckManager:
    """Handles IOC check execution and cycle management."""

    def __init__(self, orchestrator: CompetitionOrchestrator) -> None:
        self.orchestrator = orchestrator
        self.logger = logging.getLogger(__name__)
        self._cycle_history: collections.deque[dict] = collections.deque(maxlen=10)

    @property
    def inventory_manager(self) -> InventoryManager:
        """Pass-through to the orchestrator's inventory manager."""
        return self.orchestrator.inventory_manager

    @property
    def ioc_loader(self) -> IOCDefinitionLoader:
        """Pass-through to the orchestrator's IOC definition loader."""
        return self.orchestrator.ioc_loader

    @property
    def executor(self) -> NXCCheckExecutor:
        """Pass-through to the orchestrator's NXC check executor."""
        return self.orchestrator.executor

    @property
    def state(self) -> CompetitionState:
        """Pass-through to the orchestrator's competition-state singleton."""
        return self.orchestrator.state

    async def run_checks(self) -> None:
        """Run checks for all teams and boxes."""
        if not self.state.can_run_checks():
            self.logger.warning(f"Cannot run checks in state: {self.state.status}")
            return

        self.logger.info("Starting check cycle...")
        start_time = datetime.now(UTC)
        cycle_start = time.monotonic()

        # Drain retries from previous cycle before enqueuing fresh tasks
        retried = self.executor.drain_retry_queue()
        if retried:
            self.logger.info(f"Re-queued {retried} retry tasks from previous cycle")

        try:
            # Create new check instance
            check_ids = await self._create_check_instance()

            # Snapshot cumulative IOC completions before this cycle's tasks land
            with self.executor.stats_lock:
                iocs_done_before = (
                    self.executor.stats["completed"] + self.executor.stats["failed"]
                )

            # Queue all IOC checks
            box_count, ioc_count = await self._queue_all_checks(check_ids)

            self.logger.info("Queued %d BoxIOCTasks (%d IOCs)", box_count, ioc_count)

            # Update state
            self.state.increment_checks()
            self.state.update_check_times(
                last=start_time,
                next_time=datetime.now(UTC)
                + timedelta(minutes=self.state.check_interval_minutes),
            )

            # Track cycle completion asynchronously — log duration when all IOCs drain
            if ioc_count > 0:
                fire_and_forget(
                    self._monitor_cycle_completion(
                        cycle_start, iocs_done_before, box_count, ioc_count
                    )
                )

        except Exception as e:
            self.logger.error(f"Check cycle failed: {e}")

    async def _create_check_instance(self) -> dict[int, int]:
        """Create a new check instance for all teams that have boxes in the DB.

        The whole read-modify-write (create one CheckInstance per team, then
        stamp each team's ``last_check_id``) runs as one atomic transaction on
        the single-writer actor — the two legs used to be two separate commits.

        Returns:
            Dictionary mapping team_num to check_id
        """
        is_preflight = bool(getattr(self.state, "preflight", False))

        def _write(session: Session) -> dict[int, int]:
            check_instances: dict[int, int] = {}
            team_nums = session.execute(
                select(Boxes.team_num).where(Boxes.team_num.is_not(None)).distinct()
            ).scalars().all()

            for team_num in team_nums:
                check = CheckInstance(
                    blue_team_num=team_num,
                    timestamp=datetime.now(UTC),
                    score=0,
                    preflight=is_preflight,
                )
                session.add(check)
                session.flush()  # Flush to get the check_id
                check_instances[team_num] = check.check_id

            # Update team last_check_id
            for team_num, check_id in check_instances.items():
                team = session.get(BlueTeams, team_num)
                if team:
                    team.last_check_id = check_id
                    session.add(team)

            return check_instances

        return await write_actor.submit(_write, label="create_check_instance")

    def _best_credential(self, session: Session, box_ip: str, protocol: str) -> tuple[str, str] | None:
        """Return (username, password) the IOC pipeline authenticates with.

        When RED_SCORING_ENABLED, this is the box's red_scoring infrastructure
        cred and NOTHING else (operator default creds are never used for IOC
        checks — that lets blue teams harden the default cred, which the
        default-cred scoring IOC rewards, without breaking scoring). When
        disabled, falls back to the legacy selection (valid > default > any).
        Returns None when no eligible credential exists for the box/protocol.
        """
        if config.RED_SCORING_ENABLED:
            # Route through _get_best_cred so a domain member's dual infra creds
            # (local `.\red_scoring` preferred via is_default, domain as fallback)
            # resolve by validity tier then is_default: prefer a VALID local, fall
            # back to a valid domain, and only then untested/invalid. include_invalid
            # keeps a box scoreable through a transient revalidation false-negative
            # (docs/PLAN_scoring_cred_resilience.md).
            row = _get_best_cred(
                box_ip, session, protocol=protocol,
                scoring_only=True, include_invalid=True,
            )
            return (row.username, row.password) if row else None

        creds = session.execute(
            select(Credentials).where(
                Credentials.box_ip == box_ip,
                Credentials.protocol == protocol,
                Credentials.is_infrastructure == False,  # noqa: E712
            )
        ).scalars().all()

        if not creds:
            return None

        # Prefer explicitly validated credentials, then defaults, then any
        for c in sorted(creds, key=lambda c: (c.valid is not True, c.is_default is not True)):
            return (c.username, c.password)

        return None  # unreachable, but satisfies type checker

    def _operator_default_credential(
        self, session: Session, box_ip: str, protocol: str
    ) -> tuple[str, str] | None:
        """Return (username, password) of the box's OS-default OPERATOR cred.

        This is the account the default-cred scoring IOC probes (Administrator/
        root) — NEVER the red_scoring infra cred. Prefers is_default, excludes
        infrastructure rows. Returns None when no operator cred exists.
        """
        rows = session.execute(
            select(Credentials).where(
                Credentials.box_ip == box_ip,
                Credentials.protocol == protocol,
                Credentials.is_infrastructure == False,  # noqa: E712
            )
        ).scalars().all()
        if not rows:
            return None
        for c in sorted(rows, key=lambda c: c.is_default is not True):
            return (c.username, c.password)
        return None

    async def _monitor_cycle_completion(
        self,
        cycle_start: float,
        iocs_done_before: int,
        box_count: int,
        ioc_count: int,
    ) -> None:
        """Poll executor stats until all IOCs from this cycle drain, then log duration."""
        timeout_s = self.state.check_interval_minutes * 60 * 2
        deadline = time.monotonic() + timeout_s
        timed_out = True
        while time.monotonic() < deadline:
            await asyncio.sleep(2)
            with self.executor.stats_lock:
                done = self.executor.stats["completed"] + self.executor.stats["failed"]
            if done >= iocs_done_before + ioc_count:
                timed_out = False
                break

        elapsed = time.monotonic() - cycle_start
        completed_at_iso = datetime.now(UTC).isoformat() + "Z"
        entry: dict = {
            "completed_at": completed_at_iso,
            "duration_s": round(elapsed, 1),
            "boxes": box_count,
            "iocs": ioc_count,
            "timed_out": timed_out,
        }
        self._cycle_history.append(entry)
        if timed_out:
            self.logger.warning(
                "IOC cycle monitor timed out after %.0fs (boxes=%d, iocs=%d)",
                elapsed, box_count, ioc_count,
            )
        else:
            self.logger.info(
                "IOC check cycle completed in %.1fs (boxes=%d, iocs=%d)",
                elapsed, box_count, ioc_count,
            )

        # Emit `check-cycle-complete` SSE. Carry full preflight_results payload
        # during preflight states so Operations page panel can update without
        # extra round-trip.
        try:
            from fastapi_backend.c2.sliver_manager import (  # lazy-load
                sliver_manager,
            )

            payload: dict = {"type": "check-cycle-complete", "completed_at": completed_at_iso}
            if getattr(self.state, "preflight", False):
                from fastapi_backend.core.preflight import (  # lazy-load
                    build_preflight_results,
                )
                payload["preflight_results"] = await asyncio.to_thread(build_preflight_results)
            await sliver_manager.publish(payload)
        except Exception as exc:
            self.logger.debug("check-cycle-complete SSE publish failed: %s", exc)

    def get_cycle_stats(self) -> dict:
        """Return last and recent IOC check cycle timing for /admin/status."""
        history = list(self._cycle_history)
        return {
            "last": history[-1] if history else None,
            "history": history,
        }

    async def _queue_all_checks(self, check_ids: dict[int, int]) -> tuple[int, int]:
        """Queue one BoxIOCTask per box based on deployed IOCs.

        Only checks IOCs that are registered in DeployedIOCs (status='active').
        Reads credentials from the DB so that credential updates are reflected.

        The whole DB-scan + per-row credential resolution runs in a worker
        thread via ``asyncio.to_thread`` — the enumeration touches ~910 rows
        under full load with per-row SELECTs, and doing that on the loop
        thread pinned uvicorn's accept loop for 5-30s per cycle (soak N
        finding 2026-08-26).

        Args:
            check_ids: Dictionary mapping team_num to check_id

        Returns:
            Tuple of (box_count, ioc_count) — boxes queued and total IOC checks.
        """
        if not self.executor:
            raise RuntimeError("Executor not initialized")

        box_tasks = await asyncio.to_thread(self._build_box_tasks_sync, check_ids)

        # docs/PLAN_soak_backend_perf.md §3.6: split each box's IOC list into
        # fixed-size chunks so one slow IOC can't exhaust a whole batch's
        # timeout budget. Each chunk shares the same box credential + check_id;
        # only the ioc_tasks slice differs. See config.CHECK_MAX_IOCS_PER_TASK.
        chunked_tasks: list[BoxIOCTask] = []
        chunk_size = max(0, config.CHECK_MAX_IOCS_PER_TASK)
        for bt in box_tasks.values():
            if chunk_size <= 0 or len(bt.ioc_tasks) <= chunk_size:
                chunked_tasks.append(bt)
                continue
            ioc_list = bt.ioc_tasks
            for i in range(0, len(ioc_list), chunk_size):
                slice_iocs = ioc_list[i:i + chunk_size]
                chunk = BoxIOCTask(
                    team_num=bt.team_num,
                    box_ip=bt.box_ip,
                    box_os=bt.box_os,
                    check_id=bt.check_id,
                    username=bt.username,
                    password=bt.password,
                    fallback_username=bt.fallback_username,
                    fallback_password=bt.fallback_password,
                    preflight=bt.preflight,
                )
                chunk.ioc_tasks.extend(slice_iocs)
                chunked_tasks.append(chunk)

        ioc_count = sum(len(bt.ioc_tasks) for bt in chunked_tasks)
        for bt in chunked_tasks:
            self.executor.queue_task(bt)
        box_count = len(box_tasks)
        self.logger.info(
            "Queued %d BoxIOCTasks (%d chunks after split of %d IOCs; queue depth: %d)",
            box_count, len(chunked_tasks), ioc_count, self.executor.task_queue.qsize(),
        )
        return box_count, ioc_count

    def _build_box_tasks_sync(self, check_ids: dict[int, int]) -> dict[str, BoxIOCTask]:
        """Sync helper: build the BoxIOCTask map with per-row credential SELECTs.

        Runs entirely off the event loop (called via asyncio.to_thread from
        _queue_all_checks) — no awaits, only blocking SQL + dict work.
        """
        project_root = Path(__file__).parent.parent.parent
        iocs_dir = project_root / "iocs"

        box_tasks: dict[str, BoxIOCTask] = {}

        with Session(engine) as session:
            deployed = session.execute(
                select(DeployedIOCs).where(DeployedIOCs.status == "active")
            ).scalars().all()

            if not deployed:
                self.logger.info("No active DeployedIOCs — skipping check cycle")
                return {}

            # Load box metadata once
            boxes_by_ip: dict[str, Boxes] = {
                b.box_ip: b
                for b in session.execute(
                    select(Boxes).where(Boxes.team_num.is_not(None))
                ).scalars().all()
            }

            for d in deployed:
                ioc = self.ioc_loader.ioc_definitions.get(d.ioc_name)
                if not ioc:
                    self.logger.warning("DeployedIOC references unknown IOC %r — skipping", d.ioc_name)
                    continue

                box = boxes_by_ip.get(d.box_ip)
                if not box or box.team_num is None:
                    self.logger.warning("DeployedIOC box %r not in inventory — skipping", d.box_ip)
                    continue

                team_num = box.team_num
                team_check_id = check_ids.get(team_num)
                if not team_check_id:
                    self.logger.error("No check_id for team %d (box %s)", team_num, d.box_ip)
                    continue

                box_os = box.os or ioc.os
                protocol = "smb" if box_os == "windows" else "ssh"
                is_preflight = bool(getattr(self.state, "preflight", False))
                is_default_cred = isinstance(ioc.check, DefaultCredCheck)

                # Default-cred IOCs are excluded from the preflight grid.
                if is_default_cred and is_preflight:
                    continue

                # Resolve the credential THIS IOCTask authenticates with.
                if is_default_cred:
                    # Probe the box's OPERATOR default account (Administrator/
                    # root) — never the red_scoring infra cred. This is the
                    # account default-cred scoring grades blue teams on.
                    task_cred = self._operator_default_credential(session, d.box_ip, protocol)
                    if task_cred is None:
                        self.logger.warning(
                            "No operator %s credential for %s — skipping default-cred check",
                            protocol, d.box_ip,
                        )
                        continue
                else:
                    # All other IOC checks authenticate with the box's scoring
                    # cred (red_scoring when RED_SCORING_ENABLED).
                    task_cred = self._best_credential(session, d.box_ip, protocol)
                    if task_cred is None:
                        self.logger.warning("No %s credential for %s — skipping", protocol, d.box_ip)
                        continue

                username, password = task_cred

                is_sliver = isinstance(ioc.check, SliverCheck)
                if is_sliver or is_default_cred:
                    ioc_script = ""
                    check_script_path = ""
                else:
                    ioc_script = ioc.check.script
                    check_script_path = str((iocs_dir / ioc_script).absolute())

                # Persistence script resolution priority:
                #   1. d.persistence_mechanism string → look up via
                #      iocs.persistence.get_by_name (composed deploys from
                #      Generation page record the mechanism's module-attr name
                #      in this column).
                #   2. ioc.persistence on the IOC definition (legacy bundled
                #      IOCs that hardcode their persistence).
                # Falling back to the IOC's bundled persistence keeps the
                # legacy path intact for IOCs that haven't been re-deployed
                # via the composable Generation flow yet.
                persistence_script = None
                persistence_script_path = None
                pm = None
                if d.persistence_mechanism:
                    from iocs.persistence import get_by_name as _pm_lookup  # lazy-load
                    pm = _pm_lookup(d.persistence_mechanism)
                if pm is None and ioc.persistence and d.persistence_mechanism is not None:
                    pm = ioc.persistence
                if pm and pm.check_script:
                    persistence_script = pm.check_script
                    persistence_script_path = str((iocs_dir / persistence_script).absolute())

                ioc_task = IOCTask(
                    team_num=team_num,
                    box_ip=d.box_ip,
                    box_os=box_os,
                    ioc_name=ioc.name,
                    ioc_script=ioc_script,
                    check_id=team_check_id,
                    check_script_path=check_script_path,
                    username=username,
                    password=password,
                    difficulty=ioc.difficulty,
                    is_sliver_check=is_sliver,
                    is_default_cred_check=is_default_cred,
                    persistence_script=persistence_script,
                    persistence_script_path=persistence_script_path,
                    deployed_ioc_id=d.id,
                    implant_identifier=d.implant_identifier,
                    preflight=is_preflight,
                    deploy_params=d.params_dict,
                )

                key = f"{team_num}:{d.box_ip}"
                if key not in box_tasks:
                    # The BoxIOCTask combined-script call authenticates with the
                    # box's scoring cred (red_scoring). Default-cred tasks bring
                    # their own operator cred and don't use this; an empty value
                    # is fine when the box has only default-cred checks.
                    box_cred = self._best_credential(session, d.box_ip, protocol)
                    box_user, box_pass = box_cred if box_cred else ("", "")
                    # Domain-auth fallback (PLAN_domain_auth_fallback.md): when
                    # the resolved cred is domain-qualified, also resolve the
                    # box's local admin cred from inventory so check_runner can
                    # retry with it if the domain attempt produces no output.
                    fallback_user: str | None = None
                    fallback_pass: str | None = None
                    if box_user and is_domain_cred(box_user):
                        local_box = self.inventory_manager.get_box_by_ip(d.box_ip)
                        if local_box:
                            fallback_user, fallback_pass = local_box.username, local_box.password
                    box_tasks[key] = BoxIOCTask(
                        team_num=team_num,
                        box_ip=d.box_ip,
                        box_os=box_os,
                        check_id=team_check_id,
                        username=box_user,
                        password=box_pass,
                        fallback_username=fallback_user,
                        fallback_password=fallback_pass,
                        preflight=is_preflight,
                    )
                box_tasks[key].ioc_tasks.append(ioc_task)

        return box_tasks
