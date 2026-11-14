#!/bin/bash
# reset_db.sh — Delete the SQLite database so it is recreated fresh on next startup.
#
# Usage:
#   ./reset_db.sh          # Prompts for confirmation
#   ./reset_db.sh --force  # Skips confirmation (for scripted use)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_FILE="${SCRIPT_DIR}/db/database.db"
WAL_FILE="${DB_FILE}-wal"
SHM_FILE="${DB_FILE}-shm"

RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'

# Treat as "nothing to do" only when none of the three files exist —
# stale -wal/-shm sidecars from a prior crash must be cleaned even when
# the main DB has already been removed (else SQLite startup fails with
# "disk I/O error" when journal_mode=WAL replays the old shm regions
# against the fresh empty DB).
if [ ! -f "$DB_FILE" ] && [ ! -f "$WAL_FILE" ] && [ ! -f "$SHM_FILE" ]; then
    echo "No database found at $DB_FILE (or sidecars) — nothing to do."
    exit 0
fi

if [ "$1" != "--force" ]; then
    echo -e "${RED}${BOLD}WARNING:${NC} This will permanently delete $DB_FILE (and any -wal/-shm sidecars)."
    echo "All scores, check results, users, credentials, and sessions will be lost."
    read -rp "Type 'yes' to confirm: " answer
    [ "$answer" = "yes" ] || { echo "Aborted."; exit 1; }
fi

rm -f "$DB_FILE" "$WAL_FILE" "$SHM_FILE"
echo "Database (+ sidecars) deleted. It will be recreated with fresh defaults on next startup."
