"""FastAPI dependency helpers for accessing the orchestrator and its subsystems.

These wrap the lazy `from fastapi_backend.main import orchestrator` lookup
plus the not-initialized 503 guard that several routes and background loops
duplicate. Use as `Depends(get_orchestrator)` etc. in route signatures.

Cycle-breaker note: each helper imports `fastapi_backend.main` lazily inside
the function body. Importing it at module top would cycle (main imports
routes which import this module which would import main).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from fastapi_backend.core.inventory_manager import InventoryManager  # lazy-load
    from fastapi_backend.core.orchestrator import CompetitionOrchestrator  # lazy-load
    from fastapi_backend.netexec.wrapper import NXCWrapper  # lazy-load


def get_orchestrator() -> CompetitionOrchestrator:
    """Return the live orchestrator or raise 503 if not initialized.

    Returns:
        The singleton `CompetitionOrchestrator` instance set during FastAPI
        lifespan startup.

    Raises:
        HTTPException: 503 if the orchestrator hasn't been initialized
            (server is still starting or has shut down).
    """
    from fastapi_backend.main import orchestrator  # cycle-breaker
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Orchestrator not initialized")
    return orchestrator


def get_nxc_wrapper() -> NXCWrapper:
    """Return the live NXC wrapper or raise 503 if any layer isn't initialized.

    Walks orchestrator → executor → nxc_wrapper. Used by routes and helpers
    that need to issue NetExec commands.

    Raises:
        HTTPException: 503 if the orchestrator, executor, or wrapper is not
            yet initialized.
    """
    orch = get_orchestrator()
    if orch.executor is None or orch.executor.nxc_wrapper is None:
        raise HTTPException(status_code=503, detail="NXC wrapper not initialized")
    return orch.executor.nxc_wrapper


def get_inventory() -> InventoryManager:
    """Return the loaded inventory manager or raise 503 if not loaded.

    Raises:
        HTTPException: 503 if `inventory.yml` has not been parsed yet
            (orchestrator startup hasn't reached `_load_inventory`).
    """
    orch = get_orchestrator()
    if orch.inventory_manager is None:
        raise HTTPException(status_code=503, detail="Inventory not loaded")
    return orch.inventory_manager
