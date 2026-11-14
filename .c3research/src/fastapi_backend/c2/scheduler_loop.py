"""Background loop that fires due ScheduledTask rows every 30 seconds.

Each due task is dispatched as an asyncio.Task so tasks run concurrently.
Repeating tasks (repeat_secs is not None) get their next_run_at set after firing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import shlex
from types import SimpleNamespace

from fastapi_backend.core.config import config
from fastapi_backend.utils.asyncio_tasks import fire_and_forget

_log = logging.getLogger(__name__)

# Bound concurrent scheduled-task fan-out: Phase 3 populates dozens of tasks
# that can become due together on the same 30s tick; unbounded fire_and_forget
# stacked sync-Session opens on the loop thread and starved the accept loop
# (soak N 2026-08-26). None = create lazily inside a running loop so import
# time doesn't need one.
_dispatch_sem: asyncio.Semaphore | None = None


def _get_dispatch_sem() -> asyncio.Semaphore:
    global _dispatch_sem
    if _dispatch_sem is None:
        _dispatch_sem = asyncio.Semaphore(config.SCHEDULER_MAX_CONCURRENT_TASKS)
    return _dispatch_sem


async def scheduler_loop() -> None:
    """Background loop that fires due ScheduledTasks and CompetitionEvents."""
    while True:
        await asyncio.sleep(30)
        try:
            await _poll_once()
        except Exception as exc:
            _log.warning("scheduler_loop error: %s", exc)


def _poll_due_sync() -> list[int]:
    from sqlalchemy import select  # cycle-break
    from sqlalchemy.orm import Session  # cycle-break

    from fastapi_backend.database.db_writer import engine  # cycle-break
    from fastapi_backend.database.models import ScheduledTask  # cycle-break

    now = dt.datetime.now(dt.UTC).isoformat()
    with Session(engine) as db:
        due = db.execute(
            select(ScheduledTask.id).where(
                ScheduledTask.status == "pending",
                ScheduledTask.run_at <= now,
            )
        ).scalars().all()
    return list(due)


async def _poll_once() -> None:
    due_ids = await asyncio.to_thread(_poll_due_sync)
    for task_id in due_ids:
        fire_and_forget(_run_task_bounded(task_id))


async def _run_task_bounded(task_id: int) -> None:
    async with _get_dispatch_sem():
        await _run_task(task_id)


async def _run_task(task_id: int) -> None:
    from sqlalchemy.orm import Session  # cycle-break

    from fastapi_backend.core.task_tracker import (  # cycle-break
        create_task,
        finish_task,
        start_task,
    )
    from fastapi_backend.database.db_writer import engine  # cycle-break
    from fastapi_backend.database.models import Boxes, ScheduledTask  # cycle-break
    from fastapi_backend.database.write_actor import write_actor  # cycle-break

    # Claim the task through the single-writer actor. The actor commits and
    # closes its own session, so we can no longer keep a live ORM instance past
    # the write (the old expire_on_commit=False dance that kept task.command
    # readable). Instead the claim returns a detached snapshot of exactly the
    # fields the rest of the run needs — no ORM attribute is touched after the
    # session closes, so the DetachedInstanceError that stranded tasks at
    # status="running" (soak validation 2026-05-10: 150/150 stuck) cannot recur.
    def _claim(db: Session) -> dict | None:
        task = db.get(ScheduledTask, task_id)
        if not task or task.status != "pending":
            return None
        task.status = "running"
        task.last_run_at = dt.datetime.now(dt.UTC).isoformat()
        return {
            "command": task.command,
            "args": task.args,
            "target_type": task.target_type,
            "target": task.target,
            "created_by": task.created_by,
            "name": task.name,
        }

    claimed = await write_actor.submit(_claim, label="scheduler_claim_task")
    if claimed is None:
        return
    task = SimpleNamespace(**claimed)

    _log.info("Scheduled task %d firing: %s", task_id, task.command)

    # Resolve target boxes before creating OperatorTask so target_boxes is accurate.
    # Off-loaded to a worker thread: this used to hold a sync Session on the loop
    # thread and starved uvicorn's accept loop under Phase 2-7 concurrent load
    # (soak N 2026-08-26).
    def _resolve_boxes_sync() -> tuple[list[str], int | None]:
        from sqlalchemy import select  # cycle-break
        with Session(engine) as db:
            stmt = select(Boxes)
            if task.target_type == "box_ip" and task.target:
                stmt = stmt.where(Boxes.box_ip == task.target)
            elif task.target_type == "team" and task.target:
                stmt = stmt.where(Boxes.team_num == int(task.target))
            boxes = db.execute(stmt).scalars().all()
            ips = [b.box_ip for b in boxes if b.session_id]
        tn: int | None = (
            int(task.target)
            if task.target_type == "team" and task.target
            else None
        )
        return ips, tn

    box_ips, team_num = await asyncio.to_thread(_resolve_boxes_sync)

    op_task_id = create_task(
        task_type="scheduled",
        operator=task.created_by,
        team_num=team_num,
        target_boxes=box_ips,
        metadata={"scheduled_task_id": task_id, "command": task.command, "name": task.name},
    )
    start_task(op_task_id)

    # Persist link so frontend can navigate from scheduler history → task queue.
    def _link_op_task(db: Session) -> None:
        t = db.get(ScheduledTask, task_id)
        if t:
            t.operator_task_id = op_task_id

    await write_actor.submit(_link_op_task, label="scheduler_link_op_task")

    try:
        await _dispatch_task(task, box_ips, op_task_id)
        sched_status = "complete"
        op_status = "done"
    except Exception as exc:
        _log.warning("Scheduled task %d failed: %s", task_id, exc)
        sched_status = "error"
        op_status = "failed"

    finish_task(op_task_id, op_status)

    def _finalize(db: Session) -> None:
        task = db.get(ScheduledTask, task_id)
        if not task:
            return
        task.status = sched_status

        if task.repeat_secs and sched_status == "complete":
            next_run = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=task.repeat_secs)
            task.next_run_at = next_run.isoformat()
            # Re-queue as a new pending row so the original run record stays
            new_task = ScheduledTask(
                name=task.name,
                command=task.command,
                args=task.args,
                target_type=task.target_type,
                target=task.target,
                run_at=next_run.isoformat(),
                repeat_secs=task.repeat_secs,
                status="pending",
                created_by=task.created_by,
            )
            db.add(new_task)

    await write_actor.submit(_finalize, label="scheduler_finalize_task")


async def _dispatch_task(task: object, box_ips: list[str], op_task_id: int) -> None:
    """Execute the scheduled command against target boxes via active Sliver sessions."""
    from sqlalchemy import select  # cycle-break
    from sqlalchemy.orm import Session  # cycle-break

    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break
    from fastapi_backend.core.task_tracker import update_box_progress  # cycle-break
    from fastapi_backend.database.db_writer import engine  # cycle-break
    from fastapi_backend.database.models import Boxes  # cycle-break

    client = sliver_manager.get_client()

    parts = shlex.split(task.command, posix=False) if task.command else []
    exe = parts[0] if parts else task.command
    # Strip surrounding quotes that shlex.split leaves in posix=False mode
    if exe and len(exe) >= 2 and exe[0] in ('"', "'") and exe[-1] == exe[0]:
        exe = exe[1:-1]
    args_extra = shlex.split(task.args, posix=False) if task.args else []
    args = parts[1:] + args_extra

    # Resolve session_id per box — off-loaded (see _run_task comment).
    def _resolve_sessions_sync() -> dict[str, str | None]:
        with Session(engine) as db:
            boxes = db.execute(
                select(Boxes).where(Boxes.box_ip.in_(box_ips))
            ).scalars().all()
            return {b.box_ip: b.session_id for b in boxes}

    box_sessions = await asyncio.to_thread(_resolve_sessions_sync)

    _log.info(
        "Scheduled task dispatching %r to %d box(es): %s",
        task.command,
        len(box_ips),
        box_ips,
    )

    if not box_ips:
        _log.info("Scheduled task %d: no boxes with active sessions", op_task_id)
        return

    any_failed = False

    async def _exec_one(box_ip: str) -> None:
        nonlocal any_failed
        session_id = box_sessions.get(box_ip)
        update_box_progress(op_task_id, box_ip, "running")

        if not client:
            update_box_progress(op_task_id, box_ip, "failed", error="Sliver not connected")
            any_failed = True
            return

        if not session_id:
            update_box_progress(op_task_id, box_ip, "failed", error="No active session")
            any_failed = True
            return

        t0 = dt.datetime.now(dt.UTC)
        try:
            interact = await client.interact_session(session_id)
            if not interact:
                update_box_progress(op_task_id, box_ip, "failed", error="Session not found in Sliver")
                any_failed = True
                return

            result = await interact.execute(exe, args, output=True)
            stdout = result.Stdout.decode(errors="replace") if result.Stdout else ""
            stderr = result.Stderr.decode(errors="replace") if result.Stderr else ""
            exit_code = result.Status

            duration_s = (dt.datetime.now(dt.UTC) - t0).total_seconds()
            status = "done" if exit_code == 0 else "failed"
            error = f"exit {exit_code}" if exit_code != 0 else None
            details = json.dumps({"stdout": stdout, "stderr": stderr, "exit_code": exit_code})
            update_box_progress(op_task_id, box_ip, status, error=error, details=details, duration_s=duration_s)
            if status == "failed":
                any_failed = True

        except Exception as exc:
            duration_s = (dt.datetime.now(dt.UTC) - t0).total_seconds()
            update_box_progress(op_task_id, box_ip, "failed", error=str(exc)[:200], duration_s=duration_s)
            any_failed = True

    await asyncio.gather(*[_exec_one(ip) for ip in box_ips])

    if any_failed:
        raise RuntimeError("one or more boxes failed")
