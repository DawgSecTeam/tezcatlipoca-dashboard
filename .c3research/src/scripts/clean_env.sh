#!/bin/bash
# clean_env.sh — Single source of truth for C3rberus environment cleanup.
#
# Consolidates the cleanup steps that used to be scattered across stop.sh,
# restart.sh, run.sh, run_soak.sh and run_tests.sh. Run it before starting the
# app / a soak / the test suite to guarantee a clean slate.
#
# Steps (in order):
#   1. Processes  — sweep stray uvicorn + venv-python + nxc + orphan
#                   pytest/run_tests.py wrappers (SIGTERM, then SIGKILL). Catches
#                   what the port-kill misses (e.g. a backend that crashed mid-bind
#                   or a leftover that's holding Sliver gRPC subscriptions and
#                   hammering the target VMs — a prime cause of back-to-back run
#                   degradation).
#   2. Ports      — free the service + xdist test-worker ports (3000, 3001-3032, 5173).
#   3. Test data  — remove stale per-worker test DBs / nxc workspaces / inventory
#                   shards / prebuilt-implant cache from prior pytest runs.
#   4. Sliver     — reset_sliver.sh: purge sessions/beacons, restart the service,
#                   clean implant artifacts.
#   5. VMs        — reboot_and_wait_vms.py: reboot every inventory box to a clean
#                   state and wait until it answers NXC again. DEFAULT ON; skip
#                   with --no-reboot-vms (slow: reboots all VMs).
#   6. CPU check  — check_cpu_usage.py against every box. Any box still >=20%
#                   busy right after a reboot means something with persistence
#                   re-triggered on boot (e.g. a cron/systemd-launched miner);
#                   remediate_persistent_iocs.py runs every persistence-capable
#                   IOC's remediate scripts against just those boxes. Runs LAST,
#                   unconditionally — including with --no-reboot-vms, so a box
#                   left running from a prior session still gets cleaned up.
#
# Usage:
#   scripts/clean_env.sh                    # full clean incl. VM reboot
#   scripts/clean_env.sh --no-reboot-vms    # everything except the VM reboot
#
# Always exits 0 — best-effort cleanup must never block the caller.

PROJECT_DIR=${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}
VENV_PY="$PROJECT_DIR/venv/bin/python"

# .env is the single source of truth for INVENTORY_FILE (see scripts/run.sh).
# Source it here so reboot_and_wait_vms.py (step 5) resets whatever inventory
# is actually in play (e.g. live-fire boxes) instead of defaulting to the
# generic dev inventory when called standalone or from a caller that never
# exported it.
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_DIR/.env"
    set +a
fi

REBOOT_VMS=true
for arg in "$@"; do
    case "$arg" in
        --no-reboot-vms) REBOOT_VMS=false ;;
        *) echo "[clean_env] unknown argument: $arg (ignored)" >&2 ;;
    esac
done

# 1. Sweep stray processes the port-kill below would miss.
echo "[clean_env] sweeping stray uvicorn + venv-python + nxc processes..."
pkill -TERM -f uvicorn                                   2>/dev/null && echo "  TERM -> uvicorn"
pkill -TERM -f "$VENV_PY"                                2>/dev/null && echo "  TERM -> venv-python"
pkill -TERM -f 'nxc (smb|ssh|ldap|winrm|mssql|rdp|ftp|wmi|nfs|vnc)' 2>/dev/null && echo "  TERM -> nxc"
pkill -TERM -f 'tests/run_tests.py'                      2>/dev/null && echo "  TERM -> run_tests.py wrappers"
sleep 2
pkill -KILL -f uvicorn                                   2>/dev/null && echo "  KILL -> lingering uvicorn"
pkill -KILL -f "$VENV_PY"                                2>/dev/null && echo "  KILL -> lingering venv-python"
pkill -KILL -f 'nxc (smb|ssh|ldap|winrm|mssql|rdp|ftp|wmi|nfs|vnc)' 2>/dev/null && echo "  KILL -> lingering nxc"

