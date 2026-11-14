"""Public scoreboard API (no auth required)."""

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends
from sqlmodel import Session, select, text

from fastapi_backend.core.orchestrator import CompetitionOrchestrator
from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import (
    CheckInstance,
)

router = APIRouter()
logger = logging.getLogger(__name__)

_history_cache: dict | None = None
_history_cache_at: float = 0.0
_HISTORY_CACHE_TTL: float = 30.0

# Short-TTL cache for the current-totals endpoint: 8 concurrent playwright
# operators polled this at once during soak N and the two view-scans (over
# every DeployedIOC) held sync Sessions on the loop thread — visible per-
# tick as backend accept-timeout bursts. 3s TTL still shows near-real-time
# to human operators and reduces cold-path bursts dramatically.
_scoreboard_cache: dict | None = None
_scoreboard_cache_at: float = 0.0
_SCOREBOARD_CACHE_TTL: float = 3.0


def get_orchestrator() -> CompetitionOrchestrator:
    """Get orchestrator instance from app."""
    from fastapi_backend.main import orchestrator  # cycle-break

    return orchestrator


@router.get("/boxes", response_model=dict[str, Any])
async def get_boxes(orch: CompetitionOrchestrator = Depends(get_orchestrator)) -> dict[str, Any]:
    """Get list of unique boxes from inventory."""
    try:
        # Get all boxes from inventory manager
        all_boxes = orch.inventory_manager.all_boxes

        # Get unique boxes (one per IP, since multiple teams have same box setup)
        unique_boxes = {}
        for box in all_boxes:
            if (
                box.ip not in unique_boxes or box.team_num == 1
            ):  # Prefer team 1's naming
                unique_boxes[box.ip] = {"ip": box.ip, "name": box.name, "os": box.os}

        # Sort by IP for consistent ordering
        sorted_boxes = sorted(unique_boxes.values(), key=lambda x: x["ip"])

        return {"boxes": sorted_boxes}

    except Exception as e:
        logger.error(f"Failed to fetch boxes: {e}")
        return {"boxes": [], "error": "Unable to retrieve box information"}


def _fetch_scoreboard_sync() -> dict[str, Any]:
    """Sync scoreboard fetch — 2 view scans off-loaded to a worker thread."""
    with Session(engine) as session:
        # Check if competition has started (any checks exist)
        check_stmt = select(CheckInstance).limit(1)
        has_checks = session.exec(check_stmt).first() is not None

        if not has_checks:
            return {
                "teams": [],
                "message": "Competition has not started yet - no scores available",
            }

        # Get team scores + percentage from the view. last_check_score is the
        # per-cycle score (just the latest check), distinct from the cumulative
        # total_score — the soak score-correctness phase predicts this exactly
        # (cumulative total_score can't be predicted without full check history).
        sql = text("""
            SELECT team_num, total_score, last_check_score, total_possible_points, score_pct
            FROM blue_teams_scoreboard
            ORDER BY score_pct DESC, total_score DESC
        """)
        team_results = session.exec(sql).all()

        if not team_results:
            return {"teams": [], "message": "No team scores available yet"}

        # Get box stats from team_latest_ioc_details view
        box_stats_sql = text("""
            SELECT
                team_num,
                box_ip,
                COUNT(*) as total,
                SUM(CASE WHEN status = 0 THEN 1 ELSE 0 END) as successful
            FROM team_latest_ioc_details
            GROUP BY team_num, box_ip
        """)
        box_stats_results = session.exec(box_stats_sql).all()

        # Build box stats map
        team_box_stats: dict[int, dict[str, dict[str, int]]] = {}
        for stat in box_stats_results:
            if stat.team_num not in team_box_stats:
                team_box_stats[stat.team_num] = {}
            team_box_stats[stat.team_num][stat.box_ip] = {
                "successful": stat.successful,
                "total": stat.total,
            }

        return {
            "teams": [
                {
                    "team_num": r.team_num,
                    "total_score": r.total_score,
                    "last_check_score": r.last_check_score,
                    "total_possible_points": r.total_possible_points,
                    "score_pct": r.score_pct,
                    "box_stats": team_box_stats.get(r.team_num, {}),
                }
                for r in team_results
            ]
        }


