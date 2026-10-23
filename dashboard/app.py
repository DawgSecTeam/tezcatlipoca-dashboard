"""Root FastAPI app: comp CRUD, the run queue API (+SSE), MCP at /mcp, web frontend."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.streamable_http import TransportSecuritySettings
from pydantic import BaseModel

from dashboard import auth, comps_api, config, diagnose, estate
from dashboard.competition import status_for_range
from dashboard.events import EventBus
from dashboard.inventory import build_inventory
from dashboard.logging_setup import configure as configure_logging
from dashboard.mcp_server import build_mcp
from dashboard.ranges import build_range_board, build_ranges, origin_run, range_detail
from dashboard.runner import RunManager, RunRefused, artifacts_root
from dashboard.store import Store

logger = logging.getLogger(__name__)


class RunBody(BaseModel):
    comp_id: str
    action: str
    # Empty means "the service's pinned engine ref" (config.default_engine_ref) — the API
    # carries a default, not a hardcoded branch, so a run's revision is one service setting.
    engine_ref: str = ""
    teams: int = 2
    scoring_vmid: Optional[int] = None
    team_identifiers: Optional[str] = None
    engine_mgmt_ip: Optional[str] = None
    engine_node: str = ""
    team_node: str = ""
    nodes: list[str] = []
    # Names the workspace a range action must act on. Optional: without it the manager
    # resolves the competition own range workspace.
    comp_dir: str = ""
    params: dict = {}


def create_app():
    configure_logging()
    store = Store(config.data_dir() / "dashboard.sqlite3")
    bus = EventBus()
    manager = RunManager(store, bus)
    mcp = build_mcp(manager)
    # Bearer auth is the access guard; the SDK's DNS-rebinding Host check would
    # refuse the LAN clients (421 on any Host but localhost).
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @asynccontextmanager
    async def lifespan(_app):
        # Startup, not construction: a non-terminal row that outlived the service lifetime
        # executing it holds no Popen here, so it can never start, finish or be cancelled
        # (cancel refuses a run this lifetime never spawned) and its range boards as
        # `building` forever — run-31072cb0, stuck six hours on 2026-10-15.
        orphans = manager.reconcile_orphans()
        logger.info("dashboard started: data=%s engine_ref=%s tokens=%d orphans_closed=%d",
                    config.data_dir(), config.default_engine_ref(), len(config.tokens()),
                    len(orphans))
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            logger.info("dashboard stopping")

    app = FastAPI(title="tezcatlipoca-dashboard", lifespan=lifespan)
    app.add_middleware(auth.BearerAuth)
    app.router.routes.extend(comps_api.app.router.routes)

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        # uvicorn only writes a traceback for exceptions that escape the app; logging it
        # here keeps a 500 attributable to the route and the run/journal it came from.
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "internal server error"}, status_code=500)

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": "tezcatlipoca-dashboard"}

    @app.post("/api/runs")
    def submit(body: RunBody):
        params = dict(body.params or {})
        # The body's convenience fields are DEFAULTS: `params` already carries the driver's
        # own flags (a redeploy's teams selector, boxes, mode), and body.teams' 2 would
        # otherwise clobber an explicit params["teams"].
        for key, val in (("teams", body.teams), ("scoring_vmid", body.scoring_vmid),
                         ("team_identifiers", body.team_identifiers),
                         ("engine_mgmt_ip", body.engine_mgmt_ip),
                         ("engine_node", body.engine_node), ("team_node", body.team_node),
                         ("nodes", body.nodes)):
            params.setdefault(key, val)
        # Only `params` reaches the runner, so a top-level comp_dir used to be dropped
        # silently and the action fell through to range auto-resolution — which then targeted
        # whatever workspace looked newest (live 2026-10-15: a refused run stub shadowed the
        # live range and the action died on "no engine management IP recorded"). Honour the
        # documented body field as the same default the other convenience fields are.
        if getattr(body, "comp_dir", ""):
            params.setdefault("comp_dir", body.comp_dir)
        try:
            return manager.submit(body.comp_id, body.action, body.engine_ref, params)
        except RunRefused as e:
            logger.warning("refused %s %s: %s", body.comp_id, body.action, e)
            raise HTTPException(409, str(e))

    @app.get("/api/ranges")
    def ranges_list():
        return build_ranges(store)

    # Static route first: "board" must never be interpreted as an opaque range id.
    @app.get("/api/ranges/board")
    def ranges_board():
        return build_range_board(store)

    class RetireBody(BaseModel):
        # The reclaim knows the workspace, or only the run id it reclaimed by
        # (`destroy-competition.py --orphan-run` takes the estate document's run id).
        range: str = ""
        run: str = ""
        by: str = "orphan reclaim"

    @app.post("/api/ranges/retire")
    def retire_range(body: RetireBody):
        """Retire a range reclaimed OUTSIDE the queue (`destroy-competition.py --orphan-*`).

        The reclaim destroys the VMs by tag and deletes the workspace, so no `destroy` run ever
        reaches the store and the stale document kept reading `live` (and kept resolving bare
        range actions to a dead workspace).
        """
        claimed = str(Path(body.range).resolve()) if body.range else ""

        def matches(rng):
            if claimed and rng["key"] == claimed:
                return True
            return bool(body.run) and (body.run in {run["id"] for run in rng["runs"]}
                                       or body.run == estate.load_flat(rng["key"]).get("run_id"))

        found = [rng for rng in build_ranges(store) if matches(rng)]
        if not found:
            raise HTTPException(404, f"no such range: {body.range or body.run}")
        if len(found) > 1:
            raise HTTPException(409, "the run id names several ranges: "
                                     + ", ".join(rng["key"] for rng in found))
        store.retire_range(found[0]["key"], found[0]["comp_id"], body.by)
        return next(r for r in build_ranges(store) if r["key"] == found[0]["key"])

    @app.get("/api/ranges/{range_id}")
    def get_range(range_id: str):
        rng = next((rng for rng in build_ranges(store) if rng["id"] == range_id), None)
        if rng is None:
            raise HTTPException(404, f"no such range: {range_id}")
        return range_detail(store, rng)

    # A range's answer composes its own state with its runs' diagnoses, so the board's
    # "Needs attention" chip can lead somewhere that names the cause and the next step.
    @app.get("/api/ranges/{range_id}/diagnosis")
    def range_diagnosis(range_id: str):
        rng = next((rng for rng in build_ranges(store) if rng["id"] == range_id), None)
        if rng is None:
            raise HTTPException(404, f"no such range: {range_id}")
        return diagnose.diagnose_range(store, rng)

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

    # The same answer the MCP `diagnose_run` tool returns: one local read of the record,
    # its events and its log tail, instead of an agent's five-call reconstruction of them.
    @app.get("/api/runs/{run_id}/diagnosis")
    def run_diagnosis(run_id: str):
        return diagnose.diagnose_run(store, _run_or_404(run_id))

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        _run_or_404(run_id)
        try:
            return manager.cancel(run_id)
        except RunRefused as e:
            raise HTTPException(409, str(e))

    @app.post("/api/runs/{run_id}/pause")
    def pause(run_id: str):
        _run_or_404(run_id)
        try:
            return manager.pause(run_id)
        except RunRefused as e:
            raise HTTPException(409, str(e))

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str):
        _run_or_404(run_id)
        try:
            return manager.resume(run_id)
        except RunRefused as e:
            raise HTTPException(409, str(e))

    @app.get("/api/runs/{run_id}/log")
    def log(run_id: str, since: int = 0):
        run = _run_or_404(run_id)
        path = Path(run["log_path"])
        data = path.read_bytes() if path.exists() else b""
        return {"offset": len(data), "text": data[since:].decode(errors="replace"),
                "running": run["state"] in ("queued", "running", "pausing", "paused"),
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
        root = artifacts_root(run)
        if not root.exists():
            return []
        return [{"path": str(p.relative_to(root)), "bytes": p.stat().st_size}
                for p in sorted(root.rglob("*")) if p.is_file()]

    @app.get("/api/runs/{run_id}/inventory")
    def inventory(run_id: str):
        return build_inventory(store, _run_or_404(run_id))

    @app.get("/api/runs/{run_id}/artifacts/file")
    def artifact_file(run_id: str, path: str):
        run = _run_or_404(run_id)
        root = artifacts_root(run).resolve()
        f = (root / path).resolve()
        if root not in f.parents or not f.is_file():
            raise HTTPException(404, "no such artifact")
        return FileResponse(f, filename=f.name)

    # ---- competition lifecycle (the management panel) ----

    def _range_or_404(key: str):
        for rng in build_ranges(store):
            if rng["key"] == key:
                return rng
        raise HTTPException(404, f"no such range: {key}")

    @app.get("/api/competition")
    def competition(range: str = ""):
        if not range:
            raise HTTPException(400, "range key required")
        return status_for_range(store, _range_or_404(range))

    # ---- reset requests (pushed by the deployed portals) ----

    class OpsBody(BaseModel):
        requests: list[dict] = []

    @app.post("/api/ops/requests")
    def ops_requests(request: Request, body: OpsBody):
        # The global bearer middleware deliberately skips /api/ops: a deployed portal
        # presents its own per-range token instead (the dashboard minted it at submit).
        token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        rng = store.range_for_token(token)
        if rng is None:
            raise HTTPException(401, "unknown ops token")
        statuses = store.upsert_requests(rng["range_key"], rng["comp_id"],
                                         body.requests or [])
        return {"statuses": statuses}

    @app.get("/api/requests")
    def requests_list():
        return store.list_requests()

    @app.post("/api/requests/{req_id}/approve")
    def approve_request(req_id: int):
        req = store.get_request(req_id)
        if not req:
            raise HTTPException(404, f"no such request: {req_id}")
        if req["status"] == "approved":
            raise HTTPException(409, "this request is already being executed")
        if not req["team"] or not req["box"]:
            raise HTTPException(409, "a reset request needs a team and box target")
        key = str(Path(req["range_key"]).resolve())
        rng = next((rng for rng in build_ranges(store)
                    if rng["key"] == key and rng["comp_id"] == req["comp_id"]), None)
        origin = origin_run(rng) if rng else None
        engine_ref = (origin or {}).get("engine_ref") or config.default_engine_ref()
        try:
            run = manager.submit(req["comp_id"], "redeploy", engine_ref,
                                 {"mode": "reset", "boxes": req["box"], "teams": req["team"],
                                  "comp_dir": key})
        except RunRefused as e:
            raise HTTPException(409, str(e))
        store.set_request_status(req_id, "approved", approved_run=run["id"])
        return store.get_request(req_id)

    @app.post("/api/requests/{req_id}/deny")
    def deny_request(req_id: int):
        req = store.get_request(req_id)
        if not req:
            raise HTTPException(404, f"no such request: {req_id}")
        return store.set_request_status(req_id, "denied")

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
