# Architecture

Developer-facing map of the system: backend orchestration, frontend structure,
the IOC system, and the alert pipeline. Companion pages:
[Subsystems](subsystems) (NXC, SA, Sliver, cred harvesting, script wrapper),
[DB Schema](db-schema) (table + view detail),
[API Reference](api-reference) (endpoint contracts),
[Frontend](frontend) (page + component inventory), and
[Architecture Diagram](architecture-diagram) (how to regenerate the README map).

## Backend (`/fastapi_backend/`)

**Entry point:** `main.py` — FastAPI app with lifespan that init `CompetitionOrchestrator`.

**Core orchestration** (`core/orchestrator.py`) coordinates competition lifecycle:
1. **Init** → Load inventory (`InventoryManager`), IOC definitions (`IOCDefinitionLoader`), init DB (`DatabaseInitializer`)
2. **Ready** → Await admin start signal
3. **Running** → `CheckScheduler` (APScheduler) triggers IOC checks every 5 minutes
4. **Playbook** → `PlaybookScheduler` fires YAML-defined operator actions (`config/playbook.yml`) at `T+offset` relative to `competition_start_at`; loaded by `core/playbook_loader.py`, scheduled by `core/playbook_scheduler.py`
5. **Paused / Shutdown**

**IOC check execution flow:**
1. `CheckManager._queue_all_checks()` groups all IOC checks by box → one `BoxIOCTask` per box (42 queue items for 42 boxes, not 565)
2. Workers in `netexec/worker.py` execute `BoxIOCTask` items via single combined NXC call per box (using `run_nxc_box_check()` in `check_runner.py`)
3. Combined script uses `###C3R_SECTION:ioc_name###` delimiters; `split_sections()` in `netexec/batch_protocol.py` parses output, routes each section to `_parse_check_output()`
4. Results enqueued to `DatabaseWriter` — separate multiprocessing process that batch-writes to SQLite (100 results or 1s timeout)
5. On full NXC failure, individual `IOCTask` objects added to retry queue, retried via legacy per-IOC path

