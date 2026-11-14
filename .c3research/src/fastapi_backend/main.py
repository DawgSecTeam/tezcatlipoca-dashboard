"""FastAPI application entry point and lifespan management."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fastapi_backend.core.config import config
from fastapi_backend.core.orchestrator import CompetitionOrchestrator
from fastapi_backend.lifespan_helpers import (
    check_nmap,
    init_hub_registries,
    init_implant_subsystem,
    init_nxc,
    init_runtime_dirs,
    log_script_server_info,
    prune_stale_sa_results,
    register_background_loops,
    resolve_c2_ip,
    teardown,
    truncate_volatile_tables,
)
from fastapi_backend.routes import (
    admin,
    c2,
    details,
    ioc_deploy,
    iocs,
    login,
    modules,
    scheduled,
    scoreboard,
    scripts,
    tasks,
    teams,
    wiki,
)

API_VERSION = config.API_VERSION
TITLE = "C3rberus"

# Global orchestrator instance — set by the lifespan startup phase.
orchestrator: CompetitionOrchestrator | None = None

logger = logging.getLogger(__name__)


def _get_orchestrator() -> CompetitionOrchestrator | None:
    """Closure-safe getter for background loops.

    Returns the current orchestrator instance instead of a snapshot taken at
    task-creation time.
    """
    return orchestrator


# Re-export for tests that previously patched `fastapi_backend.main._init_nxc`.
_init_nxc = init_nxc


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifecycle generator.

    Each phase is a named helper in :mod:`fastapi_backend.lifespan_helpers`
    so this generator stays readable and each phase can be unit-tested in
    isolation.
    """
    global orchestrator
    from fastapi_backend.c2.sliver_manager import sliver_manager  # cycle-break

    config.setup_file_logging()
    logger.info("Starting FastAPI application...")

    # Warn on slow asyncio callbacks — helps diagnose event-loop stalls
    # (e.g. a blocked SSE handler or a sync call on the loop thread).
    # docs/PLAN_soak_backend_perf.md §1.2: `slow_callback_duration` only
    # ACTUALLY logs warnings when the loop is in debug mode. Opt in via env
    # (`C3R_ASYNCIO_DEBUG=1`) — production default is off (small overhead per
    # coro switch). Soak profiles that need the trace set the env var via
    # `runtime_overrides` in the profile YAML.
    import asyncio as _asyncio
    import os as _os
    _loop = _asyncio.get_event_loop()
    _loop.slow_callback_duration = float(_os.environ.get("C3R_ASYNCIO_SLOW_CB_S", "0.5"))
    # docs/PLAN_soak_backend_perf.md §3.3: raise default thread-pool size so
    # asyncio.to_thread off-loads don't queue behind a small default pool
    # (Python default is min(32, os.cpu_count()+4) ≈ 10-16). Under Phase 5
    # concurrent-race stress every off-loaded endpoint (/admin/status,
    # /c2/iocs/deployed, etc.) needs a thread; queue depth quickly hit the
    # default cap → read timeouts on subsequent requests. 64 workers gives
    # headroom for ~50 boxes × concurrent phase reads without oversubscribing.
    from concurrent.futures import ThreadPoolExecutor as _ThreadPool
    _executor_size = int(_os.environ.get("C3R_ASYNCIO_EXECUTOR_MAX_WORKERS", "64"))
    _loop.set_default_executor(_ThreadPool(max_workers=_executor_size,
                                     thread_name_prefix="c3r-async"))
    logger.info("asyncio default executor pool sized to %d workers", _executor_size)
    if _os.environ.get("C3R_ASYNCIO_DEBUG", "0") == "1":
        _loop.set_debug(True)
        logger.warning("asyncio debug mode ENABLED (C3R_ASYNCIO_DEBUG=1); "
                       "slow callbacks > %.2fs will be logged",
                       _loop.slow_callback_duration)

    # docs/PLAN_soak_backend_perf.md §1.2: event-loop heartbeat thread.
    # A daemon thread posts a monotonic timestamp every 100ms; a coroutine
    # reads the slot on the same 100ms cadence. Divergence > threshold =
    # event-loop stalled → WARN with sys._current_frames() sample of the loop
    # thread. Off by default; opt in via C3R_LOOP_HEARTBEAT=1. Independent of
    # asyncio-debug — heartbeat catches stalls that don't come from a single
    # slow callback (e.g. many small callbacks each just under threshold).
    if _os.environ.get("C3R_LOOP_HEARTBEAT", "0") == "1":
        from fastapi_backend.lifespan_helpers import start_loop_heartbeat  # cycle-break
        start_loop_heartbeat(_loop)
        logger.warning("event-loop heartbeat monitor STARTED (C3R_LOOP_HEARTBEAT=1)")

    init_runtime_dirs()

    # Start the single-writer DB actor FIRST — before any startup write — so every
    # write on the scoring engine funnels through one thread (no simultaneous
    # writers, no 'database is locked'). See docs/PLAN_serialized_write_actor.md.
    from fastapi_backend.database.write_actor import write_actor  # cycle-break
    write_actor.start()

    orchestrator = CompetitionOrchestrator()
    await orchestrator.initialize()

    truncate_volatile_tables()
    init_implant_subsystem()

    if config.DEV_MODE:
        logger.warning(
            "DEV_MODE enabled — Sliver, NXC, SA collection, and beacon watcher disabled. "
            "Sessions/beacons served from dev_sessions/dev_beacons DB tables."
        )
    else:
        await init_nxc()

    resolve_c2_ip()
    if not config.DEV_MODE:
        await sliver_manager.startup()
        # Purge the three pre-built Sliver beacons (beacon_linux / beacon_freebsd /
        # msupdate.exe). Their keys are wiped by any reset_sliver.sh --days 0, so a
        # stale on-disk copy launched by an IOC deploy floods the mTLS listener with
        # "unknown implant signature key" (traced 2026-08-22). payload_resolver's
        # existing _resolve_beacon auto-generates a fresh one on first use.
        from fastapi_backend.lifespan_helpers import (
            purge_stale_startup_beacons,  # cycle-break
        )
        purge_stale_startup_beacons()

    prune_stale_sa_results(hours=24)
    check_nmap(app)
    init_hub_registries(app)
    log_script_server_info()

    handles = register_background_loops(app, orchestrator, _get_orchestrator)

    # Startup writes (db-actor-exempt schema/seed migrations) are done; from here
    # the write actor must be the sole writer. Arm the runtime single-writer guard
    # (no-op unless C3R_WRITE_GUARD=log|strict). See database.write_guard.
    from fastapi_backend.database.write_guard import arm_write_guard  # cycle-break
    arm_write_guard()

    yield

    from fastapi_backend.database.write_guard import disarm_write_guard  # cycle-break
    disarm_write_guard()
    await teardown(app, orchestrator, handles)


