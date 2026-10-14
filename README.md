# tezcatlipoca-dashboard

The central place range runs happen: one service that queues and executes
deploy/verify/destroy/scrim runs against tezcatlipoca engine worktrees, pushes stage
completion to waiting agents over MCP and to humans over the web UI, and keeps every
run's state and evidence in its own data dir.

- **Engine** (execution): github.com/DawgSecTeam/tezcatlipoca — drivers run against a
  per-run workspace copy via `--comp-dir`.
- **Competition definitions**: github.com/DawgSecTeam/tezcatlipoca-competitions —
  read-only here; every run seeds a workspace snapshot.
- **Estate ground truth** (hosts, endpoints, node names, datastores, token sources, live
  ownership): the engine repo's gitignored `../tezcatlipoca/infrastructure-status.md` —
  read it before a deploy; do not copy its values into this repo.

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

Concurrency is parallel: several runs — including several of the same competition — may
execute at once, each in its own worktree and workspace. The engine's preflight/locks stay
the collision authority: a submit whose parameters provably collide (vmids, engine IPs)
fails fast at the engine with that refusal.

## API (bearer token, `Authorization: Bearer <token>`)

| Route | What |
|---|---|
| `GET /api/health` | liveness + service identity |
| `GET /api/comps`, `GET /api/comps/{id}` | competition summaries / detail (from the comps repo) |
| `PUT /api/comps/{id}/compfile`, `/boxes[...]`, `/injects[...]`, `/packet` | competition editing (same contract as the old webui) |
| `GET /api/nodes`, `PUT /api/nodes` | the node catalog (masked — tokens are write-only) |
| `POST /api/runs` | submit `{comp_id, action, engine_ref?, teams?, scoring_vmid?, nodes?, engine_node?, team_node?, params?}` |
| `GET /api/runs`, `GET /api/runs/{id}` | run records (`state`, `stage`, timings, rc) |
| `POST /api/runs/{id}/cancel` | kill the run's process group |
| `GET /api/runs/{id}/log?since=N` | log tail by byte offset |
| `GET /api/runs/{id}/events?since=N` | event cursor recovery (no polling loops needed) |
| `GET /api/runs/{id}/events/stream` | SSE of live run events |
| `GET /api/runs/{id}/artifacts` | collected evidence tree |
| `GET /api/runs/{id}/artifacts/file?path=` | one artifact file (paths under the run's evidence tree only) |

## Web UI

The React app has four surfaces: **Active scrims** (`/` — one card per run attempt, dry
runs excluded; they stay on **Runs**), **Runs** (`/runs` — every run with a detail
drawer), a per-run management page at **`/scrims/{id}`** (Overview: stages, usage,
active teams, events, log; Teams: per-team boxes + reset-a-box; Artifacts: the evidence
tree, click to download), and the per-competition editor under `/c/{id}`.

## MCP (`/mcp`, Streamable HTTP, same bearer token)

Tools: `list_comps`, `get_comp`, `submit_run`, `list_runs`, `get_run`, `watch_run`,
`unwatch_run`, `cancel_run`, `get_run_log`, `get_run_events`, `get_artifacts`.

## Nodes

Which Proxmox hosts a run may deploy to is a per-run input, not the service env. The
dashboard holds a node **catalog** in `<data>/nodes.json` (0600, edited in the UI or via
`PUT /api/nodes`); a run selects from it (`nodes: ["<node-name>"]`) and the runner writes that
subset to the run dir as the engine's node list, passed with `--nodes`. Tokens live only
in the catalog and ride the run's process env under a derived `token_env` name, so no
secret reaches a worktree, a run record, or the API (the catalog view carries
`has_token` only). A run with no nodes selected keeps the old behavior: single-node from
whatever `TEZ_ENGINE_ENV_FILE` points at. Every catalog token is injected into every run's
env, so a later verify/destroy resolves its placement's `token_env` without re-sending the
list.

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

## Run-host prerequisites

The service's interpreter runs the engine drivers AND the red agent, and the drivers shell out to
host tools — so the host needs more than the Python deps:

| Need | Why | Notes |
|---|---|---|
| `requirements.txt` in the service venv | the drivers import the engine deps (toml, requests, urllib3, yaml, dotenv, Pillow, cryptography, websocket-client), `paramiko` for bad-auto, `mysql-connector-python` for nakon's catalog | `submit_run` refuses up front when one is missing |
| `opencode` | every blue cycle is an `opencode run`; absent → all cycles rc=127, run looks undefended | `~/.opencode/bin` is prepended to the driver PATH (`TEZ_OPENCODE_BIN` overrides) |
| `terraform` | the deploy's phases 1-5 apply real infra | user-space `~/.local/bin` is on the driver PATH (no sudo needed on Fedora) |
| `sshpass` | Windows boxes are reached over password auth | same `~/.local/bin` path |
| `ssh`, `scp`, `curl`, `git`, `jq` | driver plumbing | distro packages |

Health check before submitting: `python3 -c "import toml, paramiko, mysql.connector"` in the
service venv, `command -v opencode terraform sshpass`, and `GET /api/health`.

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in tokens, repo paths, engine settings
.venv/bin/python service.py
```

`web/` is the React frontend (`cd web && npm install && npm run build`); the service
serves `web/dist`. Deployment on the ops host is a systemd unit — see `deploy/`.
