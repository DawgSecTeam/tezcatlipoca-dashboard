#!/usr/bin/env bash
# install_hooks.sh — Configure git to use versioned hooks from .githooks/
# Run once after clone; idempotent.
#
# Usage: scripts/install_hooks.sh

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

git config core.hooksPath .githooks
chmod +x .githooks/pre-commit .githooks/pre-push .githooks/_common.sh

echo "[hooks] Installed."
echo ""
echo "  pre-commit  lint + pytest + audit + playwright + smoke soak  (~15 min)"
echo "  pre-push    validation soak                                   (~45 min)"
echo ""
echo "  Bypass: git commit --no-verify / git push --no-verify  (hotfix only)"
echo "  Logs:   logs/hooks/<hook>-<ts>/"
