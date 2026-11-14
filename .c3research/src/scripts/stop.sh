#!/bin/bash
# Red Team Scoring System - Stop Script
# Gracefully stops uvicorn and React using saved PIDs.
# Falls back to port-based kill if PID files are missing or stale.

PROJECT_DIR=${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}

stop_pid() {
    local name=$1 pidfile=$2 port=$3

    if [ -f "$pidfile" ]; then
        local pid
        pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            echo "Stopping $name (PID $pid)..."
            kill -TERM "$pid"
            for i in $(seq 1 5); do
                kill -0 "$pid" 2>/dev/null || {
                    echo "  $name stopped."
                    rm -f "$pidfile"
                    return
                }
                sleep 1
            done
            echo "  $name did not stop — sending SIGKILL..."
            kill -KILL "$pid" 2>/dev/null
            rm -f "$pidfile"
        else
            echo "$name PID $pid is not running — cleaning up stale PID file."
            rm -f "$pidfile"
        fi
    else
        echo "No PID file for $name — trying port-based kill (port $port)..."
        fuser -k "${port}/tcp" 2>/dev/null && echo "  Killed process on port $port."
    fi
}

#stop_pid "uvicorn" "$PROJECT_DIR/logs/uvicorn.pid" 3000
#stop_pid "react"   "$PROJECT_DIR/logs/react.pid"   5173

# function wasn't working - PID tracking seems to be broken, temporary patch.
# Stops the live app: backend (3000) + frontend (5173). Test-worker ports
# (3001-3032) and the deeper environment sweep live in scripts/clean_env.sh.
fuser -k 3000/tcp
fuser -k 5173/tcp

echo "Done."
