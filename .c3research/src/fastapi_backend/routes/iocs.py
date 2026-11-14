"""IOC definition listing API (blocked from blue team)."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from fastapi_backend.core.ioc_definition import IOCDefinitionLoader
from fastapi_backend.utils.auth import Users, get_current_user
from iocs.base import IOC, ScriptCheck, SliverCheck

router = APIRouter(prefix="/iocs", tags=["iocs"])
logger = logging.getLogger(__name__)

# Initialize IOC loader (will be shared with orchestrator)
ioc_loader = IOCDefinitionLoader()


def _ioc_to_dict(
    ioc: IOC,
    *,
    include_check_script: bool = False,
    include_sensitive: bool = False,
) -> dict[str, Any]:
    """Serialize an IOC dataclass to the API response shape.

    Args:
        ioc: IOC dataclass instance.
        include_check_script: When True, also includes the `check_script`
            filename (basename only). Used by the per-IOC detail endpoint.
        include_sensitive: When True, includes the red/admin-only operator
            metadata (`detection_steps`, `remediation_steps`, `artifacts`).
            Default False so the fields never leak unless a caller explicitly
            opts in; `/iocs` routes pass `not current_user.is_blue_team`.

    Returns:
        Dict with the standard IOC response fields.
    """
    # Merge IOC-level + persistence-level sa_highlight maps (union per category key).
    merged_highlight: dict[str, list[str]] = {}
    for src in (ioc.sa_highlight, (ioc.persistence.sa_highlight if ioc.persistence else {})):
        for cat, subs in (src or {}).items():
            existing = merged_highlight.setdefault(cat, [])
            for s in subs:
                if s not in existing:
                    existing.append(s)

    payload: dict[str, Any] = {
        "name": ioc.name,
        "description": ioc.description,
        "difficulty": ioc.difficulty,
        "os": ioc.os,
        "points": ioc.points,
        "category": ioc.category,
        "discovery": ioc.discovery,
        "has_deploy_script": bool(
            ioc.deploy_script or (ioc.persistence and ioc.persistence.deploy_script)
        ),
        "has_effect_deploy_script": bool(ioc.deploy_script),
        "is_sliver_check": isinstance(ioc.check, SliverCheck),
        "has_persistence": ioc.persistence is not None,
        "persistence_mechanism": ioc.persistence.name if ioc.persistence else None,
        "has_immutable_persistence": bool(ioc.persistence and ioc.persistence.immutable),
        "preferred_implant_type": ioc.check.preferred_type if isinstance(ioc.check, SliverCheck) else "session",
        "sliver_inject": ioc.sliver_inject,
        "sa_highlight": merged_highlight,
        "backup_copy": getattr(ioc, "backup_copy", False),
    }
    if include_check_script:
        check_script_path = ioc.check.script if isinstance(ioc.check, ScriptCheck) else None
        payload["check_script"] = (
            check_script_path.split("/")[-1] if check_script_path else None
        )
    if include_sensitive:
        payload["detection_steps"] = list(ioc.detection_steps)
        payload["remediation_steps"] = list(ioc.remediation_steps)
        payload["artifacts"] = [
            {"path": a.path, "dynamic": a.dynamic, "description": a.description}
            for a in ioc.artifacts
        ]
    return payload


async def check_non_blue_team(current_user: Users = Depends(get_current_user)) -> Users:
    """Ensure user is not on a blue team.
    
    Only red team, white team, black team, and admin users can access IOC definitions.
    """
    if current_user.is_blue_team:
        raise HTTPException(
            status_code=403, detail="Blue team members cannot access IOC definitions"
        )
    return current_user


def get_ioc_loader() -> IOCDefinitionLoader:
    """Dependency to get IOC loader instance."""
    from fastapi_backend.main import orchestrator  # cycle-break

    if orchestrator and orchestrator.ioc_loader:
        return orchestrator.ioc_loader
    # Fallback to local instance if orchestrator not initialized
    if not ioc_loader.ioc_definitions:
        ioc_loader.load_ioc_definitions()
    return ioc_loader


@router.get("/", response_model=dict[str, Any])
async def get_all_iocs(
    current_user: Users = Depends(check_non_blue_team),
    loader: IOCDefinitionLoader = Depends(get_ioc_loader),
) -> dict[str, Any]:
    """Get all IOC definitions.
    
    Accessible only to non-blue team members (red team, white team, black team, admin).
    """
    try:
        include_sensitive = not current_user.is_blue_team
        # `disabled=True` IOCs are hidden from the browser — they never
        # deploy (see deploy_all._filter_iocs) so surfacing them here just
        # tempts an operator to try deploying one that will fail every time.
        # Direct-URL /iocs/<name> still works so operators who know the name
        # can inspect the definition.
        iocs_list = [
            _ioc_to_dict(ioc, include_sensitive=include_sensitive)
            for ioc in loader.ioc_definitions.values()
            if not getattr(ioc, "disabled", False)
        ]

        # Sort by OS and then by difficulty
        iocs_list.sort(key=lambda x: (x["os"], x["difficulty"], x["name"]))

        return {"status": "success", "total": len(iocs_list), "iocs": iocs_list}

    except Exception as e:
        logger.error(f"Failed to fetch IOC definitions: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch IOC definitions") from e


@router.get("/by-os/{os}", response_model=dict[str, Any])
async def get_iocs_by_os(
    os: str,
    current_user: Users = Depends(check_non_blue_team),
    loader: IOCDefinitionLoader = Depends(get_ioc_loader),
) -> dict[str, Any]:
    """Get IOC definitions filtered by operating system.
    
    Accessible only to non-blue team members.

    Parameters:
    - os: Operating system (windows, linux, or firewall)
    """
    try:
        os_lower = os.lower()
        if os_lower not in ["windows", "linux", "firewall"]:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid OS: {os}. Must be windows, linux, or firewall",
            )

        include_sensitive = not current_user.is_blue_team
        iocs_list = [
            _ioc_to_dict(ioc, include_sensitive=include_sensitive)
            for ioc in loader.get_iocs_for_os(os_lower)
            if not getattr(ioc, "disabled", False)
        ]

        # Sort by difficulty and name
        iocs_list.sort(key=lambda x: (x["difficulty"], x["name"]))

        return {
            "status": "success",
            "os": os_lower,
            "total": len(iocs_list),
            "iocs": iocs_list,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch IOCs for OS {os}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch IOCs for OS {os}") from e


@router.get("/{ioc_name}", response_model=dict[str, Any])
async def get_ioc_details(
    ioc_name: str,
    current_user: Users = Depends(check_non_blue_team),
    loader: IOCDefinitionLoader = Depends(get_ioc_loader),
) -> dict[str, Any]:
    """Get details for a specific IOC.
    
    Accessible only to non-blue team members.
    """
    try:
        ioc = loader.ioc_definitions.get(ioc_name)

        if not ioc:
            raise HTTPException(status_code=404, detail=f"IOC '{ioc_name}' not found")

        return {
            "status": "success",
            "ioc": _ioc_to_dict(
                ioc,
                include_check_script=True,
                include_sensitive=not current_user.is_blue_team,
            ),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch IOC details for {ioc_name}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch IOC details") from e


@router.get("/stats/summary", response_model=dict[str, Any])
async def get_ioc_stats(
    current_user: Users = Depends(check_non_blue_team),
    loader: IOCDefinitionLoader = Depends(get_ioc_loader),
) -> dict[str, Any]:
    """Get IOC statistics summary.
    
    Accessible only to non-blue team members.
    """
    try:
        stats = {
            "total_iocs": len(loader.ioc_definitions),
            "by_os": {},
            "by_difficulty": {1: 0, 2: 0, 3: 0, 4: 0},
            "total_points": 0,
        }

        # Calculate statistics
        for os, iocs in loader.os_ioc_mapping.items():
            stats["by_os"][os] = len(iocs)
            for ioc in iocs:
                stats["by_difficulty"].setdefault(ioc.difficulty, 0)
                stats["by_difficulty"][ioc.difficulty] += 1
                stats["total_points"] += ioc.points

        # Expected distribution (approximate; actual catalog may vary)
        expected = {
            "per_os": {"easy": 4, "medium": 3, "hard": 3},
            "total_per_os": 10,
            "max_points_per_os": 110,  # 4*5 + 3*10 + 3*20
        }

        return {"status": "success", "stats": stats, "expected": expected}

    except Exception as e:
        logger.error(f"Failed to fetch IOC statistics: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch IOC statistics") from e
