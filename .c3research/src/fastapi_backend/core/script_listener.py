"""Dedicated off-loop script-delivery listener + shared metrics (fix #2).

docs/PLAN_script_fetch_amplification.md — the 4 h soak failed its
``failure_rate`` gate because target boxes' per-cycle script fetches saturated
the single in-process uvicorn that also runs the IOC executor + asyncio loop;
TLS handshakes for ``/scripts`` serialised on the one loop thread, the accept
queue overflowed, fetches were refused, and every IOC on the affected box
scored ``-1``.

This module runs a SECOND ``uvicorn.Server`` in its own daemon thread with its
own event loop, on a separate port, serving ONLY the ``scripts`` router.  It
shares the in-process ``WrapperCache`` + ``HMACTokenManager`` directly (same
process → no IPC), so dynamic wrapper bodies are visible without a shared store.
TLS termination for script delivery therefore runs OFF the main loop and cannot
be starved by it.

Design choices (RESOLVED in the plan):
- **Static, not dynamic** — boxes always hit one fixed ``SCRIPT_LISTENER_PORT``
  via ``SCRIPT_SERVER_URL``.  No per-request port selection.
- **2A in-process thread**, not a separate process — avoids an IPC'd cache.
- **Bind-failure is non-fatal** — if the dedicated listener can't bind, the
  main app still has the ``scripts`` router mounted, so delivery degrades to the
  pre-fix behavior (logged loud) rather than going dark.

Observability: a shared, thread-safe metrics counter is written by BOTH the
dedicated listener and (for the fallback path) the main app's ``/scripts``
middleware, tagged by ``served_by``.  ``get_script_server_metrics()`` snapshots
it for ``/admin/status.components.script_server`` and the soak threshold.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared metrics
# ---------------------------------------------------------------------------


class _ScriptServerMetrics:
    """Thread-safe counters for script delivery, shared across both listeners."""

    def __init__(self, latency_window: int = 1024) -> None:
        self._lock = Lock()
        self._requests_total = 0
        self._in_flight = 0
        self._failures = 0  # HTTP status >= 400
        self._by_status: dict[int, int] = {}
        self._served_by: dict[str, int] = {}
        self._latency_ms: deque[float] = deque(maxlen=latency_window)

    def start(self) -> None:
        with self._lock:
            self._in_flight += 1

    def finish(self, status: int, latency_ms: float, served_by: str) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            self._requests_total += 1
            self._by_status[status] = self._by_status.get(status, 0) + 1
            self._served_by[served_by] = self._served_by.get(served_by, 0) + 1
            self._latency_ms.append(latency_ms)
            if status >= 400:
                self._failures += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self._requests_total
            lat = sorted(self._latency_ms)
            if lat:
                idx = min(len(lat) - 1, int(0.95 * (len(lat) - 1)))
                p95 = lat[idx]
            else:
                p95 = 0.0
            return {
                "requests_total": total,
                "in_flight": self._in_flight,
                "failures_total": self._failures,
                # The soak gate (max_script_fetch_failure_rate) reads this.
                "failure_rate": round(self._failures / total, 4) if total else 0.0,
                "p95_latency_ms": round(p95, 2),
                "by_status": dict(self._by_status),
                "served_by": dict(self._served_by),
            }

    def reset(self) -> None:
        with self._lock:
            self._requests_total = 0
            self._in_flight = 0
            self._failures = 0
            self._by_status.clear()
            self._served_by.clear()
            self._latency_ms.clear()


_metrics = _ScriptServerMetrics()


def get_script_server_metrics() -> dict[str, Any]:
    """Snapshot of the shared script-server counters (for /admin/status + soak)."""
    return _metrics.snapshot()


def reset_script_server_metrics() -> None:
    """Reset counters — used by tests."""
    _metrics.reset()


# ---------------------------------------------------------------------------
# Metrics middleware (added to both apps)
# ---------------------------------------------------------------------------


def _install_metrics_middleware(app: FastAPI, served_by: str, *, only_scripts: bool) -> None:
    """Register an HTTP middleware that times + counts script requests.

    ``only_scripts`` scopes recording to ``/scripts`` paths (used on the main
    app, which also serves unrelated routes).  The dedicated app serves only
    ``/scripts`` so it records everything.
    """

    @app.middleware("http")
    async def _script_metrics(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if only_scripts and not request.url.path.startswith("/scripts"):
            return await call_next(request)
        _metrics.start()
        start = time.time()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            _metrics.finish(status, (time.time() - start) * 1000.0, served_by)


def install_main_app_script_metrics(app: FastAPI) -> None:
    """Instrument the main app's /scripts fallback path (served_by='main')."""
    _install_metrics_middleware(app, served_by="main", only_scripts=True)


