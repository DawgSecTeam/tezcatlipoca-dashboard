"""Push contract: every recorded run event reaches bus subscribers (watch_run / SSE).

Regression for the 2026-10-14 e2e finding: `EventBus.publish` had no caller anywhere, so a
run's events landed in the store (the cursor worked) while `watch_run` subscribers and the
SSE stream saw nothing. The recorded event and the pushed event must be the same dict.
"""

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from dashboard.events import EventBus
from dashboard.runner import RunManager
from dashboard.store import Store


def _manager(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    manager = RunManager(store, EventBus())
    manager.data = tmp_path
    manager.runs_dir = tmp_path / "runs"
    manager.runs_dir.mkdir(parents=True, exist_ok=True)
    return store, manager


def _seed_run(store, tmp_path, run_id="run-1"):
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    store.create_run(run_id, "comp-a", "scrim", "main", {}, run_dir, run_dir / "run.log")


def test_stage_event_is_published(tmp_path):
    async def body():
        store, manager = _manager(tmp_path)
        _seed_run(store, tmp_path)
        queue = manager.bus.subscribe("run-1")
        manager._stage("run-1", "phase 3/8 prepare_engine")
        pushed = await asyncio.wait_for(queue.get(), timeout=2)
        _, stored = store.get_events("run-1", 0)
        assert pushed == stored[-1], "pushed event must be the stored event, not a copy"
        assert pushed["kind"] == "stage"
        assert pushed["stage"] == "phase 3/8 prepare_engine"
        assert pushed["seq"] == 1
    asyncio.run(body())


def test_terminal_lifecycle_event_is_published(tmp_path):
    async def body():
        store, manager = _manager(tmp_path)
        _seed_run(store, tmp_path)
        queue = manager.bus.subscribe("run-1")
        manager._finish("run-1", 0)
        pushed = await asyncio.wait_for(queue.get(), timeout=2)
        _, stored = store.get_events("run-1", 0)
        assert pushed == stored[-1]
        assert pushed["kind"] == "lifecycle" and pushed["state"] == "succeeded"
    asyncio.run(body())


def test_cancel_request_event_is_published(tmp_path):
    async def body():
        store, manager = _manager(tmp_path)
        _seed_run(store, tmp_path)

        class _Proc:
            pid = 999999
            @staticmethod
            def poll():
                return None

        manager._procs["run-1"] = _Proc()
        queue = manager.bus.subscribe("run-1")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("os.killpg", lambda pid, sig: None)
            manager.cancel("run-1")
        pushed = await asyncio.wait_for(queue.get(), timeout=2)
        assert pushed["state"] == "cancelling"
    asyncio.run(body())


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """Live service with a fake driver, so a real run produces real events in ~1 s."""
    data = tmp_path / "data"
    (data / "competitions" / "mini").mkdir(parents=True)
    (data / "competitions" / "mini" / "Compfile").write_text("name mini\n")
    monkeypatch.setenv("TEZ_DASHBOARD_DATA", str(data))
    monkeypatch.delenv("TEZ_DASHBOARD_TOKENS", raising=False)
    monkeypatch.setattr(RunManager, "ensure_repos", lambda self: None)
    monkeypatch.setattr(RunManager, "refresh_comps", lambda self: None)

    def _fake_worktree(self, worktree, engine_ref, run_dir):
        # The fake driver runs with cwd=worktree, so the dir has to exist (the real
        # provisioner cuts a git worktree there and returns what it cut).
        Path(worktree).mkdir(parents=True, exist_ok=True)
        return {"ref": "origin/main", "commit": "0" * 40}

    monkeypatch.setattr(RunManager, "_provision_worktree", _fake_worktree)
    monkeypatch.setattr("dashboard.runner.build_command",
                        lambda action, comp_id, ws, params: [
                            "python3", "-c", "import time; time.sleep(1)"])
    from dashboard.app import create_app
    port = _free_port()
    cfg = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(cfg)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp", data
    srv.should_exit = True
    thread.join(timeout=5)


def test_watch_run_delivers_push_notifications(server):
    url, data = server
    asyncio.run(_watch(url))


async def _watch(url):
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    seen = []
    got_terminal = asyncio.Event()

    async def on_log(params):
        seen.append(params.data)
        if isinstance(params.data, dict) and params.data.get("state") == "succeeded":
            got_terminal.set()

    async with create_mcp_http_client() as http:
        async with streamable_http_client(url, http_client=http) as (read, write):
            async with ClientSession(read, write, logging_callback=on_log) as session:
                await session.initialize()
                import json
                res = await session.call_tool("submit_run", {
                    "comp_id": "mini", "action": "scrim", "engine_ref": "main", "teams": 1})
                run_id = json.loads(res.content[0].text)["id"]
                await session.call_tool("watch_run", {"run_id": run_id})
                await asyncio.wait_for(got_terminal.wait(), timeout=20)

    run_events = [d for d in seen if isinstance(d, dict) and d.get("type") == "run_event"]
    assert run_events, f"no pushed run events; got {seen}"
    assert all(d["run_id"] == run_id for d in run_events)
    assert any(d.get("kind") == "lifecycle" for d in run_events)
