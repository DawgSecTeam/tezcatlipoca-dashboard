#!/bin/bash
# C3rberus Setup Script
# Run once to prepare the environment before using run.sh
#
# Usage:
#   bash scripts/setup.sh          # Full setup (includes netexec, mingw-w64)
#   bash scripts/setup.sh --dev    # Frontend dev setup (skips netexec/mingw, seeds dummy data)

set -e

PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)
VENV_DIR="${VENV_DIR:-$PROJECT_DIR/venv}"

DEV=false
for arg in "$@"; do [[ "$arg" == "--dev" ]] && DEV=true; done

echo "Setting up C3rberus at $PROJECT_DIR..."
$DEV && echo "Mode: frontend dev (Sliver/NXC dependencies skipped)"

echo "Making scripts executable..."
find "$PROJECT_DIR/scripts" -name "*.sh" -exec chmod +x {} +

# Always-needed runtime dirs
mkdir -p "$PROJECT_DIR/db" "$PROJECT_DIR/logs"

# System dependencies
echo "Installing system dependencies..."
if $DEV; then
    sudo apt update && sudo apt install -y \
        python3 python3-pip python3-venv \
        nodejs npm \
        openssl
else
    sudo apt update && sudo apt install -y \
        python3 python3-pip python3-venv \
        nodejs npm \
        openssl \
        netexec \
        mingw-w64 \
        sliver
fi

# Self-signed SSL cert
if [ ! -f "$PROJECT_DIR/certs/cert.pem" ]; then
    echo "Generating self-signed SSL certificate..."
    mkdir -p "$PROJECT_DIR/certs"
    openssl req -x509 -newkey rsa:4096 \
        -keyout "$PROJECT_DIR/certs/key.pem" \
        -out "$PROJECT_DIR/certs/cert.pem" \
        -days 365 -nodes -subj "/CN=localhost"
else
    echo "SSL certificate already exists, skipping."
fi

# Python virtual environment
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating Python virtual environment at $VENV_DIR..."
    mkdir -p "$(dirname "$VENV_DIR")"
    python3 -m venv "$VENV_DIR"
else
    echo "Virtual environment already exists at $VENV_DIR, skipping."
fi

echo "Installing Python dependencies..."
source "$VENV_DIR/bin/activate"
pip install -r "$PROJECT_DIR/requirements.txt"

# Frontend dependencies
echo "Installing frontend dependencies..."
cd "$PROJECT_DIR/react" && npm install && cd "$PROJECT_DIR"

# Playwright browser binaries (Playwright e2e tests need chromium headless shell)
if ! $DEV; then
    echo "Installing Playwright browsers..."
    (cd "$PROJECT_DIR/react" && node node_modules/@playwright/test/cli.js install chromium)
fi

# Sliver server: enable systemd unit + generate operator config + bootstrap mTLS listener
if ! $DEV; then
    if [ ! -f "$PROJECT_DIR/sliver_operator.cfg" ]; then
        echo "Bootstrapping Sliver server..."
        sudo systemctl enable --now sliver
        # Wait for the gRPC port to bind
        for i in $(seq 1 15); do
            ss -tln 2>/dev/null | grep -q ':31337' && break
            sleep 1
        done
        echo "Generating Sliver operator config..."
        sudo /usr/bin/sliver-server operator \
            --name c3rberus \
            --lhost localhost \
            --permissions all \
            --save "$PROJECT_DIR/sliver_operator.cfg"
        sudo chown "$USER:$USER" "$PROJECT_DIR/sliver_operator.cfg"

        echo "Starting persistent mTLS listener on 0.0.0.0:8888..."
        sudo /usr/bin/sliver-server console <<<'mtls -L 0.0.0.0 -l 8888' >/dev/null 2>&1 || true
    else
        echo "Sliver operator config already exists, skipping."
    fi
fi

# Dev mode: start the backend briefly to initialize the DB, then seed dummy data
if $DEV; then
    echo ""
    echo "Initializing database and seeding dummy data..."
    # Start backend in background long enough to create the DB schema
    export DEV_MODE=true
    export INVENTORY_FILE="$PROJECT_DIR/config/inventory.yml"
    uvicorn fastapi_backend.main:app \
        --host 127.0.0.1 --port 3000 \
        --ssl-keyfile=certs/key.pem \
        --ssl-certfile=certs/cert.pem \
        >> "$PROJECT_DIR/logs/uvicorn_setup.log" 2>&1 &
    UVICORN_PID=$!
    mkdir -p "$PROJECT_DIR/logs"
    echo "  Waiting for DB initialization..."
    for i in $(seq 1 20); do
        curl -sk https://localhost:3000/docs > /dev/null 2>&1 && break
        sleep 1
    done
    kill $UVICORN_PID 2>/dev/null
    sleep 1

    echo "  Seeding dummy data..."
    python "$PROJECT_DIR/scripts/testing/seed_dummy_data.py" --clear --checks 10 \
        --inventory "$PROJECT_DIR/config/inventory.yml"
fi

echo ""
echo "Setup complete!"
if $DEV; then
    echo "Run:  bash scripts/run.sh --dev"
else
    echo "Run:  bash scripts/run.sh"
fi
