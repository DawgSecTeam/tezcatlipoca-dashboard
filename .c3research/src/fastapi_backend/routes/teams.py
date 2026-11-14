"""Box/team inventory API."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from fastapi_backend.core.orchestrator import CompetitionOrchestrator
from fastapi_backend.utils.auth import Users, get_current_user
from fastapi_backend.utils.dependencies import get_orchestrator

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/teams", response_model=dict[str, Any])
async def get_teams(
    current_user: Users = Depends(get_current_user),
    orch: CompetitionOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Get list of all teams with their names and numbers."""
    try:
        # Get teams from inventory manager
        teams_data = []
        for team_num, team in orch.inventory_manager.teams.items():
            teams_data.append({"team_num": team_num, "name": team.name})

        # Sort by team number
        teams_data.sort(key=lambda x: x["team_num"])

        return {"teams": teams_data, "total": len(teams_data)}

    except Exception as e:
        logger.error(f"Failed to get teams list: {e}")
        raise HTTPException(status_code=500, detail="Failed to retrieve teams list") from e
