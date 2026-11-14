r"""Competition preflight — red_scoring account validation (Feature P).

On-demand, admin-triggered probe that validates the `red_scoring` scoring
account exists AND has the right privilege on every box before scoring starts.
The steady-state `_scoring_cred_probe_loop` checks auth only; this adds a
privilege check (Windows: BUILTIN\Administrators SID in the token; Linux /
pfSense: uid 0) and classifies each box into a distinct reason so an operator
knows whether to fix the account, the password, or the network.

See docs/PLAN_domain_creds_preflight.md (Feature P).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from fastapi_backend.core.config import config
from fastapi_backend.core.task_tracker import (
    finish_task,
    start_task,
    update_box_progress,
    update_task_metadata,
)
from fastapi_backend.database.db_writer import engine
from fastapi_backend.database.models import Boxes, Credentials
from fastapi_backend.netexec.check_runner import _AUTH_REJECT_RE
from fastapi_backend.netexec.wrapper import protocol_for_box
from fastapi_backend.utils.nxc_routing import _get_best_cred, resolve_nxc_target

if TYPE_CHECKING:
    from fastapi_backend.netexec.wrapper import NXCResult, NXCWrapper

logger = logging.getLogger(__name__)

# Privilege-probe commands. Group names are localized; the BUILTIN\Administrators
# SID is not, so match on the SID (Domain Admins membership grants it on domain
# boxes too). uid 0 is the Unix root-equivalence test.
_WINDOWS_PRIV_CMD = "whoami /groups"
_UNIX_PRIV_CMD = "id -u"
_WINDOWS_ADMIN_SID = "S-1-5-32-544"  # BUILTIN\Administrators

# Per-box preflight outcomes (reason strings surfaced in the UI grid).
REASON_PASS = "pass"
REASON_AUTH_FAIL = "auth-fail"
REASON_INSUFFICIENT_PERMS = "insufficient-perms"
REASON_UNREACHABLE = "unreachable"
REASON_ABSENT = "absent"  # no red_scoring cred seeded for the box

# Kerberos tolerates a 5-minute clock skew by default. Trigger a resync when
# the box's UTC is more than 120 s off Kali's — well inside Kerberos's
# tolerance so we catch drift before it starts rejecting tickets. Live-
# reproduced on SKELD (member): a 3-hour skew from HQ (DC) turned every
# domain-auth attempt into STATUS_NO_LOGON_SERVERS while local auth kept
# working; `w32tm /config /manualpeerlist:<DC> /update && net stop/start
# w32time` cleared the skew and domain auth returned Pwn3d! immediately.
_CLOCK_DRIFT_THRESHOLD_SEC = 120.0
_CLOCK_PROBE_CMD = 'powershell -c "[DateTime]::UtcNow.ToString(\\"o\\")"'


def classify_preflight(box_os: str, result: NXCResult) -> tuple[bool, str]:
    """Map a privilege-probe NXC result to (passed, reason).

    A box that merely blips offline must return ``unreachable`` — never
    ``pass`` and never ``insufficient-perms``. Privilege is proven by the
    Administrators SID (Windows) or uid 0 (Unix), not by a localized name.
    """
    if not result.success:
        blob = f"{result.error or ''}\n{result.raw or ''}"
        if _AUTH_REJECT_RE.search(blob):
            return (False, REASON_AUTH_FAIL)
        return (False, REASON_UNREACHABLE)
    out = result.output or ""
    if "win" in box_os.lower():
        if _WINDOWS_ADMIN_SID in out:
            return (True, REASON_PASS)
        return (False, REASON_INSUFFICIENT_PERMS)
    # Linux / firewall (pfSense): last non-empty stdout line is the uid.
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if lines and lines[-1] == "0":
        return (True, REASON_PASS)
    return (False, REASON_INSUFFICIENT_PERMS)


def _priv_command(box_os: str) -> str:
    """The privilege-probe command for a box's OS."""
    return _WINDOWS_PRIV_CMD if "win" in box_os.lower() else _UNIX_PRIV_CMD


