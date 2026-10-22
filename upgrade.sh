#!/usr/bin/env bash
# upgrade.sh — upgrade the mimir installation in its own directory
#
# Usage:
#   bash /path/to/mimir/upgrade.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$APP_DIR/src/mimir_config.sh"
mimir_resolve_config

echo "==> Deploy dir: $APP_DIR"

# 1. Stop server (ignore if not running)
echo "==> Stopping server..."
bash "$APP_DIR/mimir.sh" --stop || true

# 2. Pull latest code
echo "==> Pulling latest code..."
git -C "$APP_DIR" pull --ff-only

# 3. Update uv itself, then sync dependencies
echo "==> Updating uv..."
uv self update
echo "==> Syncing dependencies..."
uv --directory "$APP_DIR" sync

# 4. Restart server with full reindex (clears DB + waits for sync)
echo "==> Reindexing..."
bash "$APP_DIR/mimir.sh" --reindex
