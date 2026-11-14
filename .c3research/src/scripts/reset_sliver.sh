#!/bin/bash
# reset_sliver.sh — purge all Sliver sessions/beacons, restart server, clean implant artifacts.
#
# Usage:
#   bash scripts/reset_sliver.sh
#   bash scripts/reset_sliver.sh --skip-purge   # skip gRPC purge (e.g. server already down)

set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
PYTHON=${PYTHON:-"$PROJECT_DIR/venv/bin/python"}

SKIP_PURGE=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-purge) SKIP_PURGE=true; shift ;;
        *) echo "Unknown arg: $1" >&2; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# 1. Kill all active sessions and beacons via gRPC
# ---------------------------------------------------------------------------
if ! $SKIP_PURGE; then
    echo "=== Step 1: purge active sessions and beacons ==="
    "$PYTHON" "$SCRIPT_DIR/diagnostic/sliver_helper.py" purge-all \
        || echo "  [warn] purge-all failed (Sliver may already be down — continuing)"
    echo ""
fi

# ---------------------------------------------------------------------------
# 2. Stop Sliver before touching its database
# ---------------------------------------------------------------------------
# CRITICAL: clean_stale_implants.sh modifies /root/.sliver/sliver.db directly via
# sqlite3. If Sliver is running during that modification, its in-memory cert store
# becomes inconsistent with the DB — all subsequent implant connections (including
# freshly-generated ones) receive "unknown implant signature key" and are rejected.
# Solution: stop Sliver first, clean the DB while it is down, then start fresh.
echo "=== Step 2: stop Sliver service ==="
if sudo systemctl is-active --quiet sliver 2>/dev/null; then
    sudo systemctl stop sliver
    echo "  sliver.service: stopped"
else
    echo "  sliver.service: already stopped"
fi
echo ""

# ---------------------------------------------------------------------------
# 3. Rotate Sliver server logs (timestamp the current logs before a fresh run)
# ---------------------------------------------------------------------------
# Sliver appends to audit.json / sliver.json / sliver.log indefinitely — across
# many runs these grow to tens of GB. Rename them with the current timestamp now,
# while the server is stopped and not writing, so the restarted server opens fresh
# empty logs. Done after stop / before start so nothing is renamed mid-write.
echo "=== Step 3: rotate Sliver server logs ==="
_SLIVER_LOG_DIR=${SLIVER_LOG_DIR:-/root/.sliver/logs}
_LOG_TS=$(date +%Y%m%d-%H%M%S)
for _logf in audit.json sliver.json sliver.log; do
    _src="$_SLIVER_LOG_DIR/$_logf"
    _dst="$_SLIVER_LOG_DIR/${_logf%.*}-${_LOG_TS}.${_logf##*.}"
    if sudo test -f "$_src"; then
        if sudo mv "$_src" "$_dst"; then
            echo "  rotated: $_logf -> ${_logf%.*}-${_LOG_TS}.${_logf##*.}"
        else
            echo "  [warn] failed to rotate $_logf"
        fi
    else
        echo "  skip: $_logf not present"
    fi
done
echo ""

# ---------------------------------------------------------------------------
# 4. Clean all implant artifacts (--days 0 = clean everything regardless of age)
# ---------------------------------------------------------------------------
echo "=== Step 4: clean all implant artifacts ==="
bash "$SCRIPT_DIR/clean_stale_implants.sh" --days 0
echo ""

# ---------------------------------------------------------------------------
# 5. Cap Go parallelism to prevent compile-time CPU saturation
# ---------------------------------------------------------------------------
# sliver-server's go build uses GOMAXPROCS goroutines (default = CPU core count).
# On 32-core hosts this saturates all cores during implant compilation, starving
# uvicorn backends of scheduler time → TLS handshake timeouts on polling calls.
# Capping at 8 cores leaves 24+ cores free for NXC subprocesses and backends
# while still parallelising the build sufficiently (compile time increases ~20%).
echo "=== Step 5: apply GOMAXPROCS cap to sliver service ==="
_OVERRIDE_DIR=/etc/systemd/system/sliver.service.d
_OVERRIDE_FILE="$_OVERRIDE_DIR/gomaxprocs.conf"
_DESIRED_PROCS=${SLIVER_GOMAXPROCS:-8}
if [ ! -f "$_OVERRIDE_FILE" ] || ! grep -q "GOMAXPROCS=$_DESIRED_PROCS" "$_OVERRIDE_FILE" 2>/dev/null; then
    sudo mkdir -p "$_OVERRIDE_DIR"
    printf '[Service]\nEnvironment="GOMAXPROCS=%s"\n' "$_DESIRED_PROCS" | sudo tee "$_OVERRIDE_FILE" >/dev/null
    sudo systemctl daemon-reload
    echo "  Applied GOMAXPROCS=$_DESIRED_PROCS override (daemon reloaded)"
else
    echo "  GOMAXPROCS=$_DESIRED_PROCS already in place"
fi
echo ""

# ---------------------------------------------------------------------------
# 6. Start the Sliver systemd service with a clean database
# ---------------------------------------------------------------------------
echo "=== Step 6: start Sliver service ==="
sudo systemctl start sliver
echo "  Waiting 5s for daemon to start..."
sleep 5
if sudo systemctl is-active --quiet sliver; then
    echo "  sliver.service: active"
else
    echo "  [warn] sliver.service is not active — check: sudo systemctl status sliver"
fi
echo ""

echo "Done. Sliver is fresh; implant artifacts purged."
