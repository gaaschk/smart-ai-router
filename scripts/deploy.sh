#!/usr/bin/env bash
# deploy.sh — Bootstrap or manually re-deploy smart-ai-router on a remote Mac.
#
# Usage:
#   ./scripts/deploy.sh user@host
#
set -euo pipefail

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "Usage: $0 user@host"
  exit 1
fi

REMOTE_USER="${TARGET%%@*}"
REMOTE_DIR="/Users/${REMOTE_USER}/ProjectHome/smart-ai-router"
PLIST_LABEL="com.smart-ai-router"

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
  --exclude '.DS_Store' \
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

# 3. Run setup on remote (installs launchd + symlinks)
echo "→ Running setup on $TARGET"
ssh "$TARGET" bash <<EOF
  set -euo pipefail
  cd "$REMOTE_DIR"
  source .venv/bin/activate

  # Non-interactive: just install the service if providers are already configured
  python -m smart_ai_router setup <<INPUT
n
n
n
8001
INPUT
EOF

echo ""
echo "Done! Run 'smart-ai-router setup' on the remote for interactive provider config."
echo ""
echo "   Service: http://${TARGET##*@}:8001"
echo "   Logs:    ssh $TARGET 'tail -f $REMOTE_DIR/logs/server.log'"
echo "   Restart: ssh $TARGET 'launchctl kickstart -k gui/\$(id -u)/$PLIST_LABEL'"