def _parse_utc_iso(s: str) -> dt.datetime | None:
    """Parse the ISO-8601 UTC string produced by [DateTime]::UtcNow.ToString("o").

    PowerShell prints the round-trip format (e.g. 2026-09-06T05:11:56.2419740Z)
    with a trailing 'Z'; Python <3.11 rejects that, so normalise to '+00:00'
    before fromisoformat. Best-effort; returns None on any parse failure so
    the caller can degrade gracefully instead of crashing preflight.
    """
    line = (s or "").strip().splitlines()[-1].strip() if (s or "").strip() else ""
    if not line:
        return None
    try:
        if line.endswith("Z"):
            line = line[:-1] + "+00:00"
        return dt.datetime.fromisoformat(line)
    except (ValueError, IndexError):
        return None


def _dc_ip_for(box_ip: str, session: Session) -> str | None:
    """Return the LAN IP of the DC in the same team as `box_ip`, or None.

    Used by the clock resync command so a member's `w32tm /config
    /manualpeerlist:...` points at its actual DC rather than an arbitrary
    external NTP peer (offline lab = no external NTP anyway).
    """
    row = session.execute(
        select(Boxes.team_num).where(Boxes.box_ip == box_ip)
    ).first()
    if row is None or row[0] is None:
        return None
    return session.execute(
        select(Boxes.box_ip).where(
            Boxes.team_num == row[0],
            Boxes.is_domain_controller == True,  # noqa: E712
        )
    ).scalar_one_or_none()


async def _maybe_resync_windows_clock(
    box_ip: str, box_os: str, cred: Credentials,
    host: str, rproto: str, port: int | None,
    nxc_wrapper: NXCWrapper, session: Session,
) -> str:
    """Probe a Windows box's UTC and resync its clock if it drifted.

    Returns one of: "ok" (delta below threshold), "resynced" (was drifted,
    fixed), "resync-failed" (was drifted, resync command didn't run),
    "probe-failed" (couldn't read box UTC — probably auth or reachability),
    or "skipped" (not a Windows box).

    Best-effort — never raises. Called as a first pass in preflight so any
    Kerberos-blocking skew is cleared BEFORE the priv probe issues the
    domain-auth command that would otherwise trip STATUS_NO_LOGON_SERVERS
    / KRB_AP_ERR_SKEW.
    """
    if "win" not in box_os.lower():
        return "skipped"
    try:
        probe = await nxc_wrapper.execute_cred(  # nxc-wrapper-bypass: clock probe
            host, rproto, cred, _CLOCK_PROBE_CMD, port=port, box_ip=box_ip,
        )
    except Exception as exc:
        logger.debug("clock probe raised for %s: %s", box_ip, exc)
        return "probe-failed"
    if not probe.success:
        return "probe-failed"
    box_utc = _parse_utc_iso(probe.output or "")
    if box_utc is None:
        return "probe-failed"
    kali_utc = dt.datetime.now(dt.UTC)
    drift = abs((box_utc - kali_utc).total_seconds())
    if drift <= _CLOCK_DRIFT_THRESHOLD_SEC:
        return "ok"
    # Build the resync command. Members point w32tm at the team DC so it
    # syncs against the same authoritative source AD uses; the DC itself
    # (rare — a DC's clock should not drift much) gets a plain
    # /rediscover + /resync so it re-selects a peer from its config.
    dc_ip = None if _is_dc_row(box_ip, session) else _dc_ip_for(box_ip, session)
    if dc_ip:
        resync_cmd = (
            f"w32tm /config /manualpeerlist:{dc_ip} /syncfromflags:manual "
            f"/reliable:no /update && net stop w32time && net start w32time "
            f"&& w32tm /resync /force"
        )
    else:
        resync_cmd = "w32tm /resync /force /rediscover"
    logger.warning(
        "clock drift on %s: %.1fs off Kali (box=%s, kali=%s); running resync",
        box_ip, drift, box_utc.isoformat(), kali_utc.isoformat(),
    )
    try:
        r = await nxc_wrapper.execute_cred(  # nxc-wrapper-bypass: clock resync
            host, rproto, cred, resync_cmd, port=port, box_ip=box_ip,
        )
    except Exception as exc:
        logger.debug("clock resync raised for %s: %s", box_ip, exc)
        return "resync-failed"
    return "resynced" if r.success else "resync-failed"


def _is_dc_row(box_ip: str, session: Session) -> bool:
    row = session.execute(
        select(Boxes.is_domain_controller).where(Boxes.box_ip == box_ip)
    ).scalar_one_or_none()
    return bool(row)


