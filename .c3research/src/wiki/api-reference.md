# API Reference (operator subset)

Operator-facing HTTP + WebSocket endpoints. For the full reference,
load `https://localhost:3000/docs` (FastAPI auto-generated) or grep
`fastapi_backend/routes/`.

## When to use

- Building an external client / dashboard that talks to C3rberus.
- Scripting bulk operations from a CLI.
- Debugging a frontend page (read the contract the page expects).

For the in-app surface, see the per-page wiki entries
([Generation](c2-generation), [Exec](c2-exec), etc.).

## Auth

- `POST /login` — `{username, password}` → `{access_token}`.
- `GET /me` — returns the current user's roles
  (`is_admin`, `is_blue_team`, team scope).

Pass the token as `Authorization: Bearer <token>` on every subsequent
request.

Role gating:
- **admin** + **red team** + **white/black team** — full operator
  surface.
- **blue team** — scoreboard + own-team-IOC details only; C2 surface
  blocked with `403 {"code": "..._blocked"}`.

## Implants / generation

| Endpoint | Method | Notes |
|---|---|---|
| `/c2/implants/generate` | POST | Returns `202 {task_id}`. Poll `/c2/tasks/{task_id}` for `metadata_json.build_id`. Pivot-set generate (`pivot_team_nums` set) is also `202` now — task type `generate_pivot`, with `pivot_set_id` + `builds` on `metadata_json`. |
| `/c2/implants/{build_id}/deploy` | POST | Returns `202 {task_id}` (task type `implant_deploy`). Poll `/c2/tasks/{task_id}` — per-host state in `progress_json`, aggregate in `metadata_json.results`. Unknown build → `404` synchronously. |
| `/c2/implants` | GET | List `ImplantBuilds` rows. |

Implant generate + deploy are asynchronous because implant compiles serialize through a
single global Sliver mutex: holding the HTTP response open for the whole operation made
callers time out while the work was still legitimately queued. While a task waits its
turn, `current_phase` is `queued` and `queue_position` reports how many compiles are
ahead of it (both are also on the `GET /c2/tasks` summary rows). See
`docs/PLAN_async_implant_deploy.md`.

## Bulk exec / put-file

| Endpoint | Method | Notes |
|---|---|---|
| `/c2/bulk/exec/async` | POST | Async fanout exec. `202 {task_id}`. Operator-scoped concurrent-submit guard. |
| `/c2/bulk/put-file/stage` | POST | Multipart staging for file push. |
| `/c2/bulk/put-file/async` | POST | Async dispatch. `202 {task_id}`. |

## IOC deployment

