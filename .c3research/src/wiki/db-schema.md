# DB schema

SQLite at `db/database.db`. ORM in
`fastapi_backend/database/models.py`; init / migrations in
`fastapi_backend/database/db_init.py`. This page covers the
operator-relevant tables + views — for the full set, read
`models.py` directly.

## When to use

- Debugging scoring discrepancies — trace from `IOCCheckResult` back
  through `DeployedIOCs` to the IOC definition.
- Building an external dashboard reading from `db/database.db` while
  the backend is running (the DB is WAL-mode so reads don't block
  writers).
- Authoring a new IOC and want to know where the row lives.

## Tables (operator-relevant)

### `users`

| Column | Type | Notes |
|---|---|---|
| `user_id` | int PK | |
| `username` | str | |
| `password` | str | bcrypt hashed |
| `is_blue_team` | bool | |
| `is_admin` | bool | |
| `blue_team_num` | int FK | → `blue_teams.team_num`, nullable |

Default accounts seeded by `db_init.initialize_accounts(team_nums)`.
Stale rows pruned when inventory changes.

### `blue_teams`

| Column | Type | Notes |
|---|---|---|
| `team_num` | int PK | |
| `last_check_id` | int FK | → `check_instance.check_id`, nullable |

### `check_instance`

| Column | Type | Notes |
|---|---|---|
| `check_id` | int PK | |
| `blue_team_num` | int FK | → `blue_teams.team_num` |
| `timestamp` | datetime | |
| `score` | int | Cached sum of `points` for fast scoreboard reads. |
| `preflight` | bool | `true` during preflight window — excluded from scoring. |

### `ioc_check_result`

| Column | Type | Notes |
|---|---|---|
| `ioc_id` | int PK | |
| `check_instance_id` | int FK | → `check_instance.check_id` |
| `box_ip` | str | |
| `ioc_name` | str | |
| `difficulty` | int | 1/2/3/4 (4 = Ultra). |
| `status` | int | Legacy column — `-1`/`0`/`1`. |
| `effect_status` | int | `-1`/`0`/`1`/`NULL`. |
| `persistence_status` | int | `-1`/`0`/`1`/`NULL`. |
| `deployed_ioc_id` | int FK | → `deployed_iocs.id` |
| `raw_output` | text | For debugging. |

