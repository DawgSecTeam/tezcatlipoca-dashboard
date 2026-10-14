# tezcatlipoca-dashboard

The central place range runs happen: one service that queues and executes
deploy/verify/destroy/scrim runs against tezcatlipoca engine worktrees, pushes stage
completion to waiting agents over MCP and to humans over the web UI, and keeps every
run's state and evidence in its own data dir.

- **Engine** (execution): github.com/DawgSecTeam/tezcatlipoca — drivers run against a
  per-run workspace copy via `--comp-dir`.
- **Competition definitions**: github.com/DawgSecTeam/tezcatlipoca-competitions —
  read-only here; every run seeds a workspace snapshot.

## Run model

A run pins a tezcatlipoca git ref (branch/SHA/tag) and a competition. The runner:

1. cuts an engine worktree at the ref under `<data>/runs/<id>/engine`,
2. seeds the comp's definitions into `<data>/runs/<id>/comp` (per-run state stays in
   the workspace; the competitions repo stays definitions-only),
3. runs the driver (`create-competition.py` / `verify-competition.py` /
   `destroy-competition.py` / `run-agent-scrim.py` / `redeploy-competition.py`) with
   `--comp-dir <workspace>`,
4. emits stage events (deploy `[N/8]` phases via `.deploy_state.json` `last_phase`,
   scrim `run.json` phases) to subscribers as they complete,
5. archives evidence and removes the worktree.

Concurrency is parallel and fail-fast: a second run of the same competition is refused
at submit; the engine's own preflight/locks refuse provable resource collisions and the
run fails fast with that refusal.

## API (bearer token, `Authorization: Bearer <token>`)

| Route | What |
|---|---|
| `GET /api/health` | liveness + service identity |
| `GET /api/comps`, `GET /api/comps/{id}` | competition summaries / detail (from the comps repo) |
| `PUT /api/comps/{id}/compfile`, `/boxes[...]`, `/injects[...]`, `/packet` | competition editing (same contract as the old webui) |
| `POST /api/runs` | submit `{comp_id, action, engine_ref?, teams?, scoring_vmid?, engine_node?, team_node?, params?}` |
| `GET /api/runs`, `GET /api/runs/{id}` | run records (`state`, `stage`, timings, rc) |
| `POST /api/runs/{id}/cancel` | kill the run's process group |
| `GET /api/runs/{id}/log?since=N` | log tail by byte offset |
| `GET /api/runs/{id}/events?since=N` | event cursor recovery (no polling loops needed) |
| `GET /api/runs/{id}/events/stream` | SSE of live run events |
| `GET /api/runs/{id}/artifacts` | collected evidence tree |

## MCP (`/mcp`, Streamable HTTP, same bearer token)

Tools: `list_comps`, `get_comp`, `submit_run`, `list_runs`, `get_run`, `watch_run`,
`unwatch_run`, `cancel_run`, `get_run_log`, `get_run_events`, `get_artifacts`.

`watch_run(run_id)` subscribes the MCP session: stage/lifecycle events are then **pushed**
as `notifications/message` on the session's stream (structured `data`:
`{type: "run_event", run_id, kind, stage, ...}`) until `unwatch_run` or the session ends.
`get_run_events(run_id, since)` recovers missed events after a reconnect — the cursor is
the event sequence number.

Two facts a pusher must respect: every event a run records is published to the event bus by
`RunManager._emit` (recording without publishing left `watch_run` and the SSE stream silent),
and the push only lands on a **handshake-era** session. Protocol revision 2026-07-28 makes
`notifications/message` a per-request opt-in that rides that request's own stream, so a
connection-scoped emitter delivers nothing; an `mcp` `ClientSession` negotiates 2025-11-25 and
receives pushes, while a session that adopts 2026-07-28 does not (`watch_run` says so instead
of promising them, and the cursor is the fallback).

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in tokens, repo paths, engine settings
.venv/bin/python service.py
```

`web/` is the React frontend (`cd web && npm install && npm run build`); the service
serves `web/dist`. Deployment on the ops host is a systemd unit — see `deploy/`.
