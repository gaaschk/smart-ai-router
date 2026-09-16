#!/bin/bash
set -e

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

REPO_DIR="$HOME/ProjectHome/smart-ai-router"

echo "🚀 Starting Unified Dashboard Services"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Kill any existing processes
killall python node serve npm 2>/dev/null || true
sleep 1

# 1. Start smart-ai-router
echo "1️⃣  Starting Smart-AI-Router on port 8001..."
cd "$REPO_DIR"
source .venv/bin/activate
python -m smart_ai_router > /tmp/smart-router.log 2>&1 &
ROUTER_PID=$!
echo "   PID: $ROUTER_PID"
sleep 3

# 2. Start dashboard backend
echo "2️⃣  Starting Dashboard Backend on port 5050..."
cd "$REPO_DIR/dashboard/backend"
npm start > /tmp/dashboard-backend.log 2>&1 &
BACKEND_PID=$!
echo "   PID: $BACKEND_PID"
sleep 3

# 3. Start dashboard frontend
echo "3️⃣  Starting Dashboard Frontend on port 5173..."
cd "$REPO_DIR/dashboard/frontend"
serve -s dist -l 5173 > /tmp/dashboard-frontend.log 2>&1 &
FRONTEND_PID=$!
echo "   PID: $FRONTEND_PID"
sleep 2

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ All services started!"
echo ""
echo "Access the dashboard:"
echo "  🌐 http://kevins-mac-mini.local:5173"
echo ""
echo "Admin Credentials:"
echo "  📧 admin@dashboard.local"
echo "  🔐 ChangeMe2026!Secure"
echo ""
echo "Service Ports:"
echo "  • Dashboard Frontend: 5173"
echo "  • Dashboard Backend:  5050"
echo "  • Smart-AI-Router:    8001"
echo ""
echo "Logs:"
echo "  • Router:   /tmp/smart-router.log"
echo "  • Backend:  /tmp/dashboard-backend.log"
echo "  • Frontend: /tmp/dashboard-frontend.log"
echo ""
echo "To stop services: killall python node serve npm"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
