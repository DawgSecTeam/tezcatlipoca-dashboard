# Configuration

All runtime config comes from a `.env` file at the repo root, parsed by
`fastapi_backend/core/config.py` — that module is the authoritative list.
This page covers the knobs an operator actually turns; `core/config.py`
declares more (test-harness and internal values not repeated here).

## Runtime-mutable vs restart-only

Most values are read once at import and need a backend restart. A subset can be
changed live by an admin via `GET`/`PATCH /admin/settings`
(`routes/admin.py:_MUTABLE_SETTINGS`) — patching anything outside that
allowlist returns `422`. Currently live-patchable:

`CHECK_INTERVAL_MINUTES`, `SA_COLLECTION_INTERVAL_MINUTES`,
`SA_RESULT_RETENTION_COUNT`, `SA_MAX_CONCURRENT_BOXES`, `DEPLOY_MAX_ATTEMPTS`,
`NXC_MAX_CONCURRENT`,
`NXC_MAX_CONCURRENT_PER_BOX`, `NXC_PUTFILE_MAX_CONCURRENT_PER_BOX`,
`NXC_PUTFILE_MAX_RETRIES`, `NXC_PUTFILE_RETRY_BACKOFF_SEC`,
`NXC_PUTFILE_VERIFY_HASH`, `NXC_ALT_DELIVERY_ENABLED`, `NXC_WINRM_CHUNK_SIZE`,
`NXC_WINRM_PORT`, `NXC_WINRM_ALLOW_PTH`, `NXC_SUPPRESS_READBACK`,
`NXC_SWEEP_ON_SA`, `BULK_EXEC_MAX_CONCURRENT`,
`BEACON_REDEPLOY_THRESHOLD_MULTIPLIER`, `BEACON_REDEPLOY_CHECK_INTERVAL_MINUTES`,
`CRED_REVALIDATION_INTERVAL_MINUTES`, `CRED_REVALIDATION_MAX_CONCURRENT`,
`SCORING_CRED_PROBE_MAX_ATTEMPTS`, `SCORING_CRED_PROBE_RETRY_DELAY_SEC`,
`CRED_HARVEST_ENABLED`, `HASH_HARVEST_INTERVAL_MINUTES`,
`TICKET_REFRESH_INTERVAL_MINUTES`, `HASH_HARVEST_MAX_CONCURRENT`,
`TICKET_TTL_HOURS`, `TICKET_REFRESH_BEFORE_EXPIRY_MIN`,
`SSH_KERBEROS_TIMEOUT`, `GETST_TIMEOUT`, `TICKET_LINUX_USER_ID`,
`KRBTGT_DCSYNC_ENABLED`, `USE_SCRIPT_WRAPPER`, `SCRIPT_WRAPPER_TTL_SEC`,
`SCRIPT_WRAPPER_MAX_CACHE_ENTRIES`, `SCRIPT_WRAPPER_SWEEP_INTERVAL_SEC`,
`SCRIPT_RESULT_GRACE_SEC`, `USE_SCRIPT_RESULT_CALLBACK`.

Soak profiles patch these through the same endpoint via their
`runtime_overrides:` block and restore them on teardown.

## Core and scheduling

| Key | Default | Notes |
|---|---|---|
| `DATABASE_URL` | — | SQLite path for `db/database.db`. |
| `NUM_WORKERS` | 16 | IOC check worker count. |
| `WORKER_TIMEOUT` | — | Per-worker task timeout. |
| `CHECK_INTERVAL_MINUTES` | 5 | IOC check cycle cadence. |
| `USE_QUEUE_DB_WRITER`, `DB_BATCH_SIZE`, `DB_BATCH_TIMEOUT` | — | `DatabaseWriter` batching. |
| `JWT_SECRET_KEY`, `CORS_ORIGINS` | — | Auth + browser origin allowlist. |

## NXC subprocess