async def run_scoring_preflight(
    task_id: int,
    team_nums: list[int] | None,
    nxc_wrapper: NXCWrapper,
) -> None:
    """Probe every box (or team subset) for red_scoring auth + privilege.

    Writes a per-box ``{reason: ...}`` to the OperatorTask ``progress_json`` and
    a summary to ``metadata_json``. Async (202 + task) — the route in
    ``routes/admin.py`` fires this via ``fire_and_forget``.
    """
    start_task(task_id)
    try:
        with Session(engine) as session:
            stmt = select(Boxes.box_ip, Boxes.os)
            if team_nums:
                stmt = stmt.where(Boxes.team_num.in_(team_nums))
            targets = [(ip, os_name) for ip, os_name in session.execute(stmt).all()]

        sem = asyncio.Semaphore(config.SCORING_PREFLIGHT_MAX_CONCURRENT)

        async def probe_one(box_ip: str, box_os: str | None) -> str:
            os_name = box_os or ""
            proto = protocol_for_box(os_name)[0]
            async with sem:
                with Session(engine) as session:
                    cred = _get_best_cred(
                        box_ip, session, protocol=proto,
                        scoring_only=True, include_invalid=True,
                    )
                    if cred is None:
                        update_box_progress(
                            task_id, box_ip, "failed",
                            details=json.dumps({"reason": REASON_ABSENT}),
                        )
                        return REASON_ABSENT
                    # Detached copy so the NXC call doesn't hold the session open.
                    # is_infrastructure carried over so the wrapper's cross-cred
                    # fallback resolves the NEXT-best red_scoring row (not a
                    # random operator cred) when the primary auth-fails.
                    probe_cred = Credentials(
                        cred_id=cred.cred_id, username=cred.username,
                        password=cred.password or "", protocol=cred.protocol,
                        cred_type=cred.cred_type, nthash=cred.nthash,
                        ccache_blob=cred.ccache_blob, aes256_key=cred.aes256_key,
                        domain=cred.domain,
                        is_infrastructure=bool(cred.is_infrastructure),
                        is_default=bool(cred.is_default),
                    )
                    host, rproto, port = resolve_nxc_target(box_ip, proto, session)
                    # First pass: probe + resync clock on Windows boxes so a
                    # skewed member (SKELD-class, 3h drift) doesn't kill the
                    # domain-auth priv probe below with STATUS_NO_LOGON_SERVERS
                    # or KRB_AP_ERR_SKEW. Best-effort; a resync outcome is
                    # recorded in the per-box details for operator visibility.
                    clock_action = await _maybe_resync_windows_clock(
                        box_ip, os_name, probe_cred, host, rproto, port,
                        nxc_wrapper, session,
                    )
                try:
                    # nxc-wrapper-bypass: cred-validation — auth+privilege probe of the
                    # red_scoring cred (whoami /groups | id -u), not an IOC-script delivery.
                    result = await nxc_wrapper.execute_cred(  # nxc-wrapper-bypass: cred-validation
                        host, rproto, probe_cred, _priv_command(os_name), port=port,
                        box_ip=box_ip,  # PLAN_cred_loop_cooldown_defer: breaker keys on true box
                    )
                except Exception as e:  # any probe error → unreachable
                    update_box_progress(
                        task_id, box_ip, "failed", error=str(e),
                        details=json.dumps({"reason": REASON_UNREACHABLE}),
                    )
                    return REASON_UNREACHABLE
                passed, reason = classify_preflight(os_name, result)
                update_box_progress(
                    task_id, box_ip, "done" if passed else "failed",
                    details=json.dumps({
                        "reason": reason,
                        "clock_action": clock_action,
                    }),
                )
                return reason

        reasons = await asyncio.gather(
            *[probe_one(ip, os_name) for ip, os_name in targets]
        )
        by_reason: dict[str, int] = {}
        for r in reasons:
            by_reason[r] = by_reason.get(r, 0) + 1
        passed_count = by_reason.get(REASON_PASS, 0)
        update_task_metadata(task_id, {
            "total": len(reasons),
            "passed": passed_count,
            "failed": len(reasons) - passed_count,
            "by_reason": by_reason,
        })
        finish_task(task_id, "done")
        logger.info(
            "Scoring preflight complete: %d/%d boxes OK", passed_count, len(reasons)
        )
    except Exception:
        logger.exception("Scoring preflight failed")
        finish_task(task_id, "failed")
        raise