# ---------------------------------------------------------------------------
# Dedicated listener app + thread
# ---------------------------------------------------------------------------


def build_script_app() -> FastAPI:
    """Minimal FastAPI app serving only the scripts router (no orchestrator)."""
    from fastapi import FastAPI

    from fastapi_backend.routes import scripts  # cycle-break

    app = FastAPI(
        title="C3rberus-scripts",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    _install_metrics_middleware(app, served_by="dedicated", only_scripts=False)
    app.include_router(scripts.router)
    return app


# Last-started handle — module-level so /admin/status can report listener health
# without threading the app.state handle through the orchestrator.
_active_listener: ScriptListenerHandle | None = None


def get_listener_info() -> dict[str, Any]:
    """Status of the dedicated listener for /admin/status.components.script_server."""
    from fastapi_backend.core.config import config  # cycle-break

    h = _active_listener
    return {
        "enabled": config.SCRIPT_LISTENER_ENABLED,
        "port": config.SCRIPT_LISTENER_PORT,
        "bound": bool(h and h.bound),
        "running": bool(h and h.thread and h.thread.is_alive()),
    }


@dataclass
class ScriptListenerHandle:
    """Handle to the dedicated listener thread; ``bound`` says if it took the port."""

    port: int
    bound: bool = False
    thread: threading.Thread | None = None
    _server: Any = None

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the uvicorn server to exit and join its thread."""
        if self._server is not None:
            self._server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=timeout)


def assert_port_available(host: str, port: int) -> None:
    """Raise ``RuntimeError`` if a TCP socket can't bind ``(host, port)`` now.

    Startup guard for the dedicated listener: a port collision must hard-fail
    loudly rather than silently degrade to main-app ``/scripts`` (a degraded
    listener once squatted a pytest worker's app port and 404'd ``/login``).
    Binds with ``SO_REUSEADDR`` then closes — an *active* listener on the port
    still yields ``EADDRINUSE`` (``SO_REUSEADDR`` doesn't steal a live socket),
    so this reliably detects a real collision.
    """
    import socket

    probe_host = "0.0.0.0" if host in ("0.0.0.0", "::") else host
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((probe_host, port))
    except OSError as exc:
        raise RuntimeError(
            f"Script listener port {host}:{port} unavailable: {exc}. Another "
            "process (or a pytest worker's app port) holds it. Set "
            "SCRIPT_LISTENER_PORT to a free port or SCRIPT_LISTENER_ENABLED=false."
        ) from exc
    finally:
        s.close()


def start_script_listener(
    port: int,
    certfile: str,
    keyfile: str,
    host: str = "0.0.0.0",
    bind_timeout: float = 5.0,
) -> ScriptListenerHandle:
    """Start the dedicated script listener in a daemon thread with its own loop.

    Returns a handle.  ``handle.bound`` is True only if the server actually
    bound the port within ``bind_timeout``.  This function stays non-fatal on
    bind failure (returns an unbound handle); the *caller*
    (``lifespan_helpers._maybe_start_script_listener``) decides policy and
    hard-fails backend startup when ``handle.bound`` is False.
    """
    import asyncio

    import uvicorn

    global _active_listener
    handle = ScriptListenerHandle(port=port)
    _active_listener = handle
    app = build_script_app()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            ssl_certfile=certfile,
            ssl_keyfile=keyfile,
            log_level="warning",
            loop="asyncio",
            # We manage signals from the main process; the thread must not.
            lifespan="off",
        )
    )
    handle._server = server

    def _run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve())
        except (Exception, SystemExit) as exc:
            # uvicorn calls sys.exit(1) (SystemExit, a BaseException) when it
            # can't bind the port — catch it here so the thread ends cleanly and
            # leaves handle.bound False.  The caller (_maybe_start_script_listener)
            # turns an unbound handle into a hard startup failure.
            logger.error("Script listener thread exited: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                loop.close()

    thread = threading.Thread(target=_run, name="ScriptListener", daemon=True)
    handle.thread = thread
    thread.start()

    # Wait (briefly, startup-only) for the server to report it bound the port.
    deadline = time.time() + bind_timeout
    while time.time() < deadline:
        if getattr(server, "started", False):
            handle.bound = True
            break
        if not thread.is_alive():
            break
        time.sleep(0.05)

    if handle.bound:
        logger.info(
            "Dedicated script listener bound on %s:%d (off-loop script delivery)",
            host, port,
        )
    else:
        logger.error(
            "Dedicated script listener FAILED to bind %s:%d within %.1fs — "
            "backend startup will hard-fail. Check the port is free and certs "
            "exist, or set SCRIPT_LISTENER_ENABLED=false to opt out.",
            host, port, bind_timeout,
        )
    return handle
