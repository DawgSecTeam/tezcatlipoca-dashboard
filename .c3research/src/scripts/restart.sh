#!/bin/bash
# Red Team Scoring System - Restart Script
# stop -> clean_env -> run. Environment cleanup (stray-process sweep, ports,
# Sliver reset, VM reboot) is delegated to scripts/clean_env.sh. Args are
# forwarded to it — e.g. `restart.sh --no-reboot-vms` to skip the VM reboot.

PROJECT_DIR=${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}

echo "Restarting Red Team Scoring System..."
"$PROJECT_DIR/scripts/stop.sh"

echo ""
"$PROJECT_DIR/scripts/clean_env.sh" "$@"

echo ""
"$PROJECT_DIR/scripts/run.sh"