| Endpoint | Method | Notes |
|---|---|---|
| `/c2/iocs/deploy` | POST | Multi-box register-without-running. `202 {task_id}`. |
| `/c2/iocs/deploy-and-register` | POST | Run effect deploy script + register on success. `202 {task_id}`. |
| `/c2/iocs/deployed` | GET | List `DeployedIOCs` rows (active + lost by default). `?box_ip=` filters to one box; `?status=deploy_failed` lists IOCs that never fully landed (the deploy page's redeploy section). Rows carry `deploy_error` / `deploy_task_id` / `deploy_attempts`. |
| `/c2/iocs/deployed/{id}` | GET/DELETE | Read / deregister (cascades auto-creds). |
| `/c2/iocs/deployed/{id}/redeploy` | POST | Retry a `deploy_failed` IOC under its **original** pooled names (re-injects the row's recorded `deploy_params`; never re-picks from the pool). `202 {task_id}`. `409` unless the row is `deploy_failed` — redeploying an active row would plant a second copy. `400` for implant/inject IOCs (rebuild those via Generate / Deploy All). |

## Tasks

| Endpoint | Method | Notes |
|---|---|---|
| `/c2/tasks` | GET | List `OperatorTask` rows (filters: `operator`, `team_num`, `status`, `task_type`, `limit`). |
| `/c2/tasks/{id}` | GET | Single task; `progress_json` parsed. |
| `/c2/tasks/{id}/stream` | GET (SSE) | Per-task progress stream. Auth via `?token=`. |
| `/c2/tasks/stream` | GET (SSE) | Global task feed. Auth via `?token=`. |
| `/c2/tasks/{id}/kill` | POST | Cancel a `pending`/`running` task. Admin any task; non-admin operator only their own (403 otherwise). Real cancellation — cancels the tracked `asyncio.Task`, discards any stale result that lands afterward. |

## SA

| Endpoint | Method | Notes |
|---|---|---|
| `/c2/sa/{box_ip}` | GET | Latest SA frame per box. |
| `/c2/sa/{box_ip}/history` | GET | Rolling history. |
| `/c2/sa/{box_ip}/collect` | POST | Manual SA cycle. Coalesces concurrent triggers per IP. |

## Credentials / harvesting

| Endpoint | Method | Notes |
|---|---|---|
| `/c2/creds` | GET | List `Credentials` rows with `source_ioc_name` join. |
| `/c2/creds` | POST | Add a credential. |
| `/c2/harvest/{box_ip}` | POST | Manual SAM/LSA dump (`202 {task_id}`). |
| `/c2/forge-ticket` | POST | Forge golden Kerberos ticket. Body: `{team_num, principal?, duration_hours?}`. |
| `/c2/tickets/{id}/validate` | POST | Validate a forged ticket via Kerberos domain auth against team boxes (Windows SMB + domain-joined Linux SSH-GSSAPI/getST). Body: `{box_ip?}` (omit for whole team). |
| `/c2/tickets` | GET | List team-scoped ccache rows. |
| `/c2/tickets/{cred_id}/refresh` | POST | Re-forge. |
| `/c2/tickets/{cred_id}` | DELETE | Revoke + delete materialized ccache file. |

## Sessions / beacons / pivot

| Endpoint | Method | Notes |
|---|---|---|
| `/c2/sessions` | GET | List Sliver sessions. |
| `/c2/sessions/{id}/kill` | POST | Intentional kill (inserts `PendingKills` row). |
| `/c2/sessions/{id}/shell` | WS | OS shell fan-out hub. |
| `/c2/sessions/{id}/sliver-shell` | WS | Sliver text-dispatch shell hub. |
| `/c2/beacons` | GET | List Sliver beacons. |
| `/c2/beacons/{id}/kill` | POST | Intentional kill. |
| `/c2/beacons/{id}/interactive` | POST | Issue Sliver's `OpenSession` beacon task — spawns a linked session on the beacon's next check-in. Returns `{operator_task_id, state: "queued"}`; the RPC only queues the task, it doesn't wait for the session. |
| `/c2/pivot-sessions` | GET | Active `PivotSessions` rows. |

## Console

| Endpoint | Method | Notes |
|---|---|---|
| `/c2/sliver-console` | WS | Per-connection `sliver-client` PTY subprocess. Auth via `?token=`. Admin / red only. |

## Modules

| Endpoint | Method | Notes |
|---|---|---|
| `/c2/modules` | GET | List operator modules from `MODULES_DIR`. |
| `/c2/modules/rescan` | POST | Force directory rescan. |
| `/c2/modules/{module_id}/run` | POST | Dispatch on targets. |
| `/c2/modules/{module_id}/source` | GET | Module source. |
| `/c2/modules/{module_id}` | DELETE | Remove module. |

## Scoreboard / scoring

| Endpoint | Method | Notes |
|---|---|---|
| `/scoreboard` | GET | Public-readable team scores + history. |
| `/details` | GET | Per-team IOC details (blue sees own w/ obscured labels). |

## Admin

| Endpoint | Method | Notes |
|---|---|---|
| `/admin/status` | GET | Component health (Sliver, scheduler, DB writer, playbook, …). |
| `/admin/operations` | POST | start / stop / pause / resume / reset. |
| `/admin/settings` | PATCH | Mutable subset (`_MUTABLE_SETTINGS`). |
| `/admin/reload_inventory` | POST | Re-read `INVENTORY_FILE` and resync `Boxes`/creds/teams without a backend restart. `400` while competition is running (stop first). Returns `{status, num_teams, num_boxes}`. Primarily used by the live test harness to switch which team's boxes a worker's backend knows about (docs/PLAN_live_worker_team_binding.md); also usable by an operator to pick up an edited `config/inventory.yml` live. |
| `/admin/deploy-all-iocs` | POST | One-click deploy + register all IOCs across all matching boxes. `202 {task_id}`. Body: `{include_implants, skip_active, confirm, team_nums}`. RUNNING state requires `confirm=true`. Concurrent-submit guard: `409 another_deploy_all_running`. Progress on parent task `progress_json` keyed by `ioc_name`. |
| `/admin/scoring-cred-preflight` | POST | Validate the `red_scoring` account's **auth + privilege** on every box. `202 {task_id}`. Body: `{team_nums?}` (null = all). Per-box `progress_json[box_ip].details = {reason}` (`pass` / `auth-fail` / `insufficient-perms` / `unreachable` / `absent`); summary `metadata_json {total, passed, failed, by_reason}`. Concurrent-submit guard: `409 another_scoring_preflight_running`. `503` if NXC executor not ready. Orchestration in `core/scoring_preflight.run_scoring_preflight`. |
| `/admin/alerts` | GET | Filterable alert table. |
| `/admin/users` | GET/POST/DELETE | User management. |

## Events / SSE

| Endpoint | Method | Notes |
|---|---|---|
| `/c2/events` | GET (SSE) | Sliver events + alert-created frames. Frontend listens for live invalidation. |

## Wiki

| Endpoint | Method | Notes |
|---|---|---|
| `/wiki/manifest` | GET | Slug list + parsed sidebar tree. |
| `/wiki/page/{slug}` | GET | Raw markdown. |
| `/wiki/assets/{slug}/{filename}` | GET | Static binary serve. |
| `/wiki/index.json` | GET | MiniSearch prebuilt index. |

## Scripts

| Endpoint | Method | Notes |
|---|---|---|
| `/scripts/{hash}` | GET | HMAC-bearer-gated IOC script serve. |
| `/scripts/wrapper/{token_id}` | GET | Single-use dynamic combined-script wrapper (in-memory `WrapperCache`). |
| `/scripts/wrapper/{token_id}/result` | POST | Target box POSTs its check/deploy/remediate result here (result-callback mechanism, `USE_SCRIPT_RESULT_CALLBACK`) — resolves the pending `ResultRegistry` entry the dispatching call is awaiting. |

<!-- audited 2026-06-13 — added /admin/deploy-all-iocs (072e12a). operator-subset extract; full list at https://localhost:3000/docs and in fastapi_backend/routes/*.py. Updated 2026-06-26 — added /admin/scoring-cred-preflight (admin.py:345). Updated 2026-07-17 — added /scripts/wrapper/{token_id}/result (docs/PLAN_script_result_callback.md). -->
