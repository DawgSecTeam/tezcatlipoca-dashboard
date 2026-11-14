# Testing

How the suites are wired and what a change owes in tests. Commands for running
them live in `CLAUDE.md` § Commands; the subsystems under test are described in
[Subsystems](subsystems) and [Architecture](architecture).

## Test-update policy (HIGH PRIORITY)

**When you add or modify any feature, you MUST update tests in the same change.** This is non-negotiable; an untested feature is a feature that will silently regress. Treat test updates as a P0 part of the feature, not as cleanup-later work.

What "update tests" means in practice depends on what changed:

| Code change | Required test updates |
|---|---|
| New FastAPI route (`fastapi_backend/routes/*.py`) | Pytest unit tests for happy-path + each error branch (auth gating, validation 4xx, server-error 5xx). Add the file to `tests/run_tests.py:TEST_FILES`. If the route has a UI surface, add a Playwright spec exercising it. |
| Modified existing route (signature change, new param, status-code change, response shape) | Update existing pytest tests + Playwright specs that touch the route. Run `grep -r "<route_path>" tests/ react/tests/` to find every caller. |
| New / modified DB model or SQL view | Add migration test + a query-returns-expected-shape unit test. If the view feeds the scoreboard, add a Playwright assertion on the rendered cell. |
| New IOC (`iocs/definitions/*.py`) | Add to the IOC unit-test fixture (`tests/test_ioc_definitions.py`). Soak-eligible IOCs (deploy + remediate scripts) get picked up by the soak's deploy-cycle automatically. |
| New React page or component | Add a Playwright spec — at minimum a navigation smoke test (`/path` loads, key data-testids visible, no console errors). Tag `@live` if it requires a live backend. Every actionable element needs a `data-testid="..."` attribute. |
| New WebSocket / SSE endpoint | Pytest unit test for connection + first-frame; Playwright spec using `page.on('websocket')` to capture frames (xterm uses WebGL canvas — can't read terminal output via DOM). |
| Refactor / rename | Run `grep -rn "<old_symbol>" tests/ react/tests/` and update every reference. Pytest collection (`run_tests.sh --pytest`) catches missed renames. |
| New runtime config (`.env` key, `core/config.py` field) | Update `_MUTABLE_SETTINGS` allowlist in `routes/admin.py` if runtime-mutable. Update soak's `_MUTABLE_SETTINGS_ALLOWLIST` mirror in `tests/perf/bench_soak.py`. Add a smoke test exercising the new value. |

### Workflow checklist before declaring a change complete

1. **Pytest unit tests added / updated.** `scripts/testing/run_tests.sh --pytest --fast` to verify the suite passes.
2. **Playwright spec added / updated** for any UI surface. `cd react && npm run test:e2e` (add `PLAYWRIGHT_LIVE=1` if the spec is `@live`).
3. **New `test_*.py` files** added to `tests/run_tests.py:TEST_FILES`. The audit script's A4 check fails on drift.
4. **Every new actionable UI element** has a `data-testid="..."` attribute.
5. **If the change touches** IOC scoring, deploy/remediate cycles, scheduler, or C2 surface, run `scripts/testing/run_soak.sh --profile smoke` (~10 min) to verify the soak still passes.

A PR / commit that adds significant feature code with no test diff is a defect. Lint/format failures are caught loudly by `ruff check` + `npm run lint` — apply the same bar to "did this introduce or modify a test?"

---

## Suite coverage

Both suites are fully built out. Pytest covers every backend endpoint; Playwright
covers every frontend page (`cd react && npm run test:e2e`), including the `@live`
matrix specs that drive real VMs.

- **Pytest** — `scripts/testing/run_tests.sh --pytest`, or `python tests/run_tests.py`
  directly. Collection is driven by the explicit `tests/run_tests.py:TEST_FILES`
  allowlist; the `run_audit.sh` A4 fence fails on drift between it and the on-disk
  `tests/test_*.py` glob.
- **Playwright** — ~70 spec files under `react/tests/`, `@live` ones gated behind
  `PLAYWRIGHT_LIVE=1` (default-on in the full suite; `--no-live` opts out).

> Snapshot as of 2026-06-03: pytest 2376 passed / 2 skipped / 0 failures across
> ~124 allowlist entries; Playwright 428 passed / 7 skipped / 1 pre-existing flake.
> Counts drift with every feature — treat them as an order-of-magnitude reference,
> not a gate.

## Recent infrastructure additions

- **`INVENTORY_FILE` retargeting.** `scripts/testing/run_tests.sh` — and the
  live conftest — honor `INVENTORY_FILE`. Point it at a non-default range
  (`INVENTORY_FILE=config/testing_inventory.yml scripts/testing/run_tests.sh …`)
  and the whole harness follows: the per-worker inventory subsets, the live-team
  probe, and the live test's box derivation all read box layout + creds
  role-based from that file (no more `192.168.1.{base}` formula). Live cache
  files are per-inventory (`tests/.live_teams_cache_<stem>.json`) so a
  testing-range run never eats a dev-range cache.
- **Live e2e staging authenticates as `red_scoring`, not the inventory login user.**
  Tests that *plant* implant artifacts (put_file into root-only paths, overwrite
  root-owned/immutable files, run privileged deploy scripts) resolve their cred
  via `tests/scoring_creds.py::scoring_login(box)` — the box's `red_scoring`
  identity (uid-0 / local-admin), mirroring the production deploy path
  (`scoring_only=True`). This means (a) the range must have `red_scoring`
  provisioned (`scripts/infra/create_red_scoring.py`) before live tests, and
  (b) an inventory's interactive login user no longer needs to be root/admin for
  staging — so a range like `testing_inventory.yml` (login user `blackteam`,
  uid 1000) works. `scoring_login` is proven to agree with the backend source of
  truth (`orchestrator._scoring_rows_for_box`) by `tests/test_scoring_login_helper.py`.
  Tests whose *subject* is a credential (`test_c2_creds`, `test_cred_harvester_live`,
  `tests/perf/*`) intentionally keep the box login cred. See
  `docs/PLAN_test_staging_red_scoring.md`.
- **`C2_IP` / `SLIVER_HOST` / `SCRIPT_SERVER_URL` come from the inventory.**
  `run_tests.sh` reads `c2_settings:` from the active inventory and exports
  those three values, so switching `INVENTORY_FILE` carries the correct
  call-home address with it (implants baked in tests actually reach this host).
  Precedence: `TEST_C2_IP` env override → inventory `c2_settings` → host
  default-route auto-derive. Guard test: `tests/test_run_tests_c2_settings.sh`.
- **`pytest-rerunfailures`** is enabled globally via `pytest.ini` (`--reruns 1 --reruns-delay 5`). Failed tests retry once after a 5s delay; only failures that recur on retry are reported as actual failures. Backstop for live-VM jitter.
- **Timestamped per-run logs** — `scripts/testing/run_tests.sh` writes `logs/pytest_<ts>.log` and `logs/resource_usage_<ts>.tsv` per invocation. Old runs survive across re-invocations (was previously overwritten on each call).
- **Implant cleanup at startup** — `scripts/testing/run_tests.sh` invokes `scripts/clean_stale_implants.sh` before pytest spawns. Default 1-day threshold. Cleans `./implants/<uuid>/`, C3rberus DB `implant_builds`, `~/.sliver/sliver.db`, `/root/.sliver/{sliver.db,builds,slivers}/` (latter via passwordless sudo).

  In full-purge mode (`--days 0`, used by `reset_sliver.sh`) it additionally sweeps the
  live boxes, in this order — **the order is load-bearing**:

  1. **IOC remediate sweep** (`fastapi_backend/c2/remediate_sweep.py`) — runs *every*
     IOC's remediate script on each box, disarming persistence.
  2. **Implant scrub** (`fastapi_backend/c2/implant_scrub.py`) — kills implant
     processes and deletes binaries.
  3. **Config purge** — DB + Sliver `implant_builds` / `implant_configs`.

  Remediation must precede the kill: a kill cannot converge while the mechanism that
  re-launches the implant is still installed. `linux_implant_inject_sshd` leaves an
  `LD_PRELOAD` drop-in at `/etc/systemd/system/ssh.service.d/security-module.conf`
  whose shim forks a memfd child on **every sshd connection**, so each NXC login the
  cleanup itself made minted a fresh orphan. Measured 2026-08-09: the fleet held at
  137 rejected handshakes per 45 s ("unknown implant signature key") and no amount of
  killing converged; after the remediate sweep landed, 0.

  Timeouts are env-tunable: `C3R_SCRUB_TIMEOUT` (default 90 s — the scrub polls for a
  live C2 connection, since a beacon's connection is too brief for a single snapshot)
  and `C3R_REMEDIATE_TIMEOUT` (default 180 s).

  The linux sweep is piped to **bash**, not `sh` — most linux remediate scripts are
  `#!/bin/bash` and mis-parse under dash *without erroring* (`command -v x &>/dev/null`
  becomes a background job that always succeeds). pfSense/FreeBSD stays `sh`.
- **`--no-clean-env` / `SKIP_CLEAN_ENV=1`** — skip the pre-test `clean_env.sh` sweep (Sliver reset + implant scrub + IOC remediate against the live range). Use when the target range was just snapshotted / freshly prepared and you don't want the sweep to touch it. Default is **on** — most runs want the clean sweep. Pairs naturally with `--no-build-check` / `SKIP_BUILD_CHECK=1` (skip the pre-test artifact CHECK + auto Build-All).
- **`tests/run_tests.py:TEST_FILES` allowlist** — explicit list of test files run by `run_tests.sh`. New `tests/test_*.py` files MUST be added to the list or they won't be collected by the suite. The audit script's A4 check (`scripts/audit/run_audit.sh`) flags drift between the allowlist and the on-disk glob.
- **`scripts/testing/run_tests.sh --coverage`** — opt-in CI-only flag. Runs `pytest --cov=fastapi_backend --cov=iocs --cov=scripts --cov-report=term-missing --cov-report=xml --cov-fail-under=90`. Off by default. Defer enabling in CI until Phase 6 backfill reaches 90% (currently ~83%; see `audit/PHASE6_PROGRESS.md`). Same 90% threshold as `scripts/audit/run_audit.sh --coverage`.
- **Codebase audit script** — `scripts/audit/run_audit.sh` (modes: `--quick`, default, `--strict`, `--coverage`, `--update-progress`, `--check`) replaces the manual Phase 0 baseline run. Default mode adds A1-A8 regression fences (flake stats, db-writer bypass scan, inline imports, test_files drift, persistence_coverage, mutable_settings_drift, nxc_wrapper_bypass, timestamp_predicates); `--strict` adds S1-S5 concision scans (vulture@80, noqa leak, ruff C901, jscpd@30, ruff PLR091X).
- **Playwright @live default-on** (`scripts/testing/run_tests.sh`) — full suite now exercises live-VM browser-driven tests by default. `--no-live` opts out for environments without live VMs / Sliver. `--live` (kept for clarity) sets both `PLAYWRIGHT_LIVE=1` and `PLAYWRIGHT_LIVE_DISRUPTIVE=1`. Live tests are tagged `@live` in their titles; the gate is in `react/playwright.config.ts` (`grep` filter + `PLAYWRIGHT_LIVE` env var).
- **Playwright parallelism tuned** — default 12 workers for mocked tests, 4 workers for `@live` tests (live tests bottleneck on backend NXC + Sliver gRPC, not local CPU; >4 races on shared state). Override via `PLAYWRIGHT_WORKERS=N` (universal) or `PLAYWRIGHT_LIVE_WORKERS=N` (live-only). Precedence: `PLAYWRIGHT_WORKERS` > `PLAYWRIGHT_LIVE_WORKERS` (only when `PLAYWRIGHT_LIVE=1`) > defaults. Bench notes inline in `react/playwright.config.ts`.
- **`@live` matrix driver** — `react/tests/live/_matrix.ts` builds M2 (implant generation) + M3b (IOC deploy) cells. A precompute step (`npx tsx tests/live/_build_matrix_cache.ts`) writes `react/tests/live/.matrix-cache.json` before Playwright discovers cells; spec files load it synchronously via `loadMatrixSync()`. Run via `scripts/testing/run_tests.sh --matrix=smoke` (default cell count, ~10-15 min) or `--matrix=full` (pruned cartesian, ~2-4 h, also sets `PLAYWRIGHT_LIVE_WORKERS=2`). Wrapper `scripts/testing/run_live_matrix.sh` runs the full matrix with the JSON reporter + a per-axis pass/fail summary TSV.
- **`ensure_live_session()` liveness probe** — `tests/live_session_helper.py` `_find_existing` now skips `is_dead=True` rows and probes session responsiveness via a lightweight Sliver gRPC call before returning. `linux_session` / `windows_session` / `freebsd_session` fixtures in `test_c2_shell_live.py` are **function-scoped** (was module-scoped) so a stale Sliver session doesn't cascade across 32 downstream tests.
- **PTY session-shell tests** — `tests/test_pty_session_shell.py` (unit, no backend) covers `PtyHub` lifecycle (start / add_client / history replay / 64 KB ring buffer / keepalive / shutdown / session_dead broadcast / effectively-dead guard); `tests/test_pty_session_shell_live.py` (live) DoD-maps the plan's acceptance bar (cd/env persist across bursts per OS, vim alt-screen bytes, multi-operator broadcast, late-join replay, 180 s keepalive). The 180 s keepalive test carries `@pytest.mark.slow` so `--fast` skips it; the soak profile picks it up. See docs/PLAN_pty_session_shell.md.
- **`tests/run_tests.py:TEST_FILES` allowlist** has grown organically with feature work (inject + bulk_exec + timestomp + wrapper + matrix specs added since 2026-05-09). `run_audit.sh` A4 fence flags any drift between allowlist and on-disk `tests/test_*.py` glob.
- **Port-collision pkill fix** — `scripts/testing/run_tests.sh` cleanup regex was matching the dev backend on port 3000, killing it after pytest runs and breaking Playwright's globalSetup. Fixed to match only `--port 30(0[1-9]|1[0-9]|2[0-9]|3[0-2])\b` so pytest test backends (3001-3032) get cleaned without touching 3000.
- **Orphan/child cleanup in `run_tests.sh`** — the `EXIT INT TERM` trap now also sweeps the script's **descendant tree** (`_kill_descendants`), and a watchdog aborts the run if its launcher dies.
  - **Why:** an aborted run used to leave `clean_env.sh`, per-worker uvicorn backends and in-flight `nxc` calls against competition boxes running with no owner and no log. On 2026-08-06 a killed launcher orphaned a run twice; the untracked NXC traffic dragged the range from 50/50 to 47/50.
  - **Not process groups.** `kill -- -$$` is the usual idiom and is wrong here: the script is normally *not* a process-group leader, so signalling the group also kills the shell that launched it. (Proven the hard way — a `pkill -f run_tests.sh` matched the launching shell's own command line and killed it.) The sweep walks the tree from our own PID instead, so it can only reach processes we spawned.
  - **Watchdog** (`C3R_NO_ORPHAN_WATCHDOG=1` to disable): the trap only fires when *this* script is signalled. When the parent is killed the script is reparented to init and keeps running headless. The watchdog polls for `PPID == 1` and tears the tree down **itself** rather than just signalling — bash defers a trap handler until the running foreground command returns, so a `kill -TERM $$` during an hour-long pytest would not be acted on until pytest finished. It only arms when the run did *not* start detached (`nohup`/`setsid` legitimately give PPID 1).
  - **Known limit:** a bare `kill -TERM <script-pid>` while a foreground command is running is still deferred by bash until that command returns — a pre-existing property, not introduced here. Ctrl+C (signals the whole foreground group) and launcher-death (watchdog) are both handled promptly.
- **Serialized worker-backend boot (`C3R_BACKEND_SPAWN_CONCURRENCY`)** — xdist starts every worker at once, so each worker's uvicorn backend used to boot simultaneously: measured 2026-08-05, 15 → 29 backends in 9s, host CPU 25% → 99%, RSS 11.8 → 20.9 GB. Backends caught in that spike exceed their readiness budget, and because `backend_process` is **session-scoped and pytest caches fixture exceptions**, a single timeout ERRORs every later test on that worker — one slow boot on gw17 produced 48 ERRORs in `test_ioc_deploy_unit.py`, all of which pass in isolation.
  - `tests/conftest.py:_backend_spawn_gate` holds an `flock` across the spawn **and** its readiness wait, so only N backends are *booting* at once (default `1` = strictly one at a time). `flock` rather than the `O_EXCL` slot files `_acquire_sliver_slot` uses: the kernel drops an flock when the holder dies, so a crashed worker can't leave a stale lock that wedges everyone. If no slot frees within `C3R_BACKEND_SPAWN_LOCK_TIMEOUT` (120s) it boots **ungated with a warning** rather than deadlocking.
  - Cost is small: a backend boots in ~2s (0.5s import + 0.8s app init), so ~30 workers serialize in ~60s of ramp-up. Raise `C3R_BACKEND_SPAWN_CONCURRENCY` to trade herd-flatness for a shorter ramp.
  - Timeouts now **retry** (`C3R_BACKEND_SPAWN_ATTEMPTS`, default 3) instead of being terminal, and the failure message reports the real budget plus the tail of the worker's log. The old message hardcoded "30 seconds" for what was actually a 60s wait — worth knowing when reading historical logs.
  - Budgets are sized to stay under `pytest.ini`'s `--timeout=900`: `3 × (120 gate + 60 readiness) + 2 × 10 retry delay = 560s`.
- **`backend_count` in `logs/resource_usage_*.tsv` was double-counting** — each uvicorn backend forks a `DatabaseWriter` (`multiprocessing.Process`, Linux default start method `fork`), and a forked child inherits the parent's `/proc/<pid>/cmdline` verbatim, so `pgrep -c -f 'uvicorn .*main:app'` matched both. The column read 58 during the boot herd when only 29 backends existed (one per xdist worker, as designed). Now counts only processes whose PPID is not itself a match. **Historical TSVs are inflated ~2× in that column.**
- **Per-run box-assignment rotation (`C3R_BOX_SEED`)** — box-scoped live tests pick their target box by hashing the test's own nodeid (`tests/conftest.py:_box_for_item`), and full-team files pick a team by hashing the file stem (`_team_for_full_team_item`). Both hashes are now salted with `C3R_BOX_SEED`, which `run_tests.sh` exports fresh each run (defaults to `RUN_TS`) and echoes at startup.
  - **Why:** an unsalted hash freezes the mapping forever, so one box draws the same IOCs on every run for all time. That is not theoretical — `linux_disable_firewall` resolved to `192.168.1.248` on every single run, so only that box ever exercised the IOC. It accumulated `ufw-*` chain residue no other linux box had, and the IOC's remediate bricked it three consecutive runs while the bug stayed invisible fleet-wide.
  - **Invariants the seed must not break:** the value is read from the environment, never generated inside `conftest.py` — every xdist worker collects independently, so a per-worker value would make workers disagree about box assignment and reintroduce the two-workers-one-box collisions box-scoped grouping exists to prevent. It is also stable within a session, so `pytest-rerunfailures` reruns hit the same box. The seed shifts *which* team a force-live file lands on, never the one-team-per-file property.
  - **Reproducing a run:** re-export the seed the run printed — `C3R_BOX_SEED=20260805_190322 scripts/testing/run_tests.sh` — to replay that run's exact box layout. Leaving it unset falls back to the historical fixed mapping. Covered by `tests/test_conftest_grouping.py` (rotation, in-run stability, replay, candidate-set containment, one-team-per-file).
- **Server concurrency improvements** — `POST /c2/implants/generate` is now mutex-serialised via `_serialized_sliver_compile()` (single + pivot paths) — concurrent generates from multiple operators queue at the FastAPI layer rather than racing inside Sliver gRPC. Response carries `queue_position_was` (body) + `X-Generate-Queue-Position` (header). `POST /c2/sa/{ip}/collect` now coalesces concurrent triggers per `box_ip` via `_sa_in_flight: dict[str, asyncio.Future]` — second operator awaits the first's NXC run rather than spawning a duplicate.
- **Scoreboard test isolation (2026-05-09)** — `tests/scoreboard.spec.ts` toggle/persist tests previously shared `adminCtx` (which inherits `.auth/admin.json` storage state).  Parallel workers from other specs hitting `/` polluted the shared localStorage and flaked the GRAPH/TABLE toggle tests ~40% of the time despite `mode: 'serial'`.  Fixed by replacing `adminCtx` with a `freshAdminPage(browser)` helper that creates a brand-new browser context per test (still seeded from the auth state file but isolated cookies + localStorage).  Net flake rate on those tests dropped to ~0%.
- **Live test timeout headroom (2026-05-09)** — `c2_live_vm.spec.ts` M1 + M3b raised deploy POST timeout 60s → 120s and poll budgets 30s → 60s / 15s → 30s.  Backend NXC subprocesses + Sliver compile latency spike when full pytest suite is hammering shared backend in parallel.
- **Soak test run profiles (2026-05-10)** — `scripts/testing/run_soak.sh --profile smoke|validation|full` runs the soak harness against committed profile YAMLs in `tests/perf/soak_profiles/`. Profile YAMLs hold the soak config (no backend creds); operator fills `tests/perf/soak_backend.yml` (gitignored) once. Wrapper merges them into an ephemeral `tests/perf/soak_config.yml`, backed up + restored on exit.
  - `smoke` (~10 min) — per-PR sanity check; cadences compressed via `runtime_overrides` (`PATCH /admin/settings`); skips Phase 1 implant lifecycle + Phase 6 fault injection. Default operator after touching anything except trivial UI.
  - `validation` (~45 min) — implant lifecycle (with reuse via `prebuild_implants_from_recent`), 3 compressed fault windows, score-correctness assertion. Operator runs after touching `routes/c2.py`, `c2/sliver_manager.py`, `core/scheduler.py`, `core/orchestrator.py`, IOC pipeline.
  - `full` (4 h) — pre-prod / pre-competition validation. All phases at full duration. RSS-drift leak detection.

  Each run emits `tests/perf/results/soak_<ts>.json`. View summary with `python tests/perf/soak_report.py`. Threshold defaults that fire even with empty `thresholds:` block: `max_playwright_error_rate=0.05`, per-action latency p95 ceilings (nav 5s / exec 30s / terminal 15s / sa 20s), `max_scheduled_task_failure_rate=0.05`, `min_pivot_success_rate=0.95`, `max_concurrent_race_failures_per_type=1`, `max_sliver_disconnects_per_hour=10`, `max_mtls_signature_rejections_per_hour=5000`, `max_smb_netbios_timeouts_per_hour=200`, `require_score_movement=true`, `max_host_ram_used_mb` (per-profile). Disable individually by setting the key to `null` in the profile's `thresholds:` block. `max_smb_netbios_timeouts_per_hour` auto-skips (reports `-1`) unless `NXC_AUDIT_LOG` is set — `run_soak.sh` sets it automatically.

  Soak phases (gated by `enable_*` flags per profile): **Phase 0** SliverImplantCleanup teardown safety net; **Phase 1** generate-or-reuse 4 implants (Win/Linux beacon/session) and deploy to all matching-OS boxes; **Phase 1.55** protect-path immutability check (Linux `lsattr`, pfSense `schg`, Windows ACL Deny-Write + ReadOnly probe per `docs/PLAN_soak_full_coverage.md` §6.9); **Phase 1.6** beacon task lifecycle (queue `whoami` per beacon, measure round-trip via `/c2/beacons/{id}/tasks`); **Phase 2** spawn `node node_modules/@playwright/test/cli.js test --grep @soak --workers=N` subprocess running N operator action loops with weighted action mix (nav 30 / scoreboard 8 / terminal 10 / exec 8 / SA 8 / scheduler 4 / sliver-shell 4 / deploy_register 6 / bulk_exec 5 / timestomp_deploy 3 / deploy_iocs_selective 4 / run_checks 3 / sa_per_category 3), per-action latency p50/p95/p99, exponential backoff on 5xx, BrowserContext recycle every `playwright_actions_per_context` actions; **Phase 2.5** file upload/download round-trip on N live sessions; **Phase 3** pre-populate ~50 scheduled tasks and poll `/c2/scheduled` for status transitions; **Phase 4** pivot session TCP-probe stability tracking; **Phase 5** concurrent-op race bursts (sa_collision via `_sa_in_flight` map; cred_revalidation_collision via PATCH); **Phase 6** scheduled fault injection (session_kill, admin_pause_resume) with recovery timing; **Phase 7** cross-stack anomaly detection via `/c2/events` SSE + scoreboard movement + WAL size sampling + SMB NETBIOS-timeout counts parsed from the NXC audit log (`NXC_AUDIT_LOG`, set by `run_soak.sh`; splits reachable auth-path stalls — Server 2025 SMB auth rate limiter / workstation→DC pass-through — from true box wedges, gated by `max_smb_netbios_timeouts_per_hour`) + mTLS signature-rejection flood; **Phase 7.5** Sliver-side SA freshness validation across every live-implant box; **PhaseFullCoverage** per-IOC positive+negative+stay-gone verify (docs/PLAN_soak_full_coverage.md §6.5 — full profile enumerates every eligible IOC, validation rotates ~15/run); **PhaseAlertEmission** trigger `session_lost`/`beacon_lost` and confirm alert lands on `/c2/alerts`; **PhaseDefaultCred** mutate/restore default-cred round-trip on `soak_default_cred_boxes` test boxes; **PhaseScoreCorrectness** rotates the subset-delta IOC across runs via `tests/perf/results/pool_rotation_state.json`. New gate keys: `enable_full_coverage`, `enable_alert_emission`, `enable_default_cred`, `score_correctness_full_matrix`, `stay_gone_cycles`, `full_coverage_iocs_per_run`, `expected_alert_kinds`, `soak_default_cred_boxes`. New threshold keys: `max_full_coverage_misses`, `max_alert_emission_misses`, `max_default_cred_misses`.

## Box functionality smoke (phase 8)

Every soak profile (and a dedicated live pytest spec) runs a canonical per-OS
command set on every competition box before + after each deploy cycle. A
command that PASSED pre-deploy and FAILED post-deploy is a **regression** and
fails the phase — zero tolerance. Motivation: an IOC (or a badly-remediated
one) can wedge basic userspace on a box — `ls` losing to a `GLIBC_2.34 not
found (required by /usr/lib/libnss_sss.so)` after an NSS-touching change, a
Windows box where AppCertDlls corruption keeps PowerShell from loading the
CLR, a pfSense box where residual `LD_PRELOAD` entries survive remediate.
Blue teams cannot compete on a wedged VM.

- **Fixture module** — `tests/box_health_commands.py` is the single source of
  truth. `LINUX_COMMANDS` (~19), `PFSENSE_COMMANDS` (~15) and
  `WINDOWS_COMMANDS` (~18) each carry a `HealthCmd` with a decisive-stdout
  regex and an optional decisive-stderr-bad regex — the second catches
  cases like the glibc error where `rc == 0` but stderr carries the fatal
  line. `applies_when` gates a command on a boolean-shaped precondition
  (`sssd_joined`, `systemd`) so a non-domain-joined linux box does not
  count `getent group domain-admins` as a regression.
- **Live pytest spec** — `tests/test_e2e_box_health.py` (marked `@live`,
  gated on `PLAYWRIGHT_LIVE=1`). Snapshots every box in the bound team's
  inventory, dispatches `POST /admin/deploy-all-iocs`, re-snapshots after
  the parent task drains, asserts zero regressions.
- **Soak Phase 8** — `Phase8BoxHealth` in `tests/perf/bench_soak.py`.
  Pre-snapshot runs inline BEFORE the deploy-remediate loop starts so
  baseline damage is captured as baseline, not attributed to cycle 1.
  A background thread re-snapshots every `phase8_repeat_interval_s`
  seconds. Threshold: `max_box_health_regressions: 0` (in every profile).
- **Disable per profile** — set `enable_box_health: false` in the profile's
  YAML.
- **Widen the command set** — edit `tests/box_health_commands.py`; both the
  live spec and Phase 8 pick up the change automatically. Every new
  `HealthCmd` MUST have a unique `key` (`tests/test_box_health_commands.py`
  enforces this).
- **Root-cause diagnostic** — `scripts/diagnostic/repro_glibc_break.sh`
  bisects the specific `GLIBC_[0-9.]+ not found` failure class end-to-end
  on one live linux box (revert → snapshot → solo-deploy each Linux IOC →
  probe → identify the poison IOC). Do not run while any pytest / soak
  session is in flight — it mutates live-box state via the operator
  backend.

## Backend perf under sustained load

`_ssl.c:1016: The handshake operation timed out` in a soak log = the uvicorn accept loop is wedged. Root cause is a callback that blocks the asyncio event loop for hundreds of ms → new TLS handshakes queue past their client deadline.

**Fast reproducer** — `scripts/diagnostic/backend_stress_probe.py --pattern mixed --duration 120 --workers 4` (5–10 min, no full soak). Exits non-zero on any handshake timeout or a per-op failure rate above 1%. Use it as the gate for individual backend fixes.

**Stall detector** — start the backend with `C3R_LOOP_HEARTBEAT=1`. `fastapi_backend/lifespan_helpers.start_loop_heartbeat` posts a monotonic timestamp on the loop every 100ms; a daemon thread compares against wall-clock and logs `WARNING event-loop stall: <ms> — loop-thread stack: <sys._current_frames sample>` whenever the loop is starved past `stall_threshold_ms` (default 500). Every stall trace pins a specific offending callback — the fix is usually `asyncio.to_thread`.

**`get_status` per-block profile** — `C3R_STATUS_PROFILE=1` logs `WARNING get_status profile: total=…s {state:…, preflight:…, playbook:…, schedule:…, metrics:…}` and the metrics inner block `collect_metrics profile: total=…s {check_pipeline:…, coverage:…, resource:…, per_team:…}` whenever a call takes longer than 1s. Use it to attribute a slow `/admin/status` to a specific sub-block rather than guessing.

**Baked-in fixes (docs/PLAN_soak_backend_perf.md §3.3)** — kept documented so a regression is easy to spot:
- NXC subprocess spawn runs on a worker thread (`_spawn_nxc_sync` + `asyncio.to_thread` in `fastapi_backend/netexec/wrapper.py`). `asyncio.create_subprocess_exec` on the loop thread makes `logging._afterFork` (weakref sweep over every handler) block the loop 1–2 s per spawn under our RSS.
- Default asyncio executor pool bumped to 64 workers (`fastapi_backend/main.py`; env `C3R_ASYNCIO_EXECUTOR_MAX_WORKERS`) so `to_thread` off-loads don't queue behind the default 10–16.
- SQLAlchemy pool bumped to 32 + 32 overflow (`fastapi_backend/database/db_writer.py`) so concurrent `/admin/status` + `/c2/iocs/deployed` readers don't stall on the default 5-conn pool.
- `_per_team_block` folded from N+1 (10 teams × 3 queries) to 3 aggregate queries (`fastapi_backend/core/metrics.py`). The `blue_teams_scoreboard` view's correlated subqueries were the dominant cost.
- uvicorn spawned with `--backlog 4096 --limit-concurrency 500 --timeout-keep-alive 15` and `ulimit -n 8192` (`scripts/run.sh`, `scripts/testing/run_tests.sh`).

## Known environmental flakes (NOT code regressions)

A 5-run sweep of `scripts/testing/run_tests.sh` on 2026-05-09 measured per-test flake rates.  These are surfaced here so contributors don't chase them as bugs.

| Test | Flake rate | Cause | Mitigation |
|---|---|---|---|
| `test_sliver_ioc_e2e::test_linux_implant_obvious_systemd_lifecycle@live_team_7` | 4/5 (80%) | Sliver C2 callback intermittent on team-8 routing (192.168.1.238).  VM state itself is clean. | `pytest-rerunfailures` (1 retry) covers most. |
| `test_sliver_ioc_e2e::test_linux_implant_systemd_retrigger@live_team_6` | ~1/5 | Same callback-timing class, different team. | Same. |
| `test_e2e_pivot::test_internal_sessions_still_alive` | ~2/5 | Pivot session keepalive timing under load. | Same. |
| `c2_live_vm M1 / M2 smoke / M3b smoke` | ~80% first-attempt | Backend NXC + Sliver compile contention when pytest-live and Playwright-@live run in the same `run_tests.sh` invocation. | Playwright `retries: 2` covers; isolated runs (`scripts/testing/run_tests.sh --playwright`) have ~0% flake. |
| Scoreboard toggle tests (pre-fix) | ~40% | `adminCtx` shared localStorage. | **Fixed** via `freshAdminPage` per-test isolation. |

**Reset hook between pytest and Playwright phases** — `scripts/testing/run_tests.sh` fires a between-phase reset against the Playwright backend (port 3000) after pytest_end and before playwright_start:

1. `POST /c2/sessions/kill-all` — drops every live Sliver session, inserts PendingKills so `_handle_implant_disconnect()` flips auto-registered DeployedIOCs to `status='lost'`.
2. `POST /c2/beacons/kill-all` — same for beacons.
3. `POST /c2/iocs/deregister-all` — flips every `active` DeployedIOCs row to `failed` and cascade-deletes linked Credentials rows.

Skip the hook with `scripts/testing/run_tests.sh --no-reset` when Playwright is run in isolation (`--playwright`) or to reproduce the pre-hook flake pattern.

<!-- audited 2026-07-30 — created from docs/TESTING.md lines 1-86 (test-update policy, infrastructure additions, known environmental flakes, between-phase reset hook), verbatim. The rest of that file — "Phase 1 COMPLETE" / "Phase 2 Future" build-out tracker, ~840 lines — was dropped as historical: it described an app state since superseded (Phase 2 lists Playwright as unbuilt). Recoverable from git history at the pre-move commit. -->