| Key | Default | Notes |
|---|---|---|
| `DEPLOY_MAX_ATTEMPTS` | 3 | Whole-IOC deploy attempts before the row is marked `deploy_failed` (unscoreable) and one `redeploy_failed` alert fires. Retries re-inject the row's RECORDED pooled names — never re-picked, or artifacts the failed attempt already planted are orphaned. `docs/PLAN_failed_deploy_redeploy.md`. |
| `NXC_PATH`, `NXC_TIMEOUT` | — | Binary path + per-call timeout. |
| `NXC_MAX_CONCURRENT` | 40 | Caps simultaneous `nxc` subprocesses. |
| `NXC_MAX_CONCURRENT_PER_BOX` | 5 | Per-box cap; avoids SSH/SMB `MaxStartups` throttling. |
| `NXC_BATCH_IOC_SCRIPT_SIZE_LIMIT` | 30000 | Max combined IOC script size before falling back to per-IOC execution. Raise for large IOC sets on SSH targets. |
| `NXC_DEGRADED_THRESHOLD` | 3 | Consecutive NXC failures before a `box_unreachable` alert. |
| `NXC_DEGRADED_WINDOW_MINUTES` | 10 | Window for that failure tracking. |

**`put_file` serialization** — `NXC_PUTFILE_MAX_CONCURRENT_PER_BOX` (1),
`NXC_PUTFILE_MAX_RETRIES` (5), `NXC_PUTFILE_RETRY_BACKOFF_SEC` (2),
`NXC_PUTFILE_VERIFY_HASH` (true). Per-box dedicated semaphore acquired
**outermost** (order: putfile → per-box → global, so it can't deadlock), plus
transient-only retry and a remote SHA256 verify. Fixes concurrent large-write
NETBIOS timeouts.

**Exec/auth retry** — `NXC_EXEC_MAX_RETRIES` (5),
`NXC_EXEC_RETRY_BACKOFF_SEC` (2). Bounded transient-NETBIOS retry for
**exec/auth only** (`NXCWrapper._run` → `_run_once`); `put_file` has its own
retry above. Backoff is exponential per attempt (`backoff * 2**attempt`) —
at defaults: 2s, 4s, 8s, 16s, 32s (62s worst-case total). Each attempt
independently re-acquires the per-box semaphore and the backoff sleep runs
with the slot released — so a retry never adds concurrent load to the box.
`0` disables.

**File-delivery fallback** — `NXC_ALT_DELIVERY_ENABLED` (`true`),
`NXC_WINRM_CHUNK_SIZE` (1500), `NXC_WINRM_PORT` (5985),
`NXC_WINRM_ALLOW_PTH` (on). When an SMB `put_file` to a Windows box exhausts
its retries with a transient NETBIOS-class failure, the *file push* escalates
SMB → WinRM → SSH. WinRM accepts a plaintext password or an nthash via
pass-the-hash (the only viable tier for an nthash-only box, since SSH can't
NTLM-PtH); SSH is tried only when TCP/22 is open per nmap SA. With
`SCRIPT_SERVER_URL` set, WinRM delivery is **server-pull** — the bytes are
staged in the wrapper cache and the box `WebClient.DownloadFile`s them in one
`run_ps` from `GET /scripts/file/<token_id>`; base64-chunked push is only the
no-server fallback (impractical for multi-MB implants). SMB stays the default
first tier and command `execute` is untouched. Delivering tier lands on
`OperatorTask.progress_json[ip].exec_path`. `false` → SMB-only. Operator-facing
symptoms: [Troubleshooting § SMB put_file NETBIOS timeouts](troubleshooting#smb-put-file-netbios-timeouts-windows-file-delivery).

**Readback suppression** — `NXC_SUPPRESS_READBACK` (`true`) kills smbexec's
Windows `C:\` output-readback artifact. smbexec redirects each command's stdout
to `\\HOST\C$\<6 random letters>`, reads it back and deletes it — but leaks the
file (and its IOC-check/SA content) on read/delete races. When on, IOC-scoring
and SA Windows execs run `--no-output` and take output from the result callback
instead; on a callback miss they retry with readback, then scrub the leftover
via `NXCWrapper.cleanup_smbexec_artifacts`. Operator/callback-less paths (exec,
bulk, module, timestomp, beacon redeploy, implant persistence, playbook) keep
readback. Linux/pfSense are unaffected (SSH carries stdout over the channel, no
artifact). `NXC_SWEEP_ON_SA` (`true`) fires that same cleanup once per Windows
box per SA cycle, self-healing leftovers that predate the suppression.

## Network health

| Key | Default | Notes |
|---|---|---|
| `NMAP_HEALTH_INTERVAL_MINUTES` | 5 | Ping-sweep frequency. |
| `NMAP_PATH` | `nmap` | Path to the nmap binary. |

## SA collection and cred revalidation

| Key | Default | Notes |
|---|---|---|
| `SA_COLLECTION_INTERVAL_MINUTES` | 5 | SA loop cadence. |
| `SA_MAX_CONCURRENT_BOXES` | — | Parallel SA boxes per cycle. |
| `SA_RESULT_RETENTION_COUNT` | — | SA rows kept per box. |
| `CRED_REVALIDATION_INTERVAL_MINUTES` | 5 | Matches `CHECK_INTERVAL_MINUTES` so a freshly-added cred (e.g. IOC auto-registered) shows valid/invalid within one check cycle. |
| `CRED_REVALIDATION_MAX_CONCURRENT` | 8 | Deliberately far below `NXC_MAX_CONCURRENT`: each `nxc` subprocess costs ~150–200 MB, so 40 concurrent saturates host RAM and stalls the asyncio loop for minutes. |

## Script delivery

- **`SCRIPT_SERVER_URL`** — the URL target boxes use to reach the C3rberus
  server (e.g. `https://192.168.1.x:3000`). When set, individual IOC scripts
  inside the combined wrapper are fetched at runtime via
  `WebClient.DownloadString` / `curl` with an HMAC-signed bearer token instead
  of being base64-embedded. With `USE_SCRIPT_WRAPPER` on, the **combined body
  itself** is also served from the in-memory wrapper cache
  (`/scripts/wrapper/<token>`) — no `put_file` disk artifact on Windows, no
  inline-SSH process-tree leak on Linux. Required for the no-disk-artifact
  invariant.
- **`SCRIPT_HMAC_SECRET`** — shared secret `HMACTokenManager`
  (`core/script_server.py`) signs per-(cycle, box, path) bearer tokens with.
  Must be identical across every backend process behind the same
  `SCRIPT_SERVER_URL`. Generate with
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`. It also keys
  `batch_protocol.section_token()`, which obfuscates the per-IOC
  `###C3R_SECTION:<name>###` markers so a raw IOC name never appears in a
  script body (SA-collection category markers stay raw).
- **`ALLOW_DISK_SCRIPT_FALLBACK`** (`false`) — gates the legacy Windows
  disk-drop delivery (a combined `.ps1` put to `C:\Windows\Temp`) used when the
  wrapper is unavailable (cache full, `USE_SCRIPT_WRAPPER` off, or
  `SCRIPT_SERVER_URL` unset). Default `false` means that box's IOCs **fail
  loud** rather than leak a script to disk. Set `true` only in dev/CI. The
  Linux inline-SSH fallback writes nothing to disk and is never gated.
- **`USE_SCRIPT_RESULT_CALLBACK`** (`true`) — kill switch for the result
  callback: boxes POST their check/deploy/remediate output to
  `POST /scripts/wrapper/<token_id>/result` rather than the caller trusting
  NXC's own readback, which has no completion check on the remote process and
  can read an empty or partial redirect file as success (proven live on
  Windows — the deploy itself always worked, only NXC's report of it was lost).
  Falls back to NXC's output unchanged when off, unscoped, or timed out.
- **`SCRIPT_RESULT_GRACE_SEC`** (`5`) — how long a caller awaits that POSTed
  result after its NXC call returns. Intentionally short, not a fresh timeout:
  the dispatching NXC call has already burned real wall-clock time internally,
  so the POST has almost always landed by the time the wait starts.
- **`USE_SCRIPT_WRAPPER`**, `SCRIPT_WRAPPER_TTL_SEC`,
  `SCRIPT_WRAPPER_MAX_CACHE_ENTRIES` (1024),
  `SCRIPT_WRAPPER_SWEEP_INTERVAL_SEC` (30) — wrapper cache lifecycle.

## Sliver and pivot

| Key | Default | Notes |
|---|---|---|
| `SLIVER_ENABLED` | `false` | Master switch. |
| `SLIVER_BIN` | `sliver-server` | Server binary. |
| `SLIVER_CLIENT_BIN` | `sliver-client` | Operator client used by the `WS /c2/sliver-console` PTY endpoint. |
| `SLIVER_CFG_PATH` | `./sliver_operator.cfg` | Operator config. The gRPC host/port the backend dials come from **this file**, not from `.env`. |
| `SLIVER_HOST` | `localhost` | Sliver server host. |
| `MTLS_LISTENER_PORT` | 8888 | The mTLS listener implants call home to. Backend verifies it via `SliverClient.jobs()` at startup and starts one if missing. Implants must be generated with `c2_url=mtls://<host>:<MTLS_LISTENER_PORT>`; soak's `sliver_c2_url` must match. |
| `STAGE_LISTENER_PORT` | 8890 | TCP stage-listener serving the second stage to the small stager `windows_shellcode_registry` plants in the registry. Distinct from `MTLS_LISTENER_PORT`: implants carrying a full beacon call home on mTLS, stagers fetch here first. Startup-only — not runtime-mutable, since rebinding a listener needs a restart. |
| `C2_IP` | derived | Call-home address baked into `c2_url`; takes precedence over `SLIVER_HOST`. Must be the interface the **target boxes** route back on. Unset → derived from the host default route, which is wrong on a multi-homed host (implants deploy but never check in) — set it explicitly. `run_tests.sh` overrides it with the host default-route IP for the dev test env, so `.env` can hold the live/competition value. |
| `PIVOT_LISTENER_PORT` | 9898 | Legacy single-port fallback, used only when the pivot setup path doesn't know the team number. |
| `PIVOT_LISTENER_PORT_BASE` | 9900 | Base for per-team TCP-pivot ports: team 1 → 9901, team 2 → 9902, … |
| `BEACON_REDEPLOY_THRESHOLD_MULTIPLIER`, `BEACON_REDEPLOY_CHECK_INTERVAL_MINUTES` | —, 5 | Automatic beacon redeploy when a beacon goes silent. |

> **`SLIVER_PORT` is not a real setting.** Older notes describe it as the
> operator gRPC port (31337). Nothing in the backend reads it — `core/config.py`
> never declares it, and the gRPC endpoint is parsed from `SLIVER_CFG_PATH`.
> Setting it in `.env` has no effect. Do not confuse it with
> `MTLS_LISTENER_PORT`, which is the port implants actually dial.

See [Sliver setup](sliver-setup) for the server/operator-config procedure.

## Credential harvesting

| Key | Default | Notes |
|---|---|---|
| `CRED_HARVEST_ENABLED` | `true` | Master switch for the SAM/LSA dump + golden-ticket loops. |
| `HASH_HARVEST_INTERVAL_MINUTES` | 5 | Hash harvest cadence. |
| `TICKET_REFRESH_INTERVAL_MINUTES` | 5 | Ticket refresh cadence. |
| `HASH_HARVEST_MAX_CONCURRENT` | 4 | Lower than plain exec — `--sam --lsa` is heavier. |
| `TICKET_TTL_HOURS` | 10 | Matches the default Kerberos TGT lifetime. |
| `TICKET_REFRESH_BEFORE_EXPIRY_MIN` | 30 | Re-forge window. |
| `SSH_KERBEROS_TIMEOUT` | 15 | Per-attempt timeout for the `ssh -o GSSAPIAuthentication=yes` login used to validate a forged ticket against a domain-joined Linux box (`POST /c2/tickets/{id}/validate`). See `docs/PLAN_ticket_multibox_validation.md`. |
| `GETST_TIMEOUT` | 30 | Timeout for the `impacket-getST host/<fqdn>` service-ticket fallback used when the Linux SSH GSSAPI login itself fails but the KDC still accepts the ticket (proves the ticket is valid; auth failure is box-side, not ticket-side). |
| `TICKET_LINUX_USER_ID` | 1000 | RID baked into the on-the-fly golden ticket forged for a Linux box's domain user during validation. SSSD/`krb5_kuserok` maps the login by principal NAME, so the exact RID is not load-bearing for auth success. |
| `KRBTGT_DCSYNC_ENABLED` | `true` | Kill switch for noise-sensitive environments — DCSync raises Event 4662 on the DC. |

## Scoring

| Key | Default | Notes |
|---|---|---|
| `IOC_PARTIAL_SCORE_PCT` | 40 | Effect-only share of an IOC's base points. |
| `IOC_COMPLETION_BONUS_PCT` | 20 | Bonus when both effect and persistence are present. |

Base points are 5/10/20/40 for Easy/Medium/Hard/Ultra; the remaining 40% is the
persistence share. See [Scoreboard](scoreboard-reading) for how that renders.

### Auto-difficulty formula (Generate page badge)

The Generate page shows a computed difficulty for each build. The formula lives in
`fastapi_backend/core/difficulty_formula.py`, is published to the frontend at
`GET /c2/difficulty-formula`, and is mirrored (verbatim) in
`react/src/lib/difficultyFormula.ts`. Weights + bucket boundaries are **code-only** —
edit the Python module to tune; `tests/test_difficulty_formula.py` locks the
frontend mirror to the Python constants and fails on drift.

Weight table:

```
Base:  PersistenceMechanism.tier    (1|2|3)

Storage-mode weights (additive on top of base):
  file                                +0.0
  self_inject                         +0.5
  self_inject_persist                 +1.0
  registry (Windows)                  +1.0
  pe_section (Windows)                +1.0
  xattr (Linux)                       +0.5
  elf_section (Linux/pfSense)         +0.5
  extattr (pfSense/FreeBSD)           +0.5

Protection weights (additive):
  enable_hiding                       +1.5   (single most decisive)
  apply_immutable + binary_only       +0.5
  apply_immutable + binary_and_persistence  +0.75
  timestomp                           +0.25
  backup_copy_enabled                 +0.5
    (zero when storage_mode starts with `self_inject` — no on-disk file to copy)

Bucket score → difficulty:
  score <= 1.5   → Easy   (1)
  score <= 2.5   → Medium (2)
  score <= 3.5   → Hard   (3)
  score  > 3.5   → Ultra  (4)
```

Only consumed on the INSERT path (`_auto_register_deployed_ioc`); an existing
`DeployedIOCs` row's `difficulty` column is NEVER rewritten from a subsequent
generate — blue teams are graded against the number that was in place when the
row was first written. See docs/PLAN_generate_page_labels_autodifficulty.md.

## Red scoring account

| Key | Default | Notes |
|---|---|---|
| `RED_SCORING_ENABLED` | `true` | IOC check/deploy/remediate **and** implant deploy authenticate only with the `red_scoring` infrastructure cred (`is_infrastructure=true`, root/admin-equivalent); operator paths never resolve it. `false` → legacy operator-cred selection. |
| `RED_SCORING_USERNAME` | `red_scoring` | Account name. |
| `RED_SCORING_DOMAIN` | — | Domain, when the account is a domain account. |
| `RED_SCORING_PASSWORD` | — | Empty → derived per-box from the admin password. |

Precedence for both username and password: per-box inventory
`scoring_username` / `scoring_password` → `.env` → admin-password fallback.
Provision it with `scripts/infra/create_red_scoring.py` and declare it in
inventory via the `scoring_account:` stanza — see
[Inventory YAML § scoring account](inventory-yaml#scoring-account) and
[Pre-Competition § scoring-account](pre-competition#scoring-account).

## Dev server reload flag

`scripts/run.sh` starts the backend under uvicorn. Auto-reload is **OFF by
default** (as of 2026-08-11) and must be opted into. These are `run.sh`
behaviours, not `.env`/`config.py` keys.

| Control | Effect |
|---|---|
| *(nothing)* | Reload **disabled** — file edits do not restart the backend. |
| `C3R_RELOAD=1 bash scripts/run.sh` | Reload **enabled** (env opt-in). |
| `bash scripts/run.sh --reload` | Reload **enabled** (CLI opt-in). |
| `C3R_NO_RELOAD=1 …` | Reload **forced off** — wins over any opt-in above. |

Precedence: `C3R_NO_RELOAD=1` (off) > `C3R_RELOAD=1` / `--reload` (on) > default (off).

**Why default-off:** uvicorn `--reload` watches the *whole* repo (WatchFiles), so
editing any file — including under `tests/` — restarts the worker and drops every
open operator terminal/WebSocket. A wedged reload can also hold the listening
socket while joining the dead worker, so new clients hang in the TLS handshake
instead of being refused (measured 2026-08-11: one edit hung a 45-min soak for 4
hours). `run_soak.sh` sets `C3R_NO_RELOAD=1` for the same reason. Enable reload
only for a focused dev loop where you want edits to hot-restart the backend.

<!-- audited 2026-07-30 — page created from CLAUDE.md § Configuration. Every stated default verified programmatically against fastapi_backend/core/config.py; _MUTABLE_SETTINGS list copied from routes/admin.py:1160. SLIVER_PORT documented as a phantom: no os.getenv("SLIVER_PORT") exists anywhere in the backend. -->
