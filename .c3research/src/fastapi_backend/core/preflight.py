"""Preflight check-result aggregation.

Helpers for building the `preflight_results` payload used by both
`GET /admin/preflight/results` (initial load) and the `check-cycle-complete`
SSE event (live updates).

Option A filter: only non-passing entities included in `teams`/`boxes`/`iocs`
arrays; passing counts surfaced at each level for "X passing" notes.

A box is "passing" iff every preflight IOCCheckResult row for that box (from
that team's latest preflight CheckInstance) has `effect_status=1` and
`persistence_status in (NULL, 1)`. `effect_status=-1` (error) and
`effect_status=0` (remediated) both count as not-passing; `-1` beats `0` for
box-level determination because it indicates the check itself failed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import (
    BlueTeams,
    Boxes,
    CheckInstance,
    IOCCheckResult,
)


def _ioc_passing(effect_status: int | None, persistence_status: int | None) -> bool:
    """An IOC entry is passing iff effect=1 and persistence is null or 1."""
    if effect_status != 1:
        return False
    return not (persistence_status is not None and persistence_status != 1)


def build_preflight_results() -> dict[str, Any]:
    """Build the team→box→IOC preflight results payload.

    Uses each team's latest CheckInstance row WHERE preflight=True.
    """
    with Session(engine) as session:
        team_nums = session.execute(
            select(BlueTeams.team_num).order_by(BlueTeams.team_num)
        ).scalars().all()

        box_os_map: dict[str, str | None] = {
            b.box_ip: b.os
            for b in session.execute(select(Boxes)).scalars().all()
        }

        teams_out: list[dict[str, Any]] = []
        for team_num in team_nums:
            latest_ci = session.execute(
                select(CheckInstance)
                .where(
                    CheckInstance.blue_team_num == team_num,
                    CheckInstance.preflight == True,  # noqa: E712
                )
                .order_by(CheckInstance.timestamp.desc())
                .limit(1)
            ).scalar_one_or_none()
            if latest_ci is None:
                continue

            rows = session.execute(
                select(IOCCheckResult).where(
                    IOCCheckResult.check_instance_id == latest_ci.check_id,
                )
            ).scalars().all()

            # Group by box_ip → list of IOC dicts
            box_groups: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                box_groups.setdefault(r.box_ip, []).append({
                    "ioc_name": r.ioc_name,
                    "effect_status": r.effect_status,
                    "persistence_status": r.persistence_status,
                    "error": r.error,
                    "checked_at": latest_ci.timestamp.isoformat()
                    if latest_ci.timestamp else None,
                    "passing": _ioc_passing(r.effect_status, r.persistence_status),
                })

            boxes_out: list[dict[str, Any]] = []
            passing_box_count = 0
            for box_ip, ioc_entries in sorted(box_groups.items()):
                passing_ioc_count = sum(1 for e in ioc_entries if e["passing"])
                non_passing = [e for e in ioc_entries if not e["passing"]]
                box_passing = len(non_passing) == 0

                if box_passing:
                    passing_box_count += 1
                    continue  # Option A: omit fully-passing boxes

                boxes_out.append({
                    "box_ip": box_ip,
                    "os": box_os_map.get(box_ip),
                    "passing": False,
                    "passing_ioc_count": passing_ioc_count,
                    "iocs": [
                        {k: v for k, v in entry.items() if k != "passing"}
                        for entry in non_passing
                    ],
                })

            team_passing = len(boxes_out) == 0 and bool(box_groups)
            if team_passing:
                teams_out.append({
                    "team_num": team_num,
                    "passing": True,
                    "passing_box_count": passing_box_count,
                    "boxes": [],
                })
            else:
                teams_out.append({
                    "team_num": team_num,
                    "passing": False,
                    "passing_box_count": passing_box_count,
                    "boxes": boxes_out,
                })

    return {"teams": teams_out}
