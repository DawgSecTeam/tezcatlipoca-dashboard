"""PTY-based sliver-client console WebSocket handler.

(fastapi_backend/c2/sliver_console.py)

Each WebSocket connection spawns an isolated `sliver-client console` subprocess
attached to a PTY. Raw terminal I/O is proxied between the PTY master fd and
the WebSocket. No hub, no sharing — operator sessions are fully isolated.

Control messages (JSON):
  WS → backend:  {"type": "resize", "cols": N, "rows": N}   — resize PTY
  All other WS text → written verbatim to PTY stdin.
  PTY stdout/stderr → sent as UTF-8 text to WS.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import pty
import shutil
import struct
import termios

from starlette.websockets import WebSocket, WebSocketDisconnect

from fastapi_backend.core.config import config
from fastapi_backend.utils.auth import get_user_from_token

logger = logging.getLogger(__name__)

_DEFAULT_COLS = 220
_DEFAULT_ROWS = 50

# Seconds to wait for a concurrency slot before rejecting the connection. Kept
# short so an operator sees a clear "too many consoles" message instead of a
# terminal that hangs indefinitely.
_ACQUIRE_TIMEOUT = 0.5

# Lazily-built global semaphore capping concurrent console subprocesses. Rebuilt
# if config.SLIVER_CONSOLE_MAX_CONCURRENT changes at runtime. See config.py.
_console_sem: asyncio.Semaphore | None = None
_console_sem_size: int = -1


def _get_console_semaphore() -> asyncio.Semaphore:
    """Return the shared console-concurrency semaphore, sized to config."""
    global _console_sem, _console_sem_size
    size = max(1, int(getattr(config, "SLIVER_CONSOLE_MAX_CONCURRENT", 4)))
    if _console_sem is None or _console_sem_size != size:
        _console_sem = asyncio.Semaphore(size)
        _console_sem_size = size
    return _console_sem


def _build_console_argv() -> list[str]:
    """Argv for the console subprocess, wrapped in a memory-capped cgroup scope.

    When SLIVER_CONSOLE_MEMORY_MAX is set and `systemd-run` is available, the
    console runs inside its own `systemd-run --user --scope` cgroup with
    MemoryMax + MemorySwapMax=0, so a runaway console is OOM-killed inside its
    scope at the ceiling instead of taking the host down. `--quiet` keeps
    systemd's "Running as unit" line out of the PTY; `--collect` reaps the scope
    on exit. Falls back to a bare spawn when the cap is disabled or systemd-run
    is absent (concurrency cap still bounds total consoles).
    """
    base = [config.SLIVER_CLIENT_BIN, "console"]
    mem_max = (getattr(config, "SLIVER_CONSOLE_MEMORY_MAX", "") or "").strip()
    if mem_max and shutil.which("systemd-run"):
        return [
            "systemd-run", "--user", "--scope", "--quiet", "--collect",
            "-p", f"MemoryMax={mem_max}",
            "-p", "MemorySwapMax=0",
            "--", *base,
        ]
    return base


async def handle_sliver_console(websocket: WebSocket) -> None:
    """Auth → accept → spawn PTY → bidirectional I/O → cleanup."""
    token = websocket.query_params.get("token")
    try:
        user = get_user_from_token(token) if token else None
    except Exception:
        user = None

    if not user or user.is_blue_team:
        logger.info("ws CLOSE sliver-console session=- code=4003 reason=auth/blue-team rejected")
        await websocket.close(code=4003)
        return

    await websocket.accept()
    logger.info("ws OPEN sliver-console session=- user=%s", getattr(user, "username", "?"))

    if config.DEV_MODE:
        await websocket.send_text(
            "\r\n[DEV MODE] Sliver console not available — Sliver is disabled.\r\n"
            "Set SLIVER_ENABLED=true and configure SLIVER_CFG_PATH to use this feature.\r\n"
        )
        logger.info("ws CLOSE sliver-console session=- code=None reason=dev-mode stub")
        with contextlib.suppress(Exception):
            await websocket.close()
        return

    # Concurrency cap: reject rather than spawn an (N+1)th console subprocess.
    # Each console can balloon to many GB against a cyclic pivot graph, so an
    # unbounded fan-out of consoles can OOM the host (2026-08-31). Held until the
    # PTY session ends; released in every terminal path via _release_slot().
    sem = _get_console_semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=_ACQUIRE_TIMEOUT)
    except TimeoutError:
        logger.info(
            "ws CLOSE sliver-console session=- code=None reason=at-capacity (max=%d)",
            _console_sem_size,
        )
        with contextlib.suppress(Exception):
            await websocket.send_text(
                f"\r\n[busy] Too many Sliver consoles open (limit {_console_sem_size}). "
                "Close another console and reconnect.\r\n"
            )
        with contextlib.suppress(Exception):
            await websocket.close()
        return

    slot_held = True

    def _release_slot() -> None:
        nonlocal slot_held
        if slot_held:
            slot_held = False
            sem.release()

    master_fd: int | None = None
    proc: asyncio.subprocess.Process | None = None

    try:
        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(
            master_fd, termios.TIOCSWINSZ,
            struct.pack("HHHH", _DEFAULT_ROWS, _DEFAULT_COLS, 0, 0),
        )

        proc = await asyncio.create_subprocess_exec(
            *_build_console_argv(),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
        )
        os.close(slave_fd)

    except FileNotFoundError:
        logger.warning("sliver-client binary not found at %r", config.SLIVER_CLIENT_BIN)
        await websocket.send_text(
            f"\r\n[error] sliver-client binary not found: {config.SLIVER_CLIENT_BIN!r}\r\n"
            "Set SLIVER_CLIENT_BIN env to the correct path.\r\n"
        )
        with contextlib.suppress(Exception):
            await websocket.close()
        if master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(master_fd)
        _release_slot()
        return

    except Exception as exc:
        logger.exception("Failed to start sliver-client console")
        await websocket.send_text(f"\r\n[error] Failed to start console: {exc}\r\n")
        with contextlib.suppress(Exception):
            await websocket.close()
        if master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(master_fd)
        _release_slot()
        return

    loop = asyncio.get_event_loop()

    async def pty_to_ws() -> None:
        """Read PTY output and forward to WebSocket."""
        while True:
            try:
                data = await loop.run_in_executor(None, os.read, master_fd, 4096)
                if not data:
                    break
                text = data.decode("utf-8", errors="replace")
                await websocket.send_text(text)
            except OSError:
                # PTY hangup — subprocess exited
                break
            except Exception:
                break

    async def ws_to_pty() -> None:
        """Read WebSocket messages and forward to PTY stdin."""
        while True:
            try:
                msg = await websocket.receive_text()
                # Resize control frame
                try:
                    ctrl = json.loads(msg)
                    if ctrl.get("type") == "resize":
                        cols = max(1, int(ctrl.get("cols", _DEFAULT_COLS)))
                        rows = max(1, int(ctrl.get("rows", _DEFAULT_ROWS)))
                        fcntl.ioctl(
                            master_fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", rows, cols, 0, 0),
                        )
                    # Other control types silently ignored
                    continue
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
                # Raw terminal input
                os.write(master_fd, msg.encode("utf-8"))
            except WebSocketDisconnect:
                break
            except Exception:
                break

    pty_task = asyncio.create_task(pty_to_ws())
    ws_task = asyncio.create_task(ws_to_pty())

    try:
        _done, pending = await asyncio.wait(
            [pty_task, ws_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        with contextlib.suppress(OSError):
            os.close(master_fd)
        if proc is not None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (TimeoutError, Exception):
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
        _release_slot()
        logger.info("ws CLOSE sliver-console session=- code=None reason=pty session ended")
        with contextlib.suppress(Exception):
            await websocket.close()
