"""Admin-only API: user management, competition control, runtime settings."""

import datetime as dt
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from fastapi_backend.core.competition_state import (
    CompetitionStatus,
)
from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import CompetitionConfig, Users
from fastapi_backend.utils.asyncio_tasks import fire_and_forget
from fastapi_backend.utils.auth import (
    add_user as add_user_record,
)
from fastapi_backend.utils.auth import (
    get_current_user,
    hash_password,
)
from fastapi_backend.utils.dependencies import get_orchestrator
from fastapi_backend.utils.exceptions import user_not_found

if TYPE_CHECKING:
    from fastapi_backend.core.orchestrator import CompetitionOrchestrator  # lazy-load

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)


class DelUserRequest(BaseModel):
    """DELETE /admin/remove_user request body."""

    user_id: int


class AddUserRequest(BaseModel):
    """POST /admin/add_user request body.

    ``user_id`` is optional — backend auto-assigns the next available id
    when omitted (max(user_id) + 1, falling back to 1).
    """

    user_id: int | None = None
    username: str
    password: str
    is_admin: bool
    is_blue_team: bool
    blue_team_num: int | None = None


class ChangePasswordRequest(BaseModel):
    """POST /admin/change_password request body."""

    user_id: int
    password: str


class SelectiveDeployRequest(BaseModel):
    """POST /admin/deploy_iocs_selective request body."""

    ioc_names: list[str]
    target_boxes: list[str] | None = None
    background: bool = False  # If True, run in background and return immediately
    # Remediate-only. True (default) = RED remediate: a successful on-box
    # remediate RETIRES the DeployedIOCs row (status='failed'), so the check
    # pipeline stops scoring it — the operator/teardown semantics. False = BLUE
    # clear: run the same on-box remediate but LEAVE the row active, so the next
    # check reports effect_status=0 and the row still scores (base points). The
    # score-correctness subset needs this: its baseline is a remediated-but-still
    # -scoring row. Ignored by deploy_iocs_selective.
    retire_rows: bool = True


class DeployAllRequest(BaseModel):
    """POST /admin/deploy-all-iocs request body."""

    include_implants: bool = True       # also generate+deploy SliverCheck implant IOCs
    skip_active: bool = True            # skip (box, ioc) pairs already active in DeployedIOCs
    confirm: bool = False              # required to run while competition is RUNNING
    team_nums: list[int] | None = None  # None = all teams; scope for tests / cautious ops
    ioc_names: list[str] | None = None  # None = all IOCs; limits scope for focused tests/ops


class ScoringPreflightRequest(BaseModel):
    """POST /admin/scoring-cred-preflight request body."""

    team_nums: list[int] | None = None  # None = all teams; scope for focused validation


class TicketCycleRequest(BaseModel):
    """POST /admin/cred-harvest/ticket-cycle request body."""

    team_nums: list[int] | None = None  # None = every team with a flagged DC


class BuildAllRequest(BaseModel):
    """POST /admin/build-all-iocs request body (docs/PLAN_build_check_deploy_name_pools.md)."""

    include_implants: bool = True       # compile SliverCheck implant IOCs build-only
    force: bool = False                # rebuild all even if a valid build exists
    team_nums: list[int] | None = None  # None = all teams; scopes which OSes are in scope


class BuildCheckRequest(BaseModel):
    """POST /admin/build-check request body."""

    team_nums: list[int] | None = None


async def check_admin(current_user: Users) -> None:
    """Raise 403 if `current_user` is not an admin."""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=403, detail="Account accessing is not an administrator."
        )
    return


@router.get("/get_users")
async def get_users(current_user: Users = Depends(get_current_user)) -> dict[str, Any]:
    """Returns a list of all users with relevant info. Admin-only.
    """
    await check_admin(current_user)

    try:
        with Session(engine) as session:
            statement = select(Users).order_by(Users.user_id)
            results = session.exec(statement).all()

        users_data = [
            {
                "user_id": user.user_id,
                "username": user.username,
                "is_admin": user.is_admin,
                "is_blue_team": user.is_blue_team,
                "blue_team_num": user.blue_team_num,
            }
            for user in results
        ]

        return {"status": "success", "users": users_data}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to retrieve users: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch users") from e




