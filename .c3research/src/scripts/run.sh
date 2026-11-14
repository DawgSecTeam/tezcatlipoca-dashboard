#!/bin/bash
# Red Team Scoring System - Start Script
# Starts both backend API and React frontend as background jobs.
# Logs are written to logs/uvicorn_<ts>.log and logs/react_<ts>.log.
# Symlinks logs/uvicorn.log and logs/react.log always point to the latest run.
#
# Usage:
#   bash scripts/run.sh          # Normal mode (requires Sliver + NXC)
#   bash scripts/run.sh --dev    # Dev mode (stubs Sliver/NXC, serves seeded dummy data)

# Configuration (can be overridden with environment variables)
PROJECT_DIR=${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}
VENV_DIR=${VENV_DIR:-$PROJECT_DIR/venv}

# .env is the single source of truth for INVENTORY_FILE, C2_IP,
# SCRIPT_SERVER_URL, etc. Source it here so this shell (and the uvicorn
# subprocess it spawns) sees the same values without duplicating defaults
# in this script.
_PRESET_INVENTORY_FILE="$INVENTORY_FILE"  # preserve pre-set override — source below would clobber it
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_DIR/.env"
    set +a
fi
[ -n "$_PRESET_INVENTORY_FILE" ] && INVENTORY_FILE="$_PRESET_INVENTORY_FILE"
export INVENTORY_FILE="${INVENTORY_FILE:-$PROJECT_DIR/config/inventory.yml}"
BACKEND_PORT=3000
FRONTEND_PORT=5173
BACKEND_STARTUP_TIMEOUT=${BACKEND_STARTUP_TIMEOUT:-15}

DEV=false
_RELOAD_ARG=0
for arg in "$@"; do
    [[ "$arg" == "--dev" ]] && DEV=true
    [[ "$arg" == "--reload" ]] && _RELOAD_ARG=1
done

if $DEV; then
    export DEV_MODE=true
    echo "Starting in DEV MODE — Sliver and NXC stubbed, seeded data served"
fi

echo "Starting Red Team Scoring System..."
echo "Project directory: $PROJECT_DIR"
echo "Virtual environment: $VENV_DIR"
echo "Inventory: $INVENTORY_FILE"
echo ""

# Navigate to project directory
cd "$PROJECT_DIR" || { echo "Error: Cannot access project directory $PROJECT_DIR"; exit 1; }

# Activate virtual environment (required)
if [ -d "$VENV_DIR" ]; then
    echo "Activating virtual environment..."
    source "$VENV_DIR/bin/activate"
else
    echo "Error: Virtual environment not found at $VENV_DIR"
    exit 1
fi

# Ensure logs directory exists
mkdir -p "$PROJECT_DIR/logs"

# Timestamped log files for this run; symlinks always point to the latest.
_TS="$(date +%Y%m%d_%H%M%S)"
UVICORN_LOG="$PROJECT_DIR/logs/uvicorn_${_TS}.log"
REACT_LOG="$PROJECT_DIR/logs/react_${_TS}.log"
ln -sf "uvicorn_${_TS}.log" "$PROJECT_DIR/logs/uvicorn.log"
ln -sf "react_${_TS}.log"   "$PROJECT_DIR/logs/react.log"

# Prune uvicorn/react logs older than 7 days to avoid accumulation.
find "$PROJECT_DIR/logs" -maxdepth 1 -name "uvicorn_*.log" -mtime +7 -delete 2>/dev/null || true
find "$PROJECT_DIR/logs" -maxdepth 1 -name "react_*.log"   -mtime +7 -delete 2>/dev/null || true

# Environment cleanup (Sliver reset, stray-process sweep, VM reboot) now lives
# in scripts/clean_env.sh — run that first (restart.sh does: stop -> clean_env
# -> run). Here we only free our own listen ports so uvicorn/vite can bind.
echo "Freeing ports $BACKEND_PORT and $FRONTEND_PORT to bind..."
fuser -k $BACKEND_PORT/tcp 2>/dev/null
fuser -k $FRONTEND_PORT/tcp 2>/dev/null
sleep 1

