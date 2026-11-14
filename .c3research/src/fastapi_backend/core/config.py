"""Configuration module for Red Team Scoring system.

Centralizes all configuration settings and environment variables.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()


def _load_dir_pools_yaml(path: str) -> dict[str, dict[str, int]]:
    """Read the ``dir_pools:`` section of the naming-pool YAML: {os: {dir: tier}}.

    Same shape/loader pattern as ``core.naming_pools`` reads ``name_pools:`` — the
    deploy-dir pools live in the same file so an operator edits one place to
    reshape both. Missing file / missing key / parse error → {} (deploy_paths.py
    already degrades an empty pool to a single OS default, e.g. C:\\Windows\\Temp).
    """
    try:
        import yaml  # type: ignore[import]  # cycle-break
        p = Path(path)
        if not p.exists():
            return {}
        with p.open() as f:
            data = yaml.safe_load(f) or {}
        raw = data.get("dir_pools") or {}
        return {
            str(os_key).lower(): {str(d): int(t) for d, t in (dirs or {}).items()}
            for os_key, dirs in raw.items()
        }
    except Exception:
        return {}


@dataclass
class Config:
    """System configuration."""

    # Database settings
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///db/database.db")

    # Queue settings
    USE_QUEUE_DB_WRITER: bool = (
        os.getenv("USE_QUEUE_DB_WRITER", "true").lower() == "true"
    )
    DB_QUEUE_SIZE: int = int(os.getenv("DB_QUEUE_SIZE", "10000"))
    DB_BATCH_SIZE: int = int(os.getenv("DB_BATCH_SIZE", "100"))
    DB_BATCH_TIMEOUT: float = float(os.getenv("DB_BATCH_TIMEOUT", "1.0"))

    # Script encryption settings
    USE_SCRIPT_ENCRYPTION: bool = (
        os.getenv("USE_SCRIPT_ENCRYPTION", "false").lower() == "true"
    )

    # Worker settings
    NUM_WORKERS: int = int(os.getenv("NUM_WORKERS", "16"))
    WORKER_TIMEOUT: int = int(os.getenv("WORKER_TIMEOUT", "40"))
    # docs/PLAN_soak_backend_perf.md §3.6: cap IOCs per BoxIOCTask.
    # Prior behavior bundled every deployed IOC on a box into one task; under
    # sustained load a single slow IOC (per-box NXC contention) blew the whole
    # batch's hard_timeout budget (155s × (num_iocs//5+1)), poisoning up to 22
    # IOCs at once. Splitting into fixed-size chunks isolates failures, caps
    # hard_timeout at ~310s per chunk, and lets healthy IOCs on the same box
    # keep flowing while a slow one waits. Zero disables the split.
    CHECK_MAX_IOCS_PER_TASK: int = int(os.getenv("CHECK_MAX_IOCS_PER_TASK", "5"))

    # Whole-IOC deploy attempts before the row is marked deploy_failed and an
    # alert fires (docs/PLAN_failed_deploy_redeploy.md). Retries re-inject the
    # row's RECORDED pooled names, so a retry never re-picks from the pool —
    # fresh names would orphan whatever the failed attempt already planted.
    DEPLOY_MAX_ATTEMPTS: int = int(os.getenv("DEPLOY_MAX_ATTEMPTS", "3"))

    # NetExec settings
    NXC_PATH: str = os.getenv("NXC_PATH", "./nxc_workspace")
    NXC_MAX_CONCURRENT: int = int(os.getenv("NXC_MAX_CONCURRENT", "40"))
    # docs/PLAN_soak_backend_perf.md §3.6: raised 5 → 10. Under sustained
    # Phase 2 Playwright + Phase 6 fault-injection load, check-cycle NXC calls
    # compete with operator NXC calls for the same per-box slots; 5 was too
    # tight — chunks of 5 IOCs still serialized 1-by-1 while operator calls
    # held the other slots. 10 gives the check pipeline breathing room.
    NXC_MAX_CONCURRENT_PER_BOX: int = int(os.getenv("NXC_MAX_CONCURRENT_PER_BOX", "10"))
    # SMB put_file reliability (docs/PLAN_implant_deploy_live_fire_fixes.md C).
    # Large (~17 MB implant) SMB writes fail under concurrency ("NETBIOS
    # connection timed out" / ECONNRESET) — reproduced 5/5 concurrent vs 3/5
    # serialized on a flaky box. A dedicated per-box put_file semaphore
    # serializes uploads to one box (default 1) WITHOUT throttling exec, plus
    # bounded retries and remote-hash verification of the written file.
    NXC_PUTFILE_MAX_CONCURRENT_PER_BOX: int = int(os.getenv("NXC_PUTFILE_MAX_CONCURRENT_PER_BOX", "1"))
    NXC_PUTFILE_MAX_RETRIES: int = int(os.getenv("NXC_PUTFILE_MAX_RETRIES", "5"))
    NXC_PUTFILE_RETRY_BACKOFF_SEC: float = float(os.getenv("NXC_PUTFILE_RETRY_BACKOFF_SEC", "2"))
    NXC_PUTFILE_VERIFY_HASH: bool = (os.getenv("NXC_PUTFILE_VERIFY_HASH", "true").lower() == "true")
    # SSH put_file: mirrors the SMB reliable path (see _put_file_ssh_reliable in
    # netexec/wrapper.py). TIMEOUT is per-nxc-subprocess and overrides the flat
    # NXC_TIMEOUT (120s) for large-binary uploads — chiefly the ones routed
    # through a pfSense pivot portfwd where the 120s cap starves fake_ntpd /
    # kill_shim / self_inject payloads under the full-deploy preflight load
    # (surfaced 2026-08-25 soak: project_fake_ntpd_pivot_putfile_timeout).
    #
    # TIMEOUT sizing (2026-08-25 soak I refinement):
    # payload sizes: fake_ntpd 785KB, hide_library 31KB, kill_shim 785KB,
    # self_inject_freebsd 4.4MB, shellcode_win 17MB. Realistic transfer time
    # over a healthy pivot at ~2 MB/s: 4s / <1s / 4s / 20s / 90s. Setting
    # NXC_PUTFILE_SSH_TIMEOUT=180s gives ~2x margin on the biggest payload
    # (shellcode_win, which goes over SMB not SSH anyway) and ~40x margin on
    # the largest SSH-routed payload (self_inject_freebsd).
    #
    # 600s was the initial value but proved fatal: when a Sliver TCP portfwd
    # goes silent mid-transfer (observed 2026-08-25 soak I on team-1 + team-4
    # linux members), the put-file hangs the FULL 600s per attempt, so 3
    # retries = 1800s, blowing past bench_soak's 600s operator-task budget on
    # the first outer retry. 180s per attempt × MAX_RETRIES=3 = 540s worst
    # case, under budget, and a slightly-flaky pivot often catches a healthy
    # window on retry 2 or 3 — a hard-dead pivot fails-fast at 540s instead
    # of at 1800s. See docs/PLAN_ssh_putfile_reliable.md.
    NXC_PUTFILE_SSH_TIMEOUT: int = int(os.getenv("NXC_PUTFILE_SSH_TIMEOUT", "180"))
    NXC_PUTFILE_SSH_MAX_RETRIES: int = int(os.getenv("NXC_PUTFILE_SSH_MAX_RETRIES", "3"))
    NXC_PUTFILE_SSH_VERIFY_HASH: bool = (
        os.getenv("NXC_PUTFILE_SSH_VERIFY_HASH", "true").lower() == "true"
    )
    # Per-pivot-portfwd connection pacer — minimum seconds between NXC TCP
    # connections to the same 127.0.0.1:<port> Sliver portfwd. Sliver's
    # `delayBeforeClose = 10 * time.Second` keeps a tunnel half-closed for
    # ~10s after a client's FIN; new connections within that window race
    # with the still-closing tunnel state and paramiko errors on the SSH
    # banner ("Unknown SSH Version" / "Internal Paramiko error reading SSH
    # protocol banner"). Isolated repro measured 42.5% failure at 5s
    # spacing (scripts/diagnostic/pivot_tunnel_repro_v2.py, 2026-08-26).
    # 10s (Sliver's own close delay) eliminates the race entirely. Set to
    # 0 to disable. See project_pivot_portfwd_race_definitive.
    NXC_PIVOT_PORTFWD_MIN_INTERVAL_SEC: float = float(
        os.getenv("NXC_PIVOT_PORTFWD_MIN_INTERVAL_SEC", "10.0"),
    )
    # Global cap on concurrent put-files across ALL pivot portfwd tunnels.
    # Every pivot session's portfwds multiplex over one shared Sliver gRPC
    # channel; when the full-deploy preflight fires heavy binary uploads
    # (fake_ntpd 785KB, hide_library .so, kill_shim, self_inject) to ~16 pivot
    # boxes at once, the aggregate saturates that channel so each 785KB
    # transfer can't finish inside NXC_PUTFILE_SSH_TIMEOUT (180s) → they all
    # time out (soak Z 2026-08-28: 16/16 fail under the burst, but the SAME
    # transfer succeeds fast when it has the tunnel to itself — .208/.243 on
    # attempt 1). Capping concurrent pivot uploads gives each enough bandwidth
    # to complete. The per-box put-file semaphore does NOT bound this — two
    # boxes on one pfSense session, and many sessions, still storm the channel.
    NXC_PIVOT_PUTFILE_MAX_CONCURRENT: int = int(
        os.getenv("NXC_PIVOT_PUTFILE_MAX_CONCURRENT", "6"),
    )
    # Deliver LARGE binary payloads to pivot-routed boxes via HTTP-pull (the box
    # curls the operator's script server over the network) instead of pushing
    # them through the Sliver TCP portfwd. ROOT FIX for the pivot deploy-fail
    # class (project_pivot_deploy_TRUE_rootcause): the portfwd chokes on
    # sustained transfers — a 785KB put_file through it hangs >200s even solo,
    # while the same file pulled over HTTP completes in ~1s (proven in isolation
    # 2026-08-28, scripts/diagnostic/pivot_putfile_vs_exec.py). Only payloads at
    # or above _MIN_BYTES take the pull path (small files put_file fine and don't
    # need the script server); on any pull failure the caller falls back to
    # put_file, so this never regresses a box the script server can't reach.
    NXC_PIVOT_HTTP_PULL_ENABLED: bool = (
        os.getenv("NXC_PIVOT_HTTP_PULL_ENABLED", "true").lower() == "true"
    )
    NXC_PIVOT_HTTP_PULL_MIN_BYTES: int = int(
        os.getenv("NXC_PIVOT_HTTP_PULL_MIN_BYTES", "65536"),
    )
    # When SSH put-file to a pivot loopback (127.0.0.1) exhausts retries with
    # pure "Timeout after Ns" errors — the signature of a Sliver TCP tunnel
    # that has drifted into idle-death (project_sliver_pivot_2min_tunnel_death,
    # observed as 8 residual failures in soak K after the pacer eliminated the
    # 10s-close-window race) — fire a fire-and-forget rebuild request:
    # reverse-look up the target box from PortfwdMappings and register FRESH
    # portfwds on the same pivot session, updating the row so subsequent
    # resolve_nxc_target calls return the fresh ports. Set to false to disable.
    NXC_PIVOT_REBUILD_ON_TIMEOUT: bool = (
        os.getenv("NXC_PIVOT_REBUILD_ON_TIMEOUT", "true").lower() == "true"
    )
    # Bounded transient-NETBIOS retry for exec/auth (NOT put_file, which has its
    # own retry above). A NETBIOS timeout means the box's SMB is momentarily
    # congested — one bounded, back-off'd retry (each attempt re-acquires the
    # per-box semaphore, so retries never add concurrent load to the same box, and
    # the back-off sleep happens with the slot released) recovers most transients
    # without a retry-storm. 0 disables. See NXCWrapper._run.
    NXC_EXEC_MAX_RETRIES: int = int(os.getenv("NXC_EXEC_MAX_RETRIES", "5"))
    NXC_EXEC_RETRY_BACKOFF_SEC: float = float(os.getenv("NXC_EXEC_RETRY_BACKOFF_SEC", "2"))
    # Per-box circuit-breaker (NXCWrapper._run + netexec.nxc_semaphore). The
    # per-op retry above recovers a MOMENTARY transient, but a box that is
    # genuinely saturated (CPU-pegged by an inject/compile, or its SMB stack
    # wedged) keeps NETBIOS-timing-out every op — and under the full storm many
    # DIFFERENT ops hammer it concurrently, so it never recovers (hundreds of
    # timeouts/box observed). After THRESHOLD consecutive transient (NETBIOS/
    # connection-class) failures on one box, impose a COOLDOWN before the next op
    # to THAT box (checked at the top of _run, BEFORE the semaphore is acquired, so
    # the wait holds no slot). A single success clears it. 0 threshold disables.
    NXC_BOX_COOLDOWN_THRESHOLD: int = int(os.getenv("NXC_BOX_COOLDOWN_THRESHOLD", "3"))
    NXC_BOX_COOLDOWN_SEC: float = float(os.getenv("NXC_BOX_COOLDOWN_SEC", "15"))
    # Alternate file-delivery fallback (docs/PLAN_windows_delivery_fallback.md).
    # When SMB put_file exhausts its retries with a transient NETBIOS-timeout-class
    # failure (_TRANSIENT_PUT_RE), escalate the *file push* to WinRM (password
    # creds only — pywinrm NTLM can't pass-the-hash), then SSH (only when the box
    # has TCP/22 open per nmap SA). SMB stays the default first tier; command exec
    # is unaffected. false → SMB-only (pre-fallback behavior).
    NXC_ALT_DELIVERY_ENABLED: bool = (os.getenv("NXC_ALT_DELIVERY_ENABLED", "true").lower() == "true")
    # WinRM base64-upload chunk size (chars per run_ps append). Small enough to
    # stay under the WinRM command-envelope limit; larger = fewer round-trips.
    NXC_WINRM_CHUNK_SIZE: int = int(os.getenv("NXC_WINRM_CHUNK_SIZE", "1500"))
    NXC_WINRM_PORT: int = int(os.getenv("NXC_WINRM_PORT", "5985"))
    # Pass-the-hash over WinRM: when true, an nthash cred can drive the WinRM
    # delivery tier (requests-ntlm/pyspnego accept an "LM:NT" hex password). For
    # an nthash-only Windows box this is the ONLY viable fallback tier (SSH can't
    # NTLM-PtH). false → nthash creds skip WinRM (legacy behavior).
    NXC_WINRM_ALLOW_PTH: bool = (os.getenv("NXC_WINRM_ALLOW_PTH", "true").lower() == "true")
    NXC_TIMEOUT: int = int(os.getenv("NXC_TIMEOUT", "120"))
    NXC_DEPLOY_TIMEOUT: int = int(os.getenv("NXC_DEPLOY_TIMEOUT", "120"))
    # smbexec/wmiexec/mmcexec poll the output file this many times before
    # giving up ("Could not retrieve output file, it may have been detected
    # by AV"). nxc's own built-in default (10) isn't always enough — observed
    # empirically needing ~20 on some boxes under load; SMB-only (SSH/WinRM
    # exec methods have no output-file retrieval step).
    NXC_GET_OUTPUT_TRIES: int = int(os.getenv("NXC_GET_OUTPUT_TRIES", "20"))

    # Kill NXC output readback on Windows (docs/PLAN_kill_nxc_readback.md).
    # smbexec redirects each command's stdout to \\HOST\C$\<rand> (C:\ root),
    # reads it back, deletes it — but leaks the file (and its IOC-check/SA
    # content) on read/delete races.  When true, IOC-scoring + SA Windows execs
    # run --no-output (no C$ file at all) and take output from the result
    # callback; on a callback-miss they retry with readback and then scrub the
    # leftover.  Set false to revert every Windows path to readback-always.
    NXC_SUPPRESS_READBACK: bool = (
        os.getenv("NXC_SUPPRESS_READBACK", "true").lower() == "true"
    )
    # Fire a defensive smbexec-artifact sweep once per box per SA cycle — clears
    # historical leftovers (files that predate NXC_SUPPRESS_READBACK) and any
    # stray drop, since SA visits every Windows box every cycle.
    NXC_SWEEP_ON_SA: bool = os.getenv("NXC_SWEEP_ON_SA", "true").lower() == "true"

    # Script serving settings
    # URL that target boxes use to reach this server, e.g. https://192.168.1.100:3000
    # When set, build_inline_command uses irm|iex (Windows) or curl|bash (Linux)
    # instead of base64 inline encoding.  Leave unset to use base64 fallback.
    SCRIPT_SERVER_URL: str | None = os.getenv("SCRIPT_SERVER_URL", None)
    # SCRIPT_HMAC_SECRET — shared by all backend processes so any of them can
    # validate any token (multi-backend operator + test deployments). Tokens
    # are HMAC-signed per (box_ip, script_path, exp_unix) with 1-hour TTL —
    # see fastapi_backend/core/script_server.py.
    SCRIPT_HMAC_SECRET: str = os.getenv(
        "SCRIPT_HMAC_SECRET",
        # Generated fallback when unset — fine for single-host dev, but cross-
        # backend setups (operator on :3000 + test on :3001 sharing
        # SCRIPT_SERVER_URL) MUST set SCRIPT_HMAC_SECRET explicitly in .env.
        "DEV_FALLBACK_INSECURE_SCRIPT_HMAC_SECRET_CHANGE_ME",
    )

    # Unified script wrapper (netexec/script_wrapper.py).  Master switch lets
    # operators roll back to the legacy put_file (Windows) / inline-SSH
    # (Linux) combined-script delivery if the wrapper subsystem misbehaves
    # mid-competition.  Cache TTL is the maximum age a registered dynamic
    # body lives in memory before lazy or sweep eviction.  Max entries is
    # the hard cap that triggers WrapperCacheFullError → per-call fallback
    # (the global flag is NOT flipped).
    USE_SCRIPT_WRAPPER: bool = (
        os.getenv("USE_SCRIPT_WRAPPER", "true").lower() == "true"
    )
    # Completion-based lifecycle: callers evict via cache.evict(cmd.token_id)
    # after NXC returns.  TTL is a leak-guard fallback only — 600 s so crashed
    # or stalled callers don't pin plaintext bodies in memory indefinitely.
    SCRIPT_WRAPPER_TTL_SEC: int = int(os.getenv("SCRIPT_WRAPPER_TTL_SEC", "600"))
    SCRIPT_WRAPPER_MAX_CACHE_ENTRIES: int = int(
        os.getenv("SCRIPT_WRAPPER_MAX_CACHE_ENTRIES", "1024")
    )
    SCRIPT_WRAPPER_SWEEP_INTERVAL_SEC: int = int(
        os.getenv("SCRIPT_WRAPPER_SWEEP_INTERVAL_SEC", "30")
    )

    # Result-callback (docs/PLAN_script_result_callback.md) — target boxes POST
    # their check/deploy/remediate output back to
    # POST /scripts/wrapper/<token_id>/result instead of the caller trusting
    # NXC's own output readback (smbexec/wmiexec have no completion check on
    # the remote process — a redirected-stdout file can be read back empty or
    # partial, with no error, before the script finishes writing to it).
    # SCRIPT_RESULT_GRACE_SEC is intentionally short, not a fresh full
    # timeout: the dispatching NXC call has already spent real wall-clock
    # time internally (its own sleep()+retries) by the point callers start
    # this wait, so the POST has almost always already landed.
    SCRIPT_RESULT_GRACE_SEC: float = float(os.getenv("SCRIPT_RESULT_GRACE_SEC", "5"))
    # Kill switch — when false, callers skip registration/await entirely and
    # behave exactly as before this feature (pure NXCResult.output), independent
    # of USE_SCRIPT_WRAPPER.
    USE_SCRIPT_RESULT_CALLBACK: bool = (
        os.getenv("USE_SCRIPT_RESULT_CALLBACK", "true").lower() == "true"
    )

    # Dedicated off-loop script-delivery listener (2A) —
    # docs/PLAN_script_fetch_amplification.md fix #2.  Serves /scripts and
    # /scripts/wrapper from a second uvicorn.Server running in its own thread +
    # event loop on a separate port, so target-box script fetches terminate TLS
    # OFF the main asyncio loop and can't be starved by it under load.  Shares
    # the in-process WrapperCache + HMAC token manager directly (no IPC).  Point
    # SCRIPT_SERVER_URL at SCRIPT_LISTENER_PORT.  On bind failure the backend
    # HARD-FAILS startup (see lifespan_helpers._maybe_start_script_listener) —
    # a silently-degraded listener once squatted a pytest worker's app port and
    # answered /login with 404, erroring hundreds of tests before anyone noticed.
    SCRIPT_LISTENER_ENABLED: bool = (
        os.getenv("SCRIPT_LISTENER_ENABLED", "true").lower() == "true"
    )
    # Default port is dynamic: 3001 + worker count, so it sits ABOVE the pytest
    # per-worker app-port band (3001 + worker_idx).  A fixed 3010 collided with
    # xdist worker gw9's app port.  Override via SCRIPT_LISTENER_PORT; conftest
    # sets a unique per-worker value so each worker's listener is collision-free.
    SCRIPT_LISTENER_PORT: int = int(os.getenv("SCRIPT_LISTENER_PORT", "4000"))
    SCRIPT_LISTENER_HOST: str = os.getenv("SCRIPT_LISTENER_HOST", "0.0.0.0")
    # TLS material for the dedicated listener.  Unset → fall back to the repo's
    # certs/{cert,key}.pem (same pair scripts/run.sh hands the main uvicorn).
    SCRIPT_LISTENER_CERTFILE: str | None = os.getenv("SCRIPT_LISTENER_CERTFILE")
    SCRIPT_LISTENER_KEYFILE: str | None = os.getenv("SCRIPT_LISTENER_KEYFILE")
    # ALLOW_DISK_SCRIPT_FALLBACK — gates the LEGACY Windows disk-drop delivery
    # (put_file a combined .ps1 to C:\Windows\Temp) used by IOC checks
    # (check_runner.run_nxc_box_check) and IOC deploy/remediate
    # (deploy_manager._run_box_action) when the wrapper cache is unavailable
    # (USE_SCRIPT_WRAPPER off, SCRIPT_SERVER_URL unset, or WrapperCacheFullError).
    #
    # Default FALSE so a competition never silently leaks an IOC script (or the
    # cleartext IOC-name section markers) to a target's disk on wrapper
    # exhaustion — the box's IOCs fail loud instead.  Set TRUE only in dev/CI
    # where blue-team observability is not a concern.  The Linux inline-SSH
    # fallback writes nothing to disk and is therefore NOT gated by this flag.
    ALLOW_DISK_SCRIPT_FALLBACK: bool = (
        os.getenv("ALLOW_DISK_SCRIPT_FALLBACK", "false").lower() == "true"
    )

    # Diagnostic capture for status=-1 check sections. Default ON. A failed
    # effect/persistence section writes one check_failure_diagnostics row
    # (combined-output length + head/tail excerpt + section ordinal) so a run can
    # distinguish empty-body (delivery) vs truncated (late sections cut) vs
    # garbled output. Rows are written only on failures (rare in a healthy run);
    # the table has no retention cap yet — set CHECK_DIAG_CAPTURE=false to
    # disable. See docs/PLAN_soak_failure_instrumentation.md.
    CHECK_DIAG_CAPTURE: bool = (
        os.getenv("CHECK_DIAG_CAPTURE", "true").lower() == "true"
    )

    # Difficulty→points mapping
    DIFFICULTY_EASY_POINTS: int = int(os.getenv("DIFFICULTY_EASY_POINTS", "5"))
    DIFFICULTY_MEDIUM_POINTS: int = int(os.getenv("DIFFICULTY_MEDIUM_POINTS", "10"))
    DIFFICULTY_HARD_POINTS: int = int(os.getenv("DIFFICULTY_HARD_POINTS", "20"))
    DIFFICULTY_ULTRA_POINTS: int = int(os.getenv("DIFFICULTY_ULTRA_POINTS",  "40"))

    # Split scoring: 40% for removing effect, 40% for removing persistence, 20% completion bonus
    IOC_PARTIAL_SCORE_PCT: int = int(os.getenv("IOC_PARTIAL_SCORE_PCT", "40"))
    IOC_COMPLETION_BONUS_PCT: int = int(os.getenv("IOC_COMPLETION_BONUS_PCT", "20"))

    # Log any HTTP request whose full server-side duration exceeds this
    # threshold. Backstop for event-loop starvation: TLS handshake / accept
    # timeouts don't reach here (the loop never got to serve the request),
    # but a served-but-slow request >1s is often the same class of blocker
    # (sync SQL, sync file I/O, bulk aggregation on the loop thread).
    SLOW_REQUEST_LOG_THRESHOLD_SEC: float = float(
        os.getenv("SLOW_REQUEST_LOG_THRESHOLD_SEC", "1.0"),
    )

    # Competition settings
    CHECK_INTERVAL_MINUTES: int = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))
    # Enable-gate for the background credential re-validation loop. Default on
    # (production wants live cred state), but the pytest-xdist harness sets it
    # false: with N per-worker backends each re-validating every domain cred
    # against every domain box every cycle, the redundant bare-SMB-auth flood is
    # the proven source of the win2 NETBIOS-timeout instability (bare auth
    # ~21% timeout vs ~0% for exec/put_file — auth forwards NTLM to the DC and
    # stalls under load). Mirrors SA_COLLECTION_ENABLED / NMAP_HEALTH_ENABLED.
    CRED_REVALIDATION_ENABLED: bool = (
        os.getenv("CRED_REVALIDATION_ENABLED", "true").lower() == "true"
    )
    CRED_REVALIDATION_INTERVAL_MINUTES: int = int(
        os.getenv("CRED_REVALIDATION_INTERVAL_MINUTES", "5")
    )
    # Cap concurrent NXC subprocesses for the background credential
    # revalidation loop. NXC_MAX_CONCURRENT (default 40) saturates host RAM
    # when 40 nxc processes spawn simultaneously (~150-200 MB each), which
    # has stalled the asyncio event loop for minutes during long test runs.
    # Default to 8 — completes in a few seconds for ~50 creds without
    # competing with the IOC check workers.
    CRED_REVALIDATION_MAX_CONCURRENT: int = int(
        os.getenv("CRED_REVALIDATION_MAX_CONCURRENT", "8")
    )

    # ── NXC health tracker (per-box rolling failure window) ─────────────
    # Powers the C2 overview alert badge. A box flips to "degraded" when it
    # accumulates >= NXC_DEGRADED_THRESHOLD failures inside the most recent
    # NXC_DEGRADED_WINDOW_MINUTES. Loopback hosts (pivot-rewritten) are
    # excluded.
    NXC_DEGRADED_THRESHOLD: int = int(os.getenv("NXC_DEGRADED_THRESHOLD", "3"))
    NXC_DEGRADED_WINDOW_MINUTES: int = int(
        os.getenv("NXC_DEGRADED_WINDOW_MINUTES", "10")
    )

    # ── Network health monitoring ──────────────────────────────────────────
    NMAP_HEALTH_INTERVAL_MINUTES: int = int(os.getenv("NMAP_HEALTH_INTERVAL_MINUTES", "5"))
    NMAP_PATH: str = os.getenv("NMAP_PATH", "nmap")
    # Gate the periodic nmap ping-sweep loop. Default on for production; the
    # pytest harness sets this false per-worker so 32 backends don't all fire
    # nmap + the SA loop at boot (the thundering-herd CPU spike that times out
    # the first /admin/add_user). On-demand SA/health paths are unaffected.
    NMAP_HEALTH_ENABLED: bool = os.getenv("NMAP_HEALTH_ENABLED", "true").lower() == "true"

    # ── Credential harvesting (Phase A — c2/cred_harvester) ──────────────
    # Master kill switch — when False, neither loop runs (lifespan_helpers
    # skips starting both HashHarvestLoop + TicketRefreshLoop; on-demand
    # endpoints like /c2/harvest and /c2/forge-ticket stay available).
    #
    # DEFAULT DISABLED (2026-09-15): the background hash/ticket dump+validate
    # loops PtH-probe stale nthash rows, and every FAILED SMB auth hits the
    # Server 2025 SMB auth rate limiter (2s server-side delay on invalid auth,
    # default-on fleet-wide) which impacket/NXC surfaces as a NETBIOS timeout —
    # the proven root cause of the Windows NETBIOS instability. We aren't using
    # harvested hashes/tickets currently, so the loops are off by default. Code
    # is intact; flip back to "true" (or PATCH /admin/settings) to re-enable.
    # Revisit tracked in TODO.md ("Cred harvest/ticket loops disabled").
    CRED_HARVEST_ENABLED: bool = (
        os.getenv("CRED_HARVEST_ENABLED", "false").lower() == "true"
    )
    # Interval between SAM/LSA dump cycles. Each cycle validates existing
    # nthash rows + harvests any missing/invalid Windows boxes.
    HASH_HARVEST_INTERVAL_MINUTES: int = int(
        os.getenv("HASH_HARVEST_INTERVAL_MINUTES", "5")
    )
    # Interval between golden-ticket refresh cycles. DCSync krbtgt when
    # missing/invalid + re-forge tickets nearing expiry.
    TICKET_REFRESH_INTERVAL_MINUTES: int = int(
        os.getenv("TICKET_REFRESH_INTERVAL_MINUTES", "5")
    )
    # Concurrency cap for SAM/LSA dumps. Each `nxc --sam --lsa` is heavier
    # than a plain exec; keep low to avoid competing with IOC check pool.
    HASH_HARVEST_MAX_CONCURRENT: int = int(
        os.getenv("HASH_HARVEST_MAX_CONCURRENT", "4")
    )
    # Lifetime of forged golden tickets. 10h matches default Kerberos TGT.
    TICKET_TTL_HOURS: int = int(os.getenv("TICKET_TTL_HOURS", "10"))
    # Ticket multi-box validation (docs/PLAN_ticket_multibox_validation.md).
    # SSH GSSAPI login attempt timeout (Linux domain-auth validation).
    SSH_KERBEROS_TIMEOUT: int = int(os.getenv("SSH_KERBEROS_TIMEOUT", "15"))
    # impacket-getST service-ticket fallback timeout.
    GETST_TIMEOUT: int = int(os.getenv("GETST_TIMEOUT", "30"))
    # RID/user-id baked into the on-the-fly golden ticket forged for a Linux
    # box's domain user during validation. SSSD/krb5_kuserok maps the login by
    # principal NAME, so the exact RID is not load-bearing for auth success.
    TICKET_LINUX_USER_ID: int = int(os.getenv("TICKET_LINUX_USER_ID", "1000"))
    # Refresh window before expiry — re-forge when ticket has < this many
    # minutes of life remaining.
    TICKET_REFRESH_BEFORE_EXPIRY_MIN: int = int(
        os.getenv("TICKET_REFRESH_BEFORE_EXPIRY_MIN", "30")
    )
    # Per-action kill switch — disable DCSync attempts in noise-sensitive
    # environments (DCSync triggers Event 4662 on DC).
    KRBTGT_DCSYNC_ENABLED: bool = (
        os.getenv("KRBTGT_DCSYNC_ENABLED", "true").lower() == "true"
    )

    # Paths
    PROJECT_ROOT: Path = Path(
        os.getenv("PROJECT_ROOT", Path(__file__).parent.parent.parent)
    )
    IOC_SCRIPTS_DIR: Path = Path(os.getenv("IOC_SCRIPTS_DIR", "./iocs"))
    DB_DIR: Path = Path(os.getenv("DB_DIR", "./db"))

    # Sliver C2 settings
    BULK_EXEC_MAX_CONCURRENT: int = int(os.getenv("BULK_EXEC_MAX_CONCURRENT", "40"))
    SLIVER_ENABLED: bool = os.getenv("SLIVER_ENABLED", "false").lower() == "true"
    SLIVER_BIN: str = os.getenv("SLIVER_BIN", "sliver-server")
    SLIVER_CLIENT_BIN: str = os.getenv("SLIVER_CLIENT_BIN", "sliver-client")
    # Sliver console (PTY subprocess) resource bounds. Every /sliver-console WS
    # spawns a full `sliver-client console` process. A cyclic/orphaned Sliver
    # pivot graph makes each balloon to many GB while it materializes the graph
    # (2026-08-31 OOM: 5 concurrent consoles x ~12GB each -> host OOM, box pinned
    # at max RAM until reset). Two independent bounds:
    #   * MAX_CONCURRENT — soft cap on simultaneous console subprocesses; the
    #     (N+1)th connection is rejected with a message rather than spawned.
    #   * MEMORY_MAX — each console is spawned inside its own `systemd-run --user
    #     --scope` cgroup with this MemoryMax (and MemorySwapMax=0), so a single
    #     runaway console is OOM-killed inside its own scope at this ceiling
    #     instead of taking the host down. cgroup RSS cap is Go-safe (unlike
    #     RLIMIT_AS, which the Go runtime's large virtual arenas break). Empty
    #     string disables the cgroup wrap (concurrency cap still applies); the
    #     wrap is also skipped when `systemd-run` is not on PATH.
    SLIVER_CONSOLE_MAX_CONCURRENT: int = int(os.getenv("SLIVER_CONSOLE_MAX_CONCURRENT", "4"))
    SLIVER_CONSOLE_MEMORY_MAX: str = os.getenv("SLIVER_CONSOLE_MEMORY_MAX", "2G")
    SLIVER_CFG_PATH: str = os.getenv("SLIVER_CFG_PATH", "./sliver_operator.cfg")
    SLIVER_HOST: str = os.getenv("SLIVER_HOST", "localhost")
    # mTLS listener port (where IMPLANTS call home, distinct from the operator
    # gRPC port). Backend verifies this listener exists on startup
    # via SliverClient.jobs() and creates one if not — without it, generated
    # implants connect to nothing.
    MTLS_LISTENER_PORT: int = int(os.getenv("MTLS_LISTENER_PORT", "8888"))
    # Sliver external builder name for linux/amd64 SHARED_LIB (.so) implants.
    # When set, generate requests for those artifacts route to the named
    # external builder instead of the host's native build path (which uses
    # Kali's glibc 2.42, tagging .so with GLIBC_2.34 — unloadable on any
    # pre-2.34 target). See docker/sliver-build/ for the container that
    # registers as `c3r-glibc2.27-builder` and produces .so pinned to
    # GLIBC_2.4 (loadable back to RHEL 5 / CentOS 7).
    # Empty = keep native path (no delegation, current default behaviour).
    EXTERNAL_BUILDER_SO_LINUX: str = os.getenv("EXTERNAL_BUILDER_SO_LINUX", "")
    # Stage-listener port. Serves the full beacon to the small (509-byte) stager
    # that `windows_shellcode_registry` plants in the registry — the stager dials
    # C2_IP:STAGE_LISTENER_PORT to pull its second stage. Distinct from
    # MTLS_LISTENER_PORT: implants that already carry a full beacon call home on
    # mTLS, stagers fetch here first. See docs/PLAN_shellcode_registry_stager.md.
    STAGE_LISTENER_PORT: int = int(os.getenv("STAGE_LISTENER_PORT", "8890"))
    # TCP-pivot listener port — legacy single-port fallback when team-num is
    # unknown. Most code paths should use PIVOT_LISTENER_PORT_BASE + team_num
    # so concurrent pivots from different teams do not collide on one port.
    PIVOT_LISTENER_PORT: int = int(os.getenv("PIVOT_LISTENER_PORT", "9898"))
    # Base for per-team TCP-pivot port allocation. Each team's pfSense session
    # binds `PIVOT_LISTENER_PORT_BASE + team_num` on 127.0.0.1, so team 1 →
    # 9901, team 2 → 9902, etc. Fixes the soak finding where 5 teams' pivots
    # all tried to bind 9898 and only one succeeded.
    PIVOT_LISTENER_PORT_BASE: int = int(os.getenv("PIVOT_LISTENER_PORT_BASE", "9900"))
    C2_IP: str = os.getenv("C2_IP", "")
    DISPLAY_TIMEZONE: str = os.getenv("DISPLAY_TIMEZONE", "America/New_York")

    # Implant storage
    IMPLANT_STORE_DIR: str = os.getenv("IMPLANT_STORE_DIR", "./implants")

    # Build artifacts (docs/PLAN_build_check_deploy_name_pools.md) — per-deploy
    # recompiled shim cache + the manifest recording what each compiled artifact
    # has baked into it (dir/comm/name/source-hash).
    BUILD_DIR: str = os.getenv("BUILD_DIR", "./build")
    BUILD_MANIFEST_PATH: str = os.getenv(
        "BUILD_MANIFEST_PATH", "./build/artifact_manifest.json"
    )
    SHIM_CACHE_DIR: str = os.getenv("SHIM_CACHE_DIR", "./build/shim_cache")

    # Modules directory — scripts/binaries operators can run against boxes
    MODULES_DIR: str = os.getenv("MODULES_DIR", "./modules")

    # Operator wiki — root directory served by routes/wiki.py.
    # Authoritative source = top-level wiki/. Backend reads files directly;
    # restart-to-update is the contract (no hot reload).
    WIKI_ROOT: str = os.getenv("WIKI_ROOT", "./wiki")

    # Naming pool YAML for temp-file basenames dropped on target boxes.
    # See config/naming_pools.yml for format (header comments + inline docs).
    NAMING_POOL_FILE: str = os.getenv("NAMING_POOL_FILE", "config/naming_pools.yml")

    # SA collection settings
    SA_COLLECTION_INTERVAL_MINUTES: int = int(os.getenv("SA_COLLECTION_INTERVAL_MINUTES", "5"))
    SA_RESULT_RETENTION_COUNT: int = int(os.getenv("SA_RESULT_RETENTION_COUNT", "5"))
    SA_MAX_CONCURRENT_BOXES: int = int(os.getenv("SA_MAX_CONCURRENT_BOXES", "5"))
    # Gate the periodic NXC + Sliver SA-collection loops. Default on for
    # production; the pytest harness sets this false per-worker so the immediate
    # first-cycle SA sweep (5 boxes × NXC, × every worker backend) doesn't
    # stampede at boot. SA tests trigger collection on-demand via
    # POST /c2/sa/{ip}/collect, which is unaffected by this flag.
    SA_COLLECTION_ENABLED: bool = os.getenv("SA_COLLECTION_ENABLED", "true").lower() == "true"

    # Nmap reachability scanning
    # All boxes are covered in a single nmap invocation per cycle.
    # 240s default gives nmap enough room to scan a full /24 with -T5.
    NMAP_TIMEOUT: int = int(os.getenv("NMAP_TIMEOUT", "240"))

    # Batched IOC script size limit (bytes).  If the combined IOC script for a
    # box exceeds this threshold, the worker falls back to per-IOC execution.
    # Increase if all IOC scripts fit in your SSH/WMI arg-size budget.
    NXC_BATCH_IOC_SCRIPT_SIZE_LIMIT: int = int(os.getenv("NXC_BATCH_IOC_SCRIPT_SIZE_LIMIT", "30000"))

    # Cap concurrent scheduled-task dispatch to protect the event loop when a
    # Phase 3-style burst of due tasks lands on one scheduler tick. Each task
    # opens sync Sessions before yielding to gather; without a cap dozens of
    # fire_and_forget'd tasks pile on the loop thread (soak N 2026-08-26).
    SCHEDULER_MAX_CONCURRENT_TASKS: int = int(
        os.getenv("SCHEDULER_MAX_CONCURRENT_TASKS", "8"),
    )

    # Cap concurrent Sliver portfwd rebuilds during pivot pre-warm. Each rebuild
    # spins gRPC calls through the shared Sliver client; unbounded parallelism
    # against 10-pivot fleets congested it in soak O (2026-08-27).
    PIVOT_PREWARM_MAX_CONCURRENT_REBUILDS: int = int(
        os.getenv("PIVOT_PREWARM_MAX_CONCURRENT_REBUILDS", "4"),
    )
    # Post-rebuild re-probe: soak R (2026-08-27) saw a 5s hard re-probe
    # mis-classify 36/40 healthy freshly-rebuilt tunnels as "failed" because
    # the Sliver TCP tunnel + target SSH warm-up takes 5-15s end-to-end.
    # Retry with backoff up to the total budget so the classification is honest.
    PIVOT_PREWARM_REPROBE_ATTEMPTS: int = int(
        os.getenv("PIVOT_PREWARM_REPROBE_ATTEMPTS", "4"),
    )
    PIVOT_PREWARM_REPROBE_TOTAL_BUDGET_SEC: float = float(
        os.getenv("PIVOT_PREWARM_REPROBE_TOTAL_BUDGET_SEC", "25.0"),
    )
    PIVOT_PREWARM_REPROBE_PER_ATTEMPT_TIMEOUT_SEC: float = float(
        os.getenv("PIVOT_PREWARM_REPROBE_PER_ATTEMPT_TIMEOUT_SEC", "6.0"),
    )
    PIVOT_PREWARM_REPROBE_BASE_DELAY_SEC: float = float(
        os.getenv("PIVOT_PREWARM_REPROBE_BASE_DELAY_SEC", "1.5"),
    )

    # Pivot portfwd keepalive (root fix for the deploy-fail class,
    # project_pivot_deploy_rootcause). A Sliver TCP portfwd tunnel idle-dies at
    # ~2 min; when the pacer parks a box's tail deploys past that, the tunnel
    # dies mid-queue and the parked deploys eat 180s dead-tunnel timeouts. A
    # background loop probes each active portfwd every INTERVAL seconds (well
    # under the ~120s idle-death) to keep it warm, so deploys never hit a dead
    # tunnel and the banner race the pacer guards can't occur. Default 90s.
    PIVOT_KEEPALIVE_ENABLED: bool = (
        os.getenv("PIVOT_KEEPALIVE_ENABLED", "true").lower() == "true"
    )
    PIVOT_KEEPALIVE_INTERVAL_SEC: int = int(
        os.getenv("PIVOT_KEEPALIVE_INTERVAL_SEC", "90"),
    )
    PIVOT_KEEPALIVE_MAX_CONCURRENT: int = int(
        os.getenv("PIVOT_KEEPALIVE_MAX_CONCURRENT", "8"),
    )

    # Beacon auto-redeploy settings
    BEACON_REDEPLOY_THRESHOLD_MULTIPLIER: int = int(os.getenv("BEACON_REDEPLOY_THRESHOLD_MULTIPLIER", "3"))
    BEACON_REDEPLOY_CHECK_INTERVAL_MINUTES: int = int(os.getenv("BEACON_REDEPLOY_CHECK_INTERVAL_MINUTES", "5"))

    # Deploy path rotation — pools of candidate directories per OS.
    # Override via env as JSON arrays, e.g.:
    #   DEPLOY_PATH_POOL_WINDOWS='["C:\\Windows\\Temp","C:\\ProgramData"]'
    # DEPLOY_PATH_STRATEGY: "random" (default) | "round_robin"
    DEPLOY_PATH_POOL_WINDOWS: list = None   # type: ignore[assignment]
    DEPLOY_PATH_POOL_LINUX:   list = None   # type: ignore[assignment]
    DEPLOY_PATH_POOL_FREEBSD: list = None   # type: ignore[assignment]
    DEPLOY_PATH_STRATEGY: str = os.getenv("DEPLOY_PATH_STRATEGY", "random")
    # Difficulty tiers per deploy dir (docs/PLAN_difficulty_pools_extensionless.md).
    # dir -> tier (1=obvious/shallow ... higher=obscure/deep). A higher IOC
    # difficulty draws only from higher-or-equal tiers, so hard IOCs land in
    # less-obvious directories. Any pool dir absent from the map defaults to
    # tier 1 (so an un-tiered/legacy pool still selects the whole pool).
    # Override via env as a JSON object, e.g.:
    #   DEPLOY_PATH_TIER_LINUX='{"/tmp":1,"/usr/lib":3}'
    DEPLOY_PATH_TIER_WINDOWS: dict = None   # type: ignore[assignment]
    DEPLOY_PATH_TIER_LINUX:   dict = None   # type: ignore[assignment]
    DEPLOY_PATH_TIER_FREEBSD: dict = None   # type: ignore[assignment]

    # Dev mode — disables Sliver, NXC, SA collection, and beacon watcher.
    # Sessions and beacons are served from dev_sessions/dev_beacons DB tables.
    # Populate via: python scripts/testing/seed_dummy_data.py --clear
    DEV_MODE: bool = os.getenv("DEV_MODE", "false").lower() == "true"

    # Test-only hook: when set (float seconds), _maybe_setup_pivot sleeps
    # before its first ImplantBuilds lookup, simulating compile-commit lag.
    # Default None = no delay (production behavior). Excluded from
    # _MUTABLE_SETTINGS — not runtime-tunable via /admin/settings.
    PIVOT_BUILD_COMMIT_DELAY_OVERRIDE_SEC: float | None = (
        float(os.getenv("PIVOT_BUILD_COMMIT_DELAY_OVERRIDE_SEC"))
        if os.getenv("PIVOT_BUILD_COMMIT_DELAY_OVERRIDE_SEC")
        else None
    )

    # Red scoring account (red_scoring) — the ONLY credential the IOC pipeline
    # (check/deploy/remediate) authenticates with. Operators never see or use it
    # (Credentials.is_infrastructure=True rows are hidden from /c2/creds and
    # excluded from every operator cred-resolution path). Username/password/domain
    # live here (.env) for easy editing; inventory.yml's `scoring_account` stanza
    # may override the username/domain but the password is ALWAYS read from .env.
    # RED_SCORING_ENABLED is a kill-switch: false → IOC paths fall back to the
    # legacy operator-cred selection (use when the account is not yet on the boxes).
    RED_SCORING_ENABLED: bool = os.getenv("RED_SCORING_ENABLED", "true").lower() == "true"
    RED_SCORING_USERNAME: str = os.getenv("RED_SCORING_USERNAME", "red_scoring")
    RED_SCORING_PASSWORD: str = os.getenv("RED_SCORING_PASSWORD", "")
    RED_SCORING_DOMAIN: str = os.getenv("RED_SCORING_DOMAIN", "")
    # Cadence of the dedicated scoring-cred liveness probe (infra creds are
    # excluded from _revalidate_all_creds; this loop maintains their `valid` flag).
    SCORING_CRED_PROBE_INTERVAL_MINUTES: int = int(
        os.getenv("SCORING_CRED_PROBE_INTERVAL_MINUTES", "5")
    )
    # A previously-valid red_scoring cred is flipped invalid only after this many
    # CONSECUTIVE failed probes — one transient DC round-trip / SMB blip under load
    # must not down-score a box for a whole check cycle (docs/PLAN_scoring_cred_resilience.md).
    SCORING_CRED_PROBE_MAX_ATTEMPTS: int = int(
        os.getenv("SCORING_CRED_PROBE_MAX_ATTEMPTS", "3")
    )
    SCORING_CRED_PROBE_RETRY_DELAY_SEC: float = float(
        os.getenv("SCORING_CRED_PROBE_RETRY_DELAY_SEC", "2")
    )
    # On-demand red_scoring preflight (auth + privilege) concurrency cap. A
    # separate knob from the steady-state probe loop so a one-shot full-inventory
    # validation can fan out wider without retuning the background cadence.
    SCORING_PREFLIGHT_MAX_CONCURRENT: int = int(
        os.getenv("SCORING_PREFLIGHT_MAX_CONCURRENT", "8")
    )

    # Security
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "Sup3rS3cre3tRedTe3333am")
    JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")

    # API settings
    API_VERSION: str = os.getenv("API_VERSION", "1.0.0")
    CORS_ORIGINS: list = None

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FORMAT: str = os.getenv(
        "LOG_FORMAT", "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    def __post_init__(self) -> None:
        """Validate and create necessary directories."""
        import json as _json  # cycle-break

        # Initialize CORS origins
        if self.CORS_ORIGINS is None:
            self.CORS_ORIGINS = os.getenv(
                "CORS_ORIGINS", "https://localhost:5173"
            ).split(",")

        # Deploy path pools + difficulty tiers — content lives in the naming-pool
        # YAML's `dir_pools:<os>: {dir: tier}` section (same shape/file as
        # `name_pools:`, see core/naming_pools.py), not hardcoded here. A JSON env
        # var still wins when set (DEPLOY_PATH_POOL_<OS> / DEPLOY_PATH_TIER_<OS>);
        # a missing yaml file/key degrades to an empty pool — deploy_paths.py
        # already falls back to a single OS default (C:\Windows\Temp / /tmp) when
        # its pool is empty, so this never hard-fails.
        _dir_pools_yaml = _load_dir_pools_yaml(self.NAMING_POOL_FILE)
        for _os_key, _suffix in (("windows", "WINDOWS"), ("linux", "LINUX"), ("freebsd", "FREEBSD")):
            _pool_attr, _tier_attr = f"DEPLOY_PATH_POOL_{_suffix}", f"DEPLOY_PATH_TIER_{_suffix}"
            _yaml_tiers = _dir_pools_yaml.get(_os_key, {})
            if getattr(self, _pool_attr) is None:
                raw = os.getenv(_pool_attr, "")
                setattr(self, _pool_attr, _json.loads(raw) if raw else list(_yaml_tiers))
            if getattr(self, _tier_attr) is None:
                raw = os.getenv(_tier_attr, "")
                setattr(self, _tier_attr, _json.loads(raw) if raw else dict(_yaml_tiers))

        self.refresh_script_server_url()

        # Create directories if they don't exist
        self.DB_DIR.mkdir(parents=True, exist_ok=True)
        (self.PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)

        # Configure root logger with console output only.
        # File logging is set up explicitly by the FastAPI server via
        # setup_file_logging() called from main.py lifespan — this prevents
        # importing config (e.g. in test clients) from creating empty log files.
        logging.basicConfig(
            level=getattr(logging, self.LOG_LEVEL),
            format=self.LOG_FORMAT,
        )

    def setup_file_logging(self) -> None:
        """Add a timestamped FileHandler to the root logger.

        Called once from main.py lifespan so that only the actual FastAPI
        server process creates a log file — not test clients or bench scripts
        that import this module.
        """
        logs_dir = self.PROJECT_ROOT / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = logs_dir / f"fastapi_backend_{timestamp}.log"
        handler = logging.FileHandler(log_file, mode="a")
        handler.setFormatter(logging.Formatter(self.LOG_FORMAT))
        logging.root.addHandler(handler)
        logging.getLogger(__name__).info("Logging to file: %s", log_file)

    def refresh_script_server_url(self) -> None:
        """Fill SCRIPT_SERVER_URL from C2_IP when the operator left it unset.

        Explicit SCRIPT_SERVER_URL always wins — some deployments split the
        Sliver mTLS callback (C2_IP) from the script-fetch address (e.g. a
        different VPN interface) and set both independently in .env. This
        only covers the common case where they're the same host, so a fresh
        setup needs one value instead of two. Re-callable so
        lifespan_helpers.resolve_c2_ip() can re-derive after it late-stamps
        C2_IP from the outbound interface (C2_IP is often unset at
        __post_init__ time, before that auto-detect runs).
        """
        if not self.SCRIPT_SERVER_URL and self.C2_IP:
            self.SCRIPT_SERVER_URL = f"https://{self.C2_IP}:3000"

    def is_production(self) -> bool:
        """Check if running in production mode."""
        return os.getenv("ENVIRONMENT", "development").lower() == "production"

    def summary(self) -> dict:
        """Get configuration summary."""
        return {
            "environment": "production" if self.is_production() else "development",
            "database": "SQLite" if "sqlite" in self.DATABASE_URL else "Other",
            "queue_writer": self.USE_QUEUE_DB_WRITER,
            "script_encryption": self.USE_SCRIPT_ENCRYPTION,
            "num_workers": self.NUM_WORKERS,
            "check_interval": self.CHECK_INTERVAL_MINUTES,
            "api_version": self.API_VERSION,
            "red_scoring_enabled": self.RED_SCORING_ENABLED,
            "scoring_preflight_max_concurrent": self.SCORING_PREFLIGHT_MAX_CONCURRENT,
        }


# Global configuration instance
config = Config()