@router.delete("/remove_user")
async def remove_user(
    user_req: DelUserRequest = Body(...),
    current_user: Users = Depends(get_current_user),
) -> dict[str, Any]:
    """Remove a user by user_id. Admin-only.
    """
    await check_admin(current_user)

    try:
        if current_user.user_id == user_req.user_id:
            raise HTTPException(status_code=400, detail="You cannot remove yourself.")

        from fastapi_backend.database.write_actor import write_actor  # cycle-break
        def _delete(db: Session) -> bool:
            target_user = db.get(Users, user_req.user_id)
            if target_user is None:
                return False
            db.delete(target_user)
            return True
        if not await write_actor.submit(_delete, label="remove_user"):
            raise user_not_found()

        return {
            "status": "success",
            "message": f"User ID {user_req.user_id} deleted successfully",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete user {user_req.user_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/add_user")
async def add_user(
    user: AddUserRequest, current_user: Users = Depends(get_current_user)
) -> dict[str, Any]:
    """Add a new user. Admin-only.

    When ``user.user_id`` is omitted the backend assigns the next available
    id (``max(user_id) + 1``).
    """
    await check_admin(current_user)

    try:
        # Auto-assign user_id when not provided.
        if user.user_id is None:
            from sqlalchemy import func as _func  # cycle-break
            with Session(engine) as _s:
                max_id = _s.execute(select(_func.max(Users.user_id))).scalar()
            next_id = (max_id or 0) + 1
        else:
            next_id = user.user_id
        new_user = Users(
            user_id=next_id,
            username=user.username,
            password=user.password,
            is_admin=user.is_admin,
            is_blue_team=user.is_blue_team,
            blue_team_num=user.blue_team_num if user.is_blue_team else None,
        )
        add_user_record(new_user)
        return {
            "status": "success",
            "message": f"User '{user.username}' added successfully",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to add user: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/change_password")
def change_password(
    user: ChangePasswordRequest, current_user: Users = Depends(get_current_user)
) -> dict[str, Any]:
    """Change the password for a user.
    """
    # Check if the current user is admin or the user themselves
    if not current_user.is_admin and current_user.user_id != user.user_id:
        raise HTTPException(status_code=403, detail="Permission denied")

    # Hash the new password
    hashed_password = hash_password(user.password)

    from fastapi_backend.database.write_actor import write_actor  # cycle-break
    _uid = user.user_id
    def _update(db: Session) -> bool:
        u = db.get(Users, _uid)
        if u is None:
            return False
        u.password = hashed_password
        return True
    if not write_actor.submit_sync(_update, label="change_password"):
        raise user_not_found()

    return {"status": "success", "message": "Password changed successfully"}


@router.post("/deploy_iocs")
async def deploy_iocs(
    current_user: Users = Depends(get_current_user), orch: "CompetitionOrchestrator" = Depends(get_orchestrator)
) -> dict[str, Any]:
    """Deploy IOCs to all target systems, skipping already-active deployments."""
    try:
        await check_admin(current_user)

        from fastapi_backend.database.models import DeployedIOCs  # cycle-break

        with Session(engine) as db:
            active_rows = db.exec(
                select(DeployedIOCs).where(DeployedIOCs.status == "active")
            ).all()
        already_deployed: set[tuple[str, str]] = {(r.box_ip, r.ioc_name) for r in active_rows}

        results = await orch.deploy_iocs(exclude_pairs=already_deployed)
        skipped = len(already_deployed)
        return {
            "status": "success",
            "message": f"IOCs deployment initiated (skipped {skipped} already active)",
            "results": results,
        }
    # Fix HTTPExceptions being eaten elsewhere
    except HTTPException:
        # Rethrow HTTPException without logging as error, just pass through
        raise
    except Exception as e:
        logger.error(f"Failed to deploy IOCs: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


def _has_running_deploy_all() -> bool:
    """True if a deploy_all parent task is already pending/running."""
    from fastapi_backend.database.models import OperatorTask  # cycle-break

    with Session(engine) as db:
        existing = db.execute(
            select(OperatorTask).where(
                OperatorTask.task_type == "deploy_all",
                OperatorTask.status.in_(["pending", "running"]),
            )
        ).first()
    return existing is not None


@router.post("/deploy-all-iocs", status_code=202)
async def deploy_all_iocs(
    req: DeployAllRequest | None = Body(default=None),
    current_user: Users = Depends(get_current_user),
    orch: "CompetitionOrchestrator" = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Deploy + register every deployable IOC across all matching boxes (admin only).

    Returns ``202 {task_id}`` immediately. A parent OperatorTask fans out one
    child task per IOC (deploy-and-register for ScriptCheck IOCs, generate+deploy
    for non-inject SliverCheck implant IOCs). Progress via ``GET /c2/tasks/{task_id}``
    and the ``/c2/tasks/stream`` SSE feed. See ``core/deploy_all.run_deploy_all``.
    """
    await check_admin(current_user)
    req = req or DeployAllRequest()

    # State gate: allowed pre-competition freely; RUNNING needs explicit confirm.
    if orch.state.status == CompetitionStatus.RUNNING and not req.confirm:
        raise HTTPException(
            status_code=409,
            detail="deploy-all during RUNNING requires confirm=true",
        )

    # Concurrent-submit guard — one deploy-all at a time.
    if _has_running_deploy_all():
        raise HTTPException(
            status_code=409,
            detail={"code": "another_deploy_all_running"},
        )

    from fastapi_backend.core.deploy_all import run_deploy_all  # cycle-break
    from fastapi_backend.core.task_tracker import (  # cycle-break
        create_task,
        register_background_task,
        run_cancellable,
    )

    task_id = create_task(
        task_type="deploy_all",
        operator=current_user.username,
        team_num=None,
        target_boxes=[],
        metadata={
            "include_implants": req.include_implants,
            "skip_active": req.skip_active,
            "team_nums": req.team_nums,
            "ioc_names": req.ioc_names,
        },
    )
    register_background_task(task_id, fire_and_forget(run_cancellable(task_id,
        run_deploy_all(
            task_id,
            current_user.username,
            include_implants=req.include_implants,
            skip_active=req.skip_active,
            team_nums=req.team_nums,
            ioc_names=req.ioc_names,
        )
    )))
    return {"task_id": task_id}


@router.post("/build-all-iocs", status_code=202)
async def build_all_iocs(
    req: BuildAllRequest | None = Body(default=None),
    current_user: Users = Depends(get_current_user),
) -> dict[str, Any]:
    """BUILD step (admin only): compile every IOC's artifact once, up front, no deploy.

    Returns ``202 {task_id}``. Parent ``OperatorTask(task_type='build_all')``;
    per-IOC ``{reason}`` on ``progress_json``. See ``core/build_all.run_build_all``.
    """
    await check_admin(current_user)
    req = req or BuildAllRequest()

    from fastapi_backend.core.build_all import has_running_build_all, run_build_all
    if has_running_build_all():
        raise HTTPException(status_code=409, detail={"code": "another_build_all_running"})

    from fastapi_backend.core.task_tracker import (  # cycle-break
        create_task,
        register_background_task,
        run_cancellable,
    )
    task_id = create_task(
        task_type="build_all", operator=current_user.username, team_num=None,
        target_boxes=[],
        metadata={"include_implants": req.include_implants, "force": req.force,
                  "team_nums": req.team_nums},
    )
    register_background_task(task_id, fire_and_forget(run_cancellable(task_id,
        run_build_all(
            task_id, current_user.username,
            include_implants=req.include_implants, team_nums=req.team_nums,
            force=req.force,
        )
    )))
    return {"task_id": task_id}


@router.post("/build-check", status_code=202)
async def build_check(
    req: BuildCheckRequest | None = Body(default=None),
    current_user: Users = Depends(get_current_user),
) -> dict[str, Any]:
    """CHECK step (admin only): verify every required artifact is built + current.

    Returns ``202 {task_id}``. Per-IOC ``{reason}`` on ``progress_json`` +
    summary ``metadata_json``. See ``core/build_check.run_build_check``.
    """
    await check_admin(current_user)
    req = req or BuildCheckRequest()

    from fastapi_backend.core.build_check import (
        has_running_build_check,
        run_build_check,
    )
    if has_running_build_check():
        raise HTTPException(status_code=409, detail={"code": "another_build_check_running"})

    from fastapi_backend.core.task_tracker import (  # cycle-break
        create_task,
        register_background_task,
        run_cancellable,
    )
    task_id = create_task(
        task_type="build_check", operator=current_user.username, team_num=None,
        target_boxes=[], metadata={"team_nums": req.team_nums},
    )
    register_background_task(task_id, fire_and_forget(run_cancellable(task_id,
        run_build_check(task_id, team_nums=req.team_nums)
    )))
    return {"task_id": task_id}


def _has_running_scoring_preflight() -> bool:
    """True if a scoring_preflight task is already pending/running."""
    from fastapi_backend.database.models import OperatorTask  # cycle-break

    with Session(engine) as db:
        existing = db.execute(
            select(OperatorTask).where(
                OperatorTask.task_type == "scoring_preflight",
                OperatorTask.status.in_(["pending", "running"]),
            )
        ).first()
    return existing is not None


@router.post("/scoring-cred-preflight", status_code=202)
async def scoring_cred_preflight(
    req: ScoringPreflightRequest | None = Body(default=None),
    current_user: Users = Depends(get_current_user),
    orch: "CompetitionOrchestrator" = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Validate the red_scoring account (auth + privilege) on every box (admin only).

    Returns ``202 {task_id}`` immediately; per-box ``{reason}`` lands on the
    OperatorTask ``progress_json`` and a summary on ``metadata_json``. Poll
    ``GET /c2/tasks/{task_id}``. See ``core/scoring_preflight.run_scoring_preflight``.
    """
    await check_admin(current_user)
    req = req or ScoringPreflightRequest()

    if not orch.executor or not getattr(orch.executor, "nxc_wrapper", None):
        raise HTTPException(status_code=503, detail="NXC executor not ready")

    # Concurrent-submit guard — one preflight at a time.
    if _has_running_scoring_preflight():
        raise HTTPException(
            status_code=409,
            detail={"code": "another_scoring_preflight_running"},
        )

    from fastapi_backend.core.scoring_preflight import (
        run_scoring_preflight,  # cycle-break
    )
    from fastapi_backend.core.task_tracker import (  # cycle-break
        create_task,
        register_background_task,
        run_cancellable,
    )
    from fastapi_backend.database.models import Boxes  # cycle-break

    with Session(engine) as db:
        stmt = select(Boxes.box_ip)
        if req.team_nums:
            stmt = stmt.where(Boxes.team_num.in_(req.team_nums))
        box_ips = [row[0] for row in db.execute(stmt).all()]

    task_id = create_task(
        task_type="scoring_preflight",
        operator=current_user.username,
        team_num=None,
        target_boxes=box_ips,
        metadata={"team_nums": req.team_nums},
    )
    register_background_task(task_id, fire_and_forget(run_cancellable(
        task_id, run_scoring_preflight(task_id, req.team_nums, orch.executor.nxc_wrapper),
    )))
    return {"task_id": task_id}


@router.post("/prewarm-pivots")
async def prewarm_pivots(
    current_user: Users = Depends(get_current_user),
) -> dict[str, Any]:
    """Probe + rebuild every active PortfwdMappings entry (admin only).

    Called before a deploy fan-out so a Sliver TCP portfwd tunnel that has
    idle-drifted to death is detected + rebuilt while we have milliseconds,
    not while the caller is committed to a 180s put-file timeout budget.
    See ``c2/pivot_prewarm.prewarm_all_pivots``.

    Returns per-port counts (live/rebuilt/failed/no_pivot) + the full entry
    list. Synchronous: waits for every probe + rebuild before returning
    (typical wall-clock 5-15s for a 90-mapping fleet).
    """
    await check_admin(current_user)
    from fastapi_backend.c2.pivot_prewarm import prewarm_all_pivots  # cycle-break
    return await prewarm_all_pivots()


@router.post("/deploy_iocs_selective")
async def deploy_iocs_selective(
    request: SelectiveDeployRequest,
    current_user: Users = Depends(get_current_user),
    orch: "CompetitionOrchestrator" = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Selectively deploy specific IOCs to target systems."""
    try:
        await check_admin(current_user)
        if orch.state.status != CompetitionStatus.NOT_STARTED:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot deploy IOCs in state: {orch.state.status.value}",
            )

        results = await orch.deploy_iocs_selective(
            request.ioc_names, request.target_boxes
        )
        return {
            "status": "success",
            "message": "Selective IOC deployment initiated",
            "results": results,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to deploy IOCs selectively: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/redeploy_iocs")
async def redeploy_iocs(
    request: SelectiveDeployRequest,
    current_user: Users = Depends(get_current_user),
    orch: "CompetitionOrchestrator" = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Re-deploy IOCs during an active competition (no state restriction).

    Unlike /deploy_iocs_selective, this endpoint has no state precondition — it is
    intended for soak testing and competition-time redeployment by admins.

    Set background=true to return immediately and run the deploy asynchronously.
    Useful when deploying to many boxes where the operation may exceed client timeouts.
    """
    await check_admin(current_user)

    if request.background:
        async def _do_redeploy() -> None:
            from datetime import UTC, datetime  # cycle-break

            from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break
            attempted = succeeded = failed = 0
            error: str | None = None
            try:
                results = await orch.redeploy_iocs_selective(request.ioc_names, request.target_boxes)
                succeeded = results.get("successful", 0)
                failed = results.get("failed", 0)
                attempted = results.get("total", 0) or (succeeded + failed)
                logger.info(
                    "Background redeploy complete: %d succeeded, %d failed of %d total",
                    succeeded, failed, attempted,
                )
                if results.get("errors"):
                    logger.warning("Background redeploy errors: %s", results["errors"][:5])
            except Exception as e:
                logger.error("Background redeploy task failed: %s", e)
                error = str(e)
            finally:
                await sliver_manager.publish({
                    "type": "deploy_complete",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "attempted": attempted,
                    "succeeded": succeeded,
                    "failed": failed,
                    **({"error": error} if error else {}),
                })

        fire_and_forget(_do_redeploy())
        return {"status": "started", "message": "Redeployment running in background"}

    try:
        results = await orch.redeploy_iocs_selective(request.ioc_names, request.target_boxes)
        return {
            "status": "success",
            "message": "Redeployment initiated",
            "results": results,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to redeploy IOCs: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/remediate_iocs")
async def remediate_iocs(
    request: SelectiveDeployRequest,
    current_user: Users = Depends(get_current_user),
    orch: "CompetitionOrchestrator" = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Execute remediate scripts for specified IOCs on specified boxes.

    Simulates blue-team remediation. Useful for soak testing and automated teardown.
    Set background=true to return immediately and run asynchronously.
    """
    await check_admin(current_user)

    if request.background:
        async def _do_remediate() -> None:
            try:
                results = await orch.remediate_iocs_selective(
                    request.ioc_names, request.target_boxes, retire_rows=request.retire_rows)
                logger.info(
                    "Background remediation complete: %d succeeded, %d failed of %d total",
                    results.get("successful", 0), results.get("failed", 0), results.get("total", 0),
                )
                if results.get("errors"):
                    logger.warning("Background remediation errors: %s", results["errors"][:5])
            except Exception as e:
                logger.error("Background remediation task failed: %s", e)

        fire_and_forget(_do_remediate())
        return {"status": "started", "message": "Remediation running in background"}

    try:
        results = await orch.remediate_iocs_selective(
            request.ioc_names, request.target_boxes, retire_rows=request.retire_rows)
        return {
            "status": "success",
            "message": "Remediation complete",
            "results": results,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to remediate IOCs: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/run_checks")
async def run_checks(
    current_user: Users = Depends(get_current_user), orch: "CompetitionOrchestrator" = Depends(get_orchestrator)
) -> dict[str, Any]:
    """Manually trigger a check cycle."""
    try:
        await check_admin(current_user)
        if not orch.state.can_run_checks():
            raise HTTPException(
                status_code=400,
                detail=f"Cannot run checks in state: {orch.state.status.value}",
            )

        await orch.run_checks()
        return {
            "status": "success",
            "message": "Check cycle initiated",
            "details": {
                "total_checks": orch.state.total_checks_run,
                "last_check": orch.state.last_check_time.isoformat()
                if orch.state.last_check_time
                else None,
            },
        }
    # Fix HTTPExceptions being eaten elsewhere
    except HTTPException:
        # Rethrow HTTPException without logging as error, just pass through
        raise
    except Exception as e:
        logger.error(f"Failed to run checks: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/run_sa_collection")
async def run_sa_collection(current_user: Users = Depends(get_current_user)) -> dict:
    """Manually trigger a full SA collection cycle (NXC + Sliver)."""
    await check_admin(current_user)
    from fastapi_backend.c2.sa_collector import run_all_sa_collection  # cycle-break
    from fastapi_backend.c2.sa_collector_sliver import (
        run_all_sliver_sa_collection,  # cycle-break
    )
    await run_all_sa_collection()
    await run_all_sliver_sa_collection()
    return {"status": "ok", "message": "SA collection triggered"}


@router.post("/start_comp")
async def start_competition(
    current_user: Users = Depends(get_current_user), orch: "CompetitionOrchestrator" = Depends(get_orchestrator)
) -> dict[str, Any]:
    """Start the competition.

    Admin can transition to RUNNING from any allowed predecessor state
    (bypass arm). Non-admin gets 409 if state is not PREFLIGHT_ARMED.
    """
    try:
        await check_admin(current_user)
        # Admin-only bypass: allow PREFLIGHT_ON / NOT_STARTED / STOPPED → RUNNING directly.
        await orch.start_competition()
        return {
            "status": "success",
            "message": "Competition started",
            "details": orch.state.get_status_info(),
        }
    # Fix HTTPExceptions being eaten elsewhere
    except HTTPException:
        # Rethrow HTTPException without logging as error, just pass through
        raise
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Failed to start competition: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


# ---------------------------------------------------------------------------
# Preflight / state-machine endpoints
# ---------------------------------------------------------------------------


class PreflightArmRequest(BaseModel):
    """POST /admin/preflight/arm request body."""

    start_time: str | None = None


def _parse_iso_dt(s: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid ISO datetime: {s!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


@router.post("/preflight/start")
async def preflight_start(
    current_user: Users = Depends(get_current_user),
    orch: "CompetitionOrchestrator" = Depends(get_orchestrator),
) -> dict[str, Any]:
    """STOPPED/NOT_STARTED → PREFLIGHT_ON (admin only)."""
    await check_admin(current_user)
    try:
        await orch.start_preflight()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"status": "success", "state": orch.state.status.value}


@router.post("/preflight/stop")
async def preflight_stop(
    current_user: Users = Depends(get_current_user),
    orch: "CompetitionOrchestrator" = Depends(get_orchestrator),
) -> dict[str, Any]:
    """PREFLIGHT_ON/PREFLIGHT_ARMED → STOPPED (admin only)."""
    await check_admin(current_user)
    try:
        await orch.stop_preflight()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"status": "success", "state": orch.state.status.value}


@router.post("/preflight/arm")
async def preflight_arm(
    body: PreflightArmRequest | None = Body(default=None),
    current_user: Users = Depends(get_current_user),
    orch: "CompetitionOrchestrator" = Depends(get_orchestrator),
) -> dict[str, Any]:
    """PREFLIGHT_ON → PREFLIGHT_ARMED. Body.start_time optional if already set."""
    await check_admin(current_user)

    start_time: dt.datetime | None = None
    if body and body.start_time:
        start_time = _parse_iso_dt(body.start_time)
    else:
        # Fall back to playbook.start_time then to CompetitionConfig.armed_start_time.
        cfg = orch._playbook_config
        if cfg.start_time:
            start_time = cfg.start_time
        else:
            with Session(engine) as db:
                row = db.get(CompetitionConfig, 1)
                if row and row.armed_start_time:
                    start_time = _parse_iso_dt(row.armed_start_time)
    if start_time is None:
        raise HTTPException(
            status_code=422,
            detail="start_time required (no playbook.start_time or armed_start_time present)",
        )

    try:
        await orch.arm_preflight(start_time)
    except ValueError as e:
        # Distinguish validation errors (resume_at <= pause_at) from transition errors.
        msg = str(e)
        if "Invalid state transition" in msg:
            raise HTTPException(status_code=409, detail=msg) from e
        raise HTTPException(status_code=422, detail=msg) from e
    return {
        "status": "success",
        "state": orch.state.status.value,
        "armed_start_time": start_time.isoformat(),
    }


@router.post("/preflight/disarm")
async def preflight_disarm(
    current_user: Users = Depends(get_current_user),
    orch: "CompetitionOrchestrator" = Depends(get_orchestrator),
) -> dict[str, Any]:
    """PREFLIGHT_ARMED → PREFLIGHT_ON; clears armed_start_time + cancels APScheduler job."""
    await check_admin(current_user)
    try:
        await orch.disarm_preflight()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"status": "success", "state": orch.state.status.value}


def _parse_hms_offset(s: str) -> dt.timedelta:
    """Parse HH:MM:SS string into a timedelta."""
    parts = s.strip().split(":")
    if len(parts) != 3:
        raise HTTPException(status_code=422, detail=f"Invalid HH:MM:SS offset: {s!r}")
    try:
        h, m, sec = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid HH:MM:SS offset: {s!r}") from exc
    return dt.timedelta(hours=h, minutes=m, seconds=sec)


class ScheduleUpdateRequest(BaseModel):
    """PATCH /admin/schedule request body. All fields optional."""

    start_time: str | None = None    # ISO datetime override
    end_time: str | None = None      # HH:MM:SS offset or null to clear
    pause_at: str | None = None      # HH:MM:SS offset or null to clear
    resume_at: str | None = None     # HH:MM:SS offset or null to clear
    clear_end: bool = False
    clear_pause: bool = False
    clear_resume: bool = False


@router.patch("/schedule")
async def update_schedule(
    body: ScheduleUpdateRequest,
    current_user: Users = Depends(get_current_user),
    orch: "CompetitionOrchestrator" = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Update CompetitionConfig schedule fields and re-register date jobs.

    Merges with existing values; only fields present in body are changed.
    Does not trigger a state transition.  Requires admin.
    """
    await check_admin(current_user)

    now = dt.datetime.now(dt.UTC)

    # Parse inputs.
    new_start: dt.datetime | None = _parse_iso_dt(body.start_time) if body.start_time else None
    new_end: dt.timedelta | None = _parse_hms_offset(body.end_time) if body.end_time else None
    new_pause: dt.timedelta | None = _parse_hms_offset(body.pause_at) if body.pause_at else None
    new_resume: dt.timedelta | None = _parse_hms_offset(body.resume_at) if body.resume_at else None

    from fastapi_backend.database.write_actor import write_actor  # cycle-break
    cfg = orch._playbook_config
    # Resolve effective offsets outside the actor txn (pure config reads).
    eff_pause_td = new_pause if body.pause_at is not None else (cfg.pause_at if not body.clear_pause else None)
    eff_resume_td = new_resume if body.resume_at is not None else (cfg.resume_at if not body.clear_resume else None)
    eff_end_td = new_end if body.end_time is not None else (cfg.end_time if not body.clear_end else None)
    if eff_pause_td and eff_resume_td and eff_resume_td <= eff_pause_td:
        raise HTTPException(status_code=422, detail="resume_at must be greater than pause_at")

    def _persist(db: Session) -> None:
        row = db.get(CompetitionConfig, 1)
        if row is None:
            row = CompetitionConfig(id=1)
            db.add(row)
        eff_start_local: dt.datetime | None = new_start
        if eff_start_local is None and row.armed_start_time:
            eff_start_local = _parse_iso_dt(row.armed_start_time)
        if new_start:
            row.armed_start_time = new_start.isoformat()
        if eff_start_local:
            row.scheduled_end_time = (eff_start_local + eff_end_td).isoformat() if eff_end_td else None
            row.scheduled_pause_at = (eff_start_local + eff_pause_td).isoformat() if eff_pause_td else None
            row.scheduled_resume_at = (eff_start_local + eff_resume_td).isoformat() if eff_resume_td else None
    await write_actor.submit(_persist, label="update_schedule")

    # Re-read for the response (fresh read Session).
    with Session(engine) as db:
        row = db.get(CompetitionConfig, 1)
        eff_start = new_start if new_start else (_parse_iso_dt(row.armed_start_time) if row and row.armed_start_time else None)

        result = {
            "status": "success",
            "start_time": row.armed_start_time,
            "end_time": row.scheduled_end_time,
            "pause_at": row.scheduled_pause_at,
            "resume_at": row.scheduled_resume_at,
        }

    # Re-register asyncio schedule jobs.
    await orch._cancel_schedule_job("armed_start")
    await orch._cancel_schedule_job("scheduled_end")
    await orch._cancel_schedule_job("scheduled_pause")
    await orch._cancel_schedule_job("scheduled_resume")

    eff_start_for_jobs = new_start
    if eff_start_for_jobs is None:
        # Re-read from DB to get current effective start.
        with Session(engine) as db2:
            row2 = db2.get(CompetitionConfig, 1)
            if row2 and row2.armed_start_time:
                eff_start_for_jobs = _parse_iso_dt(row2.armed_start_time)

    if eff_start_for_jobs:
        plan = [
            ("armed_start", eff_start_for_jobs, "start"),
            ("scheduled_end", (eff_start_for_jobs + eff_end_td) if eff_end_td else None, "end"),
            ("scheduled_pause", (eff_start_for_jobs + eff_pause_td) if eff_pause_td else None, "pause"),
            ("scheduled_resume", (eff_start_for_jobs + eff_resume_td) if eff_resume_td else None, "resume"),
        ]
        for name, at, kind in plan:
            if at is None or (at - now).total_seconds() <= 0:
                continue
            import asyncio  # cycle-break
            task = asyncio.create_task(orch._run_schedule_job(name, (at - now).total_seconds(), kind))
            orch._schedule_jobs()[name] = task

    return result


@router.post("/preflight/purge")
async def preflight_purge(
    confirm: bool = Query(default=False),
    current_user: Users = Depends(get_current_user),
    orch: "CompetitionOrchestrator" = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Delete all preflight=True IOC check rows.

    Requires `?confirm=true` if state is PREFLIGHT_ARMED or RUNNING.
    """
    await check_admin(current_user)
    state = orch.state.status
    if (
        state in (CompetitionStatus.PREFLIGHT_ARMED, CompetitionStatus.RUNNING)
        and not confirm
    ):
        raise HTTPException(status_code=409, detail={"requires_confirm": True})
    n = orch.purge_preflight_rows()
    return {"status": "success", "deleted": n}


@router.post("/rollback")
async def rollback_to_preflight(
    current_user: Users = Depends(get_current_user),
    orch: "CompetitionOrchestrator" = Depends(get_orchestrator),
) -> dict[str, Any]:
    """RUNNING → PREFLIGHT_ON; aborts in-flight cycle + pauses check pipeline."""
    await check_admin(current_user)
    try:
        await orch.rollback_to_preflight()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"status": "success", "state": orch.state.status.value}


@router.get("/preflight/results")
async def preflight_results(
    current_user: Users = Depends(get_current_user),
) -> dict[str, Any]:
    """Return current preflight check-result snapshot (team→box→IOC, Option A filter)."""
    await check_admin(current_user)
    from fastapi_backend.core.preflight import (  # cycle-break
        build_preflight_results,
    )
    return build_preflight_results()


# ---------------------------------------------------------------------------
# Operator activity
# ---------------------------------------------------------------------------


@router.get("/operator-activity")
async def get_operator_activity(
    current_user: Users = Depends(get_current_user),
    orch: "CompetitionOrchestrator" = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Return roster + per-team coverage."""
    await check_admin(current_user)
    from sqlalchemy import func  # cycle-break

    from fastapi_backend.core.operator_activity import (  # cycle-break
        snapshot_coverage,
        snapshot_roster,
    )
    from fastapi_backend.database.models import (  # cycle-break
        Boxes,
        DeployedIOCs,
    )

    with Session(engine) as db:
        # Map box_ip → team_num for collision detection.
        box_team_map = {
            b.box_ip: b.team_num
            for b in db.execute(select(Boxes).where(Boxes.team_num.is_not(None))).scalars().all()
            if b.team_num is not None
        }
        # active IOCs per team
        rows = db.execute(
            select(Boxes.team_num, func.count(DeployedIOCs.id))
            .join(DeployedIOCs, DeployedIOCs.box_ip == Boxes.box_ip)
            .where(DeployedIOCs.status == "active", Boxes.team_num.is_not(None))
            .group_by(Boxes.team_num)
        ).all()
        team_active = {team: int(cnt) for team, cnt in rows}

    # Undeployed = IOCs with deploy_script that have no active row for any
    # box in that team. Counted per team.
    team_undeployed: dict[int, int] = {}
    with Session(engine) as db:
        teams = set(team_active.keys()) | set(box_team_map.values())
        team_boxes: dict[int, list[str]] = {}
        for ip, team in box_team_map.items():
            team_boxes.setdefault(team, []).append(ip)

        active_pairs = {
            (r.box_ip, r.ioc_name)
            for r in db.execute(
                select(DeployedIOCs).where(DeployedIOCs.status == "active")
            ).scalars().all()
        }

    loader = orch.ioc_loader
    deployable = [
        ioc for ioc in loader.ioc_definitions.values()
        if getattr(ioc, "deploy_script", None)
    ]
    for team in teams:
        boxes = team_boxes.get(team, [])
        undeployed = 0
        for ioc in deployable:
            covered = any((ip, ioc.name) in active_pairs for ip in boxes)
            if not covered:
                undeployed += 1
        team_undeployed[team] = undeployed

    return {
        "roster": snapshot_roster(),
        "coverage": snapshot_coverage(box_team_map, team_active, team_undeployed),
    }


@router.post("/stop_comp")
async def stop_competition(
    current_user: Users = Depends(get_current_user), orch: "CompetitionOrchestrator" = Depends(get_orchestrator)
) -> dict[str, Any]:
    """Stop the competition."""
    try:
        await check_admin(current_user)
        await orch.stop_competition()
        return {
            "status": "success",
            "message": "Competition stopped",
            "details": orch.state.get_status_info(),
        }
    # Fix HTTPExceptions being eaten elsewhere
    except HTTPException:
        # Rethrow HTTPException without logging as error, just pass through
        raise
    except Exception as e:
        logger.error(f"Failed to stop competition: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/pause_comp")
async def pause_competition(
    current_user: Users = Depends(get_current_user), orch: "CompetitionOrchestrator" = Depends(get_orchestrator)
) -> dict[str, Any]:
    """Pause the competition."""
    try:
        await check_admin(current_user)
        await orch.pause_competition()
        return {
            "status": "success",
            "message": "Competition paused",
            "details": orch.state.get_status_info(),
        }
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Failed to pause competition: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/unpause_comp")
async def unpause_competition(
    current_user: Users = Depends(get_current_user), orch: "CompetitionOrchestrator" = Depends(get_orchestrator)
) -> dict[str, Any]:
    """Unpause the competition."""
    try:
        await check_admin(current_user)
        await orch.unpause_competition()
        return {
            "status": "success",
            "message": "Competition unpaused",
            "details": orch.state.get_status_info(),
        }
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Failed to unpause competition: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/status")
async def get_status(
    current_user: Users = Depends(get_current_user), orch: "CompetitionOrchestrator" = Depends(get_orchestrator)
) -> dict[str, Any]:
    """Get comprehensive system status.

    docs/PLAN_soak_backend_perf.md §3.3 root cause: `orch.get_status()` calls
    `collect_metrics()` which runs synchronous SQLAlchemy queries per team.
    Under Phase 5 concurrent-race stress the event loop was stalling for
    500ms-10s inside metrics collection (proven by C3R_LOOP_HEARTBEAT
    stack samples, 2026-08-30 stress probe). Off-loading to a thread lets
    the accept loop keep completing TLS handshakes while metrics rolls up.
    """
    import asyncio  # cycle-break
    try:
        await check_admin(current_user)
        return await asyncio.to_thread(orch.get_status)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get status: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/script-token")
async def get_script_token(
    box_ip: str | None = None,
    path: str | None = None,
    current_user: Users = Depends(get_current_user),
) -> dict[str, Any]:
    """Mint a debugging HMAC token for the script server. Admin-only.

    Tokens are bound per (box_ip, path) and have a 1h TTL — supply both
    query params to generate a usable token. Without params, returns a
    descriptive payload explaining the new model.
    """
    await check_admin(current_user)
    from fastapi_backend.core.script_server import script_token_manager  # cycle-break

    if box_ip and path:
        return {
            "token": script_token_manager.sign(box_ip, path),
            "box_ip": box_ip,
            "path": path,
            "ttl_seconds": 3600,
        }
    return {
        "model": "hmac",
        "note": "Tokens are HMAC-signed per (box_ip, path, exp_unix). "
                "Pass ?box_ip=X&path=Y to mint a debugging token.",
    }


# ---------------------------------------------------------------------------
# Runtime-configurable settings
# ---------------------------------------------------------------------------

_MUTABLE_SETTINGS = {
    "CHECK_INTERVAL_MINUTES",
    "SA_COLLECTION_INTERVAL_MINUTES",
    "SA_RESULT_RETENTION_COUNT",
    "DEPLOY_MAX_ATTEMPTS",
    "NXC_MAX_CONCURRENT",
    "NXC_MAX_CONCURRENT_PER_BOX",
    "NXC_PUTFILE_MAX_CONCURRENT_PER_BOX",
    "NXC_PUTFILE_MAX_RETRIES",
    "NXC_PUTFILE_RETRY_BACKOFF_SEC",
    "NXC_PUTFILE_VERIFY_HASH",
    "NXC_PUTFILE_SSH_TIMEOUT",
    "NXC_PUTFILE_SSH_MAX_RETRIES",
    "NXC_PUTFILE_SSH_VERIFY_HASH",
    "NXC_PIVOT_PORTFWD_MIN_INTERVAL_SEC",
    "NXC_PIVOT_PUTFILE_MAX_CONCURRENT",
    "NXC_PIVOT_HTTP_PULL_ENABLED",
    "NXC_PIVOT_HTTP_PULL_MIN_BYTES",
    "NXC_PIVOT_REBUILD_ON_TIMEOUT",
    "SCHEDULER_MAX_CONCURRENT_TASKS",
    "SLOW_REQUEST_LOG_THRESHOLD_SEC",
    "PIVOT_PREWARM_MAX_CONCURRENT_REBUILDS",
    "PIVOT_PREWARM_REPROBE_ATTEMPTS",
    "PIVOT_PREWARM_REPROBE_TOTAL_BUDGET_SEC",
    "PIVOT_PREWARM_REPROBE_PER_ATTEMPT_TIMEOUT_SEC",
    "PIVOT_PREWARM_REPROBE_BASE_DELAY_SEC",
    "PIVOT_KEEPALIVE_ENABLED",
    "PIVOT_KEEPALIVE_INTERVAL_SEC",
    "PIVOT_KEEPALIVE_MAX_CONCURRENT",
    "NXC_ALT_DELIVERY_ENABLED",
    "NXC_WINRM_CHUNK_SIZE",
    "NXC_WINRM_PORT",
    "NXC_WINRM_ALLOW_PTH",
    "NXC_SUPPRESS_READBACK",
    "NXC_SWEEP_ON_SA",
    "BULK_EXEC_MAX_CONCURRENT",
    "BEACON_REDEPLOY_THRESHOLD_MULTIPLIER",
    "BEACON_REDEPLOY_CHECK_INTERVAL_MINUTES",
    "SA_MAX_CONCURRENT_BOXES",
    "CRED_REVALIDATION_INTERVAL_MINUTES",
    "CRED_REVALIDATION_MAX_CONCURRENT",
    "SCORING_CRED_PROBE_MAX_ATTEMPTS",
    "SCORING_CRED_PROBE_RETRY_DELAY_SEC",
    "CRED_HARVEST_ENABLED",
    "HASH_HARVEST_INTERVAL_MINUTES",
    "TICKET_REFRESH_INTERVAL_MINUTES",
    "HASH_HARVEST_MAX_CONCURRENT",
    "TICKET_TTL_HOURS",
    "TICKET_REFRESH_BEFORE_EXPIRY_MIN",
    "KRBTGT_DCSYNC_ENABLED",
    "USE_SCRIPT_WRAPPER",
    "SCRIPT_WRAPPER_TTL_SEC",
    "SCRIPT_WRAPPER_MAX_CACHE_ENTRIES",
    "SCRIPT_WRAPPER_SWEEP_INTERVAL_SEC",
    "SCRIPT_RESULT_GRACE_SEC",
    "USE_SCRIPT_RESULT_CALLBACK",
}


def require_admin(current_user: Users = Depends(get_current_user)) -> Users:
    """FastAPI dep that raises 403 unless the caller is admin."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


@router.get("/settings")
def get_settings(current_user: Users = Depends(require_admin)) -> dict[str, Any]:
    """Return runtime-mutable settings + a few read-only fields."""
    from fastapi_backend.core.config import config  # cycle-break
    result = {k: getattr(config, k) for k in _MUTABLE_SETTINGS}
    result["SLIVER_HOST"] = config.SLIVER_HOST
    return result


@router.patch("/settings")
def patch_settings(body: dict = Body(...), current_user: Users = Depends(require_admin)) -> dict[str, Any]:
    """Update one or more runtime-mutable settings, with type coercion."""
    from fastapi_backend.core.config import config  # cycle-break
    updated = {}
    for key, value in body.items():
        if key not in _MUTABLE_SETTINGS:
            raise HTTPException(status_code=422, detail=f"Setting {key!r} is not configurable at runtime")
        setattr(config, key, type(getattr(config, key))(value))
        updated[key] = getattr(config, key)

    if "NXC_MAX_CONCURRENT" in updated:
        from fastapi_backend.netexec.nxc_semaphore import (
            reset_global_semaphore,  # cycle-break
        )
        reset_global_semaphore(updated["NXC_MAX_CONCURRENT"])

    if "NXC_MAX_CONCURRENT_PER_BOX" in updated or "NXC_PUTFILE_MAX_CONCURRENT_PER_BOX" in updated:
        from fastapi_backend.netexec.nxc_semaphore import (
            reset_per_box_semaphores,  # cycle-break
        )
        reset_per_box_semaphores()

    # Bulk exec + bulk put-file each cache their own concurrency semaphores
    # sized from BULK_EXEC_MAX_CONCURRENT + NXC_MAX_CONCURRENT. Rebuild them so a
    # runtime cap change takes effect without a backend restart.
    if "BULK_EXEC_MAX_CONCURRENT" in updated or "NXC_MAX_CONCURRENT" in updated:
        from fastapi_backend.c2.bulk_exec import (
            reset_bulk_exec_semaphores,  # cycle-break
        )
        from fastapi_backend.c2.bulk_put_file import (
            reset_bulk_put_semaphores,  # cycle-break
        )
        reset_bulk_exec_semaphores()
        reset_bulk_put_semaphores()

    # WrapperCache holds its own hard entry cap; push the new value through.
    if "SCRIPT_WRAPPER_MAX_CACHE_ENTRIES" in updated:
        from fastapi_backend.netexec.script_wrapper import (
            get_wrapper_cache,  # cycle-break
        )
        get_wrapper_cache().set_max_entries(updated["SCRIPT_WRAPPER_MAX_CACHE_ENTRIES"])

    return {"updated": updated}


@router.post("/playbook/reload")
async def reload_playbook(
    body: dict = Body(...),
    current_user: Users = Depends(require_admin),
    orch: "CompetitionOrchestrator" = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Hot-reload playbook YAML.

    Body shapes:
      - ``{"yaml": "<raw yaml string>"}`` — writes to config/playbook.yml then reloads.
      - ``{}`` — reloads from disk without writing (re-read after manual edit).

    Re-registers playbook jobs if competition is already running with a known
    ``competition_start_at`` (future-only entries; past-due ones logged).
    """
    from fastapi_backend.core.config import config  # cycle-break
    from fastapi_backend.core.playbook_loader import (  # cycle-break
        PlaybookValidationError,
        load_playbook,
    )
    from fastapi_backend.core.playbook_scheduler import (  # cycle-break
        clear_playbook_jobs,
        register_playbook_jobs,
    )

    playbook_path = config.PROJECT_ROOT / "config" / "playbook.yml"
    if "yaml" in body and body["yaml"] is not None:
        playbook_path.write_text(body["yaml"])

    try:
        cfg = load_playbook(playbook_path)
    except PlaybookValidationError as exc:
        raise HTTPException(status_code=422, detail=f"Playbook validation error: {exc}") from exc

    orch._playbook_config = cfg
    orch._playbook_entries = cfg.entries

    re_registered = 0
    start_at = getattr(orch.state, "start_time", None)
    if start_at is not None:
        clear_playbook_jobs()
        re_registered = register_playbook_jobs(cfg.entries, start_at)

    return {
        "entries_loaded": len(cfg.entries),
        "re_registered": re_registered,
        "competition_start_at": start_at.isoformat() if start_at else None,
    }


@router.post("/reset_data")
async def reset_data(
    current_user: Users = Depends(get_current_user), orch: "CompetitionOrchestrator" = Depends(get_orchestrator)
) -> dict[str, Any]:
    """Reset competition data (clear scores and checks)."""
    try:
        await check_admin(current_user)
        active = orch.state.is_active()
        if active:
            raise HTTPException(
                status_code=400, detail="Cannot reset data while competition is running"
            )

        # Reset state
        orch.state.reset()

        # Clear database (keeping teams and users)
        from sqlmodel import Session, delete, update  # cycle-break

        from fastapi_backend.database.models import (  # cycle-break
            BlueTeams,
            CheckInstance,
            DeployedIOCs,
            IOCCheckResult,
        )
        from fastapi_backend.database.write_actor import write_actor  # cycle-break
        def _reset(db: Session) -> None:
            db.execute(delete(IOCCheckResult))
            db.execute(delete(CheckInstance))
            db.execute(delete(DeployedIOCs))
            db.execute(update(BlueTeams).values(last_check_id=None))
        write_actor.submit_sync(_reset, label="reset_data")

        return {"status": "success", "message": "Competition data reset successfully"}

    # Fix HTTPExceptions being eaten elsewhere
    except HTTPException:
        # Rethrow HTTPException without logging as error, just pass through
        raise

    except Exception as e:
        logger.error(f"Failed to reset data: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/reload_inventory")
async def reload_inventory(
    current_user: Users = Depends(get_current_user), orch: "CompetitionOrchestrator" = Depends(get_orchestrator)
) -> dict[str, Any]:
    """Re-read INVENTORY_FILE and resync Boxes/creds/teams without a backend restart."""
    try:
        await check_admin(current_user)
        await orch.reload_inventory()
        return {
            "status": "success",
            "num_teams": orch.state.num_teams,
            "num_boxes": orch.state.num_boxes,
        }
    except HTTPException:
        raise
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Failed to reload inventory: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/cred-harvest/ticket-cycle")
async def run_ticket_refresh_cycle(
    req: TicketCycleRequest | None = Body(default=None),
    current_user: Users = Depends(get_current_user),
    orch: "CompetitionOrchestrator" = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Run ONE TicketRefreshLoop pass on demand: DCSync krbtgt, then forge/probe.

    Normally this work is done by the background ``TicketRefreshLoop``, which
    ``lifespan_helpers`` only starts when ``CRED_HARVEST_ENABLED`` is true. The test
    backends deliberately set it false — the background bare-auth flood is the proven
    source of the win2 NETBIOS timeouts (docs/PLAN_cred_loop_efficiency.md) — which
    left NO reachable code path that DCSyncs krbtgt. ``POST /c2/forge-ticket`` then
    always 400s ("DC has no domain or domain_sid yet"), so the ticket e2e tests could
    only ever skip, with a misleading "DC may not be domain-joined" reason.

    This runs the same methods the loop calls, once, for the requested teams — the
    on-demand counterpart to the periodic loop, with none of the background flood.
    Deliberately NOT gated on CRED_HARVEST_ENABLED: an explicit operator/test request
    is not the background loop.
    """
    from fastapi_backend.core.config import config  # cycle-break

    await check_admin(current_user)
    req = req or TicketCycleRequest()

    if config.DEV_MODE:
        return {"status": "ok", "teams": [], "message": "[DEV] ticket cycle stubbed"}
    if not orch.executor or not getattr(orch.executor, "nxc_wrapper", None):
        raise HTTPException(status_code=503, detail="NXC executor not ready")

    from fastapi_backend.c2.cred_harvester import TicketRefreshLoop  # cycle-break
    from fastapi_backend.database.models import Boxes  # cycle-break

    with Session(engine) as db:
        stmt = select(Boxes).where(Boxes.is_domain_controller.is_(True))
        if req.team_nums:
            stmt = stmt.where(Boxes.team_num.in_(req.team_nums))
        dcs = [
            (b.team_num, b.box_ip, b.domain)
            for b in db.execute(stmt).scalars().all()
            if b.team_num is not None
        ]
    if not dcs:
        raise HTTPException(
            status_code=404,
            detail=f"no domain controller found for teams={req.team_nums or 'all'}",
        )

    loop = TicketRefreshLoop(orch.executor.nxc_wrapper)
    results: list[dict[str, Any]] = []
    for team_num, dc_ip, domain in dcs:
        entry: dict[str, Any] = {"team_num": team_num, "dc_ip": dc_ip}
        try:
            if config.KRBTGT_DCSYNC_ENABLED:
                await loop._ensure_krbtgt(team_num, dc_ip, domain)
            await loop._ensure_team_ticket(team_num, dc_ip, domain)
            await loop._probe_team_ticket(team_num, dc_ip)
            entry["status"] = "ok"
        except Exception as exc:  # one bad DC must not abort the rest
            logger.error("ticket-cycle failed for team %s (%s): %s", team_num, dc_ip, exc)
            entry["status"] = "failed"
            entry["error"] = str(exc)
        results.append(entry)

    return {"status": "ok", "teams": results}
