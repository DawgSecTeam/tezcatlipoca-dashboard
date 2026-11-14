"""IOC deployment registration API.

Provides endpoints for registering, listing, and deregistering deployed IOCs.
The check pipeline (CheckManager._queue_all_checks) only checks IOCs that have
an active DeployedIOCs row, so deployment registration is required before scoring.

Routes:
  POST   /c2/iocs/deploy                  — Register a deployed IOC (one or more boxes)
  POST   /c2/iocs/deploy-and-register     — Run effect deploy script via NXC then register
  GET    /c2/iocs/deployed                — List all active DeployedIOC records
  GET    /c2/iocs/deployed/{id}           — Get single deployed IOC + latest check result
  DELETE /c2/iocs/deployed/{id}           — Deregister (set status=failed)
"""

import asyncio
import datetime as dt
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from fastapi_backend.core.config import config
from fastapi_backend.core.deploy_retry import (
    mark_deploy_failed,
    mark_deploy_succeeded,
    with_deploy_retries,
)
from fastapi_backend.core.ioc_definition import IOCDefinitionLoader
from fastapi_backend.core.task_tracker import (
    create_task,
    finish_task,
    start_task,
    update_box_progress,
    update_task_metadata,
)
from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import (
    Credentials,
    DeployedIOCs,
    ImplantBuilds,
    IOCCheckResult,
    assign_blue_team_label,
)
from fastapi_backend.database.write_actor import write_actor
from fastapi_backend.netexec.check_runner import _parse_check_output
from fastapi_backend.netexec.script_result import (
    await_callback_or_none,
    await_callback_result,
)
from fastapi_backend.netexec.script_wrapper import build_inline_raw_command
from fastapi_backend.netexec.wrapper import (
    build_inline_command,
    protocol_for_box,
    remote_temp_path,
)
from fastapi_backend.utils.asyncio_tasks import fire_and_forget
from fastapi_backend.utils.auth import Users, get_current_user
from fastapi_backend.utils.exceptions import cred_not_found
from fastapi_backend.utils.nxc_routing import (
    _get_best_cred,
    is_domain_cred,
    resolve_nxc_target,
)
from iocs.base import SliverCheck

if TYPE_CHECKING:
    from fastapi_backend.netexec.wrapper import NXCWrapper  # lazy-load

