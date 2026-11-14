"""Modules routes — manage and run operator scripts against target boxes.

All routes require non-blue authentication (admin / red / white team).
"""

import asyncio
import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, computed_field
from sqlalchemy import select
from sqlalchemy.orm import Session

from fastapi_backend.core.config import config
from fastapi_backend.core.task_tracker import (
    create_task,
    finish_task,
    register_background_task,
    run_cancellable,
    start_task,
    update_box_progress,
)
from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import Boxes, Modules
from fastapi_backend.routes.c2 import require_non_blue
from fastapi_backend.utils.asyncio_tasks import fire_and_forget
from fastapi_backend.utils.auth import Users
from fastapi_backend.utils.db_helpers import query_one

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/c2/modules", tags=["modules"])

# Extension → compatible box OS values for run validation
_OS_COMPAT = {
    "windows": {"windows"},
    "linux": {"linux"},
    "any": {"windows", "linux", "firewall"},
}

# Script extensions that have a disk-drop delivery path and may therefore honor
# an operator-chosen deploy_dir (binaries always honor it; .bat/.py have no
# disk-drop path and reject deploy_dir).
_SCRIPT_DISK_DROP_EXTS = frozenset({".ps1", ".sh"})


def _validate_deploy_dir(deploy_dir: str, target_os: str) -> None:
    """Validate an operator-chosen deploy directory for the target OS.

    Raises HTTPException(422) on a relative path, a path with '..' segments,
    or (Windows) a non-drive-letter path.
    """
    d = deploy_dir.strip()
    if ".." in re.split(r"[\\/]", d):
        raise HTTPException(status_code=422, detail="deploy_dir must not contain '..' segments")
    if target_os == "windows":
        if not re.match(r"^[A-Za-z]:\\", d):
            raise HTTPException(
                status_code=422,
                detail="Windows deploy_dir must be an absolute drive path, e.g. C:\\Users\\Public",
            )
    elif not d.startswith("/"):
        raise HTTPException(
            status_code=422,
            detail="Linux deploy_dir must be an absolute path, e.g. /opt/tools",
        )


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ModuleResponse(BaseModel):
    """Pydantic mirror of the `Modules` ORM model for response payloads."""

    id: int
    name: str
    description: str | None
    target_os: str
    params: str | None
    file_path: str
    file_hash: str | None
    last_modified: float | None
    created_at: dt.datetime
    is_active: bool

    model_config = {"from_attributes": True}

    @computed_field
    @property
    def is_service_module(self) -> bool:
        """True when this module is a synthetic service-interruption row."""
        return self.file_path == "__services__"


class RunModuleRequest(BaseModel):
    """POST /c2/modules/{id}/run request body."""

    box_ips: list[str]
    params: dict[str, str] | None = None
    # Deliver .ps1/.sh bodies via the unified script-server bootstrap (no disk
    # artifact / no process-tree leak) on the NXC path. Default on; falls back
    # to legacy delivery when the wrapper is unavailable. No-op on the Sliver
    # path (already in-memory).
    use_script_serving: bool = True
    # Operator-chosen destination directory on the target box. None → today's
    # behavior (binaries → C:\Windows\Temp / /tmp; .ps1/.sh served/inline).
    # When set: binaries drop here; .ps1/.sh are FORCED to a disk-drop here
    # (overriding script-serving). Rejected (422) for .bat/.py modules.
    deploy_dir: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("")
