"""Per-team IOC detail API with blue-team obscured names."""

import logging
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import User
from fastapi_backend.utils.auth import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/details", response_model=dict[str, Any])
async def get_status(
    current_user: User = Depends(get_current_user),
    team_num: int | None = Query(
        None, description="Team number to view (admin/red team only)"
    ),
) -> dict[str, Any]:
    """Gets IOC details for a specific team or all teams (if admin/red team)."""
    try:
        with Session(engine) as session:
            # Check if we have any check data
            from fastapi_backend.database.models import CheckInstance  # cycle-break

            check_stmt = select(CheckInstance).limit(1)
            has_checks = session.execute(check_stmt).first() is not None

            if not has_checks:
                return {
                    "details": [],
                    "message": "No check results available - competition has not started",
                }

            # Use raw SQL for view query
            _cols = "team_num, box_ip, ioc_name, difficulty, status, effect_status, persistence_status, error, points"
            if current_user.is_blue_team:
                # Blue team users can only see their own team (ignore team_num parameter)
                sql = text(f"""
                    SELECT {_cols}
                    FROM team_latest_ioc_details
                    WHERE team_num = :team_num
                """)
                results = session.execute(
                    sql, params={"team_num": current_user.blue_team_num}
                ).all()
            elif not current_user.is_blue_team:
                # Admin and red team users can view specific team or all teams
                if team_num is not None:
                    sql = text(f"""
                        SELECT {_cols}
                        FROM team_latest_ioc_details
                        WHERE team_num = :team_num
                    """)
                    results = session.execute(sql, params={"team_num": team_num}).all()
                else:
                    sql = text(f"""
                        SELECT {_cols}
                        FROM team_latest_ioc_details
                    """)
                    results = session.execute(sql).all()
            else:
                raise HTTPException(
                    status_code=403, detail="Unauthorized to view details"
                )

            if not results:
                return {"details": [], "message": "No IOC check results available yet"}

            scoring_by_team: dict[int, dict] = {}
            try:
                scoreboard_rows = session.execute(
                    text(
                        "SELECT team_num, total_score, total_possible_points, score_pct"
                        " FROM blue_teams_scoreboard ORDER BY score_pct DESC, total_score DESC"
                    )
                ).all()
                scoring_by_team = {
                    row.team_num: {
                        "rank": idx + 1,
                        "score_pct": row.score_pct,
                        "total_possible_points": row.total_possible_points,
                    }
                    for idx, row in enumerate(scoreboard_rows)
                }
            except Exception:
                pass

            if current_user.is_blue_team:
                # Obscure IOC names: sort by (os, difficulty, ioc_name) for stable
                # labels, then replace with <OS>_<DIFFICULTY>_<N>
                rows = sorted(
                    results,
                    key=lambda r: (r.ioc_name.split("_")[0], r.difficulty, r.ioc_name),
                )
                counters: dict[tuple, int] = defaultdict(int)
                details = []
                for r in rows:
                    os_prefix = r.ioc_name.split("_")[0]
                    key = (os_prefix, r.difficulty)
                    counters[key] += 1
                    details.append(
                        {
                            "team_num": r.team_num,
                            "box_ip": r.box_ip,
                            "ioc_name": f"{os_prefix}_{r.difficulty}_{counters[key]}",
                            "difficulty": r.difficulty,
                            "status": r.status,
                            "effect_status": r.effect_status,
                            "persistence_status": r.persistence_status,
                            "error": r.error,
                            "points": r.points,
                        }
                    )
                team_scoring = scoring_by_team.get(current_user.blue_team_num, {})
                return {
                    "details": details,
                    "scoring": {
                        "score_pct": team_scoring.get("score_pct", 0.0),
                        "rank": team_scoring.get("rank"),
                    },
                }

            scoring = None
            if team_num is not None:
                ts = scoring_by_team.get(team_num, {})
                scoring = {
                    "score_pct": ts.get("score_pct", 0.0),
                    "rank": ts.get("rank"),
                    "total_possible_points": ts.get("total_possible_points", 0),
                }
            return {
                "details": [
                    {
                        "team_num": r.team_num,
                        "box_ip": r.box_ip,
                        "ioc_name": r.ioc_name,
                        "difficulty": r.difficulty,
                        "status": r.status,
                        "effect_status": r.effect_status,
                        "persistence_status": r.persistence_status,
                        "error": r.error,
                        "points": r.points,
                    }
                    for r in results
                ],
                "scoring": scoring,
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get team details: {e}")
        # Return empty details instead of error
        return {"details": [], "error": "Unable to retrieve IOC details at this time"}
