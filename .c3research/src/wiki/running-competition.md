# Running a competition

Admin-only control surface: start, pause, resume, stop, reset. Plus
runtime settings and playbook scheduler interaction.

## When to use

- Open the competition gates at kick-off.
- Pause mid-comp for a fault investigation.
- Reset between rounds in a test environment.
- Tune a runtime setting (concurrency cap, interval) without restart.

For the per-page operator surface, see [Operator Actions](operator-actions).

## Prerequisites

- Logged in as admin (`is_admin=True`).
- Inventory loaded; orchestrator past **Init** state.
- For start: at least one `DeployedIOCs` row or red-team activity to
  generate one — otherwise the scoreboard is permanently zero.
- The `red_scoring` infrastructure account provisioned + reachable on
  every box (see [Scoring-preflight](#scoring-preflight)) — the IOC
  check pipeline **only** authenticates with this account.

## Start-stop

The `/admin` Operations page exposes the lifecycle buttons:

- **START** — transitions orchestrator to **Running**. Hooks:
  - `CheckScheduler` registers the IOC-check job (fires every
    `CHECK_INTERVAL_MINUTES`).
  - `PlaybookScheduler.register_playbook_jobs(competition_start_at)`
    fires playbook entries from `config/playbook.yml` at `T+offset`.
  - `competition_start_at` is persisted to `CompetitionConfig`
    (singleton `id=1`) so playbook jobs survive backend restarts. The
    competition's status itself (Running/Paused) is persisted the same
    way and auto-resumes on the next boot — see
    [Component restart procedure](troubleshooting#component-restart-procedure)
    for details and the alert that signals it happened.
- **STOP** — transitions to **Shutdown**. Cancels playbook tasks
  (`cancel_playbook_jobs()`); leaves DB rows intact for AAR.

## Scoring-preflight

**The IOC check / deploy / remediate pipeline authenticates with one
dedicated account only — `red_scoring` — never operator-added or
harvested creds.** This isolates scoring from blue-team password
rotations and from operator activity: scoring keeps working no matter
what red does to the other credentials.

How it's wired:

- Declared once in `config/inventory.yml` under a top-level
  `scoring_account:` block (`username` default `red_scoring`,
  `domain`, `enabled`).
- Provisioned onto the boxes pre-competition by
  `scripts/infra/create_red_scoring.py` (`--team N …`, `--dry-run`).
- Seeded as one `Credentials` row per box on backend startup
  (`is_infrastructure=true`, `source='red_scoring'`). The routing
  helper resolves these rows via `scoring_only=True` so the pipeline
  can never fall back to an operator cred.

Before kick-off, validate it with **VALIDATE SCORING ACCT** on the
`/admin` Operations page (`data-testid` panel
`scoring-preflight-panel`), which calls:

```
POST /admin/scoring-cred-preflight
{ "team_nums": null }    // null = all teams; [1, 3] = subset
```

Returns `202 {task_id}`. Per box it checks **both** auth and privilege:

- Windows — `whoami /groups` must contain `S-1-5-32-544`
  (BUILTIN\Administrators).
- Linux / pfSense — `id -u` must be `0`.

Per-box result lands on the task `progress_json[box_ip].details.reason`:
`pass` / `auth-fail` / `insufficient-perms` / `unreachable` / `absent`;
a summary lands on `metadata_json {total, passed, failed, by_reason}`.
A completed preflight with any failing box makes **DEPLOY ALL IOCS**
warn-and-confirm before proceeding. Second concurrent submit →
`409 another_scoring_preflight_running`.

Fix every non-`pass` box before START — a box the scoring account
can't auth (or can't reach as admin/root) scores zero all competition.

## Deploy-all-iocs

After clicking START, use **DEPLOY ALL IOCS** to plant and register the
entire IOC catalog in one shot rather than deploying IOCs one by one
from the Deployment page.

```
POST /admin/deploy-all-iocs
{
  "include_implants": true,   // also generate + deploy Sliver implant IOCs
  "skip_active": true,        // skip IOCs already active on a box (idempotent re-run)
  "confirm": false,           // must be true when competition is in Running state
  "team_nums": null           // null = all teams; [1, 3] = specific teams only
}
```

The endpoint returns `202 {task_id}` immediately. Progress is a
`deploy_all` parent `OperatorTask` with per-IOC rows in `progress_json`
(keyed by `ioc_name`); each row carries a `child_task_id` pointing to
the per-box fanout task. Watch via the `DeployAllProgressPanel` in the
admin UI or the Task Queue page.

Inject-type IOCs are always skipped (`reason: inject_ioc`) — those
require live Sliver sessions and must be triggered from the Generation
page.

**High-blast-radius IOCs** (`deploy_all_default=False`) are also skipped
by an unnamed Deploy All run (`reason: deploy_all_opt_out`). These IOCs
must be chosen consciously:
- Name them explicitly: `"ioc_names": ["linux_pam_any_password", "windows_rid_hijack", ...]`
- Or deploy individually from the **Deployment page**

Current opt-out IOCs: `linux_pam_any_password`, `firewall_pam_any_password`,
`windows_rid_hijack`, `windows_adminsdholder`, and all resilient/inject variants
(`linux_self_inject`, `windows_self_inject`, `pfsense_*_inject`, `*_shellcode_registry`,
`*_xattr_shellcode`, `*_elf_section_embed`, `*_pe_section_embed`, `*_extattr_shellcode`).
See `deploy_all_default` column in [IOC Catalog](ioc-catalog).

## Pause-resume

- **PAUSE** — flips orchestrator state to **Paused**. The
  `CheckScheduler` keeps the job registered but skips dispatch
  while paused; the playbook scheduler keeps tasks alive but they
  won't fire actions until resume.
- **RESUME** — flips back to **Running**. Any `T+offset` job whose
  offset has passed during pause fires immediately on resume.

Pause is the right tool for fault investigation (debug a stuck IOC
without dropping check results); reset is the right tool when you
need a clean slate.

## Reset

- **RESET** — destructive. Truncates `IOCCheckResult`,
  `CheckInstance`, `DeployedIOCs`, `OperatorTask`, `Alerts`,
  `PivotSessions`, plus a handful of derived tables. Leaves
  `Users`, `BlueTeams`, `Credentials`, inventory parsing intact.
- After reset: orchestrator returns to **Ready**; click **START** to
  begin a fresh competition.

For Sliver-side cleanup, run `scripts/reset_sliver.sh` from the
host shell. For DB-only reset, `scripts/reset_db.sh`.

## Runtime-settings

`/admin/settings` exposes a PATCH form over `_MUTABLE_SETTINGS`
(`fastapi_backend/routes/admin.py`). Each key in the allowlist can
be changed at runtime without backend restart:

- Check / SA / cred intervals.
- Concurrency caps (`NXC_MAX_CONCURRENT`,
  `BULK_EXEC_MAX_CONCURRENT`, etc.).
- Scoring percentages (`IOC_PARTIAL_SCORE_PCT`,
  `IOC_COMPLETION_BONUS_PCT`).
- Feature toggles (`CRED_HARVEST_ENABLED`,
  `KRBTGT_DCSYNC_ENABLED`).

A PATCH that flips `NXC_MAX_CONCURRENT` or
`BULK_EXEC_MAX_CONCURRENT` also calls
`reset_bulk_exec_semaphores()` so the new cap takes effect on the
next dispatch (existing in-flight tasks finish under the old cap).

Soak runs PATCH this surface at startup via
`runtime_overrides:` in the soak profile; keys must appear in the
allowlist or the soak fails fast.

## Playbook

`config/playbook.yml` (gitignored; `config/playbook.example.yml`
committed as template) defines a series of operator actions to fire
at `T+offset` from `competition_start_at`. Entry shape:

```yaml
- at: "00:05:00"          # T+HH:MM:SS
  action: exec            # exec | deploy | implant | module
  os: windows             # filter: all boxes matching this OS
  command: "whoami"       # for action=exec
```

Loaded once at server start by `core/playbook_loader.py`; scheduled
by `PlaybookScheduler` as a list of `asyncio.create_task(...)`
entries stored in module-level `_playbook_tasks`.

Each fired job:

1. Fans out to all inventory boxes matching the `os` filter.
2. Creates an `OperatorTask` row with `operator='playbook'`.
3. The Task Queue page renders it like any other operator action.

Job count surfaced on `/admin/status` as
`components.playbook.job_count`. `cancel_playbook_jobs()` runs on
stop / reset.

## What you'll see

- Operations panel with state chip (Init / Ready / Running / Paused
  / Shutdown).
- Component status indicators: `CheckScheduler`, `PlaybookScheduler`,
  `Sliver client`, `DatabaseWriter`, `nmap health`, `cred harvester`
  loops.
- Live counters: live sessions, live beacons, current cycle's check
  results.
- Mutable-settings form with one row per `_MUTABLE_SETTINGS` key.

## Troubleshooting

- **START is greyed** — orchestrator still in **Init**; inventory
  hasn't finished parsing OR the DB writer process hasn't started.
  Tail `logs/fastapi_backend_<ts>.log`.
- **PAUSE doesn't seem to take** — paused state only blocks dispatch
  at the scheduler tick boundary; an in-flight cycle finishes first.
  Wait `CHECK_INTERVAL_MINUTES` to confirm.
- **RESET button is hidden** — only admins see it; confirm
  `is_admin=True` on your user.
- **`runtime_overrides` rejected** — soak profile cited a key not in
  `_MUTABLE_SETTINGS`. Add it to the admin.py allowlist AND the
  `bench_soak.py:_MUTABLE_SETTINGS_ALLOWLIST` mirror; the A6 audit
  fence enforces the mirror.
- **Playbook job count stuck at 0** — `config/playbook.yml` missing
  or empty; loader is a no-op when absent. Copy
  `config/playbook.example.yml` → `config/playbook.yml`.

<!-- audited 2026-06-13 — added Deploy-all-iocs section (072e12a). orchestrator state machine, _MUTABLE_SETTINGS surface, and playbook scheduler verified against wiki/architecture.md + wiki/subsystems.md § Playbook scheduler. Updated 2026-06-26 — added Scoring-preflight section + red_scoring prerequisite (admin.py:345 /admin/scoring-cred-preflight, core/scoring_preflight, OperationsPage.tsx scoring-preflight-panel). -->