def list_modules(
    target_os: str | None = None,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """List all active modules, optionally filtered by target_os."""
    with Session(engine) as db:
        stmt = select(Modules).where(Modules.is_active == True)  # noqa: E712
        if target_os:
            stmt = stmt.where(Modules.target_os == target_os)
        modules = db.execute(stmt).scalars().all()
        return {"modules": [ModuleResponse.model_validate(m) for m in modules]}


@router.get("/services")
def list_services(current_user: Users = Depends(require_non_blue)) -> dict:
    """Return configured service-interrupt command map (operator view)."""
    from fastapi_backend.core.services_loader import all_services  # cycle-break
    return all_services()


@router.post("", status_code=201)
async def upload_module(
    file: UploadFile = File(...),
    params: str | None = Form(None),
    current_user: Users = Depends(require_non_blue),
) -> ModuleResponse:
    """Upload a new module file to MODULES_DIR and register it in the DB.

    Accepts any supported extension (.ps1, .sh, .py, .bat, .exe, .elf, .msi, .dll).
    Automatically determines target_os from the file extension.
    Returns the created (or updated) module row.
    """
    from fastapi_backend.c2.module_scanner import (  # cycle-break
        _SUPPORTED_EXT,
        _extract_description,
        _file_hash,
        _target_os_for_ext,
    )
    from fastapi_backend.core.config import config as _cfg  # cycle-break

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in _SUPPORTED_EXT:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported file type {suffix!r}. Supported: {sorted(_SUPPORTED_EXT)}",
        )

    modules_dir = Path(_cfg.MODULES_DIR)
    modules_dir.mkdir(parents=True, exist_ok=True)
    dest = modules_dir / (file.filename or "upload")

    data = await file.read()
    dest.write_bytes(data)

    name = dest.stem
    mtime = dest.stat().st_mtime
    file_hash = _file_hash(dest)
    target_os = _target_os_for_ext(suffix)
    description = _extract_description(dest)

    from fastapi_backend.database.write_actor import write_actor  # cycle-break
    def _upsert(db: Session) -> "ModuleResponse":
        existing = query_one(db, Modules, name=name)
        if existing:
            existing.file_path = str(dest.absolute())
            existing.file_hash = file_hash
            existing.last_modified = mtime
            existing.target_os = target_os
            existing.description = description
            existing.is_active = True
            if params is not None:
                existing.params = params
            db.flush()
            return ModuleResponse.model_validate(existing)
        module = Modules(
            name=name,
            description=description,
            target_os=target_os,
            params=params,
            file_path=str(dest.absolute()),
            file_hash=file_hash,
            last_modified=mtime,
            is_active=True,
        )
        db.add(module)
        db.flush()
        return ModuleResponse.model_validate(module)
    return write_actor.submit_sync(_upsert, label="upload_module")


