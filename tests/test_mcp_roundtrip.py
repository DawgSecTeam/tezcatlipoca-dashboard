"""MCP integration: a real streamable-HTTP client against a live service instance.

Validates the /mcp mount, the session-manager lifespan, and the tool surface —
the pieces a curl smoke test cannot prove (session state across requests)."""

import socket
import threading
import time

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from dashboard.runner import RunManager


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("TEZ_DASHBOARD_TOKENS", raising=False)  # dev mode: no auth dance
    monkeypatch.setattr(RunManager, "ensure_repos", lambda self: None)
    monkeypatch.setattr(RunManager, "refresh_comps", lambda self: None)
    from dashboard.app import create_app
    port = _free_port()
    cfg = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(cfg)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


def test_initialize_tools_and_calls(server):
    import asyncio
    asyncio.run(_roundtrip(server))


async def _roundtrip(server):
    async with streamable_http_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            assert {"submit_run", "watch_run", "get_run_events", "cancel_run",
                    "diagnose_run", "diagnose_range"} <= names
            result = await session.call_tool("list_runs", {})
            assert not result.is_error
            assert not result.content or result.content[0].text == "[]"
            result = await session.call_tool("get_run", {"run_id": "run-ghost"})
            assert result.is_error
            result = await session.call_tool("diagnose_run", {"run_id": "run-ghost"})
            assert result.is_error
