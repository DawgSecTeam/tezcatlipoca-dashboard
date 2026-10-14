"""Root FastAPI app: comp CRUD, the run queue API (+SSE), MCP at /mcp, web frontend."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dashboard import auth, comps_api, config
from dashboard.events import EventBus
from dashboard.mcp_server import build_mcp
from dashboard.runner import RunManager, RunRefused
from dashboard.store import Store


class RunBody(BaseModel):
    comp_id: str
    action: str
    engine_ref: str = "main"
    teams: int = 2
    scoring_vmid: Optional[int] = None
    engine_node: str = ""
    team_node: str = ""
    params: dict = {}


def create_app():
    store = Store(config.data_dir() / "dashboard.sqlite3")
    bus = EventBus()
    manager = RunManager(store, bus)
    mcp = build_mcp(manager)
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp")

    @asynccontextmanager
    async def lifespan(_app):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="tezcatlipoca-dashboard", lifespan=lifespan)
    app.add_middleware(auth.BearerAuth)
    app.router.routes.extend(comps_api.app.router.routes)

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "tezcatlipoca-dashboard"}

    @app.post("/api/runs")
    def submit(body: RunBody):
        params = dict(body.params or {})
        params.update({"teams": body.teams, "scoring_vmid": body.scoring_vmid,
                       "engine_node": body.engine_node, "team_node": body.team_node})
        try:
            return manager.submit(body.comp_id, body.action, body.engine_ref or "main", params)
        except RunRefused as e:
            raise HTTPException(409, str(e))

    @app.get("/api/runs")
    def runs(state: str = "", comp_id: str = ""):
        return store.list_runs(state=state or None, comp_id=comp_id or None)

    def _run_or_404(run_id: str):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(404, f"no such run: {run_id}")
        return run

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        return _run_or_404(run_id)

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        _run_or_404(run_id)
        try:
            return manager.cancel(run_id)
        except RunRefused as e:
            raise HTTPException(409, str(e))

    @app.get("/api/runs/{run_id}/log")
    def log(run_id: str, since: int = 0):
        run = _run_or_404(run_id)
        path = Path(run["log_path"])
        data = path.read_bytes() if path.exists() else b""
        return {"offset": len(data), "text": data[since:].decode(errors="replace"),
                "running": run["state"] in ("queued", "running"),
                "returncode": run["returncode"]}

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, since: int = 0):
        _run_or_404(run_id)
        nxt, evs = store.get_events(run_id, since)
        return {"next": nxt, "events": evs}

    @app.get("/api/runs/{run_id}/events/stream")
    async def stream(run_id: str):
        _run_or_404(run_id)
        queue = bus.subscribe(run_id)

        async def gen():
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(event)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                bus.unsubscribe(run_id, queue)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/runs/{run_id}/artifacts")
    def artifacts(run_id: str):
        run = _run_or_404(run_id)
        root = Path(run["run_dir"]) / "comp" / ".automated-tests"
        if not root.exists():
            return []
        return [{"path": str(p.relative_to(root)), "bytes": p.stat().st_size}
                for p in sorted(root.rglob("*")) if p.is_file()]

    # MCP before the SPA catch-all (routes match in order)
    app.router.routes.extend(mcp_app.routes)

    dist = Path(__file__).resolve().parents[1] / "web" / "dist"
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        def spa(path: str):
            if path.startswith("api/") or path.startswith("mcp"):
                raise HTTPException(404)
            f = dist / path
            if path and f.is_file() and dist in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(dist / "index.html")

    return app
