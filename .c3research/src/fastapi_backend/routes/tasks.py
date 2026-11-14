"""Operator task history routes.

GET /c2/tasks                 — list OperatorTask summaries (filterable)
GET /c2/tasks/{id}            — full OperatorTask detail with parsed progress_json
GET /c2/tasks/{id}/stream     — per-task SSE feed (operator_task_update events)
GET /c2/tasks/stream          — global SSE feed (sidebar v2)
POST /c2/tasks/{id}/kill      — cancel a pending/running task (admin or owning operator)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import OperatorTask
from fastapi_backend.utils.auth import Users, get_current_user, get_user_from_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/c2/tasks", tags=["operator_tasks"])


def require_non_blue(current_user: Users = Depends(get_current_user)) -> Users:
    """Gate the task routes to admin / red team — blue-team callers get 403."""
    if current_user.is_blue_team:
        raise HTTPException(status_code=403, detail="Blue team members cannot access C2 data")
    return current_user


def _require_non_blue_from_query_token(token: str) -> Users:
    """SSE-friendly auth: EventSource cannot set Authorization header, so the
    token is passed as a `?token=` query parameter and validated here."""
    user = get_user_from_token(token)
    if user.is_blue_team:
        raise HTTPException(status_code=403, detail="Blue team members cannot access C2 data")
    return user


def _detail_dict(task: OperatorTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "current_phase": task.current_phase,
        "operator": task.operator,
        "team_num": task.team_num,
        "target_boxes": json.loads(task.target_boxes or "[]"),
        "progress_json": json.loads(task.progress_json or "{}"),
        "metadata_json": json.loads(task.metadata_json) if task.metadata_json else None,
        "description": task.description,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "completed_at": task.completed_at,
    }


def _summary_dict(task: OperatorTask) -> dict[str, Any]:
    target_boxes = json.loads(task.target_boxes or "[]")
    progress = json.loads(task.progress_json or "{}")
    error_count = sum(1 for v in progress.values() if v.get("status") == "failed")
    # Only the queue position is lifted out of metadata_json — the Task Queue list needs
    # it to render "queued · position N", and shipping whole metadata blobs (which carry
    # full per-host deploy results) on every summary row would be far heavier.
    meta = json.loads(task.metadata_json) if task.metadata_json else {}
    queue_position = meta.get("queue_position") if isinstance(meta, dict) else None
    return {
        "id": task.id,
        "task_type": task.task_type,
        "status": task.status,
        "current_phase": task.current_phase,
        "queue_position": queue_position,
        "operator": task.operator,
        "team_num": task.team_num,
        "box_count": len(target_boxes),
        "error_count": error_count,
        "description": task.description,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "completed_at": task.completed_at,
    }


# NOTE: /stream and /{task_id}/stream MUST be registered BEFORE /{task_id} —
# otherwise FastAPI matches the literal "stream" as a task_id string and returns 422.


@router.get("/stream")
async def stream_all_tasks(request: Request, token: str = Query(...)) -> StreamingResponse:
    """Global SSE feed — every operator_task_update event across every task."""
    _require_non_blue_from_query_token(token)
    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break

    q: asyncio.Queue = sliver_manager.subscribe()

    async def event_gen() -> AsyncIterator[str]:
        try:
            while True:
                # Reap on client disconnect — an un-checked loop parks on q.get()
                # forever after the client closes, holding the subscriber queue and
                # leaving the socket in CLOSE_WAIT (the 2026-06-26 full-soak leak).
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=30)
                    if payload.get("kind") == "operator_task_update":
                        yield f"data: {json.dumps(payload)}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            sliver_manager.unsubscribe(q)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/{task_id}/stream")
async def stream_task(request: Request, task_id: int, token: str = Query(...)) -> StreamingResponse:
    """Per-task SSE feed — only events for this task_id."""
    _require_non_blue_from_query_token(token)
    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break

    q: asyncio.Queue = sliver_manager.subscribe()

    async def event_gen() -> AsyncIterator[str]:
        try:
            while True:
                # Reap on client disconnect (see stream_all_tasks above).
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=30)
                    if (
                        payload.get("kind") == "operator_task_update"
                        and payload.get("task_id") == task_id
                    ):
                        yield f"data: {json.dumps(payload)}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            sliver_manager.unsubscribe(q)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/{task_id}")
def get_task_detail(
    task_id: int,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Return a single OperatorTask row with progress + metadata expanded from JSON."""
    with Session(engine) as db:
        task = db.get(OperatorTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return _detail_dict(task)


@router.get("")
def list_tasks(
    operator: str | None = Query(default=None),
    team_num: int | None = Query(default=None),
    status: list[str] | None = Query(default=None),
    task_type: list[str] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """List OperatorTask rows with optional filters; returns lightweight summary dicts.

    ``status`` and ``task_type`` both support repeated query params for
    multi-value filtering (``?status=done&status=failed``). Single-value
    callers continue to work — a single ``?status=done`` binds to a one-element
    list. ``offset`` pages through results when ``limit`` is hit.
    """
    with Session(engine) as db:
        stmt = (
            select(OperatorTask)
            .order_by(OperatorTask.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if operator:
            stmt = stmt.where(OperatorTask.operator == operator)
        if team_num is not None:
            stmt = stmt.where(OperatorTask.team_num == team_num)
        if status:
            stmt = stmt.where(OperatorTask.status.in_(status))
        if task_type:
            stmt = stmt.where(OperatorTask.task_type.in_(task_type))
        tasks = db.execute(stmt).scalars().all()
    return {"tasks": [_summary_dict(t) for t in tasks]}


@router.post("/{task_id}/kill", status_code=200)
def kill_task(
    task_id: int,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Cancel a pending/running task.

    Admin can kill any task; a non-admin operator can only kill their own
    tasks. Real cancellation: cancels the tracked top-level asyncio.Task
    (which cascades to every per-box coroutine gathered under it) and flips
    the DB row to ``cancelled`` — a terminal status that update_box_progress/
    finish_task will refuse to overwrite if a stale in-flight result lands
    afterward.
    """
    from fastapi_backend.core.task_tracker import cancel_task, finish_task  # cycle-break

    with Session(engine) as db:
        task = db.get(OperatorTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if not current_user.is_admin and task.operator != current_user.username:
            raise HTTPException(status_code=403, detail="Not your task")
        if task.status not in ("pending", "running"):
            raise HTTPException(status_code=400, detail=f"Task is already {task.status}")

    had_live_task = cancel_task(task_id)
    finish_task(task_id, "cancelled")
    return {"task_id": task_id, "status": "cancelled", "had_live_task": had_live_task}
