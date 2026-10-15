"""MCP surface: run control tools + push subscriptions (Streamable HTTP, /mcp).

watch_run subscribes the calling MCP session: from then on every stage/lifecycle event
for that run is pushed to the session as a log notification (structured data:
{type: "run_event", ...}) — the agent waits for the push instead of polling. The event
cursor (get_run_events since=N) recovers anything missed across a reconnect."""

import asyncio
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from dashboard.runner import RunRefused


def build_mcp(manager):
    mcp = MCPServer("tezcatlipoca-dashboard")
    store, bus = manager.store, manager.bus
    pumps = {}  # (session_id, run_id) -> task

    def _summary(comp_dir):
        compfile = (comp_dir / "Compfile").read_text() if (comp_dir / "Compfile").exists() else ""
        name = comp_dir.name
        scenario, difficulty = "", 0
        for line in compfile.splitlines():
            if line.startswith("name "):
                name = line[5:].strip()
            elif line.startswith("scenario "):
                scenario = line[9:].strip()
            elif line.startswith("difficulty "):
                try:
                    difficulty = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
        return {"id": comp_dir.name, "name": name, "scenario": scenario,
                "difficulty": difficulty}

    @mcp.tool()
    def list_comps() -> list:
        """List the competitions available to run (from the competitions repo)."""
        manager.ensure_repos()
        root = manager.comps_repo
        return [_summary(p) for p in sorted(root.iterdir())
                if p.is_dir() and (p / "Compfile").exists()]

    @mcp.tool()
    def get_comp(comp_id: str) -> dict:
        """One competition's summary plus its box/service/misconfig counts."""
        manager.ensure_repos()
        comp_dir = manager.comps_repo / comp_id
        if not (comp_dir / "Compfile").exists():
            raise ToolError(f"no such competition: {comp_id}")
        import json
        detail = _summary(comp_dir)
        for key, fname in (("boxes", "boxes.json"), ("services", "box_services.json"),
                           ("misconfigs", "box_vulns.json")):
            try:
                data = json.loads((comp_dir / fname).read_text())
                detail[key] = len(data) if isinstance(data, list) else sum(
                    len(v) for v in data.values()) if isinstance(data, dict) else 0
            except (OSError, ValueError):
                detail[key] = 0
        return detail

    @mcp.tool()
    def submit_run(comp_id: str, action: str, engine_ref: str = "main", teams: int = 2,
                   scoring_vmid: int = 0, team_identifiers: str = "",
                   engine_mgmt_ip: str = "", engine_node: str = "", team_node: str = "",
                   nodes: list[str] = [], params: dict = {}) -> dict:
        """Queue a run: action is deploy|plan|verify|destroy|scrim|redeploy, or one of the
        competition-lifecycle kinds start|freeze|resume|end|delete_team (which act on the
        range's workspace). engine_ref pins the tezcatlipoca revision (branch/SHA/tag).
        `nodes` names the catalog nodes this run may deploy to (the engine gets them as its
        node list, not the .env); `engine_node`/`team_node` then pin within that list.
        `team_identifiers` ("130,131") and `engine_mgmt_ip` give THIS run its own team vmid
        block and management address, which is what lets two ranges of one competition
        coexist on a single node. Returns the run record."""
        p = dict(params or {})
        # Defaults, not overrides: an explicit params entry (a redeploy's teams selector, a
        # pinned scoring vmid) must survive the tool's own argument defaults.
        for key, val in (("teams", teams), ("scoring_vmid", scoring_vmid),
                         ("team_identifiers", team_identifiers),
                         ("engine_mgmt_ip", engine_mgmt_ip),
                         ("engine_node", engine_node), ("team_node", team_node),
                         ("nodes", [str(n) for n in nodes] if nodes else None)):
            if val:
                p.setdefault(key, val)
        try:
            return manager.submit(comp_id, action, engine_ref or "main", p)
        except RunRefused as e:
            # ToolError, not ValueError: the SDK only forwards a ToolError's own text to the
            # client — anything else arrives as "Error executing tool submit_run" with the
            # reason swallowed, so a refused overlap looked like a crash (2026-10-14).
            raise ToolError(f"refused: {e}") from e

    @mcp.tool()
    def list_runs(state: str = "") -> list:
        """Run records, newest first; optionally filter by state
        (queued|running|pausing|paused|succeeded|failed|cancelled)."""
        return store.list_runs(state=state or None)

    @mcp.tool()
    def get_run(run_id: str) -> dict:
        """One run's record: state, current stage, timings, result."""
        run = store.get_run(run_id)
        if not run:
            raise ToolError(f"no such run: {run_id}")
        return run

    @mcp.tool()
    async def watch_run(run_id: str, ctx: Context) -> str:
        """Subscribe this MCP session to a run's events. Stage and lifecycle events are
        pushed as notifications from here on; no polling needed."""
        if not store.get_run(run_id):
            raise ToolError(f"no such run: {run_id}")
        key = (id(ctx.session), run_id)
        if key not in pumps:
            queue = bus.subscribe(run_id)
            pumps[key] = asyncio.create_task(_pump(ctx.session, run_id, queue))
        era = getattr(ctx.session, "protocol_version", "")
        if era in MODERN_PROTOCOL_VERSIONS:
            # 2026-07-28 makes notifications/message a per-request opt-in riding the
            # request's own stream, and connection-scoped emitters deliver nothing —
            # the SDK drops the pump's sends silently. Say so instead of promising pushes.
            return (f"subscribed to {run_id}, but this session speaks {era}: the spec "
                    "forbids connection-scoped notifications, so stage pushes will NOT "
                    "arrive. Reconnect with a handshake-era session (an mcp ClientSession) "
                    "or read the events through the cursor: get_run_events(run_id, since=N).")
        return f"subscribed to {run_id}; stage events will arrive as notifications"

    @mcp.tool()
    def unwatch_run(run_id: str, ctx: Context) -> str:
        """Stop receiving pushed events for a run."""
        task = pumps.pop((id(ctx.session), run_id), None)
        if task:
            task.cancel()
        return f"unsubscribed from {run_id}"

    @mcp.tool()
    def cancel_run(run_id: str) -> dict:
        """Cancel a running run (kills its process group; recoverable)."""
        try:
            return manager.cancel(run_id)
        except RunRefused as e:
            raise ToolError(f"refused: {e}") from e

    @mcp.tool()
    def pause_run(run_id: str) -> dict:
        """Hold a running deploy at its next phase boundary (only deploy has boundaries)."""
        try:
            return manager.pause(run_id)
        except RunRefused as e:
            raise ToolError(f"refused: {e}") from e

    @mcp.tool()
    def resume_run(run_id: str) -> dict:
        """Withdraw a pause request, or let a held deploy continue."""
        try:
            return manager.resume(run_id)
        except RunRefused as e:
            raise ToolError(f"refused: {e}") from e

    @mcp.tool()
    def get_run_log(run_id: str, since: int = 0) -> dict:
        """Run log tail from byte offset `since`: {offset, text}."""
        run = store.get_run(run_id)
        if not run:
            raise ToolError(f"no such run: {run_id}")
        data = Path(run["log_path"]).read_bytes() if Path(run["log_path"]).exists() else b""
        return {"offset": len(data), "text": data[since:].decode(errors="replace")}

    @mcp.tool()
    def get_run_events(run_id: str, since: int = 0) -> dict:
        """Events after sequence number `since`: {next, events}. Reconnect recovery."""
        if not store.get_run(run_id):
            raise ToolError(f"no such run: {run_id}")
        nxt, events = store.get_events(run_id, since)
        return {"next": nxt, "events": events}

    @mcp.tool()
    def get_artifacts(run_id: str) -> list:
        """Collected evidence/artifact files under the run's test folders."""
        run = store.get_run(run_id)
        if not run:
            raise ToolError(f"no such run: {run_id}")
        from dashboard.runner import artifacts_root
        root = artifacts_root(run)
        if not root.exists():
            return []
        return [{"path": str(p.relative_to(root)), "bytes": p.stat().st_size}
                for p in sorted(root.rglob("*")) if p.is_file()]

    async def _pump(session, run_id, queue):
        try:
            while True:
                event = await queue.get()
                await session.send_log_message(
                    level="info", data={"type": "run_event", "run_id": run_id, **event})
        except asyncio.CancelledError:
            pass
        except Exception:
            pass  # a dead session must never kill the run or the service

    return mcp