router = APIRouter(prefix="/c2/iocs", tags=["ioc-deploy"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hiding-library config helpers
# ---------------------------------------------------------------------------

def _resolve_hiding_entry_value(entry: Any, params: dict | None) -> str | None:
    """Resolve a HidingEntry to its concrete value at deploy time.

    value_literal  → returned as-is.
    value_env_key  → looks up params[key]; falls back to value_default when key absent.
    value_default  → used as fallback for env_key misses (no pool pick / standalone deploy).
    """
    lit = getattr(entry, "value_literal", None)
    if lit:
        return lit
    key = getattr(entry, "value_env_key", None)
    if key:
        if params:
            val = params.get(key)
            if val:
                return val
        return getattr(entry, "value_default", None)
    return None


def _add_hiding_entries(ioc: Any, deployed_id: int, box_ip: str, params: dict | None) -> int:
    """Insert HidingLibraryConfig rows for each wants_hiding entry on this IOC.

    Returns count of rows inserted.  Idempotent: duplicate (box_ip, entry_type,
    entry_value, source_deployed_ioc_id) combinations are ignored via try/except.
    """
    from fastapi_backend.database.models import HidingLibraryConfig  # cycle-break

    entries = getattr(ioc, "wants_hiding", None) or []
    if not entries:
        return 0
    inserted = 0
    for entry in entries:
        value = _resolve_hiding_entry_value(entry, params)
        if not value:
            logger.warning(
                "wants_hiding entry on %s has no resolvable value (env_key=%s, params=%s)",
                ioc.name, getattr(entry, "value_env_key", None), params,
            )
            continue

        def _write(db: Session, _value: str = value, _entry: Any = entry) -> None:
            db.add(HidingLibraryConfig(
                box_ip=box_ip,
                os=ioc.os,
                entry_type=_entry.type,
                entry_value=_value,
                source_ioc_name=ioc.name,
                source_deployed_ioc_id=deployed_id,
            ))

        # One short actor transaction per entry so the unique-constraint dedup
        # (duplicate box_ip/entry_type/entry_value/source_deployed_ioc_id) stays
        # per-row: a duplicate rolls back only its own insert instead of failing
        # the whole batch. Same idempotent skip the old per-entry commit gave.
        try:
            write_actor.submit_sync(_write, label="add_hiding_entry")
            inserted += 1
        except Exception:
            logger.debug("hiding entry already present (dedup) for %s on %s", ioc.name, box_ip)
    return inserted


_HIDE_LIB_IOC_NAMES = frozenset({
    "linux_hide_library",
    "firewall_hide_library",
    "windows_hide_library",
})


def _inject_hide_lib_self_protect(
    ioc: Any,
    deployed_id: int,
    box_ip: str,
    deploy_params: dict | None,
) -> int:
    """Auto-inject self-protection HidingLibraryConfig rows for hide-lib IOC deploys.

    Linux/FreeBSD: file_path for the .so basename → hides it from ls in deploy dir.
    Windows: file_path for DLL basename + reg_value for AppCertDLLs value name.
    The preload/libmap line-filter is always-on in the C source (driven by g_so_path);
    no blob entry needed for it — only the deploy-dir file listing needs a blob entry.
    """
    if ioc.name not in _HIDE_LIB_IOC_NAMES:
        return 0
    from fastapi_backend.database.models import HidingLibraryConfig  # cycle-break

    entries_to_add: list[tuple[str, str]] = []
    params = deploy_params or {}

    if getattr(ioc, "os", None) in ("linux", "firewall"):
        so_name = params.get("SO_NAME", "")
        if so_name:
            entries_to_add.append(("file_path", so_name))
    elif getattr(ioc, "os", None) == "windows":
        dll_name = params.get("DLL_NAME", "")
        certdll_value = params.get("CERTDLL_VALUE", "")
        if dll_name:
            entries_to_add.append(("file_path", dll_name))
        if certdll_value:
            entries_to_add.append(("reg_value", certdll_value))

    if not entries_to_add:
        return 0

    inserted = 0
    for etype, value in entries_to_add:

        def _write(db: Session, _etype: str = etype, _value: str = value) -> None:
            db.add(HidingLibraryConfig(
                box_ip=box_ip,
                os=getattr(ioc, "os", ""),
                entry_type=_etype,
                entry_value=_value,
                source_ioc_name=ioc.name,
                source_deployed_ioc_id=deployed_id,
            ))

        # Per-entry actor transaction: a duplicate (unique constraint) rolls back
        # just its own insert, matching the old per-entry commit's idempotent skip.
        try:
            write_actor.submit_sync(_write, label="inject_hide_lib_self_protect")
            inserted += 1
        except Exception:
            logger.debug("self-protect entry already present (dedup) for %s on %s", ioc.name, box_ip)
    if inserted:
        logger.info(
            "Injected %d self-protect entries for %s on %s", inserted, ioc.name, box_ip,
        )
    return inserted


async def _push_hiding_config_async(
    box_ip: str,
    os_type: str,
    wrapper: "NXCWrapper",
    cred_info: dict,
    use_ps: bool,
) -> None:
    """Push the current XOR-encrypted hiding config blob to the box via NXC.

    Queries all HidingLibraryConfig rows for this box, builds the blob, and
    executes the push command.  No-op if there are no entries.
    """
    from fastapi_backend.core.hide_config import get_push_cmd  # cycle-break
    from fastapi_backend.database.models import HidingLibraryConfig  # cycle-break

    with Session(engine) as db:
        rows = db.execute(
            select(HidingLibraryConfig).where(
                HidingLibraryConfig.box_ip == box_ip,
            )
        ).scalars().all()
        entries = [{"type": r.entry_type, "value": r.entry_value} for r in rows]

    if not entries:
        return

    push_cmd = get_push_cmd(os_type, entries)
    cmd = build_inline_raw_command(push_cmd, os_type)
    is_smb = cred_info["routed_proto"] == "smb"
    await wrapper.execute(  # nxc-wrapper-bypass: wrapper-delivered
        cred_info["routed_host"], cred_info["routed_proto"],
        cred_info["username"], cred_info["password"], cmd,
        powershell=use_ps, port=cred_info["port"],
        exec_method="smbexec" if is_smb else None,
    )
    logger.info("Pushed hiding config (%d entries) to %s", len(entries), box_ip)


# NOTE: the deploy-dir injection was generalized into
# `c2/deploy_params.py::with_params` (records a distinct pool dir on the
# DeployedIOCs row + injects it, per docs/PLAN_recorded_deploy_dirs.md). The old
# single-purpose `_with_drop_dir` (random pick, no record) was removed.


def _check_non_blue(current_user: Users = Depends(get_current_user)) -> Users:
    if current_user.is_blue_team:
        raise HTTPException(status_code=403, detail="Blue team cannot access deployment endpoints")
    return current_user


def _register_added_credentials(
    db: Session, ioc_def: Any, box_ip: str, deployed_id: int,
    deploy_params: dict | None = None,
) -> int:
    """INSERT a Credentials row per ioc_def.added_credentials entry, linked to deployed_id.

    ``deploy_params`` is the pool-resolved dict from ``resolve_deploy_params`` (Part 2,
    ``docs/PLAN_build_check_deploy_name_pools.md`` §5B.3). When the IOC pools a "user"-kind
    name (e.g. ``pooled_names=[("USER_NAME", "user")]``), the deploy/check/remediate scripts
    create/look up an account whose name is that pooled value, not ``spec.username`` — so the
    registered credential must use the SAME resolved value or an operator relying on it to log
    in gets the wrong (unpooled-default) username. Falls back to ``spec.username`` when no pool
    pick was made (pool exhausted/unconfigured) or the IOC has no "user"-kind pooled name.

    If a row already exists for (box_ip, protocol, username) we leave it alone.
    Returns count of new rows inserted.
    """
    specs = getattr(ioc_def, "added_credentials", None) or []
    if not specs:
        return 0
    user_pool_value = None
    for env_key, pool_kind in getattr(ioc_def, "pooled_names", None) or []:
        if pool_kind == "user" and deploy_params and deploy_params.get(env_key):
            user_pool_value = deploy_params[env_key]
            break
    if user_pool_value is None:
        for env_key, pool_kind, _count in getattr(ioc_def, "pooled_name_lists", None) or []:
            if pool_kind == "user" and deploy_params and deploy_params.get(env_key):
                # pooled_name_lists gives a space-separated list of N names; use the first
                names = deploy_params[env_key].split()
                if names:
                    user_pool_value = names[0]
                break
    inserted = 0
    for spec in specs:
        username = user_pool_value or spec.username
        existing = db.execute(
            select(Credentials).where(
                Credentials.box_ip == box_ip,
                Credentials.protocol == spec.protocol,
                Credentials.username == username,
            )
        ).scalar_one_or_none()
        if existing:
            continue
        db.add(  # db-actor-exempt: caller-transaction fragment — every caller of _register_added_credentials passes a Session opened INSIDE write_actor.submit; see routes/ioc_deploy.py _write / _persist_deployed_ioc
            Credentials(
                username=username,
                password=spec.password,
                box_ip=box_ip,
                protocol=spec.protocol,
                is_default=False,
                valid=None,
                source_deployed_ioc_id=deployed_id,
            )
        )
        inserted += 1
    # No commit here: this helper only ever runs against a caller-supplied
    # Session that belongs to the write-actor transaction (via _persist_deployed_ioc
    # / _register_creds_for). The actor commits once the enclosing _write returns.
    return inserted


def _persist_deployed_ioc(
    ioc: Any, box_ip: str, ioc_name: str, persistence_mechanism: str | None,
    implant_identifier: str | None, deployed_by: str,
    source: str = "manual",
    deploy_params: dict | None = None,
    register_creds: bool = True,
) -> tuple[int, str, int]:
    """Create a DeployedIOCs row and register any added credentials.

    ``deploy_params`` records the pool-chosen artifact attributes (Part 1:
    ``{"DROP_DIR": ...}``) so check/remediate inject the same values back
    (docs/PLAN_recorded_deploy_dirs.md). Returns (deployed_ioc_id,
    blue_team_label, creds_added).

    ``register_creds=False`` creates the row ONLY. Used by the record-before-act
    path (docs/PLAN_failed_deploy_redeploy.md D2), which needs the row — and its
    pooled names — to exist before the first on-box write so a failure is
    attributable and redeployable. Credentials must NOT be registered then: the
    account the IOC creates does not exist until the deploy actually runs, and
    handing operators a credential for a box where it was never created is worse
    than having none. The caller registers them after a successful deploy.
    """
    def _write(db: Session) -> tuple[int, str, int]:
        label = assign_blue_team_label(db, ioc.os, ioc.difficulty)
        record = DeployedIOCs(
            box_ip=box_ip,
            ioc_name=ioc_name,
            persistence_mechanism=persistence_mechanism,
            implant_identifier=implant_identifier,
            blue_team_label=label,
            deployed_at=dt.datetime.now(dt.UTC),
            deployed_by=deployed_by,
            status="active",
            source=source,
            difficulty=getattr(ioc, "difficulty", None),
        )
        record.set_params(deploy_params)
        db.add(record)
        # flush (not commit) populates the autoincrement id inside the actor
        # transaction; the actor commits once _write returns. Credential
        # registration must run in the SAME transaction so the row + its creds
        # land atomically.
        db.flush()
        deployed_id = record.id
        creds_added = (
            _register_added_credentials(db, ioc, box_ip, deployed_id, deploy_params)
            if register_creds else 0
        )
        return deployed_id, label, creds_added

    return write_actor.submit_sync(_write, label="persist_deployed_ioc")


def _register_creds_for(ioc: Any, box_ip: str, deployed_id: int, params: dict | None) -> int:
    """Register an IOC's added_credentials against an existing row. Returns count.

    Split out of ``_persist_deployed_ioc`` so the record-before-act path can
    create the row first and register credentials only once the deploy has
    actually landed.
    """
    def _write(db: Session) -> int:
        return _register_added_credentials(db, ioc, box_ip, deployed_id, params)

    return write_actor.submit_sync(_write, label="register_creds_for")


def _get_ioc_loader() -> IOCDefinitionLoader:
    from fastapi_backend.main import orchestrator  # cycle-break
    if orchestrator and orchestrator.ioc_loader:
        # `orchestrator.ioc_loader` being truthy means the LOADER exists, not that
        # it holds any definitions. An orchestrator whose loader has not loaded yet
        # was handed back empty, and callers that iterate `ioc_definitions` then did
        # nothing while reporting success: run_build_all reported
        # total_iocs=0/built=0 as a clean BUILD, CHECK then called every artifact
        # missing, and DEPLOY-ALL skipped them as 'not built'. Populate on the way
        # out, the same as the fallback path immediately below.
        loader = orchestrator.ioc_loader
        if not loader.ioc_definitions:
            loader.load_ioc_definitions()
        return loader
    loader = IOCDefinitionLoader()
    if not loader.ioc_definitions:
        loader.load_ioc_definitions()
    return loader


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class IOCDeployRequest(BaseModel):
    """POST /c2/iocs/deploy request body — register already-deployed IOCs."""

    ioc_name: str
    box_ips: list[str]
    persistence_mechanism: str | None = None
    implant_identifier: str | None = None
    deployed_by: str | None = None
    # Phase 3 — operator-level override for IOC.timestomp.  True forces stomp
    # even when the IOC dataclass has timestomp=False; respects timestomp_paths
    # from the IOC definition.  False = honour the IOC default.
    timestomp: bool = False
    # When provided (even as {}), skip auto pool-param resolution and use these
    # params as-is.  None (default) = auto-resolve from naming_pools.yml.
    # Lifecycle tests pass {} so the check pipeline uses script defaults instead
    # of a pool-picked name that differs from what the test actually deployed.
    deploy_params: dict | None = None


class IOCDeployAndRegisterRequest(BaseModel):
    """POST /c2/iocs/deploy-and-register — execute deploy then register."""

    ioc_name: str
    box_ips: list[str]
    persistence_mechanism: str | None = None
    implant_identifier: str | None = None
    deployed_by: str | None = None
    cred_id: int | None = None
    timestomp: bool = False


def _get_nxc_wrapper_lazy() -> "NXCWrapper":
    from fastapi_backend.utils.dependencies import get_nxc_wrapper  # cycle-break
    return get_nxc_wrapper()


def resolve_box_creds(
    box_ips: list[str],
    protocol: str,
    *,
    cred_id: int | None = None,
    strict: bool = True,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Resolve NXC cred + routed target per box for deploy-and-register paths.

    Returns ``(box_creds, unresolved_ips)`` where ``box_creds[ip]`` carries
    ``username``/``password``/``fallback_username``/``fallback_password``/
    ``routed_host``/``routed_proto``/``port``. The fallback fields are set
    (from ``InventoryManager``'s local admin credential) only when the
    resolved credential is domain-qualified — see PLAN_domain_auth_fallback.md.

    ``strict=True`` (synchronous route callers): raise ``HTTPException`` on the
    first box with no usable credential, or on a ``cred_id``/box mismatch —
    ``unresolved`` is then always empty. ``strict=False`` (the Deploy-All
    orchestrator): missing-cred boxes are collected into ``unresolved`` instead
    of raising, so the run proceeds on the boxes that do have creds. A bad
    ``cred_id`` still raises in both modes (operator error, not per-box jitter).
    """
    box_creds: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []
    with Session(engine) as db:
        for box_ip in box_ips:
            if cred_id is not None:
                cred = db.get(Credentials, cred_id)
                if not cred:
                    raise cred_not_found(cred_id)
                if cred.box_ip != box_ip:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Credential {cred_id} is for {cred.box_ip}, not {box_ip}",
                    )
            else:
                # IOC deploy/remediate authenticates with the red_scoring infra
                # cred only (when enabled); operator default creds are excluded.
                cred = _get_best_cred(
                    box_ip, db, protocol=protocol, include_invalid=True,
                    scoring_only=config.RED_SCORING_ENABLED,
                )
            if not cred:
                if strict:
                    raise HTTPException(
                        status_code=400, detail=f"No credential found for {box_ip}"
                    )
                unresolved.append(box_ip)
                continue
            routed_host, routed_proto, port = resolve_nxc_target(box_ip, protocol, db)
            # Domain-auth fallback (PLAN_domain_auth_fallback.md): when the
            # resolved cred is domain-qualified, also resolve the box's local
            # admin cred from inventory so _deploy_one can retry with it if
            # the domain attempt produces no output.
            fallback_username = fallback_password = None
            if is_domain_cred(cred.username):
                from fastapi_backend.utils.dependencies import (
                    get_inventory,  # cycle-break, lazy
                )

                local_box = get_inventory().get_box_by_ip(box_ip)
                if local_box:
                    fallback_username, fallback_password = local_box.username, local_box.password
            box_creds[box_ip] = {
                "username": cred.username,
                "password": cred.password,
                "fallback_username": fallback_username,
                "fallback_password": fallback_password,
                "routed_host": routed_host,
                "routed_proto": routed_proto,
                "port": port,
            }
    return box_creds, unresolved


# ---------------------------------------------------------------------------
# POST /deploy — register (no NXC call)
# ---------------------------------------------------------------------------

@router.post("/deploy", status_code=202, response_model=dict[str, Any])
async def deploy_ioc(
    req: IOCDeployRequest,
    current_user: Users = Depends(_check_non_blue),
    loader: IOCDefinitionLoader = Depends(_get_ioc_loader),
) -> dict[str, Any]:
    """Register deployed IOCs on one or more boxes (no NXC — operator ran manually).

    Returns {task_id} immediately (202). Progress via GET /c2/tasks/{task_id}.
    Per-box details JSON includes deployed_ioc_id and blue_team_label on success.
    """
    ioc = loader.ioc_definitions.get(req.ioc_name)
    if not ioc:
        raise HTTPException(status_code=404, detail=f"IOC '{req.ioc_name}' not found")
    if not req.box_ips:
        raise HTTPException(status_code=422, detail="box_ips must not be empty")

    deployed_by = req.deployed_by or current_user.username
    task_id = create_task(
        task_type="deploy",
        operator=current_user.username,
        team_num=None,
        target_boxes=req.box_ips,
        metadata={"ioc_name": req.ioc_name},
    )

    fire_and_forget(_bg_deploy_ioc(
        task_id=task_id,
        ioc=ioc,
        box_ips=req.box_ips,
        ioc_name=req.ioc_name,
        persistence_mechanism=req.persistence_mechanism,
        implant_identifier=req.implant_identifier,
        deployed_by=deployed_by,
        caller_deploy_params=req.deploy_params,
    ))

    return {"task_id": task_id}


async def _bg_deploy_ioc(
    task_id: int,
    ioc: Any,
    box_ips: list[str],
    ioc_name: str,
    persistence_mechanism: str | None,
    implant_identifier: str | None,
    deployed_by: str,
    caller_deploy_params: dict | None = None,
) -> None:
    """Background: register IOC rows for each box, update task progress."""
    start_task(task_id)
    failed_ips: list[str] = []

    async def _register_one(box_ip: str) -> None:
        update_box_progress(task_id, box_ip, "running")
        try:
            if caller_deploy_params is not None:
                # Caller supplied explicit params (even {}) — use them verbatim
                # so the check pipeline uses the same names the caller deployed
                # with (e.g. lifecycle tests deploy with script defaults; passing
                # {} here ensures the check also uses those defaults instead of
                # a pool-picked name that doesn't match).
                params = caller_deploy_params
            else:
                # Resolve pool params (C3R_USER_NAME, C3R_DROP_DIR, etc.) so the
                # DeployedIOCs row records the same names the check pipeline will
                # inject via params_env_prefix(). Without this, a register-only
                # call leaves deploy_params=NULL and the check uses pool-picked names
                # that don't match what the operator actually deployed.
                from fastapi_backend.c2.deploy_params import (  # cycle-break
                    build_ioc_pooled_map,
                    cross_ioc_active_kind_names,
                    existing_active_dirs,
                    existing_active_names,
                    resolve_deploy_params,
                )
                _name_keys = [k for k, _kind in (getattr(ioc, "pooled_names", None) or [])]
                with Session(engine) as _pdb:
                    _exclude = existing_active_dirs(_pdb, box_ip, ioc_name)
                    _exclude_names = (
                        existing_active_names(_pdb, box_ip, ioc_name, _name_keys)
                        if _name_keys else None
                    ) or {}
                    # Cross-IOC per-kind exclusion — no two IOCs on the same
                    # box draw the same value from the same pool kind (service,
                    # user, wmi, so, comm, ...). Prevents svc-name collision
                    # class from project_pool_name_svc_collision.
                    _pooled_map = build_ioc_pooled_map(
                        _get_ioc_loader().ioc_definitions,
                    )
                    _cross = cross_ioc_active_kind_names(_pdb, box_ip, ioc, _pooled_map)
                    for _k, _vs in _cross.items():
                        _exclude_names.setdefault(_k, set()).update(_vs)
                params = resolve_deploy_params(
                    ioc, ioc.os, exclude_dirs=_exclude,
                    exclude_names=(_exclude_names or None),
                )
            deployed_id, label, creds_added = _persist_deployed_ioc(
                ioc, box_ip, ioc_name, persistence_mechanism, implant_identifier, deployed_by,
                deploy_params=params,
            )
            update_box_progress(
                task_id, box_ip, "done",
                details=json.dumps({"deployed_ioc_id": deployed_id, "blue_team_label": label, "credentials_registered": creds_added}),
            )
            logger.info(
                "Registered DeployedIOC: %s on %s (id=%d, label=%s, creds_added=%d)",
                ioc_name, box_ip, deployed_id, label, creds_added,
            )
        except Exception as exc:
            failed_ips.append(box_ip)
            update_box_progress(task_id, box_ip, "failed", error=str(exc))
            logger.exception("_bg_deploy_ioc failed for %s", box_ip)

    await asyncio.gather(*[_register_one(ip) for ip in box_ips])
    finish_task(task_id, "failed" if failed_ips else "done")


# ---------------------------------------------------------------------------
# POST /deploy-and-register — run NXC then register
# ---------------------------------------------------------------------------

@router.post("/deploy-and-register", status_code=202, response_model=dict[str, Any])
async def deploy_and_register_ioc(
    req: IOCDeployAndRegisterRequest,
    current_user: Users = Depends(_check_non_blue),
    loader: IOCDefinitionLoader = Depends(_get_ioc_loader),
) -> dict[str, Any]:
    """Run an IOC's effect deploy script via NXC against target boxes, then register on success.

    Returns {task_id} immediately (202). Progress via GET /c2/tasks/{task_id}.
    """
    ioc = loader.ioc_definitions.get(req.ioc_name)
    if not ioc:
        raise HTTPException(status_code=404, detail=f"IOC '{req.ioc_name}' not found")

    if getattr(ioc, "sliver_inject", False):
        raise HTTPException(
            status_code=400,
            detail=f"IOC '{req.ioc_name}' uses inject endpoint — use POST /c2/sessions/{{session_id}}/inject-shellcode",
        )

    if isinstance(ioc.check, SliverCheck):
        raise HTTPException(
            status_code=400,
            detail="Use the Generation page to deploy Sliver implants",
        )

    if not ioc.deploy_script:
        raise HTTPException(
            status_code=400,
            detail=f"IOC '{req.ioc_name}' has no effect deploy_script",
        )

    if not req.box_ips:
        raise HTTPException(status_code=422, detail="box_ips must not be empty")

    deployed_by = req.deployed_by or current_user.username
    protocol, use_ps = protocol_for_box(ioc.os)

    # Resolve credentials per box before dispatching background task.
    # Validation errors (missing cred, wrong box) returned synchronously.
    box_creds, _ = resolve_box_creds(req.box_ips, protocol, cred_id=req.cred_id, strict=True)

    task_id = create_task(
        task_type="deploy",
        operator=current_user.username,
        team_num=None,
        target_boxes=req.box_ips,
        metadata={"ioc_name": req.ioc_name},
    )

    fire_and_forget(_bg_deploy_and_register(
        task_id=task_id,
        ioc=ioc,
        box_ips=req.box_ips,
        ioc_name=req.ioc_name,
        persistence_mechanism=req.persistence_mechanism,
        implant_identifier=req.implant_identifier,
        deployed_by=deployed_by,
        box_creds=box_creds,
        protocol=protocol,
        use_ps=use_ps,
        timestomp=req.timestomp,
    ))

    return {"task_id": task_id}


async def _bg_deploy_and_register(
    task_id: int,
    ioc: Any,
    box_ips: list[str],
    ioc_name: str,
    persistence_mechanism: str | None,
    implant_identifier: str | None,
    deployed_by: str,
    box_creds: dict[str, Any],
    protocol: str,
    use_ps: bool,
    timestomp: bool = False,
    reuse_params: dict | None = None,
    redeploy_of: int | None = None,
) -> None:
    """Background: run NXC deploy script on each box, register on success.

    ``reuse_params`` — a REDEPLOY passes the failed row's recorded pool values so
    the retry lands on the same dirs/names (never re-picked; see _deploy_one).
    ``redeploy_of`` — the DeployedIOCs row being retried, returned to ``active``
    on success so it leaves the redeploy list and re-enters scoring.
    """
    start_task(task_id)
    failed_ips: list[str] = []
    # box_ip -> DeployedIOCs.id created by record-before-act, and the last error
    # seen for that box. The retry wrapper needs both AFTER the attempts are
    # spent, to mark the row deploy_failed exactly once with a real message.
    _row_ids: dict[str, int] = {}
    _last_error: dict[str, str] = {}
    # Phase 3 — aggregate stomp stats across boxes; lands on
    # OperatorTask.metadata_json.timestomp once all per-box dispatches finish.
    _stomp_agg: dict[str, dict[str, Any]] = {}

    if config.DEV_MODE:
        for box_ip in box_ips:
            update_box_progress(task_id, box_ip, "done",
                                details=json.dumps({"deployed_ioc_id": -1, "blue_team_label": "DEV_MODE"}))
        finish_task(task_id, "done")
        return

    wrapper = _get_nxc_wrapper_lazy()
    deploy_script_path = config.PROJECT_ROOT / "iocs" / ioc.deploy_script

    async def _deploy_one(box_ip: str) -> None:
        # ADD-LOCATION: pick a pool dir not already occupied by an active row for
        # this (box, ioc), record it on the new row, and inject it so the check /
        # remediate scripts drop/detect/remove in the same place
        # (docs/PLAN_recorded_deploy_dirs.md §5.3). Pool exhausted → per-box skip.
        from fastapi_backend.c2.deploy_params import (  # cycle-break
            build_ioc_pooled_map,
            cross_ioc_active_kind_names,
            existing_active_dirs,
            existing_active_names,
            os_key,
            params_env_dict,
            probe_dir_subdirs,
            resolve_deploy_params,
        )
        if reuse_params is not None:
            # REDEPLOY of a deploy_failed row: reuse its RECORDED pooled names
            # verbatim, never re-pick from the pool. A failed attempt may already
            # have planted artifacts under the old values; redeploying under
            # fresh ones orphans them where check/remediate/scrub cannot find
            # them (docs/PLAN_failed_deploy_redeploy.md DoD 4).
            params = dict(reuse_params)
        else:
            _name_keys = [k for k, _kind in (getattr(ioc, "pooled_names", None) or [])]
            with Session(engine) as _pdb:
                _exclude = existing_active_dirs(_pdb, box_ip, ioc_name)
                _exclude_names = (
                    existing_active_names(_pdb, box_ip, ioc_name, _name_keys)
                    if _name_keys else None
                ) or {}
                # Cross-IOC per-kind exclusion: prevent two IOCs on the same
                # box drawing the same value from the same pool kind (service,
                # user, wmi, so, comm, ...). See project_pool_name_svc_collision.
                _pooled_map = build_ioc_pooled_map(_get_ioc_loader().ioc_definitions)
                _cross = cross_ioc_active_kind_names(_pdb, box_ip, ioc, _pooled_map)
                for _k, _vs in _cross.items():
                    _exclude_names.setdefault(_k, set()).update(_vs)
            # First pass with no dir_subdirs — need DROP_DIR before we can
            # probe the target box for subdir collisions. If the initial pick
            # names a subdir under DROP_DIR, re-pick with dir_subdirs feeding
            # the extra exclude. Two-pass keeps the NXC probe scoped to the
            # ACTUAL DROP_DIR (one call per deploy, not N).
            params = resolve_deploy_params(
                ioc, ioc.os,
                exclude_dirs=_exclude,
                exclude_names=(_exclude_names or None),
            )
            if params is not None and _name_keys:
                _drop_dir = params.get("DROP_DIR")
                _ci = box_creds.get(box_ip)
                if _drop_dir and _ci and os_key(ioc.os) != "windows":
                    try:
                        _subs = await probe_dir_subdirs(
                            wrapper, _ci["routed_host"], ioc.os,
                            _ci["username"], _ci["password"], _drop_dir,
                        )
                    except Exception:
                        # Probe failure is a soft no-op: we proceed without
                        # the extra exclude (legacy behaviour). A real deploy
                        # failure will surface downstream.
                        _subs = set()
                    if _subs & set().union(*[
                        {params.get(k)} for k in _name_keys if params.get(k)
                    ]):
                        # A pooled pick already matches a subdir of DROP_DIR
                        # (cp <src> <dir> would land src INSIDE the dir). Re-
                        # pick with dir_subdirs feeding the exclude, so the
                        # picker draws again from the pool minus those names.
                        params = resolve_deploy_params(
                            ioc, ioc.os,
                            exclude_dirs=_exclude,
                            exclude_names=(_exclude_names or None),
                            dir_subdirs=_subs,
                        )
        if params is None:
            update_box_progress(
                task_id, box_ip, "skipped",
                error="pool dirs exhausted for this IOC (no distinct location left)",
            )
            return

        # Build cmd per-box so the wrapper can sign a per-(box, path) HMAC
        # token. Bypasses wmiexec's 8191-char limit by sending a tiny
        # `iex(WebClient.DownloadString(...))` bootstrap instead of the
        # base64-encoded script body, which inflates beyond the SMB exec arg
        # cap (CLAUDE.md / SUBSYSTEMS.md "Windows IOC delivery").
        #
        # Pool params go in via `extra_env=` (NOT by string-prepending the
        # built command): string concatenation returns a plain `str`, dropping
        # the `WrappedSingleCommand.result_token_id` the result callback is
        # keyed on — which forces a permanent callback-miss and falls the whole
        # deploy back onto smbexec's readback, whose empty-read failure mode is
        # the exact bug the callback exists to close. `extra_env` also keeps the
        # pooled dir/names off the target's command line.
        _pool_env = params_env_dict(params)
        cmd = build_inline_command(
            deploy_script_path, ioc.os,
            server_url=config.SCRIPT_SERVER_URL, box_ip=box_ip,
            extra_env=_pool_env,
        )
        cred_info = box_creds[box_ip]
        update_box_progress(task_id, box_ip, "running")

        # RECORD BEFORE ACT (docs/PLAN_failed_deploy_redeploy.md D2). The row —
        # carrying the pooled names resolved above — must exist before the first
        # on-box write. Otherwise a deploy that fails partway leaves artifacts
        # named with values nothing recorded (unfindable by check/remediate/
        # scrub), and no row for the operator to see or redeploy. Credentials are
        # NOT registered yet: the account does not exist until the deploy runs.
        # A redeploy reuses its existing row instead of creating a second one.
        if redeploy_of is not None:
            _row_id, _label = redeploy_of, ""
        else:
            _row_id, _label, _ = _persist_deployed_ioc(
                ioc, box_ip, ioc_name, persistence_mechanism, implant_identifier,
                deployed_by, deploy_params=params, register_creds=False,
            )

        _row_ids[box_ip] = _row_id

        def _fail(msg: str, details: str | None = None) -> None:
            """Record a per-box deploy failure on the task for THIS attempt.

            Deliberately does NOT touch the row: this may be attempt 1 of 3.
            Marking here would flip the row deploy_failed (and alert) on a
            failure that is about to be retried successfully, and the row would
            then stay deploy_failed forever. `_deploy_one_retrying` marks once,
            after the retries are exhausted.
            """
            failed_ips.append(box_ip)
            _last_error[box_ip] = msg
            update_box_progress(task_id, box_ip, "failed", error=msg, details=details)

        # Upload payloads before running deploy script
        if ioc.payloads:
            from fastapi_backend.core.payload_resolver import (  # lazy-load
                PayloadResolutionError,
                _resolve_ioc_payload,
            )
            with Session(engine) as _db:
                for payload_filename in ioc.payloads:
                    try:
                        local_path = await _resolve_ioc_payload(payload_filename, ioc.os, _db)
                    except PayloadResolutionError as exc:
                        _fail(f"payload resolution failed: {exc}")
                        return
                    remote_path = remote_temp_path(ioc.os, payload_filename)
                    if "windows" not in ioc.os.lower():
                        # Fixed filename (unlike the implant-generate pipeline's
                        # uuid-suffixed paths) — a re-deploy can hit a stale
                        # artefact left by an earlier attempt under a different
                        # uid. fs.protected_regular denies opening a file you
                        # don't own inside a sticky world-writable dir like
                        # /tmp, even for root; CAP_DAC_OVERRIDE doesn't bypass
                        # it. Clear any stale artefact first.
                        await wrapper.execute(  # nxc-wrapper-bypass: binary-deploy
                            cred_info["routed_host"], cred_info["routed_proto"],
                            cred_info["username"], cred_info["password"],
                            f"rm -f '{remote_path}'", port=cred_info["port"],
                            powershell=False,
                        )
                    # ROOT FIX (project_pivot_deploy_TRUE_rootcause): the Sliver
                    # TCP portfwd chokes on sustained transfers — a 785KB
                    # put_file through it hangs >200s even solo, while the box
                    # pulling the SAME file over HTTP takes ~1s (proven in
                    # isolation). For pivot-routed (port set) LINUX boxes with a
                    # large payload, stage the bytes on the script server and
                    # drive a tiny curl exec on the box (exec through the portfwd
                    # is fine). Any failure falls through to put_file so a box
                    # the script server can't reach never regresses.
                    put_result = None
                    _is_pivot = cred_info["port"] is not None
                    _is_linux = "windows" not in ioc.os.lower()
                    try:
                        _payload_size = Path(local_path).stat().st_size
                    except OSError:
                        _payload_size = 0
                    if (
                        config.NXC_PIVOT_HTTP_PULL_ENABLED
                        and _is_pivot and _is_linux
                        and config.SCRIPT_SERVER_URL
                        and _payload_size >= config.NXC_PIVOT_HTTP_PULL_MIN_BYTES
                    ):
                        from fastapi_backend.netexec.alt_delivery import (
                            ssh_pull_file,  # lazy-load
                        )
                        pull = await ssh_pull_file(
                            wrapper, box_ip=box_ip,
                            routed_host=cred_info["routed_host"],
                            routed_proto=cred_info["routed_proto"],
                            username=cred_info["username"],
                            password=cred_info["password"],
                            local_path=local_path, remote_path=remote_path,
                            port=cred_info["port"],
                        )
                        if pull.success:
                            put_result = pull
                            update_box_progress(
                                task_id, box_ip, "running",
                                details=f"http-pulled {payload_filename} ({_payload_size}B)",
                            )
                        else:
                            logger.info(
                                "http-pull for %s on %s failed (%s); falling back to put_file",
                                payload_filename, box_ip, pull.error,
                            )
                    if put_result is None:
                        put_result = await wrapper.put_file(  # nxc-wrapper-bypass: binary-deploy
                            cred_info["routed_host"], cred_info["routed_proto"],
                            cred_info["username"], cred_info["password"],
                            local_path, remote_path, port=cred_info["port"],
                        )
                    if not put_result.success and cred_info.get("fallback_username"):
                        update_box_progress(task_id, box_ip, "running",
                                            details=f"domain cred failed on payload upload ({put_result.error}) — retrying local")
                        put_result = await wrapper.put_file(  # nxc-wrapper-bypass: binary-deploy
                            cred_info["routed_host"], cred_info["routed_proto"],
                            cred_info["fallback_username"], cred_info["fallback_password"],
                            local_path, remote_path, port=cred_info["port"],
                        )
                    if not put_result.success:
                        _fail(f"payload upload failed: {payload_filename}: {put_result.error}")
                        return
                    update_box_progress(task_id, box_ip, "running",
                                        details=f"uploaded {payload_filename}")

        is_smb = cred_info["routed_proto"] == "smb"

        async def _exec_one(command: Any, retrieve_output: bool) -> Any:
            """Primary exec + domain-credential fallback at the given readback mode.

            Domain-credential fallback (PLAN_domain_auth_fallback.md): the resolved
            credential is domain-qualified and produced no output at all — a
            pre-execution auth/connection failure, not a mid-script bug. Retry once
            with the box's local admin credential.
            """
            # docs/PLAN_soak_full_coverage.md §investigation 2026-08-30:
            # `command` returned by build_inline_command is ALREADY a fully-
            # formed `powershell.exe -EncodedCommand <b64>` string for
            # Windows. Passing powershell=True makes NXC re-encode the whole
            # thing through `-X` (UTF-16LE + base64 ~2.67x), doubling size —
            # scripts near the 8191-char cap single-encoded blow it once
            # doubled and NXC exits with `Command exceeds maximum length of
            # 8191`, empty output, deploy_script status=-1. Reproduced live:
            # windows_shellcode_registry SHELLCODE_B64 (680 chars) inflated to
            # 12220-char cmd, all 20 windows boxes failed under Phase 0
            # 12-worker concurrency. Force powershell=False for smb; check_runner
            # already uses this pattern.  See script_wrapper.py:356-361.
            _ps_flag = False if is_smb else use_ps
            r = await wrapper.execute(  # nxc-wrapper-bypass: wrapper-delivered
                cred_info["routed_host"], cred_info["routed_proto"],
                cred_info["username"], cred_info["password"], command,
                powershell=_ps_flag, port=cred_info["port"],
                exec_method="smbexec" if is_smb else None,
                retrieve_output=retrieve_output,
            )
            if not r.success and not r.output and cred_info.get("fallback_username"):
                update_box_progress(task_id, box_ip, "running",
                                    details=f"domain cred failed ({r.error}) — retrying local")
                r = await wrapper.execute(  # nxc-wrapper-bypass: wrapper-delivered
                    cred_info["routed_host"], cred_info["routed_proto"],
                    cred_info["fallback_username"], cred_info["fallback_password"], command,
                    powershell=_ps_flag, port=cred_info["port"],
                    exec_method="smbexec" if is_smb else None,
                    retrieve_output=retrieve_output,
                )
            return r

        # Result callback (docs/PLAN_script_result_callback.md): prefer the
        # deploy script's own POSTed output over NXC's own readback — the exact
        # bug this closes (windows_easy_admin proved live: smbexec/wmiexec
        # readback has no completion check and can read an empty/partial redirect
        # file as "done"). On Windows (smb) additionally suppress smbexec's C:\
        # readback artifact (docs/PLAN_kill_nxc_readback.md): --no-output happy
        # path, readback+sweep only on a callback-miss.
        try:
            if config.NXC_SUPPRESS_READBACK and is_smb:
                result = await _exec_one(cmd, retrieve_output=False)
                payload = await await_callback_or_none(cmd)
                if payload is not None:
                    effective_output = payload  # happy path — nothing dropped
                else:
                    retry_cmd = build_inline_command(
                        deploy_script_path, ioc.os,
                        server_url=config.SCRIPT_SERVER_URL, box_ip=box_ip,
                        extra_env=_pool_env,
                    )
                    result = await _exec_one(retry_cmd, retrieve_output=True)
                    await wrapper.cleanup_smbexec_artifacts(
                        cred_info["routed_host"], cred_info["username"],
                        cred_info["password"], port=cred_info["port"],
                    )
                    effective_output = await await_callback_result(retry_cmd, result.output or "")
            else:
                result = await _exec_one(cmd, retrieve_output=True)
                effective_output = await await_callback_result(cmd, result.output or "")
        except Exception as exc:
            _fail(f"NXC execute failed: {exc}")
            return

        if not result.success and not effective_output.strip():
            _fail(f"NXC subprocess failed: {result.error}", details=effective_output)
            return

        parsed = _parse_check_output(effective_output)
        parsed_status = parsed.get("status")
        if parsed_status != 1:
            msg = parsed.get("error") or parsed.get("message") or "no message"
            _fail(f"Deploy script status={parsed_status}: {msg}", details=effective_output)
            return

        # Timestomp artefacts BEFORE persistence / immutable application.
        # Runs opportunistically; partial / total failure is recorded but
        # never fails the deploy itself.  Triggered by ioc.timestomp=True OR
        # the operator's request-body override (req.timestomp).  Stats land
        # at task-level via OperatorTask.metadata_json.timestomp.
        timestomp_stats: dict[str, Any] | None = None
        ioc_timestomp = getattr(ioc, "timestomp", False) or timestomp
        if ioc_timestomp and ioc.timestomp_paths:
            from fastapi_backend.c2.timestomp import stomp_files_safe  # cycle-break
            # Expand {C3R_<KEY>} and {C3R_<KEY>_LOWER} placeholders using pool
            # params so IOC definitions can reference pool-derived names without
            # hardcoding a representative example that is wrong for every deploy.
            # _LOWER variants let definitions match OS case-conventions (Windows
            # DLL names are svc_name.ToLower() + ".dll").
            _expand_vars = dict(_pool_env)
            _expand_vars.update({f"{k}_LOWER": v.lower() for k, v in _pool_env.items()})
            _expanded_paths = [
                p.format(**_expand_vars) if "{" in p else p
                for p in ioc.timestomp_paths
            ]
            with Session(engine) as _db:
                stomp = await stomp_files_safe(
                    box_ip, _expanded_paths, ioc.os, _db,
                )
            timestomp_stats = stomp.to_metadata(enabled=True)
            _stomp_agg[box_ip] = timestomp_stats

        try:
            # The row already exists (record-before-act, above) — reuse it rather
            # than inserting a second one. Credentials are registered NOW, once
            # the deploy has actually landed and the account exists.
            deployed_id, label = _row_id, _label
            creds_added = _register_creds_for(ioc, box_ip, deployed_id, params)
            # Always clear failure bookkeeping on success — this may be a retry
            # of an attempt that already marked the row, or an operator redeploy.
            # Without this the row stays deploy_failed after a successful retry
            # and never scores again.
            mark_deploy_succeeded(deployed_id)
            details_payload: dict[str, Any] = {
                "deployed_ioc_id": deployed_id,
                "blue_team_label": label,
                "credentials_registered": creds_added,
            }
            if timestomp_stats is not None:
                details_payload["timestomp"] = timestomp_stats

            # Hiding-library integration: insert HidingLibraryConfig rows and push
            # the XOR blob to the box.  Two sources:
            #   1. ioc.wants_hiding entries (explicit caller-declared hiding)
            #   2. auto self-protect entries for hide-lib IOCs (always-on)
            hiding_entries = getattr(ioc, "wants_hiding", None) or []
            is_hide_lib = ioc_name in _HIDE_LIB_IOC_NAMES
            if hiding_entries or is_hide_lib:
                try:
                    inserted = _add_hiding_entries(ioc, deployed_id, box_ip, params)
                    inserted += _inject_hide_lib_self_protect(
                        ioc, deployed_id, box_ip, params,
                    )
                    if inserted:
                        await _push_hiding_config_async(
                            box_ip, ioc.os, wrapper, cred_info, use_ps,
                        )
                        details_payload["hiding_entries_added"] = inserted
                except Exception as hc_exc:
                    logger.warning(
                        "Hiding config push failed for %s on %s: %s",
                        ioc_name, box_ip, hc_exc,
                    )
                    details_payload["hiding_config_warning"] = str(hc_exc)

            update_box_progress(
                task_id, box_ip, "done",
                details=json.dumps(details_payload),
                exec_path="nxc",
            )
            logger.info(
                "Deploy-and-registered IOC: %s on %s (id=%d, label=%s, creds_added=%d)",
                ioc_name, box_ip, deployed_id, label, creds_added,
            )
        except Exception as exc:
            _fail(str(exc))

    async def _deploy_one_retrying(box_ip: str) -> None:
        """Whole-IOC deploy for one box, retried up to DEPLOY_MAX_ATTEMPTS.

        Retrying the WHOLE deploy (not the failed leg) is deliberate: there is no
        deploy-just-one-step entry point, and _deploy_one is idempotent enough to
        re-run — it re-uploads payloads and re-runs the script, which is what a
        redeploy would do anyway. Each attempt clears this box's prior failure
        mark so the tally reflects the FINAL outcome, not every attempt.
        docs/PLAN_failed_deploy_redeploy.md D3.
        """
        async def _attempt(_n: int) -> bool:
            while box_ip in failed_ips:
                failed_ips.remove(box_ip)
            await _deploy_one(box_ip)
            return box_ip not in failed_ips

        ok, _res, err = await with_deploy_retries(
            _attempt, label=f"deploy {ioc_name} on {box_ip}",
        )
        if not ok:
            if box_ip not in failed_ips:
                failed_ips.append(box_ip)
            # Mark the row ONCE, now that the retries are spent — this is the
            # single point that flips it deploy_failed (unscoreable) and raises
            # the one operator alert for this dead deploy.
            row_id = _row_ids.get(box_ip)
            if row_id:
                mark_deploy_failed(
                    row_id,
                    _last_error.get(box_ip) or err or "deploy failed",
                    task_id=task_id,
                    attempts=config.DEPLOY_MAX_ATTEMPTS,
                )
            logger.warning("deploy %s on %s exhausted retries: %s", ioc_name, box_ip, err)

    await asyncio.gather(*[_deploy_one_retrying(ip) for ip in box_ips])
    if _stomp_agg:
        total_attempted = sum(s.get("attempted", 0) for s in _stomp_agg.values())
        total_succeeded = sum(s.get("succeeded", 0) for s in _stomp_agg.values())
        total_failed: list[dict[str, Any]] = []
        for per_box in _stomp_agg.values():
            total_failed.extend(per_box.get("failed", []))
        update_task_metadata(task_id, {"timestomp": {
            "enabled": True,
            "attempted": total_attempted,
            "succeeded": total_succeeded,
            "failed": total_failed,
            "per_box": _stomp_agg,
        }})
    # NOTE: a redeploy needs nothing extra here. Its row IS the per-box row
    # (_row_ids[box_ip] == redeploy_of), so _deploy_one already returned it to
    # `active` on success and _deploy_one_retrying already marked it
    # deploy_failed — with the REAL error — once retries were spent. Re-marking
    # here would only overwrite that with a generic message.
    finish_task(task_id, "failed" if failed_ips else "done")


# ---------------------------------------------------------------------------
# GET/DELETE routes (unchanged)
# ---------------------------------------------------------------------------

def _strip_extension(filename: str) -> str:
    """Strip the last extension only: ``payload.dll`` → ``payload``, ``foo.tar.gz`` → ``foo.tar``."""
    if not filename or "." not in filename:
        return filename
    return filename.rsplit(".", 1)[0]


def _compute_highlight_patterns(
    db: Session, row: DeployedIOCs, loader: IOCDefinitionLoader,
) -> dict[str, list[str]]:
    """Merge IOC + persistence sa_highlight maps and Sliver implant filename.

    For SliverCheck IOCs, also looks up the implant binary in ImplantBuilds and
    adds its basename to the ``processes`` and ``services`` highlight categories.

    Join strategy mirrors `sliver_manager._auto_register_implant`:
      Primary:  last_deployed_to == box_ip AND
                persistence_mechanism == row.persistence_mechanism
      Fallback: drop the persistence_mechanism predicate when row's is NULL.
    """
    merged: dict[str, list[str]] = {}
    ioc_def = loader.ioc_definitions.get(row.ioc_name)

    sources: list[dict[str, list[str]]] = []
    if ioc_def is not None:
        sources.append(getattr(ioc_def, "sa_highlight", {}) or {})
        if ioc_def.persistence is not None:
            sources.append(getattr(ioc_def.persistence, "sa_highlight", {}) or {})

    for src in sources:
        for cat, subs in src.items():
            target = merged.setdefault(cat, [])
            for s in subs:
                if s not in target:
                    target.append(s)

    # Dynamic: derive implant binary basename for SliverCheck IOCs.
    if ioc_def is not None and isinstance(ioc_def.check, SliverCheck):
        q = (
            select(ImplantBuilds)
            .where(ImplantBuilds.last_deployed_to == row.box_ip)
            .order_by(ImplantBuilds.created_at.desc())
            .limit(1)
        )
        if row.persistence_mechanism:
            # Primary join — match the persistence as well.
            primary = db.execute(
                select(ImplantBuilds)
                .where(
                    ImplantBuilds.last_deployed_to == row.box_ip,
                    ImplantBuilds.persistence_mechanism == row.persistence_mechanism,
                )
                .order_by(ImplantBuilds.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            build = primary or db.execute(q).scalar_one_or_none()
        else:
            build = db.execute(q).scalar_one_or_none()

        if build and build.filename:
            base = _strip_extension(build.filename)
            for cat in ("processes", "services"):
                target = merged.setdefault(cat, [])
                if base not in target:
                    target.append(base)

    return merged


@router.get("/deployed", response_model=dict[str, Any])
async def list_deployed_iocs(
    box_ip: str | None = Query(default=None, description="Filter results to a single box."),
    status: str | None = Query(
        default=None,
        description="Filter by DeployedIOCs.status (e.g. 'deploy_failed'). "
                    "Default lists active+lost.",
    ),
    current_user: Users = Depends(_check_non_blue),
    loader: IOCDefinitionLoader = Depends(_get_ioc_loader),
) -> dict[str, Any]:
    """List deployed IOC registrations (active + lost by default).

    When ``box_ip`` is provided, results are filtered to that box only.
    Each row carries a ``highlight_patterns`` dict mapping SA category →
    substrings that should be highlighted in the SA panel.

    ``status`` opts into other states — notably ``deploy_failed``, which the
    IOC deploy page's redeploy section lists. Those rows are deliberately NOT in
    the default response: they are unscoreable and represent work still to do,
    not deployed IOCs (docs/PLAN_failed_deploy_redeploy.md).

    docs/PLAN_soak_backend_perf.md §3.3: body is off-loaded to a thread —
    sync SQLAlchemy fetchall + row-mapping was the 2nd-largest event-loop
    stall source (proven live 2026-08-30, 500-600ms stalls per request under
    Phase 5 concurrent load).
    """
    import asyncio  # cycle-break
    return await asyncio.to_thread(_list_deployed_iocs_sync, box_ip, status, loader)


def _list_deployed_iocs_sync(box_ip: str | None, status: str | None,
                              loader: IOCDefinitionLoader) -> dict[str, Any]:
    """Synchronous body of list_deployed_iocs — safe to run in a thread."""
    with Session(engine) as db:
        wanted = [status] if status else ["active", "lost"]
        stmt = (
            select(DeployedIOCs)
            .where(DeployedIOCs.status.in_(wanted))
            .order_by(DeployedIOCs.deployed_at.desc())
        )
        if box_ip is not None:
            stmt = stmt.where(DeployedIOCs.box_ip == box_ip)
        rows = db.execute(stmt).scalars().all()

        row_ids = [r.id for r in rows]
        latest_by_id: dict[int, IOCCheckResult] = {}
        if row_ids:
            results = db.execute(
                select(IOCCheckResult)
                .where(IOCCheckResult.deployed_ioc_id.in_(row_ids))
                .order_by(IOCCheckResult.ioc_id.desc())
            ).scalars().all()
            for res in results:
                if res.deployed_ioc_id is not None:
                    latest_by_id.setdefault(res.deployed_ioc_id, res)

        deployed = []
        for r in rows:
            latest = latest_by_id.get(r.id)
            if latest is None:
                remediated: bool | None = None
                last_effect = None
                last_persistence = None
            else:
                last_effect = latest.effect_status
                last_persistence = latest.persistence_status
                remediated = (last_effect == 0 and last_persistence in (0, None))
            deployed.append({
                "id": r.id,
                "box_ip": r.box_ip,
                "ioc_name": r.ioc_name,
                "persistence_mechanism": r.persistence_mechanism,
                "implant_identifier": r.implant_identifier,
                "blue_team_label": r.blue_team_label,
                "deployed_at": r.deployed_at.isoformat() if r.deployed_at else None,
                "deployed_by": r.deployed_by,
                "status": r.status,
                "source": getattr(r, "source", "manual"),
                "remediated": remediated,
                "last_effect_status": last_effect,
                "last_persistence_status": last_persistence,
                # Parsed pool-resolved params (DROP_DIR + pooled names). Callers need
                # this to know WHERE/under what name an IOC actually landed: the same
                # IOC may hold two active rows on one box at two different dirs (each a
                # separate scoring unit), so (box_ip, ioc_name) alone does not identify
                # a row. See docs/PLAN_recorded_deploy_dirs.md.
                "deploy_params": r.params_dict,
                # Failure bookkeeping — the redeploy section renders deploy_error
                # verbatim on click and deep-links deploy_task_id to the task's
                # per-box progress (docs/PLAN_failed_deploy_redeploy.md).
                "deploy_error": getattr(r, "deploy_error", None),
                "deploy_task_id": getattr(r, "deploy_task_id", None),
                "deploy_attempts": getattr(r, "deploy_attempts", 0),
                "highlight_patterns": _compute_highlight_patterns(db, r, loader),
            })

    return {"status": "success", "total": len(deployed), "deployed": deployed}


@router.get("/deployed/{deployed_id}", response_model=dict[str, Any])
async def get_deployed_ioc(
    deployed_id: int,
    current_user: Users = Depends(_check_non_blue),
) -> dict[str, Any]:
    """Get a single deployed IOC record with its most recent check result."""
    with Session(engine) as db:
        record = db.get(DeployedIOCs, deployed_id)
        if not record:
            raise HTTPException(status_code=404, detail=f"DeployedIOC {deployed_id} not found")

        last_result = db.execute(
            select(IOCCheckResult)
            .where(IOCCheckResult.deployed_ioc_id == deployed_id)
            .order_by(IOCCheckResult.ioc_id.desc())
            .limit(1)
        ).scalar_one_or_none()

        result_data = None
        remediated: bool | None = None
        last_effect = None
        last_persistence = None
        if last_result:
            last_effect = last_result.effect_status
            last_persistence = last_result.persistence_status
            remediated = (last_effect == 0 and last_persistence in (0, None))
            result_data = {
                "effect_status": last_effect,
                "persistence_status": last_persistence,
                "error": last_result.error,
            }

        return {
            "status": "success",
            "deployed": {
                "id": record.id,
                "box_ip": record.box_ip,
                "ioc_name": record.ioc_name,
                "persistence_mechanism": record.persistence_mechanism,
                "implant_identifier": record.implant_identifier,
                "blue_team_label": record.blue_team_label,
                "deployed_at": record.deployed_at.isoformat() if record.deployed_at else None,
                "deployed_by": record.deployed_by,
                "status": record.status,
                "source": getattr(record, "source", "manual"),
                "remediated": remediated,
                "last_effect_status": last_effect,
                "last_persistence_status": last_persistence,
                "deploy_params": record.deploy_params,
            },
            "last_result": result_data,
        }


@router.delete("/deployed/{deployed_id}", response_model=dict[str, Any])
async def deregister_deployed_ioc(
    deployed_id: int,
    current_user: Users = Depends(_check_non_blue),
) -> dict[str, Any]:
    """Deregister a deployed IOC — sets status to 'failed', stops scoring.

    Also removes any auto-registered Credentials rows linked via
    `source_deployed_ioc_id`.
    """
    def _write(db: Session) -> tuple[str, str | None, str | None, int]:
        # Read-modify-write done ENTIRELY inside one actor transaction. Validation
        # outcomes are returned as a sentinel and the HTTPException raised OUTSIDE
        # so a legitimate 404/403 does not surface as an actor txn error in logs.
        record = db.get(DeployedIOCs, deployed_id)
        if not record:
            return "not_found", None, None, 0
        if record.source == "virtual":
            # Protected scoring infrastructure (e.g. default-cred IOCs) — never
            # operator-deregisterable; doing so would silently disable scoring.
            return "forbidden", None, None, 0
        ioc_name, box_ip = record.ioc_name, record.box_ip
        record.status = "failed"

        linked_creds = db.execute(
            select(Credentials).where(Credentials.source_deployed_ioc_id == deployed_id)
        ).scalars().all()
        creds_removed = len(linked_creds)
        for c in linked_creds:
            db.delete(c)
        return "ok", ioc_name, box_ip, creds_removed

    outcome, ioc_name, box_ip, creds_removed = await write_actor.submit(
        _write, label="deregister_deployed_ioc",
    )
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail=f"DeployedIOC {deployed_id} not found")
    if outcome == "forbidden":
        raise HTTPException(
            status_code=403,
            detail="cannot deregister a virtual scoring-infrastructure IOC",
        )

    logger.info(
        "Deregistered DeployedIOC id=%d (%s on %s, creds_removed=%d)",
        deployed_id, ioc_name, box_ip, creds_removed,
    )
    return {
        "status": "success",
        "message": f"DeployedIOC {deployed_id} deregistered",
        "credentials_removed": creds_removed,
    }


@router.post("/deployed/{deployed_id}/redeploy", status_code=202, response_model=dict[str, Any])
async def redeploy_failed_ioc(
    deployed_id: int,
    current_user: Users = Depends(_check_non_blue),
    loader: IOCDefinitionLoader = Depends(_get_ioc_loader),
) -> dict[str, Any]:
    """Retry a ``deploy_failed`` IOC under its ORIGINAL pooled names.

    Whole-IOC redeploy: there is no deploy-just-persistence entry point, and the
    whole-IOC path is the one that knows how to place every artifact. The row's
    recorded ``deploy_params`` are re-injected verbatim so the retry lands on the
    same dirs/names — a redeploy that re-picks from the pool would orphan
    whatever the failed attempt already planted under the old values.

    Restricted to ``deploy_failed`` rows on purpose (409 otherwise): pointing it
    at an ``active`` row would plant a second copy of an IOC that is already
    there and already scoring.

    docs/PLAN_failed_deploy_redeploy.md §F.
    """
    with Session(engine) as db:
        row = db.get(DeployedIOCs, deployed_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"DeployedIOC {deployed_id} not found")
        if row.status != "deploy_failed":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"DeployedIOC {deployed_id} is status={row.status!r}, not 'deploy_failed'. "
                    "Only a failed deploy can be redeployed; redeploying an active row would "
                    "plant a second copy."
                ),
            )
        ioc_name, box_ip = row.ioc_name, row.box_ip
        reuse_params = row.params_dict
        persistence_mechanism = row.persistence_mechanism

    ioc = loader.ioc_definitions.get(ioc_name)
    if not ioc:
        raise HTTPException(status_code=404, detail=f"IOC '{ioc_name}' not found")

    # Implant/inject IOCs are built + placed by the generate / deploy-all
    # pipeline, not by this script-deploy path. Rather than silently re-picking
    # pool values through the wrong path, say so.
    if isinstance(ioc.check, SliverCheck) or getattr(ioc, "sliver_inject", False):
        raise HTTPException(
            status_code=400,
            detail=(
                f"IOC '{ioc_name}' is implant/inject-based — redeploy it from the "
                "Generate page or Deploy All, which rebuild and place the implant. "
                "Its recorded pool params are preserved on this row."
            ),
        )
    if not ioc.deploy_script:
        raise HTTPException(
            status_code=400, detail=f"IOC '{ioc_name}' has no effect deploy_script",
        )

    protocol, use_ps = protocol_for_box(ioc.os)
    box_creds, _ = resolve_box_creds([box_ip], protocol, strict=True)

    task_id = create_task(
        task_type="deploy",
        operator=current_user.username,
        team_num=None,
        target_boxes=[box_ip],
        metadata={"ioc_name": ioc_name, "redeploy_of": deployed_id},
    )
    fire_and_forget(_bg_deploy_and_register(
        task_id=task_id,
        ioc=ioc,
        box_ips=[box_ip],
        ioc_name=ioc_name,
        persistence_mechanism=persistence_mechanism,
        implant_identifier=None,
        deployed_by=current_user.username,
        box_creds=box_creds,
        protocol=protocol,
        use_ps=use_ps,
        timestomp=bool(getattr(ioc, "timestomp", False)),
        reuse_params=reuse_params,
        redeploy_of=deployed_id,
    ))
    return {"task_id": task_id, "deployed_ioc_id": deployed_id, "ioc_name": ioc_name}


@router.post("/deregister-all", response_model=dict[str, Any])
async def deregister_all_deployed_iocs(
    current_user: Users = Depends(_check_non_blue),
) -> dict[str, Any]:
    """Deregister every active DeployedIOC in one call.

    Flips status='active' rows to status='failed' and cascade-deletes linked
    Credentials rows (source_deployed_ioc_id). Used by run_tests.sh reset hook
    between pytest_end and playwright_start to clear scoreboard contamination
    from prior live-VM runs.
    """
    def _write(db: Session) -> tuple[int, int]:
        # Virtual scoring IOCs (default-cred) are infrastructure — never swept.
        active_rows = db.execute(
            select(DeployedIOCs).where(
                DeployedIOCs.status == "active",
                DeployedIOCs.source != "virtual",
            )
        ).scalars().all()
        ioc_count = len(active_rows)
        if ioc_count == 0:
            return 0, 0

        deployed_ids = [r.id for r in active_rows]
        for r in active_rows:
            r.status = "failed"

        linked_creds = db.execute(
            select(Credentials).where(
                Credentials.source_deployed_ioc_id.in_(deployed_ids)
            )
        ).scalars().all()
        creds_removed = len(linked_creds)
        for c in linked_creds:
            db.delete(c)
        return ioc_count, creds_removed

    ioc_count, creds_removed = await write_actor.submit(
        _write, label="deregister_all_deployed_iocs",
    )
    if ioc_count == 0:
        return {
            "status": "success",
            "deregistered": 0,
            "credentials_removed": 0,
        }

    logger.info(
        "Deregistered %d DeployedIOCs in bulk (creds_removed=%d)",
        ioc_count, creds_removed,
    )
    return {
        "status": "success",
        "deregistered": ioc_count,
        "credentials_removed": creds_removed,
    }


_DEPLOYMENT_STATS_SQL = text("""
    SELECT
        di.box_ip,
        b.team_num,
        COUNT(*) AS deployed,
        SUM(
            CASE
                WHEN icr.effect_status = 0
                     AND (icr.persistence_status IS NULL OR icr.persistence_status = 0)
                THEN 1 ELSE 0
            END
        ) AS remediated
    FROM deployed_iocs di
    LEFT JOIN boxes b ON b.box_ip = di.box_ip
    LEFT JOIN ioc_check_result icr ON icr.deployed_ioc_id = di.id
        AND icr.ioc_id = (
            SELECT MAX(r2.ioc_id)
            FROM ioc_check_result r2
            WHERE r2.deployed_ioc_id = di.id
        )
    WHERE di.status = 'active'
    GROUP BY di.box_ip, b.team_num
""")


@router.get("/deployment-stats", response_model=dict[str, Any])
async def get_deployment_stats(
    current_user: Users = Depends(_check_non_blue),
) -> dict[str, Any]:
    """Aggregate deployed + remediated IOC counts per box and per team."""
    with Session(engine) as db:
        rows = db.execute(_DEPLOYMENT_STATS_SQL).fetchall()

    box_stats: dict[str, dict[str, int]] = {}
    team_stats: dict[int, dict[str, int]] = {}

    for row in rows:
        box_ip, team_num, deployed, remediated = (
            row.box_ip, row.team_num, int(row.deployed), int(row.remediated),
        )
        box_stats[box_ip] = {"deployed": deployed, "remediated": remediated}
        if team_num is not None:
            if team_num not in team_stats:
                team_stats[team_num] = {"deployed": 0, "remediated": 0}
            team_stats[team_num]["deployed"] += deployed
            team_stats[team_num]["remediated"] += remediated

    return {"box_stats": box_stats, "team_stats": team_stats}