**Routes** (`routes/`):
- `login.py` — JWT auth (`POST /login`, `GET /me`)
- `scoreboard.py` — Team scores + history (no auth). Box column shows last IP octet (`.XXX`) from `team.box_stats` keys — no separate boxes API call.
- `details.py` — Per-team IOC status (blue teams see own data with obscured IOC names; admin/red team see all). Response includes `scoring: {score_pct, rank}` for blue team callers (rank derived from `blue_teams_scoreboard` ordering); admin callers with `?team_num=N` also get `total_possible_points`.
- `iocs.py` — IOC definitions (blocked from blue team). Each entry exposes `has_deploy_script` (effect *or* persistence has one), more specific `has_effect_deploy_script`, and `is_sliver_check` (true when `IOC.check` is `SliverCheck`). Frontend uses latter two to gate "DEPLOY & REGISTER" button.
- `admin.py` — User management, competition control, runtime settings (admin only). `POST /admin/deploy-all-iocs` (admin only, `202 {task_id}`) deploys + registers the **whole IOC catalog** across all matching boxes via a parent `deploy_all` `OperatorTask` that fans out one child task per IOC (deploy-and-register for ScriptCheck IOCs, generate+deploy for non-inject SliverCheck implant IOCs); body `{include_implants, skip_active, confirm, team_nums}`; RUNNING needs `confirm=true`; second concurrent submit → `409 another_deploy_all_running`. Orchestration in `core/deploy_all.run_deploy_all`; per-IOC progress on the parent's `progress_json` keyed by `ioc_name` (`{child_task_id, deployed, failed}` or `{reason}` for skips). Legacy `POST /admin/deploy_iocs` (no registration) retained. `POST /admin/scoring-cred-preflight` (admin only, `202 {task_id}`, body `{team_nums?}`) validates the `red_scoring` account's auth **and** privilege (Windows `whoami /groups` → `S-1-5-32-544`; Linux/pfSense `id -u` → `0`) on every box; per-box `progress_json[box_ip].details = {reason}` (`pass`/`auth-fail`/`insufficient-perms`/`unreachable`/`absent`) + summary `metadata_json {total, passed, failed, by_reason}`; second concurrent submit → `409 another_scoring_preflight_running`; orchestration in `core/scoring_preflight.run_scoring_preflight`. The OperationsPage "Validate Scoring Acct" button (left of "Deploy All IOCs") drives it; a completed preflight with failing boxes makes "Deploy All IOCs" warn-and-confirm before proceeding. `POST /admin/build-all-iocs` (admin only, `202 {task_id}`, body `{include_implants, force, team_nums}`, `409 another_build_all_running`) is the **BUILD** step (`core/build_all.run_build_all`) — compile every implant (build-only, `deploy_targets=[]`, reuse-recent unless `force`) + coupled shim, per-IOC `progress_json[ioc_name]={reason: built|reused|not-applicable|skipped|failed}`. `POST /admin/build-check` (`409 another_build_check_running`) is the **CHECK** step (`core/build_check.run_build_check`, mirrors scoring-preflight) — per-IOC `{reason: present|missing|stale|not-applicable}` + summary `metadata_json {total, passed, failed, by_reason}`; implant filename is presence-only (not name-matched — SliverCheck), coupled shims verify baked defines == recorded. OperationsPage strip is **BUILD → CHECK → Validate Scoring → DEPLOY**; a completed CHECK with failures makes Deploy-All warn-and-confirm. See `docs/PLAN_build_check_deploy_name_pools.md`.
- `teams.py` — Box/team inventory
- `c2.py` — C2 management: boxes, creds, exec, file upload, SA data, sessions, beacons, implants (WebSocket + SSE streaming). File push via `POST /c2/bulk/put-file/stage` (multipart staging) + `POST /c2/bulk/put-file/async` (async dispatch, 202); strips Windows drive letters (`C:\`, `D:\`) from `remote_path` when `protocol=smb`. `POST /c2/sa/{box_ip}/collect` manually triggers SA collection for single box. `GET /c2/pivot-sessions` returns active `PivotSessions` rows (non-blue users only). WS `/c2/sessions/{id}/shell` is the PTY-backed OS shell (`c2/pty_hub.py`, docs/PLAN_pty_session_shell.md): one Sliver `Shell(EnablePTY=true)` tunnel per session, binary WS frames carry raw stdin/stdout bytes, JSON text frames carry control (`replay_end`, `session_dead`, `client_count`, `pong`, `resize`); `cd`/env/TUIs persist because the target shell child stays alive for the tunnel's lifetime; 60 s keepalive holds Sliver's ~2 min idle timer off. WS `/c2/sessions/{id}/sliver-shell` dispatches typed commands to Sliver gRPC RPCs via `sliver_shell_dispatch.py`. WS `/c2/sliver-console` spawns a per-connection `sliver-client` PTY subprocess; auth-gated (admin + red team only, blue team → close 4003); DEV_MODE sends stub message; resize via JSON control frames `{"type":"resize","cols":N,"rows":M}` → `fcntl.ioctl TIOCSWINSZ`; handler in `c2/sliver_console.py`. `POST /c2/implants/generate` returns `202 {task_id}`; Sliver gRPC compile runs in background via `fire_and_forget(_bg_generate(...))`; poll `GET /c2/tasks/{task_id}` for status and `metadata_json.build_id`. `POST /c2/sessions/{id}/kill` and `POST /c2/beacons/{id}/kill` insert a `PendingKills` row before the gRPC call so `sliver_manager` can distinguish intentional kills from organic disconnects. `POST /c2/bulk/exec/async` returns `202 {task_id}` and queues a background dispatch across `target_boxes` via Sliver session, Sliver beacon, or NetExec. **Concurrent-submit guard is operator-scoped** — same operator double-submit → `409 another_bulk_exec_running`; different operators run in parallel. Missing implants on session/beacon → `409 missing_implants` unless `confirm_missing=true`. Missing creds on nxc → `409 missing_creds` unless `confirm_missing_creds=true`; per-box dispatch records `status='skipped'` for those boxes. Optional `cred_id` pins a specific `Credentials` row across every box (cross-team / cross-box mismatch → 422). Implementation: `fastapi_backend/c2/bulk_exec.py` with module-level semaphores capped by `BULK_EXEC_MAX_CONCURRENT` (default 40, parity with `NXC_MAX_CONCURRENT`). Legacy sync `POST /c2/exec` and `POST /c2/bulk/exec` routes removed in the exec-merge plan (Phase 3); per-task SSE feed at `GET /c2/tasks/{task_id}/stream?token=` and global feed at `GET /c2/tasks/stream?token=` drive the EXEC page progress panel (auth via query-token; EventSource can't set headers).
- `ioc_deploy.py` — IOC deployment registration: `POST /c2/iocs/deploy`, `POST /c2/iocs/deploy-and-register`, `GET /c2/iocs/deployed`, `GET /c2/iocs/deployed/{id}`, `DELETE /c2/iocs/deployed/{id}`. Both `deploy` and `deploy-and-register` accept `box_ips: list[str]` (multi-box) and return `202 {task_id}` immediately; actual work runs in `_bg_deploy_ioc()` via `fire_and_forget()`. Per-box progress written to `OperatorTask` via `update_box_progress()`; `details` field = JSON string with `{deployed_ioc_id, blue_team_label, credentials_registered}`. Check pipeline only scores IOCs with active `DeployedIOCs` row. `assign_blue_team_label()` generates obscured `OS_DIFFICULTY_N` label (e.g. `LINUX_EASY_1`). `deploy-and-register` runs IOC's effect `deploy_script` via NXC and creates row only on `status=1` parse success; rejects `SliverCheck` IOCs with 400 (use Generation page), missing `deploy_script` with 400, missing creds with 400, NXC subprocess failure with 502, unknown IOC with 404. Both deploy endpoints call `_register_added_credentials()` — each `CredentialSpec` in `IOC.added_credentials` becomes unvalidated `Credentials` row with `source_deployed_ioc_id` set; existing rows matching `(box_ip, protocol, username)` left alone. `DELETE` flips status to `failed` AND deletes any `Credentials` rows whose `source_deployed_ioc_id` matches; responses include `credentials_registered`/`credentials_removed` counts.
- `tasks.py` — `GET /c2/tasks/{task_id}` (single task, `progress_json` parsed from JSON string), `GET /c2/tasks` (list with `operator`/`team_num`/`status`/`task_type`/`limit` filters). All roles can read task rows.
- `scripts.py` — Serves IOC scripts to target boxes (`GET /scripts/<hash>`); bearer-token gated via `HMACTokenManager`. Real script paths obfuscated to 16-hex `HMAC_SHA256(secret, path)[:16]` so URL leaks no info; token = HMAC over `(box_ip, path, exp)` validated against requesting client IP. No rotation grace window. Also exposes `GET /scripts/wrapper/<token_id>` (Phase 1) for the unified script wrapper subsystem — serves dynamic combined-body scripts registered by `netexec/script_wrapper.py:build_inline_combined_command` via the in-memory `WrapperCache`. Entries are single-use, IP-bound, TTL-expired; mismatched IP → 403, expired/missing → 404, invalid signature → 401. Token binds to the box's **fetch-source** IP — a firewall's `wan_ip` when inventory sets one (it fetches from its WAN), else `box_ip`; `script_wrapper.build_inline_command`/`build_inline_combined_command` compute `bind_ip = fetch_client_ip or _wan_ip_for(box_ip) or box_ip` (map seeded at inventory load). See `docs/PLAN_firewall_wan_lan.md`. Replaces put_file delivery on Windows (no disk artifact) and inline-SSH delivery on Linux/pfSense (no large body in target process tree). Also exposes `GET /scripts/file/<token_id>` — same `HMACTokenManager`/IP-bind/single-use semantics as `/scripts/wrapper` but serves raw **file bytes** as `application/octet-stream` (token bound to `file/<id>`); used by the WinRM/SSH file-delivery fallback so the box pulls a staged binary via `WebClient.DownloadFile` in one round-trip instead of a base64 push (`WrapperCache` holds `str | bytes`; `docs/PLAN_windows_delivery_fallback.md`).

**Shared NXC routing** (`utils/nxc_routing.py`): `_get_best_cred(box_ip, db, *, protocol=None, prefer_cred_type=None, team_num=None)` — valid creds first, then untested; orders `is_default.desc()` so inventory-seeded baseline beats operator-added rows. Default cred-type ordering: `password > nthash > ccache` (least-friction first). With `prefer_cred_type='ccache'` + `team_num=N`, also considers team-scoped rows (`box_ip IS NULL AND team_num = N`). `resolve_nxc_target(box_ip, protocol, db)` returns `(host, protocol, port)`, routes through pfSense portfwd when active `PivotSessions` row covers box. All operator paths (`routes/c2.py`, `routes/ioc_deploy.py`, `c2/module_runner.py`, `c2/beacon_watcher.py`) **must** pass explicit `protocol=` kwarg matching NXC subprotocol they intend to invoke (`protocol_for_box(os)[0]`) — NXC `--put-file` flag registered only under SMB subparser, so handing WinRM cred to put_file/execute call yields "unrecognized arguments". No duplicate logic in new routes.

**Database** (`database/`) — SQLite at `db/database.db` via SQLAlchemy ORM:
- `Users`, `BlueTeams`, `CheckInstance`, `IOCCheckResult`, `DeployedIOCs`, `OperatorTask`, `PendingKills` tables. `PendingKills` records intentional session/beacon kills (`session_id` PK, `initiated_by`, `initiated_at`) — cleared by `_handle_implant_disconnect()` after consumption.
- `IOCCheckResult` carries both `effect_status` and `persistence_status` (each `-1`/`0`/`1`/`NULL`). `status` = legacy column kept for backward compat. `deployed_ioc_id` FK links each result to its `DeployedIOCs` row.
- `DeployedIOCs` tracks red-team-registered IOCs: `box_ip`, `ioc_name`, `persistence_mechanism`, `blue_team_label`, `deployed_by`, `source` (`'manual'` | `'auto'`), `status` (`active`/`failed`/`lost`). `source='auto'` rows are written by two paths: (1) **Eager** — `_auto_register_deployed_ioc()` called inside `_bg_generate()` immediately after a successful NXC deploy to each box (this is mandatory; `auto_register_ioc_name` required whenever `deploy_targets` is set, else 422); (2) **Backstop** — `sliver_manager._auto_register_implant()` on Sliver session-connect/beacon-registered events (catches cases where eager path missed, e.g. Windows-inject has `implant_identifier=None` until session checks in). The connecting session's `RemoteAddress` is resolved to a box via `sliver_manager._box_for_address(db, ip)`, which matches `box_ip OR wan_ip` so a firewall calling home from its WAN correlates to its LAN-keyed box; identity downstream stays `box_ip`. `status='lost'` set on intentional kill (not organic disconnect). Check pipeline only queues IOCs with `status='active'` row.
- `DeployedIOCs.deploy_params` (JSON string, nullable) records the pool-driven artifact attributes chosen at deploy time — Part 1: `{"DROP_DIR": "<dir>"}` (`docs/PLAN_recorded_deploy_dirs.md`). Accessors: `params_dict` (parse, `{}` on error) / `set_params(dict)` (serialize; `{}`→NULL). Check/remediate read it back and re-inject `C3R_<KEY>` env vars so deploy/check/remediate agree on the location. **Per-location scoring:** an IOC deployed to *two distinct* dirs on a box = **two active rows** = two independent scoring units (each an `IOCCheckResult` keyed by `deployed_ioc_id`; `total_possible_points` already sums per active row — no view change). A partial expression **unique index** `ux_deployed_ioc_active_dir(box_ip, ioc_name, json_extract(deploy_params,'$.DROP_DIR')) WHERE status='active'` forbids two active rows at the *same* dir. The combined check/remediate script frames each row's section with a **row-unique** token `section_token(f"{ioc_name}#{deployed_ioc_id}")` (persistence: `…#id__persistence`) and routes each parsed section back to its row by that token — never by name-splitting. A **RED remediate** success flips that row to `status='failed'`; a **blue clear** surfaces as check `status=0` and leaves the row `active`.
- `OperatorTask` (`core/task_tracker.py`) tracks async operator actions: `id` (autoincrement int PK), `task_type` (`generate`/`generate_deploy`/`generate_pivot`/`deploy`/`deploy_all`/`implant_deploy`/`build_all`/`build_check`/`module`/`exec`/`bulk_exec`/`scheduled`/`scoring_preflight`), `current_phase` (`queued`/`compiling`/`deploying`/`None` — `queued` means the task is waiting on the global Sliver compile mutex, with `metadata_json.queue_position` giving its place in line; also surfaced on `GET /c2/tasks` summary rows as `queue_position` and rendered by the Task Queue page), `operator` (username; `'playbook'` for playbook-fired tasks — visible in Task Queue), `target_boxes` (JSON array of IP strings), `team_num` (nullable int), `status` (`pending`/`running`/`done`/`failed`), `progress_json` (JSON dict keyed by IP — or by `ioc_name` for `deploy_all` parent tasks — → `{status, error, details, last_error, timestamp, attempts, exec_path, duration_s}`), `metadata_json` (task-level: `{build_id, implant_name}` for generate, `{ioc_name}` for deploy, `{module_id, params_used}` for module, `{total_iocs, deployed_iocs, skipped_iocs, failed_iocs, child_task_ids}` for `deploy_all`), `created_at`/`updated_at`/`completed_at`. Helpers: `create_task(...)`, `update_box_progress(task_id, box_ip, status, error=None, details=None, ...)`, `update_task_metadata(task_id, metadata_dict)`.
- `Credentials.source_deployed_ioc_id` (FK to `DeployedIOCs.id`, nullable): set when auto-registered from `IOC.added_credentials`. NULL for inventory-default and operator-added creds. Linked rows deleted when source DeployedIOC deregistered. `GET /c2/creds` augments each row with `source_ioc_name` (joined from `DeployedIOCs.ioc_name`); UI renders "VIA &lt;ioc&gt;" badge — see `react/src/pages/c2/C2CredsPage.tsx` `[data-testid="cred-via-ioc-badge"]`.
- `ImplantBuilds` carries `persistence_mechanism: str | None` — set from `ImplantGenerateRequest.persistence_name` (non-pivot) or `persistence_tier_group` (pivot path). Read by `sliver_manager._auto_register_implant()` when creating the auto `DeployedIOCs` row.
- SQL views handle scoring: `ioc_check_result_with_points` (split scoring: `IOC_PARTIAL_SCORE_PCT`% effect, same persistence, `IOC_COMPLETION_BONUS_PCT`% completion bonus; default 40/40/20 of base 5/10/20/40 pts for Easy/Medium/Hard/Ultra), `check_instance_with_score`, `blue_teams_scoreboard`, `team_latest_ioc_details`, `score_history`. `blue_teams_scoreboard` and `score_history` both carry `total_possible_points` (sum of base pts for active `DeployedIOCs` per team) and `score_pct = ROUND(100.0 * total_score / total_possible_points, 1)`; `score_pct=0.0` when denominator is zero.
- Pydantic models for FastAPI validation live alongside SQLAlchemy models in `models.py`

**Auth** (`utils/auth.py`) — JWT via `python-jose`, passwords hashed with `bcrypt`. Roles: `is_admin`, `is_blue_team` flags on `Users` model; red/white/black team users no flag set.

**Database initialization** (`database/db_init.py`): `DatabaseInitializer.initialize_teams(team_nums: list[int])` and `initialize_accounts(team_nums: list[int])` accept explicit list of team numbers derived from loaded inventory. Both idempotent, prune stale rows for team numbers not in list. Called from `orchestrator._load_inventory()` after inventory parsing; `team_nums` = `sorted({b.team_num for b in all_boxes if b.team_num is not None})`.

## Frontend (`/react/`)

React 19 + TypeScript, Bootstrap 5, React Router 7, Recharts for graphs. Auth state via `AuthContext.tsx`. API calls centralized in `api/api.ts`. Types in `types/types.ts`.

**Shared components** (`react/src/components/`):
- `OsIcon.tsx` — `OsIconSvg` renders Bootstrap OS SVG icon (`windows`, `linux`, `firewall`) from shared `OS_PATHS` constant. Uses `fill="currentColor"` so callers control color via CSS text-color classes. Accepts `os: string | null | undefined`; returns empty placeholder when null. Imported by `C2CredsPage.tsx`, `C2ImplantsListPage.tsx`, `C2GenerationPage.tsx` — no re-define inline.
- `ScoreChart.tsx` / `IOCChart.tsx` — Recharts wrappers used by scoreboard. Accept optional `height?: number` (default 300); passed through to `<ResponsiveContainer>`.
- `SliverConsoleModal.tsx` — xterm.js PTY terminal for the operator Sliver console. Connects to `WS /c2/sliver-console?token=`. Raw PTY passthrough: `term.onData → ws.send(raw)`, `ws.onmessage → term.write(data)`. Amber color scheme (`cursor: '#f59e0b'`). Resize via `term.onResize → ws.send(JSON.stringify({type:'resize',cols,rows}))`. Supports `contained` prop (fills flex parent for split pane) vs default floating (`fixed top-0 right-0 z-50`).

**Scoreboard toggle** (`react/src/components/scoreboard.tsx`): TABLE/GRAPH view stored in `localStorage` key `scoreboard_view` (`'table' | 'graph'`, default `'table'`). Toggle buttons carry `data-testid="toggle-table"` / `toggle-graph`. Graph view expands charts to full-width (`col-span-12`); table view uses 7/12 + 5/12 layout. Score percentage (`score_pct`) is the primary sort key and display value; raw score shown as secondary. `data-testid="score-pct-{team_num}"`, `data-testid="raw-score-{team_num}"`, `data-testid="possible-pts-{team_num}"` (admin only).

**Generation page** (`react/src/pages/c2/C2GenerationPage.tsx`, route `/c2/generation`): Implant generation + library. Auto-deploy targets list has "Select All" header checkbox (with indeterminate state when partially selected) and `N/Total` count badge. GENERATE button returns 202+task_id; `DeployProgressPanel` below the button polls `GET /c2/tasks/{task_id}` and renders per-box results + summary.

**Deployment page** (`react/src/pages/c2/C2DeploymentPage.tsx`, route `/c2/deployment`): IOC deployment registration UI — register via `POST /c2/iocs/deploy` (multi-box, async 202), view active rows, deregister via `DELETE /c2/iocs/deployed/{id}`. "DEPLOY & REGISTER" button (`data-testid="deploy-and-register-button"`) calls `POST /c2/iocs/deploy-and-register` (also async 202) — disabled for SliverCheck IOCs or IOCs without effect deploy script. Both buttons accept `box_ips: list[str]` and surface progress via `DeployProgressPanel` rendered inline below the form, polling `GET /c2/tasks/{task_id}` until done/failed. `DeployProgressModal` is used by the Task Queue / Scheduler / Targets pages (the popped-out variant).

**Task Queue page** (`react/src/pages/c2/C2TaskQueuePage.tsx`, route `/c2/tasks`): Real-time log of all operator actions (generate/generate_deploy/deploy/module/exec/bulk_exec/scheduled). Table columns: ID, Type, Status chip, Operator, Boxes, Started. Polls `GET /c2/tasks` every 5s. Filters: status dropdown, type dropdown, "My Tasks" button. `data-testid="task-queue-table"`, `data-testid="task-row-{id}"`, `data-testid="my-tasks-btn"`. Nav link in `react/src/layouts/C2Layout.tsx`.

**Targets page** (`react/src/pages/c2/C2ImplantsListPage.tsx`, route `/c2/targets`): Merged live-implant + SA view grouping sessions, beacons, and situational awareness by box IP. Each box row = collapsible group with session count, beacon count, OS icon, team number, PIVOT badge when pivot session active. Expanding a box reveals two collapsible sub-sections: **IMPLANTS** (open by default) showing sortable/filterable implant table with per-implant action buttons, and **SITUATIONAL AWARENESS** (closed by default) showing a full SA tab bar (users, processes, ports, history, auth_log, etc.) lazily fetched on first expand and polled every 60s. Group-level action buttons (exec/upload/download/shell/kill) auto-select best implant: pivot session > regular session > beacon, most-recently-checked-in first. "Run SA Now" button inline per SA tab, right-justified with CollectedAt timestamp — calls `POST /c2/sa/{box_ip}/collect`. Offline boxes render at `opacity-40` with `data-offline="1"`. Filters: search by IP/name, OS (`ALL OS / WINDOWS / LINUX / FIREWALL`), `PIVOT ONLY`, team selector (`data-testid="team-filter"`), implant-type toggle `ALL / SESS / BCN / NONE` (`data-testid="implant-filter-{value}"`).

**Network map pivot highlighting** (`react/src/components/NetworkMapView.tsx`): Connection lines between boxes and their firewall highlighted purple when that specific box has active pivot session. `pivotBoxIPs: Set<string>` computed in `C2OverviewPage` from two sources: DB-stored `PivotSessions` rows (via `GET /c2/pivot-sessions`) and live Sliver sessions where `remote_address` contains `"->"` (TCP pivot indicator). Covers both WireGuard/pfSense pivots and Sliver TCP pivots.

**Console page** (`react/src/pages/c2/C2ConsolePage.tsx`, route `/c2/console`): Operator Sliver console launch page. "OPEN CONSOLE" button (`data-testid="open-console-btn"`) opens a `SliverConsoleModal` via `TerminalContext.openTerminal()` with fixed `sessionId: 'sliver-console'` and `shellType: 'console'`. Button label reflects state: OPEN CONSOLE / RESTORE CONSOLE (minimized) / CONSOLE ACTIVE (open). Badge `data-testid="console-active-badge"` visible when console tab is active.

**Terminal tab system** (`react/src/layouts/C2Layout.tsx`): Persistent tab bar (`fixed bottom-0 z-[60]`) survives cross-page navigation. Each open terminal = one tab. Tab icons: `terminal` (OS shell), `webhook` (Sliver shell), `terminal_settings` (Sliver console). Tab labels: `{box_ip}` (OS), `[S] {box_ip}` (Sliver), `[C] Console`. `shellType: 'os' | 'sliver' | 'console'` in `TerminalContext`.

**Terminal split pane** (`react/src/layouts/C2Layout.tsx`, `TerminalContext.tsx`): Two terminals can be assigned to left/right panes via `[L]`/`[R]` tab bar buttons (appear when 2+ terminals open). Clicking `[L]`/`[R]` enters assign mode; next tab click assigns that terminal to the pane. Once both panes assigned, vertical split activates. Horizontal split toggle places terminal area and page `<Outlet>` as top/bottom halves; swap button inverts position. Both splits can be active simultaneously. Resize via drag handles (`data-testid="split-handle-vertical"` / `"split-handle-horizontal"`). Ratios persisted to `sessionStorage`. `SessionShellModal`, `SliverShellModal`, and `SliverConsoleModal` all accept a `contained` prop — when true, fill their flex parent instead of using `fixed` positioning.

**Terminal modal layout** (`SessionShellModal.tsx`, `SliverShellModal.tsx`, `SliverConsoleModal.tsx`): Floating (non-contained) mode uses `fixed inset-0 z-50` (not `top-0 right-0 left-0 bottom-10` — breaks full-screen layout). Terminal container uses `pb-14` (OS shell) or `pb-10` (Sliver shell/console) bottom padding so xterm FitAddon sizes terminal above fixed bottom tab bar.

## IOC System (`/iocs/`)

**Definition format** (Python modules in `definitions/`). Each file instantiates `IOC` dataclass from `iocs/base.py`:
```python
from iocs.base import IOC, ScriptCheck, PersistenceMechanism

my_ioc = IOC(
    name="my_ioc",
    description="...",
    category="persistence",          # remote_access | persistence | users | exfiltration | impair_defenses
    difficulty=2,                    # 1=Easy 2=Medium 3=Hard 4=Ultra → 5/10/20/40 base pts
    os="linux",                      # windows | linux | firewall
    check=ScriptCheck(script="check_scripts/linux/my_ioc_check.sh"),
    deploy_script="deploy_scripts/linux/my_ioc_deploy.sh",    # optional
    remediate_script="remediate_scripts/linux/my_ioc_remediate.sh",  # optional
    payloads=["payload.bin"],        # optional; files from iocs/payloads/
    persistence=PersistenceMechanism(  # optional
        name="cron",
        tier=2,
        retrigger_interval_seconds=60,
        check_script="check_scripts/linux/my_ioc_persist_check.sh",
        deploy_script="deploy_scripts/linux/my_ioc_persist_deploy.sh",
        remediate_script="remediate_scripts/linux/my_ioc_persist_remediate.sh",
    ),
)
```
`IOCDefinitionLoader` in `core/ioc_definition.py` imports all `*.py` modules from `iocs/definitions/` and collects top-level `IOC` instances. IOCs with both `deploy_script` and `remediate_script` = "soak-eligible".

**Check script output** (JSON printed to stdout):
```json
{"ip": "<IP>", "os": "windows|linux", "ioc_name": "<NAME>", "status": -1|0|1, "error": "<MSG>"}
```
`status`: `1` = IOC present (compromised), `0` = remediated (blue team fixed), `-1` = check failed/error.

`_parse_check_output()` searches for `"status"` anywhere in JSON line (not just as first key) so `ConvertTo-Json` hashtable key ordering on Windows no matter. Windows service missing entirely (IOC never deployed) → catch `"Cannot find any service with service name"` exception and return `status=0` — not `status=-1`.

**Inventory** (`config/` at runtime) — `InventoryManager` parses `inventory.yml` into in-memory `Box`/`Team` objects. `NXCWrapper` (`netexec/wrapper.py`) handles all remote exec via `nxc` subprocesses; SMB for Windows, SSH for Linux.

## Alert System (`/fastapi_backend/c2/alerts.py`)

Real-time alert system for operational events: session losses, credential failures, box unreachable, etc. Provides DB persistence + SSE broadcast + toast notifications.

**Alert producers**:
- Session/beacon disconnect (Sliver event handlers in `sliver_manager.py`)
- Credential validation failures (`orchestrator._revalidate_all_creds`)
- Beacon stale detection (`beacon_watcher.py`)
- Redeploy failures (`beacon_watcher.py`)
- Box unreachable via NXC failures (`nxc_failure_tracker.py`) or nmap ping sweep (`nmap_health.py`)
- Pivot session drops (`sliver_manager.py` teardown)

**Database tables**:
- `alerts`: event storage with `id`, `created_at`, `severity` (info/warn/critical), `kind`, `box_ip`, `team_num`, `summary`, `details_json`
- `alert_reads`: per-user read state tracking `(alert_id, user_id, read_at)`

**SSE event**: `alert-created` frame on `/c2/events` with alert payload. Frontend listens and shows severity-gated toasts (critical/warn only, not info).

**Terminal presence SSE event**: `terminal_presence_update` frame on `/c2/events`, payload `{box_ip, connections: [{connection_id, operator, session_type, session_id, connected_at}]}`. Fires on every WebSocket OS-shell (`/c2/sessions/{id}/shell`) and Sliver-shell (`/c2/sessions/{id}/sliver-shell`) attach and detach. Registry in `fastapi_backend/c2/terminal_presence.py` (in-memory, cleared on backend restart). Backend `c2_events` generator filters this event type out per-subscriber when `current_user.is_blue_team`; the sibling REST snapshot `GET /c2/terminal-presence` is `require_non_blue`-gated. Powers the Targets page's TERMS column (docs/PLAN_targets_terminal_presence.md).

**Helper**: `emit_alert(kind, summary, box_ip=None, team_num=None, details=None)` — inserts DB row via `asyncio.to_thread()` and publishes SSE. Severity auto-mapped per kind.

**Lifecycle**: startup truncates both tables for fresh session. Background sweep purges alerts older than 24h every 10 minutes.

**Frontend**: `AlertButton` (fixed bottom-right) + `AlertHistoryPanel` (slide-in) + detail modal. Hooks: `useAlerts`, `useUnreadCount`, `useMarkRead`. Toast via `sonner`.

**NXC failure tracking**: in-memory `dict[box_ip, deque[timestamp]]` tracks per-box failure history. Emits `box_unreachable` after `NXC_DEGRADED_THRESHOLD` consecutive failures within `NXC_DEGRADED_WINDOW_MINUTES`.

**nmap health monitoring**: periodic ping sweep of all inventory boxes every `NMAP_HEALTH_INTERVAL_MINUTES`. Emits `box_unreachable` when hosts stop responding.

**SA access-loss early-warning**: each SA cycle transition-tracks five blue-team access-cutoff actions (fail2ban ban, firewall C2-port block, SELinux enforcing, `sshd_config` restriction, `authorized_keys` removal) and emits `warn` alerts (`fail2ban_block`, `firewall_c2_block`, `selinux_enforcing`, `sshd_config_restricted`, `auth_keys_removed`) ahead of the coarse `box_unreachable`/`session_lost` signals. See `wiki/subsystems.md` § SA collection and `docs/PLAN_sa_access_loss_gaps.md`.

<!-- audited 2026-07-30 — moved verbatim from docs/ARCHITECTURE.md (git mv, content unchanged); added the intro cross-link block. Was auto-loaded into CLAUDE.md via @docs/ARCHITECTURE.md; now referenced, not imported. -->
