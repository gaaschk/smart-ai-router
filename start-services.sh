#!/bin/bash
set -e

export PATH="$HOME/.bun/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

REPO_DIR="$HOME/ProjectHome/smart-ai-router"
GBRAIN_KNOWLEDGE_DIR="$HOME/gbrain-knowledge"

echo "🚀 Starting Smart-AI-Router"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Kill any existing process
killall python 2>/dev/null || true
sleep 1

# 1. Start smart-ai-router
echo "1️⃣  Starting Smart-AI-Router on port 8001..."
cd "$REPO_DIR"
source .venv/bin/activate
python -m smart_ai_router > /tmp/smart-router.log 2>&1 &
ROUTER_PID=$!
echo "   PID: $ROUTER_PID"
sleep 3

# 2. Refresh GBrain's copy of the project docs so RAG answers stay current.
#    $GBRAIN_KNOWLEDGE_DIR mirrors README.md + docs/*.md (see
#    docs/gbrain-deployment.md); re-import is a no-op for unchanged files.
if [ -d "$GBRAIN_KNOWLEDGE_DIR" ] && command -v gbrain >/dev/null 2>&1; then
    echo "2️⃣  Refreshing GBrain project docs..."
    cp "$REPO_DIR/README.md" "$GBRAIN_KNOWLEDGE_DIR/readme.md"
    mkdir -p "$GBRAIN_KNOWLEDGE_DIR/docs"
    cp "$REPO_DIR"/docs/*.md "$GBRAIN_KNOWLEDGE_DIR/docs/"
    gbrain import "$GBRAIN_KNOWLEDGE_DIR" --no-embed >> /tmp/smart-router.log 2>&1 || true
    gbrain embed --stale >> /tmp/smart-router.log 2>&1 || true
else
    echo "2️⃣  Skipping GBrain doc refresh ($GBRAIN_KNOWLEDGE_DIR or gbrain binary not found)"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Service started!"
echo ""
echo "Access smart-ai-router:"
echo "  🌐 http://kevins-mac-mini.local:8001"
echo ""
echo "Logs:"
echo "  • Router: /tmp/smart-router.log"
echo ""
echo "To stop: killall python"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
