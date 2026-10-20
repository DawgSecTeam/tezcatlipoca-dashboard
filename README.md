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

1. cuts an engine worktree at the ref under `<data>/runs/<id>/engine` — the ref defaults to
   `main` (`TEZ_ENGINE_REF` overrides it, e.g. a SHA pinned for an event), and provisioning
   fetches `origin` first so a branch name means the fresh one, not whatever the clone had at
   clone time,
2. seeds the comp's definitions into `<data>/runs/<id>/comp` (per-run state stays in
   the workspace; the competitions repo stays definitions-only),
3. drops the engine configuration into that worktree: `TEZ_ENGINE_ENV_FILE` as `.env` and,
   when set, `TEZ_ENGINE_CONFIG_FILE` as `tezcatlipoca.toml`. A worktree is a fresh checkout
   and both files are gitignored, so without one of them a driver starts with no endpoint,
   node or API token,
4. runs the driver (`create-competition.py` / `verify-competition.py` /
   `destroy-competition.py` / `run-agent-scrim.py` / `redeploy-competition.py`) with
   `--comp-dir <workspace>`,
5. emits one stage event per step — a deploy's `[N/8]` phases from the estate document's
   `run.last_phase` (through `dashboard/estate.py`, the service-side mirror of the engine's
   `estate.state.load_flat`), a scrim's phases from its own
   `.automated-tests/<run>/run.json`, and a single stage for the actions whose driver
   reports none (`verify`, `destroy` → `teardown`, `redeploy`); every event carries
   `index`/`total`,
6. archives evidence and removes the worktree.

### Where a range's state lives

A range's state is the competition's monolithic **`estate-state.json`**: `identity` /
`secrets` / `roster` / `templates` / `run` sections plus a `resources` list (every VM and
bridge the deploy created — engine, team boxes, goldens, the engine template, jumps,
bridges). Nothing in the dashboard writes it; the engine does, and every reader here agrees
with `estate.state.load_flat` on its shape:

| Reading | Source |
|---|---|
| deploy progress / run id | document `run.last_phase`, `identity.run_id` |
| range liveness | document `resources` (a destroy forgets each VM as it goes) |
| engine IP | `identity.engine_mgmt_ip` (the read behind `estate.engine_setup.engine_ctx`) |
| teams/boxes/networks/machines | document `resources` + `teams.json` |
| teardown steps | the document's `teardown` section |

Because the document carries **every generated password, the run id and the VM inventory**,
workspace seeding treats it as runtime state and strips it on a fresh seed (keeping it only
when resuming a workspace that already exists, i.e. the same range). Seeding one in from the
definitions repo would make a fresh deploy adopt a previous run's identity.

Engine settings resolve from `.env` (canonical `TEZ_*` names) or from a `tezcatlipoca.toml`
profile. `TEZ_PAUSE_FILE`, `TEZ_OPS_URL`/`TEZ_OPS_TOKEN`, `TEZ_COMPETITIONS_ROOT` and
`TEZ_DASHBOARD_*` are read from the environment.

Concurrency is parallel: several runs — including several of the same competition — may
execute at once, each in its own worktree and workspace. The engine's preflight/locks stay
the collision authority: a submit whose parameters provably collide (vmids, engine IPs)
fails fast at the engine with that refusal.

## Ranges

A range is a WORKSPACE, not a competition: two concurrent deploys of one competition are two
ranges, and grouping them by `comp_id` would let one teardown retire the other. Every
verify/destroy/redeploy run records the workspace it acted on (`params._destroy_comp_dir`),
while a deploy/scrim run's own workspace is `<run dir>/comp`, so the service groups any run to
its range without asking the estate.

`GET /api/ranges` reads each range's state from the workspace itself rather than from the run
records: `building` (a run is queued or running), `live` (the estate document still records a
VM — the document's `resources` list is the inventory its teardown drains, and the run that
built the range having failed means nothing), `retired` (those resources are gone and a
destroy ended rc=0), or `teardown_failed` (they are gone, the newest destroy ended non-zero —
the estate never owned red01, so a VM holding the LLM key can outlive its range; the
document's `teardown` section records which teardown steps actually completed). Only `building`, `live` and `teardown_failed` reach the board. Retirement is
enforced on the way in too: `submit` refuses verify/destroy/redeploy against a retired range,
naming the run that retired it, which is what stops a dead range from collecting teardown
attempts forever.

Two ranges of one competition on ONE node each need their own team vmid block and their own
engine management address, because the engine env the runner shares across runs pins both as
single values (`TEZ_TEAM_IDENTIFIERS`, `TEZ_ENGINE_MGMT_IP`) and the node catalog holds one
`engine_mgmt_ip` per node. `team_identifiers` ("130,131") and `engine_mgmt_ip` are
therefore per-run submit inputs, passed through to `create-competition.py`'s
`--team-identifiers`/`--engine-mgmt-ip`; absent, a run keeps the env/catalog defaults exactly
as before.

A `--teams-only` teardown retires a range but keeps this competition's engine template and
goldens for its next run. The board carries them as `kept_templates` /
`reclaimable_templates`, and a full destroy is allowed to resolve that retired workspace
(only while it has no live VMs); all other actions still refuse it.

### Retention

`python3 -m dashboard.retention <run-id>` is the sanctioned way to reclaim a run dir:
it refuses unless that run's range is already retired (the same gate
`POST /api/ranges/retire` records), then MOVES `<data>/runs/<run-id>` to
`<data>/retired-runs/<YYYY-MM-DD>/<run-id>` — `comp/` and its evidence tree included — and
never deletes a workspace. A non-retired range whose workspace is gone is reported as
`workspace_missing` by `GET /api/ranges` and the inventory, so the board cannot silently
lose it.