# 2. Free the service + xdist test-worker ports.
echo "[clean_env] freeing ports 3000, 3001-3032, 5173..."
for p in 3000 5173 $(seq 3001 3032); do
    fuser -k "${p}/tcp" 2>/dev/null || true
done

# 3. Remove stale test-harness data from prior pytest runs (idempotent; these
#    files only exist after a test run, so this is a no-op for app/soak callers).
#    NOTE: playwright_test.db is deliberately NOT touched — the Playwright backend
#    holds an open fd on it during a run; it is wiped at backend startup instead.
echo "[clean_env] clearing stale pytest artifacts..."
rm -f  "$PROJECT_DIR"/db/test_database_gw*.db    "$PROJECT_DIR"/db/test_database_gw*.db-wal \
       "$PROJECT_DIR"/db/test_database_gw*.db-shm "$PROJECT_DIR"/db/test_database.db \
       "$PROJECT_DIR"/db/test_database.db-wal     "$PROJECT_DIR"/db/test_database.db-shm \
       "$PROJECT_DIR"/db/test_alerts_*.db         "$PROJECT_DIR"/db/test_alerts_*.db-wal \
       "$PROJECT_DIR"/db/test_alerts_*.db-shm 2>/dev/null || true
rm -rf "$PROJECT_DIR"/nxc_workspace_gw*                       2>/dev/null || true
rm -f  "$PROJECT_DIR"/config/inventory_test_gw*.yml          2>/dev/null || true
rm -f  /tmp/c3r_prebuilt_*                                   2>/dev/null || true
rm -f  "$PROJECT_DIR"/tests/.live_teams_cache.json          2>/dev/null || true

# 4. Sliver reset (purge sessions/beacons, restart service, clean implants).
echo "[clean_env] resetting Sliver + cleaning implant artifacts..."
bash "$PROJECT_DIR/scripts/reset_sliver.sh" || true

# 5. Reboot every inventory VM to a clean state and wait for NXC health.
if $REBOOT_VMS; then
    echo "[clean_env] rebooting target VMs + waiting for NXC health (reboot_and_wait_vms.py)..."
    "$VENV_PY" "$PROJECT_DIR/scripts/testing/reboot_and_wait_vms.py" || true

    # 5b. Post-reboot guards (mirror run_tests.sh's own reboot path). The default
    #     reboot mode is snapshot ROLLBACK, so a DC comes back with a stale clock
    #     (skew presents as a member NETBIOS/AUTH-FAILED at the range gate, never as
    #     a time error) and possibly an NLA-wedged Windows Firewall profile blocking
    #     AD ports; a member's machine-account trust can be desynced; a pfSense box
    #     can carry dangling hide-lib LD_PRELOAD residue that bricks its admin shell.
    #     These self-guarding, best-effort fixers repair those BEFORE the range-clean
    #     gate below so a healthy-but-just-rebooted range is not aborted. run_tests.sh
    #     passes --no-reboot-vms here and runs its own copy, so these only fire for
    #     soak / bare clean_env reboots.
    echo "[clean_env] DC clock guard..."
    "$VENV_PY" "$PROJECT_DIR/scripts/infra/fix_dc_clocks.py" || true
    echo "[clean_env] DC firewall guard..."
    "$VENV_PY" "$PROJECT_DIR/scripts/infra/fix_dc_firewall.py" || true
    echo "[clean_env] member domain-auth guard..."
    "$VENV_PY" "$PROJECT_DIR/scripts/infra/fix_member_secure_channel.py" || true
    echo "[clean_env] pfSense LD_PRELOAD residue guard..."
    "$VENV_PY" "$PROJECT_DIR/scripts/infra/fix_pfsense_ldpreload.py" || true
else
    echo "[clean_env] skipping VM reboot (--no-reboot-vms)."
fi

