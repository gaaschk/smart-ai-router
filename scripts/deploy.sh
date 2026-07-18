#!/usr/bin/env bash
# deploy.sh — Bootstrap or manually re-deploy smart-ai-router on the Mac mini.
#
# Usage:
#   ./scripts/deploy.sh kevingaasch@<mac-mini-ip>
#
# CI deploys automatically via SSH (see .github/workflows/ci.yml).
# Use this script for the initial bootstrap or when you need to deploy by hand.
#
set -euo pipefail

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "Usage: $0 user@host"
  exit 1
fi

REMOTE_DIR="/Users/${TARGET%%@*}/ProjectHome/smart-ai-router"
PLIST_NAME="com.kevingaasch.smart-ai-router"
PLIST_SRC="com.kevingaasch.smart-ai-router.plist"

echo "→ Deploying to $TARGET:$REMOTE_DIR"

# 1. Sync project files
rsync -avz --progress \
  --exclude '.venv' \
  --exclude 'logs' \
  --exclude '*.db' \
  --exclude '.env' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.git' \
  --exclude '.idea' \
  . "$TARGET:$REMOTE_DIR"

echo "→ Setting up venv and dependencies on $TARGET"

# 2. Create venv + install deps on remote
ssh "$TARGET" bash <<EOF
  set -euo pipefail
  cd "$REMOTE_DIR"

  mkdir -p logs

  # Install uv if missing
  if ! command -v uv &>/dev/null; then
    echo "  Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="\$HOME/.local/bin:\$PATH"
  fi
  export PATH="\$HOME/.local/bin:\$PATH"

  uv venv --python 3.12 .venv
  source .venv/bin/activate
  uv pip install -e .
  echo "  ✓ venv ready"
EOF

# 3. Install and (re)start launchd service
echo "→ Installing launchd service on $TARGET"
ssh "$TARGET" bash <<EOF
  set -euo pipefail
  LAUNCH_DIR="\$HOME/Library/LaunchAgents"
  mkdir -p "\$LAUNCH_DIR"

  launchctl unload "\$LAUNCH_DIR/$PLIST_NAME.plist" 2>/dev/null || true

  cp "$REMOTE_DIR/$PLIST_SRC" "\$LAUNCH_DIR/$PLIST_NAME.plist"

  launchctl load "\$LAUNCH_DIR/$PLIST_NAME.plist"
  sleep 2

  if launchctl list | grep -q "$PLIST_NAME"; then
    echo "  ✓ Service running"
  else
    echo "  ✗ Service failed to start — check $REMOTE_DIR/logs/server.err"
    exit 1
  fi
EOF

echo ""
echo "✅  Deploy complete!"
echo ""
echo "   Web UI:  http://${TARGET##*@}:8000"
echo "   Logs:    ssh $TARGET 'tail -f $REMOTE_DIR/logs/server.log'"
echo "   Restart: ssh $TARGET 'launchctl kickstart -k gui/\$(id -u)/$PLIST_NAME'"
