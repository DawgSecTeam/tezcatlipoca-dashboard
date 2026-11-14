"""SliverManager — manages the Sliver C2 server process and gRPC client connection.

Degrades gracefully when:
  - SLIVER_ENABLED=false (default)
  - The sliver-server binary is not present
  - The gRPC server fails to start within the timeout window

All Sliver-dependent routes should call sliver_manager.get_client() and return
503 when it returns None.
"""

import asyncio
import contextlib
import logging
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi_backend.c2.alerts import emit_alert
from fastapi_backend.c2.beacon_watcher import clear_beacon_stale_alert
from fastapi_backend.database.write_actor import write_actor
from fastapi_backend.utils.asyncio_tasks import fire_and_forget

logger = logging.getLogger(__name__)

# Dedicated session-lifecycle trace. Off unless C3R_SESSION_TRACE=1.
#
# A pivot GATEWAY session dying is currently near-silent: `_teardown_pivot` logs
# only when it finds a PivotSessions row that is still `active`, so a gateway that
# dies twice, or dies before its row lands, leaves no record at all. Sliver itself
# does not clean the pivot graph entry, so the members keep arriving and get dropped
# with "[graph] session not found for pivot" — 1,155,558 of those in a 62-minute
# window on the 2026-08-07 run, with no way to tell WHICH gateway went and WHEN.
#
# One append-only line per connect/disconnect, on a shared file so every worker
# backend writes to the same timeline (the box-access trace does the same for the
# test side; correlate the two by wall-clock).
_SESSION_TRACE = os.getenv("C3R_SESSION_TRACE") == "1"
_SESSION_TRACE_PATH = Path(
    os.getenv("C3R_SESSION_TRACE_PATH", "logs/xdist_workers/session_lifecycle.log")
)


def _log_session_lifecycle(event: str, session: Any) -> None:
    """Append one timestamped session connect/disconnect record. Never raises."""
    if not _SESSION_TRACE:
        return
    try:
        remote = getattr(session, "RemoteAddress", "") or ""
        # "1.2.3.4:5678" or "1.2.3.4:5678->NAME->" for a pivoted session; the chain
        # suffix is the signal that this session routes through a pivot.
        peer_ip = remote.split("->", 1)[0].split(":", 1)[0]
        _SESSION_TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # pid: EVERY per-worker backend subscribes to the same Sliver event stream,
        # so one real connect/disconnect lands in this file once per live backend —
        # 2422 lines for 726 distinct events on the first instrumented run. Stamping
        # the writer makes the duplicates attributable and lets analysis collapse on
        # (event, id) instead of guessing.
        line = (
            f"{time.time():.3f}\t{time.strftime('%H:%M:%S')}\t{event}\t"
            f"id={getattr(session, 'ID', '')[:8]}\tip={peer_ip}\t"
            f"name={getattr(session, 'Name', '')}\tos={getattr(session, 'OS', '')}\t"
            f"pivoted={'yes' if '->' in remote else 'no'}\tpid={os.getpid()}\t"
            f"addr={remote}\n"
        )
        with _SESSION_TRACE_PATH.open("a") as fh:
            fh.write(line)
    except Exception:  # instrumentation must never break the event loop
        logger.debug("session lifecycle trace write failed", exc_info=True)


# Lazily imported so the module still loads even when sliver-py is absent
_sliver_imported = False
SliverClient = None
SliverClientConfig = None


def _try_import_sliver() -> bool:
    global _sliver_imported, SliverClient, SliverClientConfig
    if _sliver_imported:
        return SliverClient is not None
    _sliver_imported = True
    try:
        from sliver import SliverClient as _SC  # type: ignore  # noqa: N814
        from sliver import SliverClientConfig as _SCC  # noqa: N814
        SliverClient = _SC
        SliverClientConfig = _SCC
        return True
    except ImportError:
        logger.warning("sliver-py not installed — Sliver features disabled")
        return False


_SLIVER_CFG_KNOWN_FIELDS = {
    "operator", "lhost", "lport", "ca_certificate", "certificate", "private_key", "token"
}


def _parse_sliver_config(cfg_path: str) -> Any:
    """Parse a Sliver operator config file, ignoring unknown fields (e.g. 'wg').
    
    that newer server versions add but sliver-py doesn't support yet.
    """
    import json  # cycle-break
    with Path(cfg_path).open() as f:
        data = json.load(f)
    filtered = {k: v for k, v in data.items() if k in _SLIVER_CFG_KNOWN_FIELDS}
    return SliverClientConfig(**filtered)


def _extract_box_ip(remote_address: str) -> str:
    """Extract the box IP from a Sliver RemoteAddress string.

    Handles both direct (ip:port) and TCP-pivot (gw_ip:port->target_ip:port) formats.
    For pivot sessions the first IP segment (gateway side) is the box we care about.
    """
    return remote_address.split(":")[0]


def box_by_hostname(db: Any, hostname: str | None) -> Any:
    """Find a Box by the hostname an implant reports — CASE-INSENSITIVELY.

    Only route for attributing a PIVOTED session/beacon to its real box: its
    RemoteAddress is the gateway's, not its own, so the IP tells us nothing about
    who it is.

    The case-sensitive form silently returned None for every Windows box. Sliver
    reports `Hostname` as Windows does — "C3-WIN25-1-2" — while Boxes.box_name is
    seeded from inventory in lower case ("c3-win25-1-2"), and SQLite `=` on TEXT
    is case-sensitive. Measured on the live DB:

        box_name = 'C3-WIN25-1-2'                  -> 0 rows
        box_name = 'c3-win25-1-2'                  -> 1 row
        box_name = 'C3-WIN25-1-2' COLLATE NOCASE   -> 1 row

    Every pivoted beacon therefore fell through to the IP lookup and was
    attributed to the GATEWAY firewall, in the operator-facing /c2/beacons and
    /c2/sessions payloads that the Implants and Network Map pages render — and it
    failed test_pivot_ioc_set_deploy on all 10 teams in every run, deterministically,
    because the test filters beacons by owning box IP.
    """
    from sqlalchemy import func, select  # cycle-break

    from fastapi_backend.database.models import Boxes  # cycle-break

    if not hostname:
        return None
    return db.execute(
        select(Boxes).where(func.lower(Boxes.box_name) == hostname.lower())
    ).scalars().first()


def _box_for_address(db: Any, ip: str) -> Any:
    """Find the Box a Sliver RemoteAddress belongs to — matches ``box_ip`` OR ``wan_ip``.

    A firewall calls home from its WAN IP but is keyed by its LAN ``box_ip``
    (docs/PLAN_firewall_wan_lan.md); a normal box matches on box_ip. Returns the
    Box (whose ``box_ip`` stays the canonical identity) or None.
    """
    from sqlalchemy import or_, select  # cycle-break

    from fastapi_backend.database.models import Boxes  # cycle-break
    return db.execute(
        select(Boxes).where(or_(Boxes.box_ip == ip, Boxes.wan_ip == ip))
    ).scalar_one_or_none()


def _box_for_session(db: Any, hostname: str | None, ip: str) -> Any:
    """Bind a Sliver session/beacon to its owning Box row.

    Hostname collides across teams in multi-team ranges (every team's DC is
    "HQ"), so resolve TEAM first via the RemoteAddress IP — matches a box's
    own box_ip on same-L2 ranges, or the team firewall's wan_ip when the
    implant is NAT'd out through its team firewall. Then scope the hostname
    lookup to that team. Falls back to the IP-only match when hostname is
    empty or no team-scoped row matches, so firewalls (whose Sliver hostname
    rarely matches inventory's box_name) and unnamed boxes keep resolving as
    before. Also handles TCP-pivot sessions uniformly: raw_addr split gives
    the gateway wan_ip → team, hostname is the pivoted target → real box.
    """
    from sqlalchemy import func, or_, select  # cycle-break

    from fastapi_backend.database.models import Boxes  # cycle-break

    ip_row = db.execute(
        select(Boxes).where(or_(Boxes.box_ip == ip, Boxes.wan_ip == ip))
    ).scalars().first()
    if hostname and ip_row is not None and ip_row.team_num is not None:
        team_hit = db.execute(
            select(Boxes).where(
                func.lower(Boxes.box_name) == hostname.lower(),
                Boxes.team_num == ip_row.team_num,
            )
        ).scalars().first()
        if team_hit is not None:
            return team_hit
    return ip_row