## API (bearer token, `Authorization: Bearer <token>`)

| Route | What |
|---|---|
| `GET /api/health` | liveness + service identity |
| `GET /api/comps`, `GET /api/comps/{id}` | competition summaries / detail (from the comps repo) |
| `PUT /api/comps/{id}/compfile`, `/boxes[...]`, `/injects[...]`, `/packet` | competition editing (same contract as the old webui) |
| `GET /api/nodes`, `PUT /api/nodes` | the node catalog (masked — tokens are write-only) |
| `POST /api/runs` | submit `{comp_id, action, engine_ref?, teams?, scoring_vmid?, team_identifiers?, engine_mgmt_ip?, nodes?, engine_node?, team_node?, params?}` |
| `GET /api/runs`, `GET /api/runs/{id}` | run records (`state`, `stage`, timings, rc) |
| `GET /api/ranges` | ranges derived from run workspaces (state, retire, runs) |
| `POST /api/runs/{id}/cancel` | kill the run's process group |
| `POST /api/runs/{id}/pause` | hold a deploy at its next phase boundary (`{run, detail}`) |
| `POST /api/runs/{id}/resume` | withdraw the request, or let a held deploy continue |
| `GET /api/competition?range=<key>` | one range's competition lifecycle (state, rounds, admin links + logins) |
| `GET /api/requests` | reset requests the deployed portals pushed |
| `POST /api/requests/{id}/approve` | submit a redeploy-reset run for the requested box (the run's exit resolves the request) |
| `POST /api/requests/{id}/deny` | refuse the request |
| `POST /api/ops/requests` | the portals' sync endpoint — per-range ops token, NOT the bearer token |
| `GET /api/runs/{id}/log?since=N` | log tail by byte offset |
| `GET /api/runs/{id}/events?since=N` | event cursor recovery (no polling loops needed) |
| `GET /api/runs/{id}/events/stream` | SSE of live run events |
| `GET /api/runs/{id}/artifacts` | collected evidence tree |
| `GET /api/runs/{id}/artifacts/file?path=` | one artifact file (paths under the run's evidence tree only) |
| `GET /api/runs/{id}/inventory` | the range's deployed infrastructure, projected from its workspace |

## Web UI

The React app has four surfaces: **Active ranges** (`/` — one card per range that is building
or up, plus a one-line tombstone per recently retired range; dead attempts never board and stay
on **Runs**, `/runs` — every run with a detail drawer), a per-run management page at
**`/c/{id}/runs/{run}`** (Overview: sourced facts, the stage display, timeline and this run's
own output; Teams: the range's teams, boxes and redeploy actions; Infrastructure: the deployed
networks, boxes and machines from the inventory; Evidence: collected files grouped by the run
that produced them; `/scrims/{id}` redirects here), and the per-competition editor under
`/c/{id}`.

## Deploy, then start

A deploy no longer starts the competition: it ends with everything built, teams seeded and
the student portal up — but the engine paused and the competition clock unstarted. The run
page of the deploy gains a **Competition** panel once it finishes: Start, Pause scoring,
Resume, End (End captures the final scoreboard into the range's evidence dir and freezes
scoring — terminal), plus links into the Quotient admin pages (scoreboard, /admin,
/announcements, /injects) with the logins that work there. Under the hood each action is a
run of the engine's `competition-ops.py` in the range's workspace. The old one-shot behavior
("start the competition right after deploying") is a checkbox in the deploy panel. Team
removal is a **Delete team** button on the run page's Teams tab: it runs
`destroy-competition.py --team <key>` — this team's clones die, the rest of the range is
never addressed, and the team is deactivated (Quotient has no hard delete) with its
scoreboard + portal logins revoked.

### Reset requests

A deployed portal can file box-reset requests to the dashboard: a team hits "Request reset"
on its portal box card, the request lands in the dashboard's bell (top right) and on the
range's Teams tab, and Approve submits the reset. The channel exists when the service env
sets `TEZ_PUBLIC_URL` (the portal reaches the dashboard there) and the deploy was submitted
through the dashboard — the per-range ops token is minted at submit and the portal's sync
converges on the dashboard's statuses (approved / denied / done / failed).

### Pausing a deploy

`Pause after next stage` on a run page asks the driver to hold at its next phase boundary: the
phase in progress finishes and the process parks there — the only consistent place to stop, since
a phase can have a Proxmox API burst or a VM build in flight. The range stays `building`, the
deploy lock stays held, and `Resume` carries on in the same process. Only `deploy` has
boundaries (verify/destroy/redeploy report one stage; a parked scrim would freeze its harness
while red, blue and the clock keep running), a request is refused when the run's engine ref
predates the hook, and a held run shows the phase it stopped after. The engine side is
`deploy_lib/pause.py` in the tezcatlipoca checkout.

## MCP (`/mcp`, Streamable HTTP, same bearer token)

Tools: `list_comps`, `get_comp`, `submit_run`, `list_runs`, `get_run`, `watch_run`,
`unwatch_run`, `cancel_run`, `pause_run`, `resume_run`, `get_run_log`, `get_run_events`,
`get_artifacts`.

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
| `sshpass` | Windows boxes are reached over password auth | user-space `~/.local/bin` is on the driver PATH (no sudo needed on Fedora) |
| `ssh`, `scp`, `curl`, `git`, `jq` | driver plumbing | distro packages |

Health check before submitting: `python3 -c "import toml, paramiko, mysql.connector"` in the
service venv, `command -v opencode sshpass`, and `GET /api/health`.

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in tokens, repo paths, engine settings
.venv/bin/python service.py
```

`web/` is the React frontend (`cd web && npm install && npm run build`); the service
serves `web/dist`. Deployment on the ops host is a systemd unit — see `deploy/`.