# 6. Post-reboot CPU check — retry up to 5x before remediating.
#    A box running hot right after a reboot may just be settling; only
#    trigger remediation if it is still flagged on the final attempt.
CPU_CHECK_ATTEMPTS=5
CPU_CHECK_DELAY=30
FLAGGED_IPS=""
echo "[clean_env] checking post-reboot CPU usage (up to $CPU_CHECK_ATTEMPTS attempts, ${CPU_CHECK_DELAY}s apart)..."
for attempt in $(seq 1 $CPU_CHECK_ATTEMPTS); do
    CPU_FLAGGED_JSON=$(mktemp)
    "$VENV_PY" "$PROJECT_DIR/scripts/diagnostic/check_cpu_usage.py" --flagged-out "$CPU_FLAGGED_JSON" || true
    FLAGGED_IPS=$("$VENV_PY" - "$CPU_FLAGGED_JSON" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    boxes = json.load(open(sys.argv[1]))
except (OSError, json.JSONDecodeError):
    boxes = []
print(",".join(b["ip"] for b in boxes))
PYEOF
    )
    rm -f "$CPU_FLAGGED_JSON"
    if [ -z "$FLAGGED_IPS" ]; then
        echo "[clean_env] CPU check attempt $attempt/$CPU_CHECK_ATTEMPTS — all boxes clean."
        break
    fi
    echo "[clean_env] CPU check attempt $attempt/$CPU_CHECK_ATTEMPTS — high-CPU box(es): $FLAGGED_IPS"
    if [ "$attempt" -lt "$CPU_CHECK_ATTEMPTS" ]; then
        echo "[clean_env] waiting ${CPU_CHECK_DELAY}s before next attempt..."
        sleep "$CPU_CHECK_DELAY"
    fi
done
if [ -n "$FLAGGED_IPS" ]; then
    echo "[clean_env] still high-CPU after $CPU_CHECK_ATTEMPTS attempts ($FLAGGED_IPS) — remediating persistence IOCs..."
    "$VENV_PY" "$PROJECT_DIR/scripts/diagnostic/remediate_persistent_iocs.py" --ips "$FLAGGED_IPS" || true
    # Remediate strips the persistence, but the already-running implant survives
    # until a reboot — cycle the remediated boxes so they boot clean before the
    # gate below judges them (skipped under --no-reboot-vms).
    if $REBOOT_VMS; then
        echo "[clean_env] rebooting remediated box(es) to clear running implants..."
        "$VENV_PY" "$PROJECT_DIR/scripts/testing/reboot_and_wait_vms.py" --ips "$FLAGGED_IPS" || true
    fi
fi

# 7. FINAL range-clean gate. After reset + remediate + reboot, no box may be
#    above the CPU threshold or unreachable — a survivor is almost always an IOC
#    with PERSISTENCE that re-launched its implant (or a box that won't return).
#    Fail LOUD so run_tests / run_soak don't run on a dirty range.
#    Only when we actually rebooted here: --no-reboot-vms callers (run_tests) run
#    their own VM reboot afterward and gate themselves post-reboot.
if $REBOOT_VMS; then
    #    A box is judged dirty only if it stays dirty across --settle-attempts
    #    sweeps: reboot_and_wait_vms.py's gate only proves NXC answers, and a
    #    just-booted DC legitimately runs hot for a couple of minutes.
    echo "[clean_env] final range-clean validation..."
    if ! "$VENV_PY" "$PROJECT_DIR/scripts/diagnostic/check_cpu_usage.py" \
         --fail-on-unreachable --settle-attempts 5 --settle-delay 60 --triage; then
        echo ""
        echo "[clean_env] ============================================================"
        echo "[clean_env] RANGE NOT CLEAN — aborting."
        echo "[clean_env] Box(es) above the CPU threshold or unreachable after reset +"
        echo "[clean_env] remediation (see above). Likely cause: an IOC with PERSISTENCE"
        echo "[clean_env] that survived remediation and re-launched its implant, or a box"
        echo "[clean_env] that won't return. Investigate the flagged box(es) (stuck"
        echo "[clean_env] implant / persistence mechanism) and manually reset + remediate"
        echo "[clean_env] them before re-running."
        echo "[clean_env] ============================================================"
        exit 1
    fi
    echo "[clean_env] range clean."
fi

echo "[clean_env] done."
exit 0