# Start backend API (logs to file, PID saved)
#
# Reload is OFF by default. Turn it on explicitly with EITHER:
#   C3R_RELOAD=1 bash scripts/run.sh     (env)
#   bash scripts/run.sh --reload         (CLI arg)
# C3R_NO_RELOAD=1 forces reload OFF and WINS over any opt-in (soak/endurance
# safety) — see the precedence line below.
#
# Why default-off: uvicorn --reload is a DEV convenience and a hazard. WatchFiles
# watches the WHOLE repo, so editing ANY file (including something under tests/)
# restarts the worker underneath whatever is driving the backend — dropping every
# open WebSocket/terminal. Worse, the reload can wedge — the supervisor keeps the
# listening socket open while stuck in join()-ing the old worker, so clients still
# connect over TCP but hang forever in the TLS handshake instead of getting a
# refusal. Measured 2026-08-11: a single edit to tests/test_clean_artifacts.py
# wedged the backend and hung a 45-minute soak for 4 hours with no error surfaced
# anywhere.
_WANT_RELOAD=0
[[ "${C3R_RELOAD:-0}" == "1" ]] && _WANT_RELOAD=1   # env opt-in
[[ "$_RELOAD_ARG" == "1" ]] && _WANT_RELOAD=1        # --reload CLI opt-in
[[ "${C3R_NO_RELOAD:-0}" == "1" ]] && _WANT_RELOAD=0 # explicit opt-out WINS

RELOAD_ARGS=()
if [[ "$_WANT_RELOAD" == "1" ]]; then
    RELOAD_ARGS=(--reload)
    echo "Reload ENABLED (C3R_RELOAD/--reload) — file edits WILL restart the backend."
else
    echo "Reload disabled (default) — file edits will not restart the backend. Enable with C3R_RELOAD=1 or --reload."
fi
echo "Starting backend API on port $BACKEND_PORT with SSL..."
# docs/PLAN_soak_backend_perf.md §3.2: bump FD ulimit before spawning uvicorn.
# Under Phase 5/6 concurrent-race burst the process reached 947 open FDs (near
# the default 1024) — soft ulimit raise avoids EMFILE on any additional load.
ulimit -n 8192 || echo "warning: could not raise ulimit -n to 8192"
# docs/PLAN_soak_backend_perf.md §3.1: uvicorn tuning for sustained soak load.
#   --backlog 4096            larger TCP accept queue, less handshake-drop risk
#   --limit-concurrency 500   fail-fast at 500 in-flight requests (matches
#                             threshold; prevents unbounded queue growth)
#   --timeout-keep-alive 15   matches httpx client keepalive_expiry so conns
#                             aren't churned prematurely under sustained load
uvicorn fastapi_backend.main:app \
    --host 0.0.0.0 --port $BACKEND_PORT \
    "${RELOAD_ARGS[@]}" \
    --ssl-keyfile=certs/key.pem \
    --ssl-certfile=certs/cert.pem \
    --backlog 4096 \
    --limit-concurrency 500 \
    --timeout-keep-alive 15 \
    >> "$UVICORN_LOG" 2>&1 &
echo $! > "$PROJECT_DIR/logs/uvicorn.pid"

# Start React frontend (logs to file, PID saved)
echo "Starting React frontend on port $FRONTEND_PORT..."
cd react && npm run dev >> "$REACT_LOG" 2>&1 &
echo $! > "$PROJECT_DIR/logs/react.pid"
cd "$PROJECT_DIR"

# Wait for backend to become ready
echo ""
echo "Waiting for backend to be ready (timeout: ${BACKEND_STARTUP_TIMEOUT}s)..."
BACKEND_READY=0
for i in $(seq 1 $BACKEND_STARTUP_TIMEOUT); do
    curl -sk https://localhost:$BACKEND_PORT/docs > /dev/null 2>&1 && BACKEND_READY=1 && break
    sleep 1
done

if [ $BACKEND_READY -eq 0 ]; then
    echo "  Backend: FAILED to start within ${BACKEND_STARTUP_TIMEOUT} seconds"
    echo ""
    echo "Stopping frontend..."
    kill "$(cat "$PROJECT_DIR/logs/react.pid" 2>/dev/null)" 2>/dev/null
    echo ""
    echo "--- Backend logs (last 50 lines) ---"
    tail -n 50 "$UVICORN_LOG"
    exit 1
fi

echo "  Backend: READY"
echo ""
echo "Services started!"
echo "  Backend API:  https://localhost:$BACKEND_PORT"
echo "  API Docs:     https://localhost:$BACKEND_PORT/docs"
echo "  Frontend:     https://localhost:$FRONTEND_PORT"
echo ""
echo "Logs:"
echo "  tail -f $UVICORN_LOG"
echo "  tail -f $REACT_LOG"
echo "  (symlinks: logs/uvicorn.log → uvicorn_${_TS}.log, logs/react.log → react_${_TS}.log)"