def _auto_register_implant(db: Any, box_ip: str, implant_name: str, engine: Any) -> None:
    """Auto-register DeployedIOCs rows for SliverCheck IOCs matching implant_name.

    Called when a session/beacon connects. Finds all SliverCheck IOC definitions
    whose implant_identifier exactly equals implant_name and creates or reactivates
    a DeployedIOCs row for each. Idempotent: existing 'active' rows are left alone.
    """
    import datetime as _dt  # cycle-break

    from sqlalchemy import select  # cycle-break
    from sqlalchemy.orm import Session as ActorSession  # cycle-break

    from fastapi_backend.database.models import (  # cycle-break
        DeployedIOCs,
        ImplantBuilds,
        assign_blue_team_label,
    )
    from fastapi_backend.routes.ioc_deploy import _get_ioc_loader  # cycle-break
    from iocs.base import SliverCheck  # cycle-break

    try:
        loader = _get_ioc_loader()
        matching_iocs = [
            ioc for ioc in loader.ioc_definitions.values()
            if isinstance(ioc.check, SliverCheck)
            and ioc.check.implant_identifier == implant_name
        ]
        if not matching_iocs:
            return

        # Look up persistence_mechanism from ImplantBuilds if one was deployed here
        build_row = db.execute(
            select(ImplantBuilds)
            .where(ImplantBuilds.last_deployed_to == box_ip)
            .order_by(ImplantBuilds.created_at.desc())
        ).scalars().first()
        # ImplantBuilds has no dedicated persistence_mechanism field; use None unless added later
        persistence_from_build: str | None = None
        if build_row and getattr(build_row, "persistence_mechanism", None):
            persistence_from_build = build_row.persistence_mechanism  # type: ignore[attr-defined]

        for ioc in matching_iocs:
            existing = db.execute(
                select(DeployedIOCs).where(
                    DeployedIOCs.box_ip == box_ip,
                    DeployedIOCs.ioc_name == ioc.name,
                )
            ).scalars().first()

            if existing is None:
                pass  # fall through to insert
            elif existing.status == "active":
                # Idempotent — already active, nothing to do
                continue
            elif existing.status == "lost":
                # Reactivate via the single-writer actor (this function runs on the
                # Sliver event handler thread; submit_sync blocks only this thread).
                existing_id = existing.id
                def _reactivate(adb: ActorSession, _eid: int = existing_id, _iname: str = implant_name) -> None:
                    r = adb.get(DeployedIOCs, _eid)
                    if r is None:
                        return
                    r.status = "active"
                    r.deployed_at = _dt.datetime.now(_dt.UTC)
                    r.implant_identifier = _iname
                write_actor.submit_sync(_reactivate, label="_auto_register_reactivate")
                logger.info(
                    "Auto-reactivated DeployedIOCs id=%d for %s on %s",
                    existing_id, ioc.name, box_ip,
                )
                continue
            else:
                # 'failed' or other — manual re-register required, skip
                continue

            # New row — assign label + insert in ONE actor transaction so the label
            # counter is serialised correctly (only one writer exists).
            _ioc = ioc  # capture per-loop
            def _insert(adb: ActorSession, __ioc=_ioc) -> None:
                label = assign_blue_team_label(adb, __ioc.os, __ioc.difficulty)
                record = DeployedIOCs(
                    box_ip=box_ip,
                    ioc_name=__ioc.name,
                    persistence_mechanism=persistence_from_build,
                    implant_identifier=implant_name,
                    blue_team_label=label,
                    deployed_at=_dt.datetime.now(_dt.UTC),
                    deployed_by="auto",
                    status="active",
                    source="auto",
                    difficulty=__ioc.difficulty,
                )
                adb.add(record)
            write_actor.submit_sync(_insert, label="_auto_register_insert")
            logger.info(
                "Auto-registered DeployedIOCs for %s on %s (implant=%s)",
                ioc.name, box_ip, implant_name,
            )
    except Exception as exc:
        logger.error("_auto_register_implant failed for %s on %s: %s", implant_name, box_ip, exc)


def _backfill_inject_identifier(db: Any, box_ip: str, implant_name: str) -> None:
    """B2 fix: backfill implant_identifier on inject DeployedIOCs rows after session-connected.

    _bg_inject_shellcode registers DeployedIOCs without implant_identifier (the injected
    session name isn't known at inject time). When the injected session connects, this
    function finds active inject rows for the box with null implant_identifier and
    backfills them. Called only when no normal SliverCheck IOC matched the session name.
    """
    from sqlalchemy import select  # cycle-break

    from fastapi_backend.database.models import DeployedIOCs  # cycle-break
    from fastapi_backend.routes.ioc_deploy import _get_ioc_loader  # cycle-break
    from iocs.base import SliverCheck  # cycle-break

    try:
        loader = _get_ioc_loader()
        inject_ioc_names = {
            name
            for name, ioc in loader.ioc_definitions.items()
            if isinstance(ioc.check, SliverCheck) and getattr(ioc, "sliver_inject", False)
        }
        if not inject_ioc_names:
            return

        # Skip if this session name already matched a normal (non-inject) SliverCheck IOC
        normal_matches = {
            name
            for name, ioc in loader.ioc_definitions.items()
            if isinstance(ioc.check, SliverCheck) and ioc.check.implant_identifier == implant_name
        }
        if normal_matches:
            return

        rows = db.execute(
            select(DeployedIOCs).where(
                DeployedIOCs.box_ip == box_ip,
                DeployedIOCs.ioc_name.in_(inject_ioc_names),
                DeployedIOCs.implant_identifier.is_(None),
                DeployedIOCs.status == "active",
            )
        ).scalars().all()

        if not rows:
            return
        row_ids_names = [(r.id, r.ioc_name) for r in rows]
        def _backfill(adb: Any) -> None:
            from fastapi_backend.database.models import (
                DeployedIOCs as _DI,  # cycle-break
            )
            for rid, _ in row_ids_names:
                r = adb.get(_DI, rid)
                if r is not None:
                    r.implant_identifier = implant_name
        write_actor.submit_sync(_backfill, label="_backfill_inject_identifier")
        for _, name in row_ids_names:
            logger.info(
                "Backfilled implant_identifier=%s for inject IOC %s on %s",
                implant_name, name, box_ip,
            )
    except Exception as exc:
        logger.error(
            "_backfill_inject_identifier failed for %s on %s: %s", implant_name, box_ip, exc
        )


def _handle_implant_disconnect(
    db: Any, session_id: str, box_ip: str | None, implant_name: str | None = None
) -> None:
    """Flip THIS implant's auto-registered IOC rows to 'lost' on an intentional kill.

    Checks PendingKills for the session_id. If found, marks the active auto-registered
    DeployedIOCs rows for that box **whose implant_identifier is the killed implant**
    as 'lost', and deletes the PendingKills row. If not found (organic disconnect),
    does nothing — effect_status corrects on next check cycle.

    SCOPED BY IMPLANT, not just by box. A box legitimately hosts several
    independently-scored rows (pooled deploy dirs: "same IOC to two distinct dirs
    = two active rows = two independent scoring units"), each with its own
    implant_identifier. Flipping every row on the box — as this did until
    2026-08-06 — marked unrelated, still-running implants 'lost', corrupting their
    scoring state. Observed live: 192.168.1.213 carried 4 auto rows for 4 distinct
    implants (MIGHTY_RETINA / COMPLICATED_ENVIRONMENT / DIFFERENT_CADET /
    DETERMINED_CERAMIC) and a single kill marked all of them lost.

    Rows whose implant_identifier is NULL remain in scope — eager registration
    inserts rows before the session name is known (_backfill_inject_identifier fills
    it in later), so excluding them would mean an intentional kill never marked them
    lost. Only rows explicitly attributed to a DIFFERENT implant are protected.
    """
    from sqlalchemy import or_, select  # cycle-break

    from fastapi_backend.database.models import (  # cycle-break
        DeployedIOCs,
        PendingKills,
    )

    try:
        kill_row = db.execute(
            select(PendingKills).where(PendingKills.session_id == session_id)
        ).scalar_one_or_none()

        if kill_row is None:
            return  # organic disconnect — no action

        # Intentional kill: flip THIS implant's active auto-registered rows to 'lost'.
        #
        # Rows with a NULL implant_identifier are still in scope: eager registration
        # inserts rows before the session name is known (that is what
        # _backfill_inject_identifier later fills in), so excluding them would mean an
        # intentional kill never marked them lost at all. Only rows explicitly
        # attributed to a DIFFERENT implant are protected.
        if box_ip:
            ident_filter = DeployedIOCs.implant_identifier.is_(None)
            if implant_name:
                ident_filter = or_(
                    ident_filter, DeployedIOCs.implant_identifier == implant_name
                )
            active_rows = db.execute(
                select(DeployedIOCs).where(
                    DeployedIOCs.box_ip == box_ip,
                    DeployedIOCs.status == "active",
                    DeployedIOCs.source == "auto",
                    ident_filter,
                )
            ).scalars().all()
            marked = [(r.id, r.ioc_name, r.implant_identifier) for r in active_rows]
        else:
            marked = []
        kill_sid = kill_row.session_id  # PendingKills primary key

        def _write(adb: Any) -> None:
            from fastapi_backend.database.models import (  # cycle-break
                DeployedIOCs as _DI,
            )
            from fastapi_backend.database.models import (
                PendingKills as _PK,
            )
            for rid, _, _ in marked:
                r = adb.get(_DI, rid)
                if r is not None:
                    r.status = "lost"
            kr = adb.get(_PK, kill_sid)
            if kr is not None:
                adb.delete(kr)
        write_actor.submit_sync(_write, label="_handle_implant_disconnect")

        for rid, iname, ident in marked:
            logger.info(
                "Marked DeployedIOCs id=%d (%s, implant=%s) as lost "
                "(intentional kill session=%s implant=%s)",
                rid, iname, ident, session_id, implant_name,
            )
    except Exception as exc:
        logger.error("_handle_implant_disconnect failed for session=%s: %s", session_id, exc)