### `deployed_iocs`

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `box_ip` | str | |
| `ioc_name` | str | |
| `persistence_mechanism` | str | |
| `blue_team_label` | str | Obscured (`OS_DIFFICULTY_N`) shown to blue team. |
| `deployed_by` | str | Operator username or `playbook`. |
| `source` | str | `'manual'` / `'auto'` (Sliver auto-register) / `'virtual'` (auto-seeded default-cred scoring IOC — protected; can't be deregistered). |
| `status` | str | `'active'` / `'failed'` / `'lost'` / `'deploy_failed'`. |
| `deploy_error` | str / NULL | Why the deploy failed — rendered verbatim in the deploy page's redeploy section. |
| `deploy_task_id` | int / NULL | `operator_tasks.task_id` that attempted it; deep-links to per-box progress. |
| `deploy_attempts` | int | Attempts spent by the retry wrapper (`DEPLOY_MAX_ATTEMPTS`, default 3). |

The check pipeline **only** queues IOCs with `status='active'`.

**`deploy_failed` vs `failed`** — these mean opposite things and are deliberately
distinct (`docs/PLAN_failed_deploy_redeploy.md`):

| status | meaning | redeployable? |
|---|---|---|
| `failed` | RED remediated it. It **was** deployed. | no |
| `lost` | implant/beacon went away. It **was** deployed. | no |
| `deploy_failed` | it **never fully landed** | yes — under its original pooled names |

`deploy_failed` is **unscoreable**: only `status='active'` rows count toward the
scoreboard denominator, and `check_instance_with_score` additionally excludes
their check results from the numerator, so both sides agree. Without that, a
persistence mechanism that never deployed reads to the check pipeline exactly
like one blue remediated and pays partial points (measured: 16 of 40 on an Ultra
IOC) for an artifact red never planted.

> Note: `failed` and `lost` still have the older numerator/denominator asymmetry
> (their stale check results remain in the numerator while the denominator drops
> them). That is pre-existing and intentionally untouched — excluding them would
> retroactively cut points a blue team already earned.

### `credentials`

| Column | Type | Notes |
|---|---|---|
| `cred_id` | int PK | |
| `box_ip` | str / NULL | NULL for team-scoped (Kerberos ticket) rows. |
| `team_num` | int / NULL | Set for team-scoped rows. |
| `protocol` | str | `smb` / `ssh`. |
| `cred_type` | str | `password` / `nthash` / `ccache`. |
| `username` | str | |
| `password` | str / NULL | password type only. |
| `nthash` | str / NULL | nthash type only. |
| `ccache_blob` | blob / NULL | ccache type only (base64 in some flows). |
| `ticket_expires_at` | datetime / NULL | ccache only. |
| `domain` | str / NULL | Windows domain. |
| `valid` | bool | Set by cred-revalidation loop. |
| `is_default` | bool | Seeded from inventory. |
| `is_infrastructure` | bool | `True` for the dedicated `red_scoring` scoring account — the **only** cred the IOC check/deploy/remediate pipeline authenticates with (resolved via `scoring_only=True` in `utils/nxc_routing.py`). |
| `source` | str | `inventory_default` / `manual` / `sam_dump` / `lsa_dump` / `dcsync` / `forged` / `red_scoring` / `virtual`. |
| `source_deployed_ioc_id` | int FK | → `deployed_iocs.id`, nullable. Auto-registered creds cascade-delete on deregister. |

### `boxes`

| Column | Type | Notes |
|---|---|---|
| `box_ip` | str PK | |
| `name` | str | |
| `os` | str | `windows` / `linux` / `firewall`. |
| `team_num` | int | |
| `is_domain_joined` | bool | |
| `is_domain_controller` | bool | |
| `domain` | str / NULL | |
| `domain_sid` | str / NULL | Set after first DCSync per team. |
| `last_implant_id` | str / NULL | FK → `implant_builds.build_id`. |

### `implant_builds`

| Column | Type | Notes |
|---|---|---|
| `build_id` | str PK | Sliver-assigned UUID. |
| `name` | str | Operator label. |
| `c2_url` | str | `mtls://host:port`, `tcp://...`, etc. |
| `protocol` | str | `mtls` / `http` / `https` / `dns` / `wg` / `pivot`. |
| `pivot_gateway` | bool | True for pfSense pivot implants. |
| `tcp_pivot_port` | int / NULL | Per-team pivot port. |
| `persistence_mechanism` | str / NULL | Copied into `DeployedIOCs.persistence_mechanism` on auto-register. |
| `immutable_scope` | str | `binary_only` / `binary_and_persistence` / NULL. |

### `operator_task`

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `task_type` | str | `generate` / `generate_deploy` / `deploy` / `deploy_all` / `module` / `exec` / `bulk_exec` / `harvest` / `scheduled` / `scoring_preflight`. |
| `current_phase` | str / NULL | `compiling` / `deploying` for generate; else NULL. |
| `operator` | str | Username; `playbook` for playbook-fired. |
| `target_boxes` | json | List of IP strings. |
| `team_num` | int / NULL | |
| `status` | str | `pending` / `running` / `done` / `failed`. |
| `progress_json` | json | `{ip: {status, error, details, last_error, timestamp, attempts, exec_path, duration_s}}`. |
| `metadata_json` | json | Task-level (`{build_id, implant_name}` / `{ioc_name}` / `{module_id, params_used}`). |
| `created_at`, `updated_at`, `completed_at` | datetime | |

### `pivot_sessions`

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `team_num` | int | |
| `pfsense_ip` | str | |
| `tcp_pivot_port` | int | |
| `session_id` | str | Sliver session that owns the pivot. |
| `created_at` | datetime | |

### `pending_kills`

| Column | Type | Notes |
|---|---|---|
| `session_id` | str PK | Sliver session/beacon ID. |
| `initiated_by` | str | Operator username. |
| `initiated_at` | datetime | |

Inserted before the gRPC kill so the disconnect handler can
distinguish intentional kills from organic loss. Consumed by
`_handle_implant_disconnect()`.

### `alerts` + `alert_reads`

`alerts`: `id`, `created_at`, `severity` (info/warn/critical),
`kind`, `box_ip`, `team_num`, `summary`, `details_json`.
`alert_reads`: `(alert_id, user_id, read_at)` for per-user unread
tracking.

Truncated at startup. Sweep purges rows older than 24h every 10 min.

## SQL views

Scoring math lives in views so the scoreboard endpoint reads one row
per team.

| View | Notes |
|---|---|
| `ioc_check_result_with_points` | Per-result points after split scoring (`IOC_PARTIAL_SCORE_PCT`% effect / same persistence / `IOC_COMPLETION_BONUS_PCT`% completion bonus). |
| `check_instance_with_score` | `check_instance` × points sum. |
| `blue_teams_scoreboard` | One row per team: `team_num`, `total_score`, `total_possible_points`, `score_pct`. |
| `team_latest_ioc_details` | Per-team latest IOC status grid. |
| `score_history` | Historical scores per `check_instance` per team. |

## DB writer

Most writes go through `Session(engine)` directly. The scoring hot
path (`IOCCheckResult` + SA rows) goes through `DatabaseWriter`, a
separate multiprocessing process that batches 100 results or 1s
timeout — bounds SQLite WAL write-lock contention during the 5-min
check cycle.

Routes return the inserted PK on most endpoints, so the queue is
NOT used for general CRUD — only for fire-and-forget high-frequency
writes from the check + SA hot paths.

## When NOT to use the queue

| Scenario | Why queue is wrong |
|---|---|
| HTTP route returning the created/updated object | Caller needs auto-generated PK; queue is one-way. |
| Write that must precede its consumer | Queue delivery is asynchronous. |
| Low-frequency admin / CRUD | No batching benefit. |
| Startup / lifecycle | Sequential order required. |
| Alert inserts needing `alert.id` for SSE broadcast | ID must be known synchronously. |

Rule of thumb: response channel / synchronous ack → session.
High-frequency fire-and-forget scoring data → queue.

<!-- audited 2026-06-06 — table columns extracted from models.py + db_init.py; views per wiki/architecture.md § Database. Updated 2026-06-26 — difficulty 1/2/3/4 (Ultra), deployed_iocs.source +virtual, credentials.source +dcsync/forged/red_scoring/virtual, is_infrastructure live (red_scoring), operator_task.task_type +deploy_all/+scoring_preflight. -->
