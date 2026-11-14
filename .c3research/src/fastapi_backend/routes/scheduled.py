"""Scheduled task routes.

GET    /c2/scheduled          — list tasks (pending + recent)
POST   /c2/scheduled          — create task
DELETE /c2/scheduled/{id}     — cancel task
GET    /admin/scheduled/events — list competition events (admin only)
POST   /admin/scheduled/events — create competition event (admin only)
DELETE /admin/scheduled/events/{id} — cancel competition event (admin only)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import ScheduledCompetitionEvent, ScheduledTask
from fastapi_backend.routes.admin import require_admin
from fastapi_backend.routes.c2 import require_non_blue
from fastapi_backend.utils.auth import Users

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scheduled"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class CreateTaskRequest(BaseModel):
    """POST /c2/scheduled request body — schedule a future command."""

    name: str
    command: str
    args: str | None = None
    target_type: str  # "box_ip" | "team" | "all"
    target: str | None = None
    run_at: str  # ISO timestamp
    repeat_secs: int | None = None


class CreateEventRequest(BaseModel):
    """POST /admin/scheduled/events request body."""

    event_type: str  # "start" | "pause" | "end" | "check"
    run_at: str


def _task_dict(t: ScheduledTask) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "command": t.command,
        "args": t.args,
        "target_type": t.target_type,
        "target": t.target,
        "run_at": t.run_at,
        "repeat_secs": t.repeat_secs,
        "status": t.status,
        "created_by": t.created_by,
        "created_at": t.created_at,
        "last_run_at": t.last_run_at,
        "next_run_at": t.next_run_at,
        "operator_task_id": t.operator_task_id,
    }


def _event_dict(e: ScheduledCompetitionEvent) -> dict:
    return {
        "id": e.id,
        "event_type": e.event_type,
        "run_at": e.run_at,
        "fired": e.fired,
        "created_by": e.created_by,
    }


# ---------------------------------------------------------------------------
# Task routes
# ---------------------------------------------------------------------------


@router.get("/c2/scheduled")
def list_scheduled_tasks(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """List scheduled tasks, newest first."""
    with Session(engine) as db:
        stmt = select(ScheduledTask).order_by(desc(ScheduledTask.created_at)).limit(limit)
        if status:
            stmt = stmt.where(ScheduledTask.status == status)
        tasks = db.execute(stmt).scalars().all()
        return {"tasks": [_task_dict(t) for t in tasks]}


@router.post("/c2/scheduled", status_code=201)
def create_scheduled_task(
    body: CreateTaskRequest,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Schedule a command to run at a future time."""
    from fastapi_backend.database.write_actor import write_actor  # cycle-break
    def _create(db: Session) -> dict:
        t = ScheduledTask(
            name=body.name,
            command=body.command,
            args=body.args,
            target_type=body.target_type,
            target=body.target,
            run_at=body.run_at,
            repeat_secs=body.repeat_secs,
            status="pending",
            created_by=current_user.username,
        )
        db.add(t)
        db.flush()
        return _task_dict(t)
    return write_actor.submit_sync(_create, label="create_scheduled_task")


@router.delete("/c2/scheduled/{task_id}", status_code=204)
def cancel_scheduled_task(
    task_id: int,
    current_user: Users = Depends(require_non_blue),
) -> None:
    """Cancel a pending or running scheduled task."""
    from fastapi_backend.database.write_actor import write_actor  # cycle-break
    def _cancel(db: Session) -> tuple[str, str] | None:
        t = db.get(ScheduledTask, task_id)
        if t is None:
            return ("404", "Task not found")
        if t.status in ("complete", "cancelled"):
            return ("400", f"Task is already {t.status}")
        t.status = "cancelled"
        return None
    err = write_actor.submit_sync(_cancel, label="cancel_scheduled_task")
    if err is not None:
        raise HTTPException(status_code=int(err[0]), detail=err[1])


# ---------------------------------------------------------------------------
# Competition event routes (admin only)
# ---------------------------------------------------------------------------


@router.get("/admin/scheduled/events")
def list_competition_events(
    current_user: Users = Depends(require_admin),
) -> dict[str, Any]:
    """List all competition events (admin only)."""
    with Session(engine) as db:
        events = db.execute(
            select(ScheduledCompetitionEvent).order_by(ScheduledCompetitionEvent.run_at)
        ).scalars().all()
        return {"events": [_event_dict(e) for e in events]}


@router.post("/admin/scheduled/events", status_code=201)
def create_competition_event(
    body: CreateEventRequest,
    current_user: Users = Depends(require_admin),
) -> dict[str, Any]:
    """Schedule a competition lifecycle event (admin only)."""
    valid_types = {"start", "pause", "end", "check"}
    if body.event_type not in valid_types:
        raise HTTPException(status_code=422, detail=f"event_type must be one of {sorted(valid_types)}")
    from fastapi_backend.database.write_actor import write_actor  # cycle-break
    def _create(db: Session) -> dict:
        e = ScheduledCompetitionEvent(
            event_type=body.event_type,
            run_at=body.run_at,
            fired=False,
            created_by=current_user.username,
        )
        db.add(e)
        db.flush()
        return _event_dict(e)
    return write_actor.submit_sync(_create, label="create_competition_event")


@router.delete("/admin/scheduled/events/{event_id}", status_code=204)
def cancel_competition_event(
    event_id: int,
    current_user: Users = Depends(require_admin),
) -> None:
    """Delete a competition event (admin only)."""
    from fastapi_backend.database.write_actor import write_actor  # cycle-break
    def _delete(db: Session) -> tuple[str, str] | None:
        e = db.get(ScheduledCompetitionEvent, event_id)
        if e is None:
            return ("404", "Event not found")
        if e.fired:
            return ("400", "Event has already fired")
        db.delete(e)
        return None
    err = write_actor.submit_sync(_delete, label="cancel_competition_event")
    if err is not None:
        raise HTTPException(status_code=int(err[0]), detail=err[1])