app = FastAPI(lifespan=lifespan, title=TITLE, version=API_VERSION)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,  # frontend origins from config
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _log_slow_requests(request, call_next):  # noqa: ANN001, ANN202
    import time as _time

    _t0 = _time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        _elapsed = _time.perf_counter() - _t0
    if _elapsed >= config.SLOW_REQUEST_LOG_THRESHOLD_SEC:
        logger.warning(
            "slow_request path=%s method=%s elapsed=%.3fs status=%s",
            request.url.path, request.method, _elapsed, response.status_code,
        )
    return response


app.include_router(login.router)
app.include_router(admin.router)
app.include_router(scoreboard.router)
app.include_router(details.router)
app.include_router(iocs.router)
app.include_router(ioc_deploy.router)
app.include_router(teams.router)
app.include_router(c2.router)
app.include_router(scheduled.router)
app.include_router(modules.router)
app.include_router(scripts.router)
app.include_router(tasks.router)
app.include_router(wiki.router)

# Instrument the main app's /scripts fallback path so script-delivery served
# here (when the dedicated listener is down) still shows up in the shared
# script_server metrics tagged served_by='main'.  The dedicated off-loop
# listener (fix #2) carries the bulk of delivery; see core/script_listener.py.
from fastapi_backend.core.script_listener import (  # noqa: E402
    install_main_app_script_metrics,
)

install_main_app_script_metrics(app)