class SliverManager:
    """Sliver C2 client manager — startup, event subscription, pivot setup."""

    def __init__(self) -> None:
        self._client = None
        self._proc: asyncio.subprocess.Process | None = None
        self._event_task: asyncio.Task | None = None
        self.connected: bool = False
        self._cfg_path: Path | None = None
        # Single-flight lock coalescing reconnect attempts across _handle_events'
        # loop and get_client()'s lazy path. Lazy-created on the running loop.
        # See docs/PLAN_sliver_client_reconnect.md.
        self._reconnect_lock: asyncio.Lock | None = None
        # Strong refs to background reconnect tasks — asyncio holds only weak
        # refs, so an un-held task can be GC'd mid-run. Cleared on completion.
        self._lazy_reconnect_tasks: set[asyncio.Task] = set()
        # Subscribers: list of asyncio.Queue instances receiving event dicts.
        # Total publish-time drops across the lifetime of this manager — exposed
        # on /admin/status as `dropped_events_total` so soak Phase 0 can
        # detect SSE backpressure as a measurable metric. Per-queue drop counts
        # are attached to the queue via the `_drop_count` attribute so callers
        # of `subscribe_bounded` can reconcile their own losses without scraping
        # the global counter.
        self._event_queues: list[asyncio.Queue] = []
        self.dropped_events_total: int = 0
        # Thread-safe cache: implant_name -> set[box_ip] for active sessions/beacons.
        # One-to-many: multiple boxes can share the same prebuilt binary name.
        # Updated on connect/disconnect events; read by check_runner from worker threads.
        self._active_implants: dict[str, set[str]] = {}
        self._implants_lock = threading.RLock()
        # IPs currently being processed by _maybe_setup_pivot.  Prevents multiple
        # concurrent session-connected events for the same firewall box from each
        # running the 30×10s retry loop simultaneously and exhausting the DB pool.
        self._pivot_setup_ips: set[str] = set()
        # Main event loop captured on first async entry. _process_event_sync runs
        # in a thread-pool executor where asyncio.create_task / fire_and_forget
        # would raise "no running event loop" and drop alerts silently. Sync code
        # uses run_coroutine_threadsafe(coro, self._main_loop) instead.
        self._main_loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Connect to the Sliver server and subscribe to the event stream."""
        from fastapi_backend.core.config import config  # cycle-break

        if not config.SLIVER_ENABLED:
            logger.info("Sliver disabled (SLIVER_ENABLED=false)")
            return

        if not _try_import_sliver():
            return

        cfg_path = Path(config.SLIVER_CFG_PATH)
        if not cfg_path.exists():
            logger.warning(
                f"Sliver operator config {str(cfg_path)!r} not found — "
                "Sliver features disabled"
            )
            return

        self._cfg_path = cfg_path

        # Step 1: If the server is already running, just connect to it.
        try:
            cfg = _parse_sliver_config(str(cfg_path))
            self._client = SliverClient(cfg)
            await self._client.connect()
            self.connected = True
            logger.info("Sliver client connected (server already running)")
            try:
                from fastapi_backend.c2 import sliver_debug  # cycle-break
                sliver_debug.install_channel_tracer(self._client)
            except Exception as exc:
                logger.warning("sliver_debug.install_channel_tracer failed: %s", exc)
            await self._ensure_required_listeners()
            self._event_task = asyncio.create_task(self._handle_events())
            fire_and_forget(self._bootstrap_existing_pivots())
            fire_and_forget(self._populate_implant_cache())
            fire_and_forget(self._cache_refresh_loop())
            return
        except Exception:
            logger.info("Sliver server not reachable, attempting to start it…")

        # Step 2: Server not reachable — try to start it via systemctl.
        logger.info("Starting Sliver service via systemctl…")
        start_proc = await asyncio.create_subprocess_exec(
            "systemctl", "start", "sliver",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await start_proc.wait()

        # Step 3: Check again — fail gracefully if still not reachable.
        try:
            await self._wait_for_server(str(cfg_path))
        except RuntimeError:
            logger.warning("Sliver service failed to start — Sliver features disabled")
            return

        cfg = _parse_sliver_config(str(cfg_path))
        self._client = SliverClient(cfg)
        await self._client.connect()
        self.connected = True
        logger.info("Sliver client connected")
        try:
            from fastapi_backend.c2 import sliver_debug  # cycle-break
            sliver_debug.install_channel_tracer(self._client)
        except Exception as exc:
            logger.warning("sliver_debug.install_channel_tracer failed: %s", exc)

        await self._ensure_required_listeners()
        self._event_task = asyncio.create_task(self._handle_events())
        fire_and_forget(self._bootstrap_existing_pivots())
        fire_and_forget(self._populate_implant_cache())
        fire_and_forget(self._cache_refresh_loop())
        fire_and_forget(self._pivot_orphan_loop())

    async def _pivot_orphan_loop(self) -> None:
        """Periodically flag PivotSessions whose gateway session no longer exists.

        Distinct from the startup reconcile in `_ensure_required_listeners`, which
        probes the LOCAL 127.0.0.1 portfwd listener. This checks the other end: does
        the pfSense gateway SESSION the row points at still exist in Sliver?

        That orphan state is what silently breaks a pivot set. Sliver does not prune
        its pivot graph when the parent session dies, so every member implant that
        dials the gateway's tcppivot port is dropped at lookup with "[graph] session
        not found for pivot" — 1,155,558 of those over 62 minutes on the 2026-08-07
        run, while the member side reported only "deployed but never called back".
        One row-level signal beats a million server-side lines.

        Deliberately does NOT deactivate the row: the startup reconcile owns that
        decision via the listener probe, and a transient gRPC hiccup must not flip
        live scoring state. This reports; it does not remediate.
        """
        from sqlalchemy import select  # cycle-break
        from sqlalchemy.orm import Session as _Session  # cycle-break

        from fastapi_backend.core.config import config  # cycle-break
        from fastapi_backend.database.db_writer import engine as _engine  # cycle-break
        from fastapi_backend.database.models import PivotSessions  # cycle-break

        interval = int(os.getenv("C3R_PIVOT_ORPHAN_INTERVAL", "30"))
        reported: set[str] = set()
        # Heartbeat. Without it, "no ORPHANED PIVOT lines" is ambiguous between
        # "swept and found nothing" and "never ran" — which is exactly the state
        # the first instrumented run left us in.
        logger.info("pivot orphan sweep started (interval=%ss)", interval)
        sweeps = 0
        while True:
            await asyncio.sleep(interval)
            try:
                client = self._client
                if client is None or not self.connected:
                    continue
                live = {s.ID for s in await client.sessions()}
                with _Session(_engine) as db:
                    rows = db.execute(
                        select(PivotSessions).where(PivotSessions.active == True)  # noqa: E712
                    ).scalars().all()
                    sweeps += 1
                    if sweeps % 10 == 1:  # ~every 5 min at the default interval
                        logger.info(
                            "pivot orphan sweep #%d: %d active row(s), %d live session(s)",
                            sweeps, len(rows), len(live),
                        )
                    for row in rows:
                        sid = row.pfsense_session_id
                        if not sid or sid in live:
                            reported.discard(sid or "")
                            continue
                        if sid in reported:
                            continue  # one alert per orphan, not one per tick
                        reported.add(sid)
                        logger.error(
                            "ORPHANED PIVOT: team %s row points at gateway session %s "
                            "which no longer exists in Sliver (box %s, port %s). Member "
                            "implants dialing this pivot will be dropped server-side.",
                            row.team_num, sid[:8], row.pfsense_box_ip,
                            row.tcp_pivot_port or config.PIVOT_LISTENER_PORT,
                        )
                        _log_session_lifecycle(
                            "pivot-orphaned",
                            type("_S", (), {
                                "ID": sid,
                                "RemoteAddress": f"{row.pfsense_box_ip}:0",
                                "Name": f"team{row.team_num}-gateway",
                                "OS": "freebsd",
                            })(),
                        )
                        fire_and_forget(emit_alert(
                            "pivot_lost",
                            f"Pivot for team {row.team_num} points at a dead gateway session",
                            box_ip=row.pfsense_box_ip,
                            team_num=row.team_num,
                            details={
                                "session_id": sid,
                                "pivot_ip": row.pfsense_pivot_ip,
                                "reason": "orphaned_gateway_session",
                            },
                        ))
            except Exception as exc:
                logger.debug("pivot orphan sweep error: %s", exc)

    async def _ensure_required_listeners(self) -> None:
        """On startup verify the Sliver mTLS listener is bound; reconcile stale PivotSessions.

        Creates the mTLS listener on the configured port if missing, and marks
        PivotSessions rows whose backing 127.0.0.1:<pivot_port> tunnel is no
        longer bound as inactive (they die across backend restarts).
        """
        from fastapi_backend.core.config import config  # cycle-break

        # ── mTLS listener ───────────────────────────────────────────────────
        try:
            jobs = await self._client.jobs()
        except Exception as exc:
            logger.warning("Could not list Sliver jobs: %s — skipping listener verify", exc)
            return

        mtls_port = config.MTLS_LISTENER_PORT
        has_mtls = any(
            (j.Protocol or "").lower() == "tcp"
            and "mtls" in (j.Description or "").lower()
            and j.Port == mtls_port
            for j in jobs
        )
        if has_mtls:
            logger.info("Sliver mTLS listener already active on port %d", mtls_port)
        else:
            try:
                listener = await self._client.start_mtls_listener(host="0.0.0.0", port=mtls_port)
                logger.info(
                    "Started Sliver mTLS listener on 0.0.0.0:%d (job_id=%s)",
                    mtls_port, getattr(listener, "JobID", "?"),
                )
            except Exception as exc:
                # Many backends (one per pytest xdist worker) share a single Sliver
                # server and call this concurrently.  jobs() above is a check-then-act
                # race: several see has_mtls=False and all try to bind mtls_port; only
                # one wins, the rest get "address already in use".  Re-verify via jobs()
                # — if a listener now exists someone else won the race, which is the
                # desired end state, so log info instead of a misleading error.
                try:
                    jobs_after = await self._client.jobs()
                    now_has_mtls = any(
                        (j.Protocol or "").lower() == "tcp"
                        and "mtls" in (j.Description or "").lower()
                        and j.Port == mtls_port
                        for j in jobs_after
                    )
                except Exception:
                    now_has_mtls = False
                if now_has_mtls:
                    logger.info(
                        "mTLS listener on port %d already started by another backend "
                        "(lost start race) — continuing", mtls_port,
                    )
                else:
                    logger.error("Failed to start mTLS listener on port %d: %s", mtls_port, exc)

        # ── TCP stage-listener ──────────────────────────────────────────────
        # Serves the second stage to the small stager that windows_shellcode_registry
        # plants in the registry (docs/PLAN_shellcode_registry_stager.md). Distinct
        # from the mTLS listener: implants carrying a full beacon call home on mTLS,
        # stagers fetch here first.
        stage_port = config.STAGE_LISTENER_PORT
        has_stage = any(
            (j.Protocol or "").lower() == "tcp" and j.Port == stage_port for j in jobs
        )
        if has_stage:
            logger.info("Sliver TCP stage-listener already active on port %d", stage_port)
        else:
            try:
                await self._client.start_tcp_stager_listener(host="0.0.0.0", port=stage_port)
                logger.info("Started Sliver TCP stage-listener on 0.0.0.0:%d", stage_port)
            except Exception as exc:
                # Same check-then-act race as the mTLS listener above: one Sliver
                # server, many backends (one per xdist worker). Only one wins the
                # bind; the rest must not log this as an error.
                try:
                    jobs_after = await self._client.jobs()
                    now_has_stage = any(
                        (j.Protocol or "").lower() == "tcp" and j.Port == stage_port
                        for j in jobs_after
                    )
                except Exception:
                    now_has_stage = False
                if now_has_stage:
                    logger.info(
                        "TCP stage-listener on port %d already started by another "
                        "backend (lost start race) — continuing", stage_port,
                    )
                else:
                    logger.error(
                        "Failed to start TCP stage-listener on port %d: %s", stage_port, exc,
                    )

        # ── PivotSessions reconciliation ────────────────────────────────────
        # Mark active=False on PivotSessions rows whose 127.0.0.1:port has no
        # listener. The actual SOCKS5/portfwd tunnels die across backend
        # restarts; stale rows make Phase 4 of the soak (and the network map
        # UI) believe pivots are alive when they're not.
        import socket as _socket  # cycle-break

        from sqlalchemy import select as _select  # cycle-break
        from sqlalchemy.orm import Session as _Session  # cycle-break

        from fastapi_backend.database.db_writer import engine as _engine  # cycle-break
        from fastapi_backend.database.models import (
            PivotSessions as _PivotSessions,  # cycle-break
        )

        def _is_listener_alive(port: int) -> bool:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.settimeout(0.5)
            try:
                sock.connect(("127.0.0.1", port))
                return True
            except OSError:
                return False
            finally:
                with contextlib.suppress(OSError):
                    sock.close()

        try:
            with _Session(_engine) as db:
                rows = db.execute(
                    _select(_PivotSessions).where(_PivotSessions.active == True)  # noqa: E712
                ).scalars().all()
                to_deactivate = []
                for row in rows:
                    port = row.tcp_pivot_port or config.PIVOT_LISTENER_PORT
                    if not _is_listener_alive(port):
                        to_deactivate.append((row.id, row.pfsense_box_ip,
                                              row.team_num, row.pfsense_pivot_ip, port))
                total_rows = len(rows)
            deactivated = len(to_deactivate)
            if deactivated:
                ids_only = [rid for rid, *_ in to_deactivate]
                def _mark_inactive(adb: Any) -> None:
                    for rid in ids_only:
                        r = adb.get(_PivotSessions, rid)
                        if r is not None:
                            r.active = False
                write_actor.submit_sync(_mark_inactive, label="pivot_reconcile")
                logger.info(
                    "Reconciled PivotSessions: deactivated %d stale row(s) "
                    "whose 127.0.0.1:port had no listener",
                    deactivated,
                )
                for _rid, box_ip_, tnum, pip, prt in to_deactivate:
                    fire_and_forget(emit_alert(
                        "pivot_lost",
                        f"Pivot session stale for team {tnum}",
                        box_ip=box_ip_,
                        team_num=tnum,
                        details={"pivot_ip": pip, "port": prt, "reason": "stale_listener"},
                    ))
            else:
                logger.info(
                    "Reconciled PivotSessions: all %d active row(s) have live listeners",
                    total_rows,
                )
        except Exception as exc:
            logger.warning("PivotSessions reconciliation failed: %s", exc)

    async def shutdown(self) -> None:
        """Cancel the event subscription task and disconnect."""
        if self._event_task:
            self._event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._event_task
        if self._proc:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except TimeoutError:
                self._proc.kill()
        self.connected = False
        logger.info("Sliver shut down")

    def get_client(self) -> Any:
        """Return the connected SliverClient, or None if not available.

        On cache miss (``self.connected == False``), fires a fire-and-forget
        background reconnect (``_lazy_reconnect``). Caller gets None this call;
        the next call after ~30 s will find ``self.connected == True`` and
        receive the fresh client. Prefer this over blocking the event loop
        synchronously. Guarded so a non-loop caller (NXC worker thread) does
        not crash — those go through ``is_implant_active`` today, but the guard
        keeps this method safe to call from anywhere.
        """
        if self.connected and self._client is not None:
            return self._client
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._lazy_reconnect())
            self._lazy_reconnect_tasks.add(task)
            task.add_done_callback(self._lazy_reconnect_tasks.discard)
        except RuntimeError:
            pass
        return None

    def is_connected(self) -> bool:
        """Return if the SliverClient connection is establish with the server."""
        return self.connected

    def request_portfwd_rebuild_by_port(self, dead_port: int) -> None:
        """Fire-and-forget rebuild of a suspected-dead Sliver pivot portfwd.

        Called from the wrapper's SSH put-file path when transfers to
        ``127.0.0.1:<dead_port>`` exhaust retries with pure Timeout errors —
        the signature of a Sliver tunnel that has drifted into an idle-death
        state (see project_sliver_pivot_2min_tunnel_death). Reverse-looks-up
        the PortfwdMappings row for ``dead_port``, uses the Sliver client to
        register FRESH portfwds against the same pivot session on new local
        ports, updates the row so subsequent ``resolve_nxc_target`` calls
        return the fresh port.

        Best-effort: swallows all exceptions, logs failures. The current
        in-flight put-file has already failed — the rebuild helps the NEXT
        deploy_retry outer attempt succeed on the fresh portfwd.
        """
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._do_portfwd_rebuild(dead_port))
            self._lazy_reconnect_tasks.add(task)  # reuse the strong-ref set
            task.add_done_callback(self._lazy_reconnect_tasks.discard)
        except RuntimeError:
            # Called outside the running loop — nothing to do.
            pass
        except Exception as exc:
            logger.warning(
                "request_portfwd_rebuild_by_port(%d): scheduling failed: %s",
                dead_port, exc,
            )

    async def rebuild_portfwd_await(self, dead_port: int) -> int | None:
        """Await a portfwd rebuild for ``dead_port`` and return the fresh SSH port.

        Unlike the fire-and-forget ``request_portfwd_rebuild_by_port``, this is
        awaitable and returns the NEW ssh_local_port so the caller (the
        put_file retry loop) can re-target the current attempt onto the live
        tunnel — the fire-and-forget rebuild only ever helped a *future*
        deploy because the retry kept hammering the dead port (soak Y
        2026-08-28: 3×180s timeouts on the stale port blew the 600s budget).

        Concurrent callers for the same dead_port coalesce onto one in-flight
        task and all receive the same fresh port. Returns ``None`` when the
        rebuild can't help (client down, row already moved, pivot inactive,
        registration failed).
        """
        if not hasattr(self, "_portfwd_rebuild_tasks"):
            self._portfwd_rebuild_tasks = {}  # type: ignore[attr-defined]
        existing = self._portfwd_rebuild_tasks.get(dead_port)  # type: ignore[attr-defined]
        if existing is not None:
            try:
                return await existing
            except Exception:
                return None
        task = asyncio.ensure_future(self._do_portfwd_rebuild(dead_port))
        self._portfwd_rebuild_tasks[dead_port] = task  # type: ignore[attr-defined]
        try:
            return await task
        except Exception:
            return None
        finally:
            self._portfwd_rebuild_tasks.pop(dead_port, None)  # type: ignore[attr-defined]

    async def _do_portfwd_rebuild(self, dead_port: int) -> int | None:
        """Actual portfwd rebuild. Returns the fresh ssh_local_port, or None.

        Separated so request_portfwd_rebuild_by_port stays sync and
        rebuild_portfwd_await can await + read the new port.
        """
        from sqlalchemy import select  # cycle-break
        from sqlalchemy.orm import Session  # cycle-break

        from fastapi_backend.c2.pivot_helpers import _add_session_portfwd  # cycle-break
        from fastapi_backend.database.db_writer import engine  # cycle-break
        from fastapi_backend.database.models import (  # cycle-break
            PivotSessions,
            PortfwdMappings,
        )
        from fastapi_backend.database.write_actor import write_actor  # cycle-break

        # Guard: single-flight per port. Reuse the reconnect lock semantics —
        # if there's already a rebuild running for this port, drop this one.
        if not hasattr(self, "_portfwd_rebuilds_in_flight"):
            self._portfwd_rebuilds_in_flight = set()  # type: ignore[attr-defined]
        if dead_port in self._portfwd_rebuilds_in_flight:  # type: ignore[attr-defined]
            return None
        self._portfwd_rebuilds_in_flight.add(dead_port)  # type: ignore[attr-defined]

        try:
            client = self.get_client()
            if client is None:
                logger.info(
                    "portfwd rebuild for port %d: Sliver client unavailable, skipping",
                    dead_port,
                )
                return None

            with Session(engine) as db:
                mapping = db.execute(
                    select(PortfwdMappings).where(
                        (PortfwdMappings.ssh_local_port == dead_port)
                        | (PortfwdMappings.smb_local_port == dead_port),
                    )
                ).scalar_one_or_none()
                if mapping is None:
                    logger.info(
                        "portfwd rebuild for port %d: no PortfwdMappings row, skipping",
                        dead_port,
                    )
                    return None
                pivot = db.get(PivotSessions, mapping.pivot_session_id)
                if pivot is None or not pivot.active:
                    logger.info(
                        "portfwd rebuild for port %d: pivot inactive, skipping",
                        dead_port,
                    )
                    return None
                mapping_id = mapping.id
                session_id = pivot.pfsense_session_id
                box_ip = mapping.box_ip
                old_ssh = mapping.ssh_local_port
                old_smb = mapping.smb_local_port

            logger.warning(
                "portfwd rebuild: box=%s session=%s (old_ssh=%d, old_smb=%d) — dead_port=%d",
                box_ip, session_id, old_ssh, old_smb, dead_port,
            )

            # Register fresh portfwds. Old ones are left running on their
            # loopback listeners — they may still work for other consumers or
            # may 180s-timeout again; either way this row won't refer to them
            # anymore after the DB update below.
            new_ssh = await _add_session_portfwd(client, session_id, box_ip, 22)
            new_smb = await _add_session_portfwd(client, session_id, box_ip, 445)
            if new_ssh is None and new_smb is None:
                logger.warning(
                    "portfwd rebuild for box=%s: both fresh portfwds failed to register",
                    box_ip,
                )
                return None

            def _update_row(actor_db: Session, __id: int = mapping_id,
                            __new_ssh: int | None = new_ssh,
                            __new_smb: int | None = new_smb) -> None:
                row = actor_db.get(PortfwdMappings, __id)
                if row is None:
                    return
                if __new_ssh is not None:
                    row.ssh_local_port = __new_ssh
                if __new_smb is not None:
                    row.smb_local_port = __new_smb

            await write_actor.submit(_update_row, label="portfwd_rebuild_update")
            logger.info(
                "portfwd rebuild: box=%s DB updated (ssh: %s → %s, smb: %s → %s)",
                box_ip, old_ssh, new_ssh, old_smb, new_smb,
            )
            return new_ssh
        except Exception as exc:
            logger.warning(
                "portfwd rebuild for port %d failed: %s", dead_port, exc,
            )
            return None
        finally:
            self._portfwd_rebuilds_in_flight.discard(dead_port)  # type: ignore[attr-defined]

    def is_implant_active(self, box_ip: str, implant_name: str) -> bool:
        """Thread-safe check if a session/beacon with the given name is active on box_ip.

        Called from NXC worker threads to resolve SliverCheck effect status.
        Returns True if a matching active session or beacon is found, False otherwise.
        """
        if not implant_name:
            return False
        with self._implants_lock:
            cached_ips = self._active_implants.get(implant_name)
            result = bool(cached_ips and box_ip in cached_ips)
            if not result:
                logger.debug(
                    "is_implant_active MISS: name=%r box_ip=%r cached_ips=%r",
                    implant_name, box_ip, cached_ips,
                )
            return result

    def subscribe(self) -> asyncio.Queue:
        """Register a new SSE subscriber queue. Receives event dicts.

        Default cap of 100 entries — preserves historical behaviour for the
        alerts page, deploy/remediate listener, and frontend hooks. Soak
        Phase 0 subscribers should use `subscribe_bounded` if they want a
        different cap (e.g. larger queue under burst load).
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        q._drop_count = 0  # type: ignore[attr-defined]
        self._event_queues.append(q)
        return q

    def subscribe_bounded(self, maxsize: int) -> asyncio.Queue:
        """Register a subscriber with an explicit queue cap.

        Use this when the caller wants observability over drops (e.g. soak
        Phase 0 SSE stress). The returned queue carries a `_drop_count`
        attribute incremented on each publish-time drop and an `_is_bounded`
        marker so `publish_event` keeps it in the fanout list instead of
        unsubscribing on first drop. The global counter
        `dropped_events_total` is bumped in lockstep.

        Existing `subscribe()` callers keep the historical drop-and-unsub
        behaviour and are not affected by this constructor.
        """
        if maxsize <= 0:
            raise ValueError(f"subscribe_bounded: maxsize must be > 0, got {maxsize}")
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        q._drop_count = 0  # type: ignore[attr-defined]
        q._is_bounded = True  # type: ignore[attr-defined]
        self._event_queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        """Remove an SSE subscriber queue. Idempotent."""
        with contextlib.suppress(ValueError):
            self._event_queues.remove(q)

    async def publish(self, payload: dict) -> None:
        """Push a pre-formed event dict to all SSE subscriber queues.

        Unlike _broadcast_event (which maps a Sliver protobuf event), this
        accepts an already-serialised dict — used by non-Sliver code paths
        such as background deploy completion notifications.
        """
        self.publish_event(payload)

    def publish_event(self, payload: dict) -> None:
        """Synchronous publish — same fanout as `publish` but callable from non-async code.

        Drop policy: subscribers created via `subscribe()` are kicked off the
        fanout list when their queue fills (legacy behaviour preserved).
        Subscribers created via `subscribe_bounded()` are NOT kicked — the
        drop is counted on `q._drop_count` and on `dropped_events_total` so
        the caller can observe backpressure without losing the channel.
        """
        dead: list[asyncio.Queue] = []
        for q in self._event_queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Bounded subscribers absorb the drop and keep the channel;
                # legacy subscribers (default subscribe()) get dropped — they
                # have no observability hook so leaving them in the fanout
                # would silently leak memory in the historical worst case.
                self.dropped_events_total += 1
                with contextlib.suppress(Exception):
                    q._drop_count = getattr(q, "_drop_count", 0) + 1  # type: ignore[attr-defined]
                # Legacy subscribers (created via subscribe()) get unsubscribed
                # on first drop; bounded subscribers stay in the fanout list.
                if not getattr(q, "_is_bounded", False):
                    dead.append(q)
        for q in dead:
            self.unsubscribe(q)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _wait_for_server(self, cfg_path: str, max_wait: int = 30) -> None:
        """Poll until Sliver gRPC becomes reachable (max_wait seconds)."""
        deadline = asyncio.get_event_loop().time() + max_wait
        while asyncio.get_event_loop().time() < deadline:
            try:
                cfg = SliverClientConfig.parse_config_file(cfg_path)
                test_client = SliverClient(cfg)
                await test_client.connect()
                return
            except Exception:
                await asyncio.sleep(1)
        raise RuntimeError(f"Sliver server did not become ready within {max_wait}s")

    def _get_reconnect_lock(self) -> asyncio.Lock:
        if self._reconnect_lock is None:
            self._reconnect_lock = asyncio.Lock()
        return self._reconnect_lock

    async def _reconnect_client(self, timeout: float = 30.0) -> None:
        """Build a fresh SliverClient and connect, with a bounded timeout.

        On failure leaves ``self._client = None`` + ``self.connected = False`` so
        get_client()'s "connected client vs no client" check is unambiguous —
        the pre-2026-08-25 code left a broken half-init client stashed on self,
        which caused _handle_events' ``async for .on()`` to hang forever.
        """
        if not self._cfg_path or not self._cfg_path.exists():
            raise RuntimeError(f"Sliver config path missing: {self._cfg_path}")
        cfg = _parse_sliver_config(str(self._cfg_path))
        client = SliverClient(cfg)
        try:
            await asyncio.wait_for(client.connect(), timeout=timeout)
        except Exception:
            self._client = None
            self.connected = False
            raise
        self._client = client
        self.connected = True
        try:
            from fastapi_backend.c2 import sliver_debug  # cycle-break
            sliver_debug.install_channel_tracer(client)
        except Exception as exc:
            logger.warning("sliver_debug.install_channel_tracer failed: %s", exc)

    async def _handle_events(self) -> None:
        """Subscribe to Sliver events. Reconnects automatically on transient errors.

        Loop invariant: at the top of each iteration we either have a connected
        client (fall through to consume events) or we don't (attempt reconnect,
        sleep on failure, loop). Never enters ``async for .on(...)`` on a broken
        client — that path hangs forever on sliver-py's iterator and caused
        the 2 h wedge in the 2026-08-24 soak.
        """
        retry_delay = 5
        while True:
            # Terminal exit if the manager was never fully wired (cfg missing
            # entirely) — matches the pre-2026-08-25 early-exit semantics for
            # tests that spawn this coroutine on a bare SliverManager without
            # calling startup(). A missing-BUT-configured cfg_path (e.g. Sliver
            # server is down and its file was deleted) is still retryable and
            # falls through to the reconnect branch below.
            if self._cfg_path is None:
                return
            if not self.connected:
                async with self._get_reconnect_lock():
                    if not self.connected:
                        try:
                            await self._reconnect_client(timeout=30)
                            logger.info("Sliver event stream reconnected")
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.warning(
                                "Sliver reconnect failed: %s — retry in %ds",
                                exc, retry_delay,
                            )
                            await asyncio.sleep(retry_delay)
                            continue
            try:
                async for event in self._client.on([
                    "session-connected",
                    "session-disconnected",
                    "beacon-registered",
                    "beacon-taskresult",
                    "beacon-disconnected",
                ]):
                    try:
                        await self._process_event(event)
                        await self._broadcast_event(event)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("Error processing event %s: %s", event.EventType, exc)
                logger.warning("Sliver event stream ended — will reconnect in %ds", retry_delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Sliver event stream error: %s — will reconnect in %ds", exc, retry_delay)

            self.connected = False
            await asyncio.sleep(retry_delay)

    async def _lazy_reconnect(self) -> None:
        """Single-flight background reconnect kicked by get_client() on a cache miss.

        Coalesces with any in-flight reconnect (via ``_get_reconnect_lock``): if
        _handle_events' loop is already reconnecting we return immediately. Fire-
        and-forget from the caller's perspective — the *next* get_client() after
        this task completes will see ``self.connected == True``.
        """
        lock = self._get_reconnect_lock()
        if lock.locked():
            return
        async with lock:
            if self.connected:
                return
            try:
                await self._reconnect_client(timeout=30)
                logger.info("Sliver lazy reconnect succeeded")
            except Exception as exc:
                logger.warning("Sliver lazy reconnect failed: %s", exc)

    def _emit_alert_threadsafe(self, *args: Any, **kwargs: Any) -> None:
        """Schedule emit_alert on the main loop from any thread.

        _process_event_sync runs in a thread-pool executor where there is no
        running event loop, so asyncio.create_task / fire_and_forget would
        raise "no running event loop" and the broad except in the caller
        would silently swallow the alert. run_coroutine_threadsafe submits
        the coroutine onto the captured main loop and returns a Future we
        deliberately don't await — fire-and-forget semantics, but valid
        from a non-loop thread.

        Falls back to fire_and_forget when invoked from the loop thread
        directly (e.g. unit tests calling _process_event_sync without going
        through the async _process_event wrapper).
        """
        loop = self._main_loop
        if loop is None or loop.is_closed():
            coro = emit_alert(*args, **kwargs)
            try:
                fire_and_forget(coro)
            except RuntimeError:
                coro.close()  # avoid "coroutine was never awaited" warning
                logger.error("emit_alert dropped: no main loop and no running loop (kind=%s)", args[0] if args else "?")
            return
        asyncio.run_coroutine_threadsafe(emit_alert(*args, **kwargs), loop)

    def _process_event_sync(self, event: Any) -> None:
        """Update Boxes table (sync) when sessions/beacons connect or disconnect."""
        from sqlalchemy.orm import Session  # cycle-break

        from fastapi_backend.database.db_writer import engine  # cycle-break
        from fastapi_backend.database.models import Boxes  # cycle-break
        from fastapi_backend.utils.db_helpers import query_one  # cycle-break

        etype = event.EventType
        try:
            with Session(engine) as db:
                if etype == "session-connected" and event.Session:
                    s = event.Session
                    raw_addr = s.RemoteAddress or ""
                    ip = _extract_box_ip(raw_addr)
                    box = _box_for_session(db, getattr(s, "Hostname", None), ip)
                    # Resolved box_ip drives every downstream (cache, auto-reg,
                    # backfill). Raw `ip` is the wire address — for NAT'd or
                    # pivoted implants that's the firewall's WAN IP, not the
                    # box's identity, so falling back to raw ip when box is
                    # None keeps the legacy IP-only path.
                    box_ip = box.box_ip if box else ip
                    if box:
                        _sid = s.ID
                        _now = datetime.now(UTC)
                        _bip = box.box_ip
                        def _wr(adb: Session, __bip=_bip, __sid=_sid, __now=_now) -> None:
                            b = query_one(adb, Boxes, box_ip=__bip)
                            if b:
                                b.session_id = __sid
                                b.last_seen = __now
                        write_actor.submit_sync(_wr, label="event_session_connected")
                    # Update active implant cache for SliverCheck scoring
                    if s.Name:
                        with self._implants_lock:
                            self._active_implants.setdefault(s.Name, set()).add(box_ip)
                    # Auto-register SliverCheck IOCs matching this implant name
                    if s.Name:
                        _auto_register_implant(db, box_ip, s.Name, engine)
                        _backfill_inject_identifier(db, box_ip, s.Name)

                elif etype == "session-disconnected" and event.Session:
                    s = event.Session
                    box = query_one(db, Boxes, session_id=s.ID)
                    if box:
                        _bip = box.box_ip
                        _tnum = box.team_num
                        _sid = s.ID
                        def _wr(adb: Session, __bip=_bip, __sid=_sid) -> None:
                            b = query_one(adb, Boxes, session_id=__sid) or \
                                query_one(adb, Boxes, box_ip=__bip)
                            if b:
                                b.session_id = None
                        write_actor.submit_sync(_wr, label="event_session_disconnected")
                        # Emit session_lost alert (thread-safe — this method
                        # runs in a thread executor, no running loop here)
                        self._emit_alert_threadsafe(
                            "session_lost",
                            f"Lost session on {_bip}",
                            box_ip=_bip,
                            team_num=_tnum,
                            details={"session_id": s.ID, "session_name": s.Name},
                        )
                    # Remove this specific box_ip from active implant cache
                    _disc_ip = box.box_ip if box else _extract_box_ip(s.RemoteAddress or "")
                    if s.Name and _disc_ip:
                        with self._implants_lock:
                            _ips = self._active_implants.get(s.Name)
                            if _ips:
                                _ips.discard(_disc_ip)
                                if not _ips:
                                    del self._active_implants[s.Name]
                    # Intentional kill: flip auto-registered IOCs to 'lost'
                    _handle_implant_disconnect(db, s.ID, box.box_ip if box else None, s.Name)

                elif etype == "beacon-registered":
                    b = getattr(event, "Beacon", None)
                    if b:
                        raw_addr = b.RemoteAddress or ""
                        ip = _extract_box_ip(raw_addr)
                        logger.info(
                            "beacon-registered: name=%r raw_addr=%r extracted_ip=%r",
                            b.Name, raw_addr, ip,
                        )
                        box = _box_for_session(db, getattr(b, "Hostname", None), ip)
                        box_ip = box.box_ip if box else ip
                        if box:
                            _bip = box.box_ip
                            _bid = b.ID
                            _bint = b.Interval
                            _now = datetime.now(UTC)
                            def _wr(adb: Session, __bip=_bip, __bid=_bid, __bint=_bint, __now=_now) -> None:
                                bx = query_one(adb, Boxes, box_ip=__bip)
                                if bx:
                                    bx.beacon_id = __bid
                                    bx.last_seen = __now
                                    bx.beacon_interval_ns = __bint
                            write_actor.submit_sync(_wr, label="event_beacon_registered")
                            # Clear beacon stale alert tracking (beacon checked in successfully)
                            clear_beacon_stale_alert(b.ID)
                        # Update active implant cache for SliverCheck scoring
                        if b.Name:
                            with self._implants_lock:
                                self._active_implants.setdefault(b.Name, set()).add(box_ip)
                        # Auto-register SliverCheck IOCs matching this implant name
                        if b.Name:
                            _auto_register_implant(db, box_ip, b.Name, engine)
                            # Injected shellcode calls home as a BEACON (is_beacon=True),
                            # so inject IOCs (preferred_type="beacon") backfill here — not
                            # only in the session-connected branch. Without this the
                            # DeployedIOCs row's implant_identifier stays NULL and the
                            # SliverCheck scores -1.
                            _backfill_inject_identifier(db, box_ip, b.Name)

                elif etype == "beacon-disconnected":
                    b = getattr(event, "Beacon", None)
                    if b:
                        box = query_one(db, Boxes, beacon_id=b.ID)
                        if box:
                            _bid = b.ID
                            def _wr(adb: Session, __bid=_bid) -> None:
                                bx = query_one(adb, Boxes, beacon_id=__bid)
                                if bx:
                                    bx.beacon_id = None
                            write_actor.submit_sync(_wr, label="event_beacon_disconnected")
                            # Emit beacon_lost alert (thread-safe — sync method
                            # runs in thread executor with no running loop)
                            self._emit_alert_threadsafe(
                                "beacon_lost",
                                f"Lost beacon on {box.box_ip}",
                                box_ip=box.box_ip,
                                team_num=box.team_num,
                                details={"beacon_id": b.ID, "beacon_name": b.Name},
                            )
                        # Remove this specific box_ip from active implant cache
                        _disc_ip = box.box_ip if box else _extract_box_ip(b.RemoteAddress or "")
                        if b.Name and _disc_ip:
                            with self._implants_lock:
                                _ips = self._active_implants.get(b.Name)
                                if _ips:
                                    _ips.discard(_disc_ip)
                                    if not _ips:
                                        del self._active_implants[b.Name]
                        # Intentional kill: flip auto-registered IOCs to 'lost'
                        _handle_implant_disconnect(db, b.ID, box.box_ip if box else None, b.Name)

        except Exception as exc:
            logger.error(f"Error processing Sliver event {etype}: {exc}")

    async def _process_event(self, event: Any) -> None:
        """Dispatch event to sync DB update on a thread executor, then handle pivot automation."""
        loop = asyncio.get_running_loop()
        # Capture loop ref so _process_event_sync (executor thread) can submit
        # emit_alert coroutines back onto the main loop via run_coroutine_threadsafe.
        self._main_loop = loop
        await loop.run_in_executor(None, self._process_event_sync, event)
        if event.EventType == "session-connected" and event.Session:
            ip = event.Session.RemoteAddress.split(":")[0]
            _log_session_lifecycle("connected", event.Session)
            fire_and_forget(self._maybe_setup_pivot(event.Session.ID, ip))
        elif event.EventType == "session-disconnected" and event.Session:
            _log_session_lifecycle("disconnected", event.Session)
            fire_and_forget(self._teardown_pivot(event.Session.ID))

    async def _maybe_setup_pivot(self, session_id: str, ip: str) -> None:
        """If the connecting session is a pivot_gateway implant, set up portfwd rules."""
        logger.info("_maybe_setup_pivot called for session=%s ip=%s", session_id, ip)
        if ip in self._pivot_setup_ips:
            logger.info("_maybe_setup_pivot: already in progress for ip=%s, skipping session=%s", ip, session_id)
            return
        self._pivot_setup_ips.add(ip)
        try:
            await self._do_setup_pivot(session_id, ip)
        finally:
            self._pivot_setup_ips.discard(ip)

    async def _do_setup_pivot(self, session_id: str, ip: str) -> None:
        from sqlalchemy import delete, select  # cycle-break
        from sqlalchemy.orm import Session  # cycle-break

        from fastapi_backend.c2.pivot_helpers import (  # cycle-break
            _add_session_portfwd,
            _start_tcp_pivot_listener,
            _stop_stale_pivot_listeners,
        )
        from fastapi_backend.core.config import config  # cycle-break
        from fastapi_backend.database.db_writer import engine  # cycle-break
        from fastapi_backend.database.models import (  # cycle-break
            Boxes,
            ImplantBuilds,
            PivotSessions,
            PortfwdMappings,
        )

        # Phase 1: locate the pivot_gateway build with retry.
        # The generate endpoint may commit the ImplantBuilds row *after* the newly
        # deployed pfSense implant has already connected back — a race between
        # compilation/commit time and implant start-up.  Retry for up to ~5 min.
        build_id: str | None = None
        # Per-team fallback port (PIVOT_LISTENER_PORT_BASE + team_num) used when
        # the build row carries no explicit tcp_pivot_port. Avoids the soak
        # finding where all teams shared 9898 and only one bound successfully.
        team_num_for_fallback: int | None = None
        tcp_pivot_port: int = config.PIVOT_LISTENER_PORT
        # The connecting `ip` may be the firewall's WAN address (it calls home
        # from its WAN); every DB key here — the pivot-gateway build's
        # last_deployed_to, PivotSessions.pfsense_box_ip — is the LAN box_ip the
        # implant was deployed to. Resolve WAN->LAN once and key on that, else the
        # build lookup misses and no pivot ever establishes (docs/PLAN_firewall_wan_lan.md).
        resolved_box_ip: str = ip
        # Test-only delay: simulate compile-commit lag for pivot retry test.
        if config.PIVOT_BUILD_COMMIT_DELAY_OVERRIDE_SEC:
            logger.info(
                "_maybe_setup_pivot: PIVOT_BUILD_COMMIT_DELAY_OVERRIDE_SEC=%s — sleeping before first lookup",
                config.PIVOT_BUILD_COMMIT_DELAY_OVERRIDE_SEC,
            )
            await asyncio.sleep(config.PIVOT_BUILD_COMMIT_DELAY_OVERRIDE_SEC)
        for attempt in range(30):
            with Session(engine) as db:
                if attempt == 0:
                    box_chk = _box_for_address(db, ip)
                    if box_chk is None:
                        # This session is for a box NOT in this backend's own
                        # Boxes table. Under box-scoped test inventory
                        # (docs/PLAN_box_scoped_live_workers.md) each worker's
                        # backend loads only its own box(es), yet EVERY backend
                        # receives EVERY session-connected event via the shared
                        # Sliver gRPC stream. A pivot_gateway build for a box we
                        # don't own can only ever exist on the OWNING worker's
                        # DB — that worker gets the same event and sets up the
                        # pivot. Waiting here is pointless: the row will never
                        # appear locally, so we'd burn the full 30x3s (~90s)
                        # retry loop for nothing. This was ~250 such pointless
                        # 90s waits per full run (non-firewall + cross-worker
                        # boxes) — the old code fell through to the wait when
                        # box_chk was None, which was rare when each worker
                        # loaded a whole team but became the common case under
                        # narrow per-worker inventory. Only the worker actively
                        # BUILDING this box's implant (the box is in its
                        # inventory) should wait for the commit race.
                        logger.info(
                            "_maybe_setup_pivot: ip=%s not in this backend's "
                            "inventory (cross-worker Sliver event) — not our "
                            "box, skipping", ip,
                        )
                        return
                    # Skip immediately if this IP is not a firewall box.
                    if box_chk.os != "firewall":
                        logger.info("_maybe_setup_pivot: ip=%s is not a firewall box, skipping", ip)
                        return
                    team_num_for_fallback = box_chk.team_num
                    resolved_box_ip = box_chk.box_ip
                row = db.execute(
                    select(ImplantBuilds).where(
                        ImplantBuilds.last_deployed_to == resolved_box_ip,
                        ImplantBuilds.pivot_gateway,
                    ).order_by(ImplantBuilds.created_at.desc())
                ).scalars().first()
                if not row:
                    # Fallback: the deploy execute may have been misreported as failed
                    # (e.g. FreeBSD daemon launch succeeded but NXC reported failure).
                    # Only match builds where last_deployed_to is NULL — a build that was
                    # successfully deployed to a *different* IP must not be hijacked here.
                    row = db.execute(
                        select(ImplantBuilds).where(
                            ImplantBuilds.pivot_gateway,
                            ImplantBuilds.os == "freebsd",
                            ImplantBuilds.last_deployed_to.is_(None),
                        ).order_by(ImplantBuilds.created_at.desc())
                    ).scalars().first()
                    if row:
                        logger.warning(
                            "_maybe_setup_pivot: ip=%s last_deployed_to lookup failed; "
                            "using freebsd fallback build id=%s",
                            ip, row.id,
                        )
                if row:
                    build_id = row.id
                    if row.tcp_pivot_port:
                        tcp_pivot_port = row.tcp_pivot_port
                    elif team_num_for_fallback is not None:
                        tcp_pivot_port = (
                            config.PIVOT_LISTENER_PORT_BASE + team_num_for_fallback
                        )
                    else:
                        tcp_pivot_port = config.PIVOT_LISTENER_PORT
                    break
            if attempt == 0:
                logger.info("_maybe_setup_pivot: no pivot_gateway build for ip=%s yet, will retry", ip)
            await asyncio.sleep(3)

        if not build_id:
            logger.info("_maybe_setup_pivot: no pivot_gateway build for ip=%s after retries, skipping", ip)
            return

        logger.info("_maybe_setup_pivot: found build id=%s for ip=%s", build_id, ip)
        try:
            with Session(engine) as db:

                # Idempotency: skip if this exact session already has an active pivot row.
                existing = db.execute(
                    select(PivotSessions).where(
                        PivotSessions.pfsense_session_id == session_id,
                        PivotSessions.active,
                    )
                ).scalar_one_or_none()
                if existing:
                    logger.info("Pivot already active for session %s, skipping", session_id)
                    return

                # Guard: if an *active* pivot row already exists for this IP under a
                # different session, check whether that session is still alive before
                # tearing it down.  pfSense implants reconnect frequently; each
                # reconnect fires session-connected, and a naive teardown-rebuild
                # disrupts in-flight NXC deploys through the existing portfwds.
                other_active = db.execute(
                    select(PivotSessions).where(
                        PivotSessions.pfsense_box_ip == resolved_box_ip,
                        PivotSessions.active,
                    )
                ).scalar_one_or_none()
                if other_active:
                    _guard_client = self.get_client()
                    if _guard_client is not None:
                        try:
                            _live_ids = {s.ID for s in await _guard_client.sessions()}
                            if other_active.pfsense_session_id in _live_ids:
                                logger.info(
                                    "_maybe_setup_pivot: existing pivot session %s for ip=%s"
                                    " still alive — skipping rebuild for new session %s",
                                    other_active.pfsense_session_id, ip, session_id,
                                )
                                return
                            logger.info(
                                "_maybe_setup_pivot: old pivot session %s for ip=%s confirmed"
                                " dead — rebuilding for new session %s",
                                other_active.pfsense_session_id, ip, session_id,
                            )
                        except Exception as _liveness_exc:
                            logger.warning(
                                "_maybe_setup_pivot: session liveness check failed (%s)"
                                " — proceeding with rebuild",
                                _liveness_exc,
                            )

                # Supersede any old pivot rows for the same pfSense IP (new
                # session replaces old). Route through write_actor so the outer
                # read Session doesn't take the write lock across the awaits
                # that follow.
                old_ids = [
                    r.id for r in db.execute(
                        select(PivotSessions).where(
                            PivotSessions.pfsense_box_ip == resolved_box_ip,
                        ),
                    ).scalars().all()
                ]
                if old_ids:
                    _ids = old_ids
                    def _supersede(adb: Session) -> None:
                        adb.execute(
                            delete(PortfwdMappings)
                            .where(PortfwdMappings.pivot_session_id.in_(_ids)),
                        )
                        for oid in _ids:
                            row = adb.get(PivotSessions, oid)
                            if row is not None:
                                adb.delete(row)
                    await write_actor.submit(_supersede, label="pivot_supersede_old_rows")
                    logger.info("_maybe_setup_pivot: removed %d old pivot row(s) for ip=%s", len(old_ids), ip)

                client = self.get_client()
                if client is None:
                    logger.warning("_maybe_setup_pivot: no sliver client, skipping")
                    return
                # Portfwd listens on C2 localhost; NXC connects to 127.0.0.1:port
                pfsense_pivot_ip = "127.0.0.1"

                box = _box_for_address(db, ip)
                if not box:
                    logger.warning("_maybe_setup_pivot: no box found for ip=%s", ip)
                    return

                # Prune any stale pivot listeners left on this session by an
                # earlier setup on a different (rotated) port before binding the
                # current one — stacked roots poison Sliver's pivot graph and
                # OOM attached consoles (see _stop_stale_pivot_listeners).
                await _stop_stale_pivot_listeners(client, session_id, tcp_pivot_port)
                logger.info("_maybe_setup_pivot: starting TCP pivot listener on port=%d for session=%s", tcp_pivot_port, session_id)
                await _start_tcp_pivot_listener(client, session_id, tcp_pivot_port)

                internal_boxes = db.execute(
                    select(Boxes)
                    .where(Boxes.team_num == box.team_num, Boxes.os != "firewall")
                    .order_by(Boxes.box_ip)
                ).scalars().all()

                # Set up the portfwd tunnels FIRST, while holding NO write
                # transaction. Each _add_session_portfwd is a Sliver gRPC round-trip;
                # doing them between db.flush() and db.commit() (as this used to)
                # held SQLite's single write lock across ~8 network calls — 10-30s+
                # under the pivot compile storm. Because _do_setup_pivot runs as a
                # fire_and_forget coroutine, every `await` inside that held
                # transaction handed control to the concurrent pivot-generate route,
                # whose update_box_progress write then blocked on the lock past the
                # busy_timeout -> 'database is locked' (the whole of failure-bucket A;
                # a task_tracker retry can't help because the lock is held by THIS
                # coroutine for the entire loop). Gather the mappings over the network
                # first, then write pivot_row + all PortfwdMappings in one short
                # transaction with NO awaits between flush and commit.
                internal_ips = [ibox.box_ip for ibox in internal_boxes]
                portfwd_maps: list[tuple[str, int, int]] = []
                for bip in internal_ips:
                    smb_local = await _add_session_portfwd(client, session_id, bip, 445)
                    ssh_local = await _add_session_portfwd(client, session_id, bip, 22)
                    if smb_local and ssh_local:
                        portfwd_maps.append((bip, smb_local, ssh_local))
                    else:
                        logger.warning(
                            "portfwd setup incomplete for %s (smb=%s ssh=%s)",
                            bip, smb_local, ssh_local,
                        )

                # Persist pivot_row + PortfwdMappings through the single-writer
                # actor. Reads above (box, internal_boxes) and awaits (listener,
                # portfwd calls) are done; this is a short pure-write transaction.
                team_num = box.team_num
                box_ip = box.box_ip
                def _persist_pivot(actor_db: Session) -> None:
                    prow = PivotSessions(
                        team_num=team_num,
                        pfsense_session_id=session_id,
                        pfsense_box_ip=box_ip,  # LAN identity, not the WAN call-home ip
                        pfsense_pivot_ip=pfsense_pivot_ip,
                        tcp_pivot_port=tcp_pivot_port,
                        active=True,
                    )
                    actor_db.add(prow)
                    actor_db.flush()
                    for bip, smb_local, ssh_local in portfwd_maps:
                        actor_db.add(PortfwdMappings(
                            pivot_session_id=prow.id,
                            box_ip=bip,
                            smb_local_port=smb_local,
                            ssh_local_port=ssh_local,
                        ))
                await write_actor.submit(_persist_pivot, label="_do_setup_pivot_persist")
                logger.info(
                    "Pivot session established for team %s via %s (pivot_ip: %s)",
                    team_num, ip, pfsense_pivot_ip,
                )
        except Exception as exc:
            logger.error("Error setting up pivot for session %s: %s", session_id, exc)

    async def _cache_refresh_loop(self) -> None:
        """Periodically refresh _active_implants from the live Sliver beacon/session list.

        Catches beacons that don't fire beacon-registered (e.g. raw Generate API beacons
        used by inject-shellcode).  Runs every 20 s so the check pipeline sees them within
        one poll cycle.
        """
        while True:
            await asyncio.sleep(20)
            if not self.connected:
                continue
            client = self.get_client()
            if client is None:
                continue
            try:
                sessions = await client.sessions()
                beacons = await client.beacons()
                # Resolve each (hostname, wire-ip) to the owning Box up front so
                # every downstream (cache, backfill) keys on box_ip — never on
                # the WAN IP of a NAT'ing firewall.
                from sqlalchemy.orm import (
                    Session as _RefreshSession,  # cycle-break / lazy-load
                )

                from fastapi_backend.database.db_writer import (
                    engine as _refresh_engine,  # cycle-break / lazy-load
                )
                updates: dict[str, set[str]] = {}
                with _RefreshSession(_refresh_engine) as _rdb:
                    for s in sessions:
                        if not getattr(s, "IsDead", False) and s.Name and s.RemoteAddress:
                            _ip = _extract_box_ip(s.RemoteAddress)
                            _box = _box_for_session(_rdb, getattr(s, "Hostname", None), _ip)
                            updates.setdefault(s.Name, set()).add(_box.box_ip if _box else _ip)
                    for b in beacons:
                        if not getattr(b, "IsDead", False) and b.Name and b.RemoteAddress:
                            _ip = _extract_box_ip(b.RemoteAddress)
                            _box = _box_for_session(_rdb, getattr(b, "Hostname", None), _ip)
                            updates.setdefault(b.Name, set()).add(_box.box_ip if _box else _ip)
                with self._implants_lock:
                    # Full replacement so deleted/removed implants fall out
                    # of the cache (e.g. beacons removed via RmBeacon that
                    # don't fire beacon-disconnected).
                    self._active_implants.clear()
                    self._active_implants.update(updates)
                logger.debug("implant cache refresh: %d active implants", len(updates))
                # Backfill inject DeployedIOCs whose implant_identifier is still NULL.
                # Covers beacons whose beacon-registered event was missed (gRPC/fork
                # interference from cred harvester subprocesses drops event stream frames).
                try:
                    with _RefreshSession(_refresh_engine) as _rdb:
                        for _name, _ips in updates.items():
                            for _ip in _ips:
                                _backfill_inject_identifier(_rdb, _ip, _name)
                except Exception as _bfe:
                    logger.debug("cache refresh backfill error: %s", _bfe)
            except Exception as exc:
                logger.debug("implant cache refresh error: %s", exc)

    async def _populate_implant_cache(self) -> None:
        """On startup, populate the active implant cache and sync Boxes session/beacon IDs."""
        await asyncio.sleep(1)
        client = self.get_client()
        if client is None:
            return
        try:
            from sqlalchemy.orm import Session  # lazy-load

            from fastapi_backend.database.db_writer import engine  # lazy-load
            from fastapi_backend.database.models import Boxes  # lazy-load
            from fastapi_backend.utils.db_helpers import query_one  # lazy-load

            sessions = await client.sessions()
            beacons = await client.beacons()
            # Resolve each (hostname, wire-ip) to the owning Box up front so the
            # cache keys on box_ip — never on the WAN IP of a NAT'ing firewall,
            # which would poison every SliverCheck lookup for a NAT'd box.
            new_cache: dict[str, set[str]] = {}
            with Session(engine) as _seed_db:
                for s in sessions:
                    if not getattr(s, "IsDead", False) and s.Name and s.RemoteAddress:
                        ip = s.RemoteAddress.split(":")[0]
                        box = _box_for_session(_seed_db, getattr(s, "Hostname", None), ip)
                        new_cache.setdefault(s.Name, set()).add(box.box_ip if box else ip)
                for b in beacons:
                    if not getattr(b, "IsDead", False) and b.Name and b.RemoteAddress:
                        ip = b.RemoteAddress.split(":")[0]
                        box = _box_for_session(_seed_db, getattr(b, "Hostname", None), ip)
                        new_cache.setdefault(b.Name, set()).add(box.box_ip if box else ip)
            with self._implants_lock:
                for name, ips in new_cache.items():
                    self._active_implants.setdefault(name, set()).update(ips)
            logger.info("Populated implant cache: %d active implants", len(new_cache))

            # Sync Boxes.session_id / beacon_id so SA collection and scoring work
            # immediately after a backend restart (no need to wait for session-connected events).
            # _box_for_session handles both cases uniformly: TCP-pivot sessions
            # ("->" in RemoteAddress, gateway IP) AND NAT'd non-pivot sessions
            # whose RemoteAddress is the team firewall's WAN IP resolve via
            # (team-from-IP, hostname-within-team). Falls back to IP-only when
            # hostname is empty or doesn't match — firewalls still bind.
            with Session(engine) as db:
                session_updates: list[tuple[str, str]] = []
                for s in sessions:
                    if not getattr(s, "IsDead", False) and s.RemoteAddress:
                        raw_addr = s.RemoteAddress or ""
                        ip = raw_addr.split(":")[0]
                        box = _box_for_session(db, getattr(s, "Hostname", None), ip)
                        if box and box.session_id != s.ID:
                            session_updates.append((box.box_ip, s.ID))
                beacon_updates: list[tuple[str, str]] = []
                for b in beacons:
                    if b.RemoteAddress:
                        raw_addr = b.RemoteAddress or ""
                        ip = raw_addr.split(":")[0]
                        box = _box_for_session(db, getattr(b, "Hostname", None), ip)
                        if box and box.beacon_id != b.ID:
                            beacon_updates.append((box.box_ip, b.ID))
            if session_updates or beacon_updates:
                def _sync(adb: Session) -> None:
                    for bip, sid in session_updates:
                        bx = query_one(adb, Boxes, box_ip=bip)
                        if bx:
                            bx.session_id = sid
                    for bip, bid in beacon_updates:
                        bx = query_one(adb, Boxes, box_ip=bip)
                        if bx:
                            bx.beacon_id = bid
                await write_actor.submit(_sync, label="_populate_implant_cache_sync")
            logger.info("Synced Boxes session/beacon IDs from %d sessions, %d beacons",
                        len(sessions), len(beacons))

            # Auto-register existing sessions/beacons so DeployedIOCs rows exist
            # after a backend restart without waiting for new session-connected events.
            # Idempotent — _auto_register_implant skips rows already 'active'.
            # Skip TCP-pivot sessions (RemoteAddress contains "->") to match event handler.
            # Resolve box_ip via _box_for_session — raw ip is WAN for NAT'd boxes
            # and would create the DeployedIOCs row against the firewall, not the
            # box the implant actually landed on.
            with Session(engine) as reg_db:
                for s in sessions:
                    raw = s.RemoteAddress or ""
                    if not getattr(s, "IsDead", False) and s.Name and raw and "->" not in raw:
                        ip = raw.split(":")[0]
                        if ip:
                            box = _box_for_session(reg_db, getattr(s, "Hostname", None), ip)
                            _box_ip = box.box_ip if box else ip
                            _auto_register_implant(reg_db, _box_ip, s.Name, engine)
                            _backfill_inject_identifier(reg_db, _box_ip, s.Name)
                for b in beacons:
                    raw = b.RemoteAddress or ""
                    if b.Name and raw and "->" not in raw:
                        ip = raw.split(":")[0]
                        if ip:
                            box = _box_for_session(reg_db, getattr(b, "Hostname", None), ip)
                            _box_ip = box.box_ip if box else ip
                            _auto_register_implant(reg_db, _box_ip, b.Name, engine)
                            _backfill_inject_identifier(reg_db, _box_ip, b.Name)
        except Exception as exc:
            logger.warning("Failed to populate implant cache: %s", exc)

    async def _bootstrap_existing_pivots(self) -> None:
        """On startup, run _maybe_setup_pivot for any sessions already connected."""
        await asyncio.sleep(2)  # let event subscription settle
        client = self.get_client()
        if client is None:
            return
        try:
            sessions = await client.sessions()
            for s in sessions:
                ip = s.RemoteAddress.split(":")[0]
                fire_and_forget(self._maybe_setup_pivot(s.ID, ip))
        except Exception as exc:
            logger.warning("Bootstrap pivot check failed: %s", exc)

    async def _teardown_pivot(self, session_id: str) -> None:
        """Mark pivot session inactive when the pfSense session disconnects."""
        from sqlalchemy import select  # cycle-break
        from sqlalchemy.orm import Session  # cycle-break

        from fastapi_backend.database.db_writer import engine  # cycle-break
        from fastapi_backend.database.models import PivotSessions  # cycle-break

        try:
            with Session(engine) as db:
                pivot = db.execute(
                    select(PivotSessions).where(PivotSessions.pfsense_session_id == session_id)
                ).scalar_one_or_none()
                if not (pivot and pivot.active):
                    return
                pivot_id = pivot.id
                team_num = pivot.team_num
                box_ip_ = pivot.pfsense_box_ip
                pivot_ip_ = pivot.pfsense_pivot_ip
            def _mark_inactive(adb: Session) -> None:
                p = adb.get(PivotSessions, pivot_id)
                if p is not None:
                    p.active = False
            await write_actor.submit(_mark_inactive, label="_teardown_pivot")
            logger.info("Pivot session deactivated for session %s", session_id)
            # Emit pivot_lost alert
            fire_and_forget(emit_alert(
                "pivot_lost",
                f"Lost pivot session for team {team_num}",
                box_ip=box_ip_,
                team_num=team_num,
                details={"session_id": session_id, "pivot_ip": pivot_ip_},
            ))
        except Exception as exc:
            logger.error("Error tearing down pivot for session %s: %s", session_id, exc)

    async def _broadcast_event(self, event: dict[str, Any]) -> None:
        """Push event dict to all SSE subscriber queues.

        Enriches session/beacon events with the resolved Box (box_ip, box_name,
        team_num) via the same `_box_for_session` team-first lookup the DB
        write path uses. Without this, SSE subscribers only see Sliver's wire
        `remote_address` — the firewall's WAN IP for NAT'd/pivoted implants —
        so the frontend toast reads "New session from <fw WAN IP>" instead of
        the real target hostname.
        """
        from sqlalchemy.orm import Session  # cycle-break

        from fastapi_backend.database.db_writer import engine  # cycle-break

        payload = {
            "type": event.EventType,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        implant_ip: str | None = None
        implant_hostname: str | None = None
        if event.Session:
            payload["session_id"] = event.Session.ID
            payload["remote_address"] = event.Session.RemoteAddress
            implant_ip = _extract_box_ip(event.Session.RemoteAddress or "")
            implant_hostname = getattr(event.Session, "Hostname", None)
        beacon = getattr(event, "Beacon", None)
        if beacon:
            payload["beacon_id"] = beacon.ID
            payload["remote_address"] = beacon.RemoteAddress
            implant_ip = _extract_box_ip(beacon.RemoteAddress or "")
            implant_hostname = getattr(beacon, "Hostname", None)
        if implant_ip:
            try:
                with Session(engine) as db:
                    box = _box_for_session(db, implant_hostname, implant_ip)
                if box is not None:
                    payload["box_ip"] = box.box_ip
                    payload["box_name"] = box.box_name
                    payload["team_num"] = box.team_num
            except Exception as exc:
                # SSE broadcast must never fail on resolution — subscribers
                # still get raw remote_address, matching pre-enrichment behavior.
                logger.debug("broadcast box-resolve failed for %s/%s: %s",
                             implant_hostname, implant_ip, exc)

        dead: list[asyncio.Queue] = []
        for q in self._event_queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)


# Module-level singleton
sliver_manager = SliverManager()
