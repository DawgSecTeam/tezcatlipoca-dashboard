"""Lifespan phase helpers for `fastapi_backend.main:lifespan`.

The lifespan generator used to inline ~330 lines of startup, background
loop spawn, and teardown logic, putting its cyclomatic complexity at 56
and obscuring phase boundaries. Each subsystem is extracted here as a
named function so `lifespan` reads as a short ordered sequence and each
helper can be unit-tested in isolation.

Helpers fall into four groups:

- ``init_*``        — synchronous-style setup steps called once before yield
- ``loop_*``        — long-running async background loops scheduled via
                      ``asyncio.create_task`` or ``fire_and_forget``
- ``register_background_loops`` — wires every loop above into the running
                                  event loop and returns task handles for
                                  the ones that need cancellation on
                                  shutdown
- ``teardown``      — cancels handles + flushes Sliver/orchestrator
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi_backend.core.config import config
from fastapi_backend.utils.asyncio_tasks import fire_and_forget

if TYPE_CHECKING:
    from fastapi import FastAPI  # lazy-load

    from fastapi_backend.core.orchestrator import CompetitionOrchestrator  # lazy-load

# Zero-arg callable returning the current orchestrator instance (or None).
GetOrchestrator = Callable[[], "CompetitionOrchestrator | None"]

logger = logging.getLogger(__name__)
_nxc_logger = logging.getLogger("nxc.init")


# ---------------------------------------------------------------------------
# Synchronous-style init phases
# ---------------------------------------------------------------------------


def init_runtime_dirs() -> None:
    """Create runtime directories used by NXC + implant store.

    Run before anything tries to read or write them.
    """
    Path(config.NXC_PATH).mkdir(parents=True, exist_ok=True)
    Path(config.IMPLANT_STORE_DIR).mkdir(parents=True, exist_ok=True)


def start_loop_heartbeat(loop: object, *, tick_ms: int = 100,
                          stall_threshold_ms: int = 500) -> "Callable[[], None]":
    """Event-loop stall detector — docs/PLAN_soak_backend_perf.md §1.2.

    A CORO on the event loop posts ``time.monotonic()`` every ``tick_ms`` into
    a shared slot. A daemon thread reads the slot; if the age exceeds
    ``stall_threshold_ms`` the event loop hasn't been scheduled in that long
    → log a WARN with a stack sample of the loop thread so the culprit is
    visible.

    This shape is critical — an earlier version had the thread post + the
    coro read, which meant a blocked coro NEVER observed a stall (thread
    kept updating the slot). The thread must be the observer since the
    thread continues to run while the loop is stalled.

    Detects stalls that a single `slow_callback_duration` warning misses: many
    small callbacks each just under threshold that together starve the loop.

    Off by default (gated on ``C3R_LOOP_HEARTBEAT=1`` in `main.lifespan`).
    Small overhead: one coroutine posting a float + one thread reading it.
    """
    import asyncio  # cycle-break
    import sys  # cycle-break
    import threading  # cycle-break
    import time  # cycle-break

    slot: dict[str, float] = {"ts": time.monotonic()}
    stop_event = threading.Event()
    loop_thread_id: dict[str, int | None] = {"tid": None}

    async def _posting_coro() -> None:
        # Runs on the loop; posts a fresh timestamp each tick. A stall makes
        # this coro not-run → slot goes stale → watchdog thread notices.
        loop_thread_id["tid"] = threading.get_ident()
        while True:
            slot["ts"] = time.monotonic()
            await asyncio.sleep(tick_ms / 1000.0)

    def _watchdog_thread() -> None:
        last_warn_mono = 0.0
        while not stop_event.is_set():
            stop_event.wait(tick_ms / 1000.0)
            if stop_event.is_set():
                return
            age_ms = (time.monotonic() - slot["ts"]) * 1000.0
            if age_ms <= stall_threshold_ms:
                continue
            now = time.monotonic()
            if now - last_warn_mono < 1.0:
                continue  # rate-limit
            last_warn_mono = now
            stack_lines: list[str] = []
            tid = loop_thread_id["tid"]
            if tid is not None:
                frames = sys._current_frames()
                frame = frames.get(tid)
                if frame is not None:
                    f = frame
                    depth = 0
                    while f and depth < 12:
                        stack_lines.append(
                            f"  {f.f_code.co_filename}:{f.f_lineno} in {f.f_code.co_name}"
                        )
                        f = f.f_back
                        depth += 1
            logger.warning(
                "event-loop stall: %.0fms (threshold %dms) — loop-thread stack:\n%s",
                age_ms, stall_threshold_ms,
                "\n".join(stack_lines) if stack_lines else "  (no stack available)",
            )

    threading.Thread(target=_watchdog_thread, name="loop-heartbeat-watchdog",
                     daemon=True).start()
    posting_task = loop.create_task(_posting_coro())

    def _stop() -> None:
        stop_event.set()
        try:
            posting_task.cancel()
        except Exception:
            pass

    return _stop


def truncate_volatile_tables() -> None:
    """Empty per-session tables on startup for a fresh alerts/tasks state.

    ScheduledTask rows are DELETED (not just FK-nulled). They are per-session
    volatile state like OperatorTask/Alerts: the soak's Phase 3 (and a live
    operator) re-create whatever scheduling they need after startup. Leaving
    them behind was a real defect — a REPEATING task (``repeat_secs``) re-queues
    a fresh pending row every fire, so any run that reached Phase 3 left 30
    self-perpetuating tasks that survived every backend restart and kept
    dispatching commands to up to 48 boxes via the shared Sliver gRPC client.
    That accumulated to 34,749 rows and its sustained gRPC load starved
    pivot-routed IOC deploys during preflight (they multiplex over the same
    channel; direct NXC deploys were unaffected) — the pivot deploy-fail class
    (project_pivot_deploy_rootcause). Delete ScheduledTask BEFORE OperatorTask
    so no FK reference dangles.
    """
    from sqlalchemy import delete  # cycle-break
    from sqlalchemy.orm import Session  # cycle-break

    from fastapi_backend.database.db_writer import engine  # cycle-break
    from fastapi_backend.database.models import (  # cycle-break
        AlertReads,
        Alerts,
        OperatorTask,
        ScheduledTask,
    )

    with Session(engine) as session:
        session.execute(delete(AlertReads))
        session.execute(delete(Alerts))
        session.execute(delete(ScheduledTask))
        session.execute(delete(OperatorTask))
        session.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)
    logger.info("Truncated alerts, alert_reads, scheduled_tasks, and operator_tasks on startup")


def _seed_service_modules(eng: Any) -> None:
    """Upsert synthetic 'service interruption' Modules rows (file_path='__services__')."""
    import json as _json  # cycle-break

    from sqlalchemy.orm import Session  # cycle-break

    from fastapi_backend.database.models import Modules  # cycle-break

    _service_modules = [
        (
            "stop_dns",
            "Stop the DNS server service (DC: DNS; Linux: named/bind9; pfSense: named)",
            _json.dumps({"service": {"type": "string", "description": "Override service name"}}),
        ),
        (
            "disable_web_server",
            "Stop and disable the web server (Windows: IIS; Linux: nginx/apache2; pfSense: nginx). "
            "WARNING: disabling pfSense nginx kills the web UI.",
            _json.dumps({"service": {"type": "string", "description": "Override service name"}}),
        ),
        (
            "disable_smb",
            "Stop and disable SMB file-sharing (LanmanServer). Windows-only.",
            _json.dumps({}),
        ),
        (
            "disable_ad",
            "Stop Active Directory services (ADWS, kdc, Netlogon). DC-only; fails silently on members.",
            _json.dumps({}),
        ),
        (
            "disable_ntp",
            "Stop NTP/time-synchronization service (Windows: W32Time; Linux: timesyncd/chronyd; pfSense: ntpd).",
            _json.dumps({}),
        ),
    ]

    with Session(eng) as db:
        for name, desc, params_json in _service_modules:
            row = db.query(Modules).filter_by(name=name).first()
            if row is None:
                row = Modules(
                    name=name,
                    description=desc,
                    target_os="any",
                    params=params_json,
                    file_path="__services__",
                    file_hash="config",
                    last_modified=0.0,
                    is_active=True,
                )
                db.add(row)
            else:
                row.description = desc
                row.target_os = "any"
                row.params = params_json
                row.file_path = "__services__"
                row.file_hash = "config"
                row.is_active = True
        db.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)
    logger.info("Service modules seeded (%d rows)", len(_service_modules))


def purge_stale_startup_beacons() -> list[str]:
    """Delete the pre-built ``iocs/payloads/beacon_*`` files at backend startup.

    The three beacons ``beacon_linux``, ``beacon_freebsd``, and ``msupdate.exe``
    are Sliver-generated implants that carry a Sliver-side mTLS key. They are
    documented as MANUAL to regenerate (``scripts/build/build_payloads.sh`` §57)
    and were committed to a dev's local ``iocs/payloads/`` once, then went stale:
    every ``reset_sliver.sh --days 0`` (soak pre-flight, every ``run_tests.sh``)
    purges Sliver's build DB, invalidating those keys. Deploys that upload the
    stale files then launch DOA implants whose 60s check-in beacon floods the
    mTLS listener with ``unknown implant signature key`` (traced 2026-08-22: 40k+
    rejections in one soak, dominated by pfSense boxes whose ``firewall_kill_shim``
    /``firewall_implant_*`` IOCs uploaded the stale beacon_freebsd).

    ``core/payload_resolver.py`` already has ``_resolve_beacon`` — a Sliver-gRPC
    auto-generate that runs when the file is MISSING. Deleting the stale files at
    startup lets that path do its job: next deploy generates a fresh beacon with a
    key Sliver knows. Cost: ~30s once per startup for the first deploy that needs
    each beacon; runs are then flood-free. Idempotent — a missing file is a no-op.

    Returns the list of paths removed (for the log line + tests).
    """
    payloads_dir = Path(config.PROJECT_ROOT) / "iocs" / "payloads"
    stale = ["beacon_linux", "beacon_freebsd", "msupdate.exe"]
    removed: list[str] = []
    for name in stale:
        p = payloads_dir / name
        try:
            if p.exists():
                p.unlink()
                removed.append(str(p))
        except OSError as exc:
            logger.warning("purge_stale_startup_beacons: could not remove %s: %s", p, exc)
    if removed:
        logger.info(
            "Purged %d stale pre-built beacon(s) at startup — next deploy will "
            "auto-regenerate via _resolve_beacon: %s",
            len(removed), removed,
        )
    return removed


def init_implant_subsystem() -> None:
    """Bring the implant subsystem online.

    Migrates legacy implant build directories into the ``implant_builds`` DB
    table, scans ``modules/`` for available scripts, warms the naming pool,
    and seeds synthetic service-interruption module rows.
    """
    from fastapi_backend.c2.implant_migration import (
        migrate_implant_builds,  # cycle-break
    )
    from fastapi_backend.c2.module_scanner import rescan_modules_dir  # cycle-break
    from fastapi_backend.core import services_loader as _sl  # cycle-break
    from fastapi_backend.core.naming_pools import naming_pool_manager  # cycle-break
    from fastapi_backend.database.db_writer import engine as _eng  # cycle-break

    migrate_implant_builds()
    Path(config.MODULES_DIR).mkdir(parents=True, exist_ok=True)
    rescan_modules_dir()
    naming_pool_manager.load()
    _sl.load_services()
    _seed_service_modules(_eng)


async def init_nxc() -> None:
    """Run ``nxc --help`` once at startup to warm the netexec workspace.

    Creates its workspace directory + protocol SQLite DBs before any real
    request comes in. Honours ``config.NXC_PATH`` via env so workspace files
    land where configured.
    """
    if not shutil.which("nxc"):
        _nxc_logger.warning(
            "nxc not found on PATH — netexec features will not work. "
            "Install via: sudo apt install netexec"
        )
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            "nxc", "--help",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "NXC_PATH": config.NXC_PATH},
        )
        await asyncio.wait_for(proc.wait(), timeout=10)
        _nxc_logger.info(f"nxc workspace initialized at {config.NXC_PATH!r}")
    except TimeoutError:
        _nxc_logger.warning("nxc --help timed out during startup init")
    except Exception as e:
        _nxc_logger.warning(f"nxc startup init failed: {e}")

    # Repair duplicate credential rows left by nxc's own concurrent
    # add_credential race (see netexec/nxc_credwarm.py). A poisoned workspace
    # makes every later nxc call for that credential die before running our
    # command, so it must be healed at boot, not just prevented going forward.
    from fastapi_backend.netexec import nxc_credwarm  # cycle-break

    repaired = await asyncio.to_thread(nxc_credwarm.dedupe_workspace, config.NXC_PATH)
    if repaired:
        _nxc_logger.warning(
            "nxc workspace credential dedupe repaired %d db(s): %s",
            len(repaired), repaired,
        )


def resolve_c2_ip() -> None:
    """Stamp ``config.C2_IP`` with the host's outbound IP if unset.

    Used by implant generation to bake the correct C2 callback URL into binaries.
    """
    if config.C2_IP:
        return
    import socket  # cycle-break
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            config.C2_IP = s.getsockname()[0]
    except Exception:
        config.C2_IP = "127.0.0.1"
    # This picks the INTERNET default-route interface, which is NOT necessarily
    # the interface the target boxes route back to on a multi-homed host (a live
    # bug: derived 192.168.1.161 while live-fire boxes only reach 10.50.50.5 —
    # implants baked an unreachable c2_url and never checked in). Surface it so a
    # wrong interface is visible instead of silently baked into every implant.
    logger.warning(
        "C2_IP was unset — auto-derived to %s from the internet default route. "
        "If target boxes reach this host on a different interface (VPN/second NIC), "
        "set C2_IP explicitly in .env.", config.C2_IP,
    )
    config.refresh_script_server_url()


def prune_stale_sa_results(hours: int = 24) -> None:
    """Delete SAResult rows older than ``hours``.

    Keeps the SA history table bounded across restart cycles.
    """
    from sqlalchemy import delete  # cycle-break
    from sqlalchemy.orm import Session  # cycle-break

    from fastapi_backend.database.db_writer import engine  # cycle-break
    from fastapi_backend.database.models import SAResult  # cycle-break

    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    with Session(engine) as db:
        result = db.execute(delete(SAResult).where(SAResult.collected_at < cutoff))
        db.commit()  # db-actor-exempt: startup schema/seed migration (single-threaded, pre-serving)
        if result.rowcount:
            logger.info("Pruned %d SA results older than %dh", result.rowcount, hours)


def check_nmap(app: FastAPI) -> None:
    """Probe nmap availability once at startup.

    Result stashed on ``app.state.nmap_available`` so the SA loop doesn't
    re-probe every cycle.
    """
    from fastapi_backend.c2.nmap_scanner import check_nmap_available  # cycle-break

    app.state.nmap_available = check_nmap_available()
    if not app.state.nmap_available:
        logger.warning(
            "nmap not found on PATH — reachability scanning disabled. "
            "Install with: sudo apt install nmap"
        )


def init_hub_registries(app: FastAPI) -> None:
    """Initialise the per-session hub dicts.

    Cleaned up 5 minutes after the last client disconnects via
    :func:`loop_hub_cleanup`.
    """
    app.state.session_hubs = {}
    app.state.sliver_hubs = {}


def log_script_server_info() -> None:
    """Log the script-server URL + HMAC scheme if one is configured.

    Lets the operator confirm the env var lined up with the running backend.
    """
    if config.SCRIPT_SERVER_URL:
        logger.info(
            f"Script server URL: {config.SCRIPT_SERVER_URL} "
            f"(HMAC-signed tokens, 1h per-request TTL)"
        )


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------


async def loop_hub_cleanup(app: FastAPI) -> None:
    """Reap idle SessionHub / SliverHub entries every 60s.

    Prevents disconnected operator terminals from leaking their internal
    queues forever.
    """
    while True:
        await asyncio.sleep(60)
        for registry_name, registry in [
            ("session_hubs", app.state.session_hubs),
            ("sliver_hubs", app.state.sliver_hubs),
        ]:
            expired = [sid for sid, hub in list(registry.items()) if hub.is_idle_expired()]
            for sid in expired:
                hub = registry.pop(sid, None)
                if hub:
                    await hub.shutdown()
                    logger.info(f"{registry_name}: {sid[:8]}… cleaned up after idle timeout")


async def loop_metrics_tick(get_orchestrator: GetOrchestrator) -> None:
    """Publish a ``metrics-tick`` SSE event every 5 seconds while server is up.

    ``get_orchestrator`` is a zero-arg callable returning the current
    orchestrator instance (or ``None`` during teardown). Pass a callable so
    the loop sees the current global rather than capturing a snapshot at
    task-creation time.
    """
    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break
    from fastapi_backend.core.metrics import collect_metrics  # cycle-break

    while True:
        try:
            await asyncio.sleep(5.0)
            orch = get_orchestrator()
            if orch is None:
                continue
            # collect_metrics does psutil.process_iter over hundreds of NXC
            # subprocesses + ~35 sync SELECTs — off-load to a worker thread so
            # the 5s tick doesn't pin the accept loop (soak N 2026-08-26).
            metrics = await asyncio.to_thread(collect_metrics, orch)
            payload = {"type": "metrics-tick", "metrics": metrics}
            await sliver_manager.publish(payload)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug("metrics-tick loop error: %s", exc)


async def loop_sa_collection(app: FastAPI, get_orchestrator: GetOrchestrator) -> None:
    """Run NXC SA collection on every box every ``SA_COLLECTION_INTERVAL_MINUTES``.

    Each cycle is followed by an nmap reachability scan when available. First
    cycle fires immediately at boot so operators don't wait the full interval
    for initial data.
    """
    from sqlalchemy import select as sa_select  # cycle-break
    from sqlalchemy.orm import Session  # cycle-break

    from fastapi_backend.c2.nmap_scanner import run_nmap_scan  # cycle-break
    from fastapi_backend.c2.sa_collector import run_all_sa_collection  # cycle-break
    from fastapi_backend.core.metrics import record_loop_run  # cycle-break
    from fastapi_backend.database.db_writer import engine  # cycle-break
    from fastapi_backend.database.models import Boxes  # cycle-break

    first_iteration = True
    while True:
        if not first_iteration:
            await asyncio.sleep(config.SA_COLLECTION_INTERVAL_MINUTES * 60)
        first_iteration = False
        started = datetime.now(UTC)
        orch = get_orchestrator()
        if orch:
            orch.sa_stats["last_run_started_at"] = started.isoformat()
        try:
            await run_all_sa_collection()
            record_loop_run("sa_collection")
            duration = (datetime.now(UTC) - started).total_seconds()
            if orch:
                orch.sa_stats["cycles_completed"] += 1
                orch.sa_stats["last_run_duration_s"] = round(duration, 2)
                orch.sa_stats["last_error"] = None
        except Exception as exc:
            logger.warning(f"SA collection error: {exc}")
            if orch:
                orch.sa_stats["cycles_failed"] += 1
                orch.sa_stats["last_error"] = str(exc)

        # Same-cycle nmap reachability sweep.
        if app.state.nmap_available and orch and orch.executor:
            try:
                with Session(engine) as db:
                    box_ips = [
                        row.box_ip
                        for row in db.execute(sa_select(Boxes)).scalars().all()
                    ]
                if box_ips:
                    nmap_result = await run_nmap_scan(
                        box_ips=box_ips,
                        db_writer_manager=orch.executor.db_writer_manager,
                        retention_count=config.SA_RESULT_RETENTION_COUNT,
                        timeout=config.NMAP_TIMEOUT,
                    )
                    orch.sa_stats["nmap_cycle_duration_s"] = nmap_result["duration_s"]
                    if nmap_result["failed"]:
                        orch.sa_stats["nmap_cycles_failed"] = (
                            orch.sa_stats.get("nmap_cycles_failed", 0) + 1
                        )
            except Exception as exc:
                logger.warning(f"nmap scan error: {exc}")
                if orch:
                    orch.sa_stats["nmap_cycles_failed"] = (
                        orch.sa_stats.get("nmap_cycles_failed", 0) + 1
                    )

        # Same-cycle DC clock-skew check — alert-only, NOT persisted to SA data.
        # Rides the SA cadence to catch Kerberos-breaking DC clock drift and emit
        # a clock_skew alert when any DC exceeds the tolerance.
        try:
            from fastapi_backend.c2.clock_skew import (  # cycle-break
                check_dc_clock_skew_and_alert,
            )

            skew_result = await check_dc_clock_skew_and_alert()
            if skew_result.get("skewed"):
                logger.warning(
                    "DC clock-skew check: %d/%d DC(s) beyond tolerance",
                    skew_result["skewed"],
                    skew_result["checked"],
                )
        except Exception as exc:
            logger.warning(f"DC clock-skew check error: {exc}")


async def loop_sliver_sa_collection() -> None:
    """Run Sliver-based SA collection on the NXC SA loop cadence.

    Sessions and beacons keep refreshing their last-seen + process snapshots.
    """
    from fastapi_backend.c2.sa_collector_sliver import (
        run_all_sliver_sa_collection,  # cycle-break
    )

    first = True
    while True:
        if not first:
            await asyncio.sleep(config.SA_COLLECTION_INTERVAL_MINUTES * 60)
        first = False
        try:
            await run_all_sliver_sa_collection()
        except Exception as exc:
            logger.warning(f"Sliver SA collection error: {exc}")


async def loop_beacon_watcher() -> None:
    """Re-deploy beacons that have gone silent.

    Threshold: ``BEACON_REDEPLOY_THRESHOLD_MULTIPLIER * checkin_interval``.
    """
    from fastapi_backend.c2.beacon_watcher import (
        check_and_redeploy_beacons,  # cycle-break
    )
    from fastapi_backend.core.metrics import record_loop_run  # cycle-break

    while True:
        await asyncio.sleep(config.BEACON_REDEPLOY_CHECK_INTERVAL_MINUTES * 60)
        try:
            await check_and_redeploy_beacons()
            record_loop_run("beacon_watcher")
        except Exception as exc:
            logger.warning(f"Beacon watcher error: {exc}")


async def loop_pivot_keepalive() -> None:
    """Keep every active pivot portfwd tunnel warm so it never idle-dies.

    Fires every ``PIVOT_KEEPALIVE_INTERVAL_SEC`` (default 90s, under the Sliver
    ~120s idle-death). Root fix for the pivot deploy-fail class — see
    ``c2/pivot_prewarm.keepalive_all_pivots`` and
    project_pivot_deploy_rootcause. The first tick fires after one interval
    (pivots are set up on session connect, not at boot).
    """
    from fastapi_backend.c2.pivot_prewarm import keepalive_all_pivots  # cycle-break
    from fastapi_backend.core.metrics import record_loop_run  # cycle-break

    while True:
        await asyncio.sleep(config.PIVOT_KEEPALIVE_INTERVAL_SEC)
        try:
            report = await keepalive_all_pivots()
            if report["total"]:
                logger.debug(
                    "pivot keepalive: %d warmed, %d cold of %d",
                    report["warmed"], report["cold"], report["total"],
                )
            record_loop_run("pivot_keepalive")
        except Exception as exc:
            logger.warning("pivot keepalive error: %s", exc)


async def loop_nmap_health(get_orchestrator: GetOrchestrator) -> None:
    """Per-box ping sweep every ``NMAP_HEALTH_INTERVAL_MINUTES``.

    Emits ``box_unreachable`` alerts via the nmap_health module when hosts
    stop responding.
    """
    from fastapi_backend.c2.nmap_health import run_nmap_health_check  # cycle-break

    first = True
    while True:
        if not first:
            await asyncio.sleep(config.NMAP_HEALTH_INTERVAL_MINUTES * 60)
        first = False
        started = datetime.now(UTC)
        orch = get_orchestrator()
        if orch:
            if not hasattr(orch, "nmap_stats"):
                orch.nmap_stats = {
                    "cycles_completed": 0,
                    "last_run_started_at": None,
                    "last_run_duration_s": None,
                    "last_error": None,
                }
            orch.nmap_stats["last_run_started_at"] = started.isoformat()
        try:
            await run_nmap_health_check()
            duration = (datetime.now(UTC) - started).total_seconds()
            if orch:
                orch.nmap_stats["cycles_completed"] += 1
                orch.nmap_stats["last_run_duration_s"] = round(duration, 2)
                orch.nmap_stats["last_error"] = None
        except Exception as exc:
            logger.warning(f"Nmap health check error: {exc}")
            if orch:
                orch.nmap_stats["last_error"] = str(exc)


async def loop_alert_cleanup() -> None:
    """Purge alerts older than 24h every 10 minutes.

    Keeps the alerts table from growing without bound across long-running
    competitions.
    """
    from sqlalchemy import delete  # cycle-break
    from sqlalchemy.orm import Session  # cycle-break

    from fastapi_backend.database.models import Alerts  # cycle-break
    from fastapi_backend.database.write_actor import write_actor  # cycle-break

    while True:
        await asyncio.sleep(10 * 60)
        try:
            cutoff = datetime.now(UTC) - timedelta(hours=24)

            def _purge(session: Session, _cutoff: datetime = cutoff) -> int:
                # Route the DELETE through the single-writer actor. This is a
                # RUNTIME loop, not a startup migration — a direct commit here is
                # a second writer that races the actor's lock and (on the event
                # loop) blocks it for the full 30s busy_timeout. It was previously
                # mis-tagged `# db-actor-exempt: startup ...` and slipped the A2
                # fence; the actor is the single writer.
                return session.execute(
                    delete(Alerts).where(Alerts.created_at < _cutoff)
                ).rowcount

            deleted_count = await write_actor.submit(_purge, label="alert_cleanup")
            if deleted_count > 0:
                logger.info("Alert cleanup: purged %d alerts older than 24h", deleted_count)
        except Exception as exc:
            logger.warning("Alert cleanup error: %s", exc)


# ---------------------------------------------------------------------------
# Register + teardown
# ---------------------------------------------------------------------------


def _maybe_start_script_listener(app: FastAPI) -> None:
    """Start the dedicated off-loop script listener and stash its handle.

    No-op (handle=None) when disabled or when SCRIPT_SERVER_URL is unset (no box
    fetches scripts over HTTP, so the listener would serve nothing).

    Bind failure is FATAL: we hard-fail backend startup if the configured port
    is unavailable, rather than silently degrading to main-app ``/scripts``.
    The old degrade path masked a real collision — the listener squatted a
    pytest worker's app port (3010) and answered ``/login`` with 404, erroring
    372 tests before anyone noticed.  Opt out with SCRIPT_LISTENER_ENABLED=false.
    See docs/PLAN_script_fetch_amplification.md fix #2.
    """
    app.state.script_listener = None
    if not config.SCRIPT_LISTENER_ENABLED or not config.SCRIPT_SERVER_URL:
        if config.SCRIPT_LISTENER_ENABLED and not config.SCRIPT_SERVER_URL:
            logger.info(
                "Script listener enabled but SCRIPT_SERVER_URL unset — not "
                "starting (no box fetches scripts over HTTP)."
            )
        return
    from fastapi_backend.core.script_listener import (  # cycle-break
        assert_port_available,
        start_script_listener,
    )
    host = config.SCRIPT_LISTENER_HOST
    port = config.SCRIPT_LISTENER_PORT
    # Startup check — hard-fail loud if the port is already taken instead of
    # serving a degraded listener whose collision hides until tests mass-error.
    assert_port_available(host, port)
    certfile = config.SCRIPT_LISTENER_CERTFILE or str(
        config.PROJECT_ROOT / "certs" / "cert.pem"
    )
    keyfile = config.SCRIPT_LISTENER_KEYFILE or str(
        config.PROJECT_ROOT / "certs" / "key.pem"
    )
    handle = start_script_listener(
        port=port,
        certfile=certfile,
        keyfile=keyfile,
        host=host,
    )
    if not handle.bound:
        raise RuntimeError(
            f"Dedicated script listener failed to bind {host}:{port} — refusing "
            "to start backend. Set SCRIPT_LISTENER_PORT to a free port or "
            "SCRIPT_LISTENER_ENABLED=false to opt out."
        )
    app.state.script_listener = handle


def register_background_loops(
    app: FastAPI,
    orchestrator: CompetitionOrchestrator | None,
    get_orchestrator: GetOrchestrator,
) -> dict[str, asyncio.Task]:
    """Spawn every background loop needed for a non-DEV_MODE backend.

    Returns a dict of task handles for the loops we explicitly cancel during
    shutdown (``hub_cleanup``, ``metrics_tick``, ``wrapper_sweep``). Loops
    spawned via ``fire_and_forget`` are intentionally untracked — they
    self-terminate when the asyncio event loop closes during shutdown.
    """
    from fastapi_backend.c2.bulk_put_file import (  # cycle-break
        staged_upload_sweep_loop,
    )
    from fastapi_backend.netexec.script_wrapper import (  # cycle-break
        wrapper_cache_sweep_loop,
    )
    handles: dict[str, asyncio.Task] = {
        "hub_cleanup":          asyncio.create_task(loop_hub_cleanup(app)),
        "metrics_tick":         asyncio.create_task(loop_metrics_tick(get_orchestrator)),
        "wrapper_sweep":        asyncio.create_task(wrapper_cache_sweep_loop()),
        "staged_upload_sweep":  asyncio.create_task(staged_upload_sweep_loop()),
    }

    # Dedicated off-loop script-delivery listener (fix #2,
    # docs/PLAN_script_fetch_amplification.md).  Started even in DEV_MODE when
    # enabled — boxes fetch scripts regardless of competition state — but only
    # when SCRIPT_SERVER_URL is set (otherwise nothing fetches via HTTP).
    _maybe_start_script_listener(app)

    if config.DEV_MODE:
        return handles

    # SA collection (NXC + Sliver) and nmap health each fire an immediate first
    # cycle at boot.  Under pytest-xdist every worker backend boots at once, so
    # 32 simultaneous first-cycle sweeps stampede host CPU/RAM (~330 concurrent
    # nxc, 99% CPU for ~60s) and time out the first /admin/add_user.  Gate them
    # so the harness can run with the loops off (data is seeded / triggered
    # on-demand in tests); production keeps the defaults on.
    if config.SA_COLLECTION_ENABLED:
        fire_and_forget(loop_sa_collection(app, get_orchestrator))
        fire_and_forget(loop_sliver_sa_collection())
    else:
        logger.info("SA collection loops disabled (SA_COLLECTION_ENABLED=false)")
    fire_and_forget(loop_beacon_watcher())
    if config.NMAP_HEALTH_ENABLED:
        fire_and_forget(loop_nmap_health(get_orchestrator))
    else:
        logger.info("nmap health loop disabled (NMAP_HEALTH_ENABLED=false)")
    fire_and_forget(loop_alert_cleanup())

    from fastapi_backend.c2.beacon_task_poller import (
        beacon_task_poll_loop,  # cycle-break
    )
    from fastapi_backend.c2.scheduler_loop import scheduler_loop  # cycle-break
    fire_and_forget(beacon_task_poll_loop())
    fire_and_forget(scheduler_loop())

    if config.PIVOT_KEEPALIVE_ENABLED:
        fire_and_forget(loop_pivot_keepalive())
    else:
        logger.info("pivot keepalive loop disabled (PIVOT_KEEPALIVE_ENABLED=false)")

    if config.CRED_HARVEST_ENABLED and orchestrator and orchestrator.executor:
        from fastapi_backend.c2.cred_harvester import (  # lazy-load
            HashHarvestLoop,
            TicketRefreshLoop,
        )
        nxc = orchestrator.executor.nxc_wrapper
        if nxc is not None:
            hash_loop = HashHarvestLoop(nxc)
            ticket_loop = TicketRefreshLoop(nxc)
            app.state.cred_harvester_loops = (hash_loop, ticket_loop)
            fire_and_forget(hash_loop.run_forever())
            fire_and_forget(ticket_loop.run_forever())
            logger.info("Credential harvester loops started")
        else:
            logger.warning(
                "CRED_HARVEST_ENABLED but NXC wrapper not available — skipping"
            )

    return handles


async def teardown(
    app: FastAPI,
    orchestrator: CompetitionOrchestrator | None,
    handles: dict[str, asyncio.Task],
) -> None:
    """Tear down all background loops and subsystems.

    Cancels tracked background loops, stops credential harvester loops, and
    shuts down Sliver + orchestrator in the reverse order they came up.
    """
    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break
    from fastapi_backend.netexec.script_wrapper import (  # cycle-break
        get_wrapper_cache,
    )

    for task in handles.values():
        task.cancel()
    logger.info("Shutting down FastAPI application...")
    # Stop the dedicated script listener thread (fix #2) before clearing the
    # cache so no in-flight fetch reads a half-cleared body.
    listener = getattr(app.state, "script_listener", None)
    if listener is not None:
        try:
            listener.stop()
        except Exception as exc:
            logger.warning("Error stopping script listener: %s", exc)
    # Clear plaintext script bodies from memory before the process exits.
    get_wrapper_cache().clear()
    if hasattr(app.state, "cred_harvester_loops"):
        for loop in app.state.cred_harvester_loops:
            await loop.stop()
    await sliver_manager.shutdown()
    if orchestrator:
        await orchestrator.shutdown()

    # Stop the single-writer DB actor LAST — after every other teardown write has
    # drained through it (docs/PLAN_serialized_write_actor.md).
    from fastapi_backend.database.write_actor import write_actor  # cycle-break
    write_actor.stop(drain=True)