@router.get("/scoreboard", response_model=dict[str, Any])
async def get_scoreboard() -> dict[str, Any]:
    """Public scoreboard endpoint — current totals per blue team."""
    global _scoreboard_cache, _scoreboard_cache_at
    now = time.monotonic()
    if _scoreboard_cache is not None and (now - _scoreboard_cache_at) < _SCOREBOARD_CACHE_TTL:
        return _scoreboard_cache
    try:
        result = await asyncio.to_thread(_fetch_scoreboard_sync)
    except Exception as e:
        logger.error(f"Failed to fetch scoreboard data: {e}")
        return {"teams": [], "error": "Unable to retrieve scoreboard data at this time"}
    _scoreboard_cache = result
    _scoreboard_cache_at = now
    return result


@router.get("/scoreboard/history", response_model=dict[str, Any])
async def get_scoreboard_history(
    orch: CompetitionOrchestrator = Depends(get_orchestrator),
) -> dict[str, Any]:
    """Get historical data for scoreboard graphs."""
    global _history_cache, _history_cache_at
    if _history_cache is not None and (time.monotonic() - _history_cache_at) < _HISTORY_CACHE_TTL:
        return _history_cache

    boxes_per_team = len(
        [box for box in orch.inventory_manager.all_boxes if box.team_num == 1]
    )
    total_iocs = len(orch.ioc_loader.ioc_definitions)
    total_possible_iocs = total_iocs * boxes_per_team

    try:
        result = await asyncio.to_thread(_fetch_history_sync, total_possible_iocs)
    except Exception as e:
        logger.error(f"Failed to fetch scoreboard history: {e}")
        return {
            "score_history": [],
            "ioc_history": [],
            "total_possible_iocs": 0,
            "error": "Unable to retrieve history data",
        }
    _history_cache = result
    _history_cache_at = time.monotonic()
    return result


def _fetch_history_sync(total_possible_iocs: int) -> dict[str, Any]:
    """Sync history fetch — 2 view scans + join off-loaded to worker thread."""
    with Session(engine) as session:
        check_stmt = select(CheckInstance).limit(1)
        has_checks = session.exec(check_stmt).first() is not None

        if not has_checks:
            return {
                "score_history": [],
                "ioc_history": [],
                "total_possible_iocs": 0,
                "message": "No check data available",
            }

        score_sql = text("""
            SELECT team_num, timestamp, cumulative_score, score_pct
            FROM score_history
            ORDER BY timestamp DESC
            LIMIT 2000
        """)
        score_results = session.exec(score_sql).all()

        ioc_sql = text("""
            SELECT
                ci.blue_team_num as team_num,
                ci.timestamp,
                COUNT(CASE WHEN icr.status = 0 THEN 1 END) as remediated_count
            FROM (
                SELECT check_id, blue_team_num, timestamp
                FROM check_instance
                ORDER BY timestamp DESC
                LIMIT 2000
            ) ci
            LEFT JOIN ioc_check_result icr ON ci.check_id = icr.check_instance_id
            GROUP BY ci.check_id, ci.blue_team_num, ci.timestamp
            ORDER BY ci.timestamp, ci.blue_team_num
        """)
        ioc_results = session.exec(ioc_sql).all()

    score_by_time: dict[str, dict[str, Any]] = {}
    ioc_by_time: dict[str, dict[str, Any]] = {}

    for result in score_results:
        timestamp_str = (
            result.timestamp
            if isinstance(result.timestamp, str)
            else result.timestamp.isoformat()
        )
        if timestamp_str not in score_by_time:
            score_by_time[timestamp_str] = {"timestamp": timestamp_str}
        score_by_time[timestamp_str][f"team_{result.team_num}"] = (
            result.cumulative_score
        )
        score_by_time[timestamp_str][f"team_{result.team_num}_pct"] = result.score_pct

    for result in ioc_results:
        timestamp_str = (
            result.timestamp
            if isinstance(result.timestamp, str)
            else result.timestamp.isoformat()
        )
        if timestamp_str not in ioc_by_time:
            ioc_by_time[timestamp_str] = {"timestamp": timestamp_str}
        ioc_by_time[timestamp_str][f"team_{result.team_num}"] = result.remediated_count

    return {
        "score_history": sorted(score_by_time.values(), key=lambda x: x["timestamp"]),
        "ioc_history": sorted(ioc_by_time.values(), key=lambda x: x["timestamp"]),
        "total_possible_iocs": total_possible_iocs,
    }