@router.post("/rescan")
def rescan_modules(current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """Trigger a rescan of MODULES_DIR and sync the modules table."""
    from fastapi_backend.c2.module_scanner import rescan_modules_dir  # cycle-break
    return rescan_modules_dir()


@router.post("/{module_id}/run", status_code=202)
async def run_module(
    module_id: int,
    body: RunModuleRequest,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Enqueue a module run against one or more boxes.

    Returns {task_id} immediately (202). Per-box progress tracked via
    GET /c2/tasks/{task_id}.
    """
    with Session(engine) as db:
        module = db.get(Modules, module_id)
        if not module or not module.is_active:
            raise HTTPException(status_code=404, detail="Module not found")

        if not body.box_ips:
            raise HTTPException(status_code=422, detail="box_ips must not be empty")

        if body.deploy_dir:
            if module.file_path == "__services__":
                raise HTTPException(
                    status_code=422,
                    detail="deploy_dir is not supported for service-interruption modules",
                )
            ext = Path(module.file_path).suffix.lower()
            if ext not in _BINARY_EXTS and ext not in _SCRIPT_DISK_DROP_EXTS:
                raise HTTPException(
                    status_code=422,
                    detail=f"deploy_dir is not supported for {ext} modules",
                )
            _validate_deploy_dir(body.deploy_dir, module.target_os)

        allowed_os = _OS_COMPAT.get(module.target_os, {module.target_os})
        boxes = db.execute(
            select(Boxes).where(Boxes.box_ip.in_(body.box_ips))
        ).scalars().all()
        box_map = {b.box_ip: b for b in boxes}

        for ip in body.box_ips:
            box = box_map.get(ip)
            box_os = (box.os or "unknown").lower() if box else "unknown"
            if box_os not in allowed_os:
                raise HTTPException(
                    status_code=422,
                    detail=f"Box {ip} OS ({box_os!r}) is incompatible with module target_os ({module.target_os!r})",
                )

        session_ids = {ip: (box_map.get(ip) and box_map[ip].session_id) for ip in body.box_ips}
        box_os_map = {
            ip: (box_map[ip].os or "unknown").lower() if ip in box_map else "unknown"
            for ip in body.box_ips
        }
        team_nums = {b.team_num for b in boxes if b.team_num is not None}
        team_num = next(iter(team_nums)) if len(team_nums) == 1 else None

        module_id_snap = module.id
        module_name_snap = module.name
        module_snap = module

    task_id = create_task(
        task_type="module",
        operator=current_user.username,
        team_num=team_num,
        target_boxes=body.box_ips,
        metadata={"module_id": module_id_snap, "module_name": module_name_snap},
    )

    register_background_task(task_id, fire_and_forget(run_cancellable(task_id, _bg_run_module(
        task_id=task_id,
        module=module_snap,
        box_ips=body.box_ips,
        params=body.params,
        session_ids=session_ids,
        box_os_map=box_os_map,
        use_script_serving=body.use_script_serving,
        deploy_dir=body.deploy_dir,
    ))))

    return {"task_id": task_id}


async def _bg_run_module(
    task_id: int,
    module: Modules,
    box_ips: list[str],
    params: dict[str, str] | None,
    session_ids: dict[str, str | None],
    box_os_map: dict[str, str] | None = None,
    use_script_serving: bool = True,
    deploy_dir: str | None = None,
) -> None:
    """Background: run module on all boxes, update OperatorTask progress."""
    start_task(task_id)

    try:
        from fastapi_backend.routes.c2 import _get_nxc_wrapper  # cycle-break
        nxc_wrapper = _get_nxc_wrapper()
    except HTTPException:
        nxc_wrapper = None

    from fastapi_backend.c2.module_runner import run_module_on_box  # cycle-break

    failed_ips: list[str] = []

    async def _run_one(ip: str) -> None:
        update_box_progress(task_id, ip, "running")
        try:
            result = await run_module_on_box(
                module=module,
                box_ip=ip,
                params=params,
                nxc_wrapper=nxc_wrapper,
                session_id=session_ids.get(ip),
                box_os=(box_os_map or {}).get(ip),
                use_script_serving=use_script_serving,
                deploy_dir=deploy_dir,
            )
            box_status = "done" if result["success"] else "failed"
            if box_status == "failed":
                failed_ips.append(ip)
            # {stdout,stderr,exit_code} shape — matches what DeployProgressPanel's
            # parseCmdOutput expects for the expandable per-box output row (see
            # docs/PLAN_operator_ux.md § A3). A bare output string here would
            # silently fail JSON.parse and never render as expandable.
            update_box_progress(
                task_id, ip, box_status,
                error=result.get("error"),
                details=json.dumps({
                    "stdout": result.get("output") or "",
                    "stderr": result.get("error") or "",
                    "exit_code": 0 if result.get("success") else 1,
                }),
                exec_path=result.get("exec_path"),
                duration_s=result.get("duration_s"),
            )
        except Exception as exc:
            failed_ips.append(ip)
            update_box_progress(task_id, ip, "failed", error=str(exc))

    await asyncio.gather(*[_run_one(ip) for ip in box_ips])
    finish_task(task_id, "failed" if failed_ips else "done")


@router.delete("/{module_id}", status_code=204)
def delete_module(module_id: int, current_user: Users = Depends(require_non_blue)) -> None:
    """Soft-delete a module (sets is_active=False). Does not remove the file."""
    from fastapi_backend.database.write_actor import write_actor  # cycle-break
    def _deact(db: Session) -> bool:
        m = db.get(Modules, module_id)
        if m is None:
            return False
        m.is_active = False
        return True
    if not write_actor.submit_sync(_deact, label="delete_module"):
        raise HTTPException(status_code=404, detail="Module not found")


# ---------------------------------------------------------------------------
# Module source viewer
# ---------------------------------------------------------------------------

_BINARY_EXTS = frozenset({".exe", ".dll", ".msi", ".elf", ".bin", ".so"})

_LANG_MAP = {
    ".ps1": "powershell",
    ".sh": "bash",
    ".py": "python",
    ".bat": "batch",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def _lang_for_ext(ext: str) -> str:
    return _LANG_MAP.get(ext.lower(), "text")


@router.get("/{module_id}/source")
def get_module_source(module_id: int, current_user: Users = Depends(require_non_blue)) -> dict[str, Any]:
    """Return the source text of a script module.

    Returns 400 for binary files (.exe, .dll, .msi, .elf, .bin, .so).
    Returns 403 if the resolved file path escapes MODULES_DIR.
    """
    if config.DEV_MODE:
        return {
            "filename": "stub.ps1",
            "language": "powershell",
            "source": "# DEV MODE stub\nWrite-Host 'Hello from DEV_MODE'",
        }

    with Session(engine) as db:
        mod = db.get(Modules, module_id)
    if not mod:
        raise HTTPException(status_code=404, detail="Module not found")

    if mod.file_path == "__services__":
        raise HTTPException(
            status_code=400,
            detail="Service-interruption modules have no source file — command is resolved from services.yml at run time",
        )

    file_path = Path(mod.file_path)
    modules_dir = Path(config.MODULES_DIR).resolve()

    try:
        file_path.resolve().relative_to(modules_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden") from None

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    ext = file_path.suffix.lower()
    if ext in _BINARY_EXTS:
        raise HTTPException(status_code=400, detail="Binary file — source not viewable")

    return {
        "filename": file_path.name,
        "language": _lang_for_ext(ext),
        "source": file_path.read_text(errors="replace"),
    }


class ModuleSourceUpdate(BaseModel):
    """PUT /c2/modules/{id}/source request body."""

    source: str


@router.put("/{module_id}/source")
def update_module_source(
    module_id: int,
    body: ModuleSourceUpdate,
    current_user: Users = Depends(require_non_blue),
) -> dict[str, Any]:
    """Overwrite a script module's source on disk and re-sync its row.

    Returns 400 for binary files (.exe, .dll, .msi, .elf, .bin, .so).
    Returns 403 if the resolved file path escapes MODULES_DIR.
    Returns 404 for an unknown module or a file missing on disk.

    On success, recomputes file_hash + last_modified + description so a
    subsequent rescan sees no change.
    """
    if config.DEV_MODE:
        return {
            "saved": True,
            "filename": "stub.ps1",
            "language": "powershell",
            "bytes_written": len(body.source.encode("utf-8")),
            "file_hash": "dev",
        }

    from fastapi_backend.c2.module_scanner import (  # cycle-break
        _extract_description,
        _file_hash,
    )

    # Read + validate outside the write txn.
    with Session(engine) as db:
        mod = db.get(Modules, module_id)
        if not mod:
            raise HTTPException(status_code=404, detail="Module not found")
        file_path = Path(mod.file_path)

    modules_dir = Path(config.MODULES_DIR).resolve()
    try:
        file_path.resolve().relative_to(modules_dir)
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden") from None
    ext = file_path.suffix.lower()
    if ext in _BINARY_EXTS:
        raise HTTPException(status_code=400, detail="Binary file — not editable")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    file_path.write_text(body.source, encoding="utf-8")
    new_hash = _file_hash(file_path)
    new_mtime = file_path.stat().st_mtime
    new_desc = _extract_description(file_path)

    from fastapi_backend.database.write_actor import write_actor  # cycle-break
    def _persist(db: Session) -> None:
        m = db.get(Modules, module_id)
        if m is None:
            return
        m.file_hash = new_hash
        m.last_modified = new_mtime
        m.description = new_desc
    write_actor.submit_sync(_persist, label="update_module_source")

    return {
        "saved": True,
        "filename": file_path.name,
        "language": _lang_for_ext(ext),
        "bytes_written": len(body.source.encode("utf-8")),
        "file_hash": new_hash,
    }
