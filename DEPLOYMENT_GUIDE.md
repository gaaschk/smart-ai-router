# Deployment Guide — Unified Dashboard on Mac Mini

This guide walks you through deploying the Unified Dashboard (Smart-AI-Router + Dashboard + GBrain) on a Mac Mini for multi-user access.

## Table of Contents

1. [System Requirements](#system-requirements)
2. [Pre-Deployment Checklist](#pre-deployment-checklist)
3. [Step 1: Install Dependencies](#step-1-install-dependencies)
4. [Step 2: Set Up Smart-AI-Router](#step-2-set-up-smart-ai-router)
5. [Step 3: Set Up GBrain](#step-3-set-up-gbrain)
6. [Step 4: Set Up Dashboard Backend](#step-4-set-up-dashboard-backend)
7. [Step 5: Set Up Dashboard Frontend](#step-5-set-up-dashboard-frontend)
8. [Step 6: Start All Services](#step-6-start-all-services)
9. [Monitoring & Troubleshooting](#monitoring--troubleshooting)
10. [Optional: Systemd/Launchd Services](#optional-systemdlaunchd-services)

---

## System Requirements

- **Mac Mini** (any recent model) with macOS 12+
- **Node.js** 18+ (for dashboard backend & frontend)
- **Bun** 1.0+ (for GBrain)
- **PostgreSQL** 14+ (for dashboard database & optional GBrain)
- **Python** 3.10+ (for smart-ai-router)
- **8 GB RAM** minimum (16 GB recommended for multi-user)
- **10 GB free disk** (for logs, databases, brain data)

---

## Pre-Deployment Checklist

Before starting, verify:

- [ ] All three repositories cloned/available on Mac Mini
- [ ] Node.js 18+: `node --version` → v18.0.0+
- [ ] Bun 1.0+: `bun --version` → 1.0.0+
- [ ] PostgreSQL running: `psql --version`
- [ ] Python 3.10+: `python3 --version`
- [ ] Network access: dashboard will listen on ports 5050 (backend), 5173 (frontend)
- [ ] Internet access: for downloading packages & LLM API calls

---

## Step 1: Install Dependencies

### 1.1 Install Node.js (if not already present)

```bash
# Check if installed
node --version

# If not, install via Homebrew
brew install node
```

### 1.2 Install Bun (for GBrain)

```bash
curl -fsSL https://bun.sh/install | bash
bun --version
```

### 1.3 Install PostgreSQL (if not already present)

```bash
# Check if installed
psql --version

# If not, install via Homebrew
brew install postgresql@16
brew services start postgresql@16

# Create a database for the dashboard
createdb dashboard
```

### 1.4 Verify Python

```bash
python3 --version  # Should be 3.10+
```

---

## Step 2: Set Up Smart-AI-Router

### 2.1 Install smart-ai-router

```bash
cd /path/to/smart-ai-router
pip3 install -e .
```

### 2.2 Configure Models & API Keys

Ensure your `.env` in the smart-ai-router root has:

```bash
# Example (adjust with your actual keys)
OPENAI_API_KEY=sk-...
CLAUDE_API_KEY=sk-ant-...
GEMINI_API_KEY=AIzaSy...
# Add other LLM provider keys as needed
```

### 2.3 Start smart-ai-router

```bash
# In the smart-ai-router directory
python3 -m smart_ai_router.cli.api

# Should see: "INFO: Uvicorn running on http://0.0.0.0:8001"
```

**Verify it's running:**
```bash
curl http://localhost:8001/health
# Should return: {"status": "ok", ...}
```

---

## Step 3: Set Up GBrain

### 3.1 Install GBrain

```bash
bun install -g github:garrytan/gbrain
gbrain --version
```

### 3.2 Initialize GBrain Brain

```bash
# Create a default brain in ~/.gbrain/
gbrain init

# Create a new brain context
gbrain new-context my-brain
```

### 3.3 Configure GBrain to Use Smart-AI-Router

Edit `~/.gbrain/config.json`:

```json
{
  "models": {
    "gateway": {
      "baseUrl": "http://localhost:8001/v1",
      "provider": "openai-compatible",
      "apiKey": ""
    },
    "chat": "auto-route",
    "rerank": "auto-route"
  }
}
```

### 3.4 (Optional) Set Up PostgreSQL for GBrain

For multi-user concurrency, use PostgreSQL instead of PGLite:

```bash
# Create a GBrain-specific database
createdb gbrain

# Set env variable before starting dashboard backend
export GBRAIN_DATABASE_URL="postgresql://localhost:5432/gbrain"
```

**Verify GBrain is working:**
```bash
gbrain list  # Should show existing pages
gbrain call get_stats  # Should return brain statistics
```

---

## Step 4: Set Up Dashboard Backend

### 4.1 Navigate to Backend Directory

```bash
cd /path/to/smart-ai-router/dashboard/backend
npm install
```

### 4.2 Create `.env` File

```bash
# dashboard/backend/.env

NODE_ENV=production
PORT=5050

# Smart-AI-Router
SMART_ROUTER_URL=http://localhost:8001

# GBrain
GBRAIN_BIN=gbrain
# GBRAIN_DATABASE_URL=postgresql://localhost:5432/gbrain  # Optional: use Postgres
GBRAIN_CLI_TIMEOUT_MS=30000

# Database (Dashboard multi-user)
DATABASE_URL=postgresql://postgres@localhost:5432/dashboard

# Auth
AUTH_JWT_SECRET=your-super-secret-jwt-key-change-this-in-production

# CORS
CORS_ORIGIN=http://localhost:5173

# Logging
LOG_LEVEL=info
```

**Important:** In production, use strong random values for `AUTH_JWT_SECRET`.

### 4.3 Run Setup CLI

This initializes the database and creates an admin user:

```bash
npm run setup

# Prompts will appear for:
# - Database verification
# - Admin email, name, password
```

### 4.4 Verify Backend Starts

```bash
npm run build
npm start

# Should see: "🚀 Dashboard server running on port 5050"
```

---

## Step 5: Set Up Dashboard Frontend

### 5.1 Navigate to Frontend Directory

```bash
cd /path/to/smart-ai-router/dashboard/frontend
npm install
```

### 5.2 Build Frontend

```bash
npm run build

# Produces dist/ folder for deployment
```

### 5.3 (Dev/Testing) Run Dev Server

```bash
npm run dev

# Frontend will be at http://localhost:5173
# API calls proxy to http://localhost:5050 (backend)
```

---

## Step 6: Start All Services

### Development Mode (for testing/debugging)

In separate terminal windows/tabs:

**Terminal 1 - Smart-AI-Router:**
```bash
cd /path/to/smart-ai-router
python3 -m smart_ai_router.cli.api
```

**Terminal 2 - Dashboard Backend:**
```bash
cd /path/to/smart-ai-router/dashboard/backend
npm run dev
```

**Terminal 3 - Dashboard Frontend (dev):**
```bash
cd /path/to/smart-ai-router/dashboard/frontend
npm run dev
```

Then visit: **http://localhost:5173**

### Production Mode (recommended for Mac Mini deployment)

Build the frontend:
```bash
cd dashboard/frontend
npm run build
```

Use a reverse proxy (nginx/Apache) to serve:
- `https://your-domain.local/api/*` → `http://localhost:5050/api/*` (backend API)
- `https://your-domain.local/*` → `dist/` folder (frontend static files)

Or use Node to serve both:
```bash
# Install serve globally
npm install -g serve

# In a new terminal
serve -s dashboard/frontend/dist -l 5173 &

# Backend runs on 5050 as usual
```

---

## Monitoring & Troubleshooting

### Check Service Health

```bash
# Smart-AI-Router
curl http://localhost:8001/health

# Dashboard Backend
curl http://localhost:5050/health

# GBrain
gbrain call get_stats
```

### Common Issues

#### 1. **Port Already in Use**

```bash
# If port 5050 is busy, use a different one:
PORT=5051 npm start

# Check what's using a port:
lsof -i :5050
```

#### 2. **Database Connection Failed**

```bash
# Verify PostgreSQL is running:
psql -U postgres -d dashboard

# Check DATABASE_URL format:
# postgresql://user:password@host:port/dbname
```

#### 3. **GBrain Times Out**

```bash
# Check GBrain process:
ps aux | grep gbrain

# Increase timeout in .env:
GBRAIN_CLI_TIMEOUT_MS=60000
```

#### 4. **Smart-AI-Router API Key Issues**

```bash
# Verify API keys are set:
echo $OPENAI_API_KEY
echo $CLAUDE_API_KEY

# Restart smart-ai-router with correct keys
```

### View Logs

```bash
# Backend logs (if using file logging)
tail -f dashboard/backend/logs/app.log

# Frontend console (in browser DevTools)
# Backend console (terminal where it's running)
```

---

## Optional: Systemd/Launchd Services

### macOS Launchd Service (Recommended for Mac Mini)

Create `~/Library/LaunchAgents/com.dashbo ard.backend.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.dashboard.backend</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/node</string>
        <string>/path/to/dashboard/backend/dist/server.js</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/dashboard/backend</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>NODE_ENV</key>
        <string>production</string>
        <key>PORT</key>
        <string>5050</string>
        <key>DATABASE_URL</key>
        <string>postgresql://postgres@localhost:5432/dashboard</string>
        <key>SMART_ROUTER_URL</key>
        <string>http://localhost:8001</string>
        <key>AUTH_JWT_SECRET</key>
        <string>your-secret-key</string>
    </dict>
    <key>StandardOutPath</key>
    <string>/var/log/dashboard-backend.log</string>
    <key>StandardErrorPath</key>
    <string>/var/log/dashboard-backend.log</string>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
```

Load and start:
```bash
launchctl load ~/Library/LaunchAgents/com.dashboard.backend.plist
launchctl start com.dashboard.backend

# Check status
launchctl list | grep dashboard
```

---

## Summary

Once all services are running, you should be able to:

1. **Sign in** at http://localhost:5173 with your admin credentials
2. **Chat** with LLMs (routed via smart-ai-router for cost optimization)
3. **View Analytics** of LLM usage by model, domain, cost
4. **Search Memory** across your GBrain knowledge base
5. **Run Skills** (background jobs for GBrain maintenance)
6. **Manage Users** (if you're an admin) and view per-user stats

Enjoy your unified dashboard!

---

## Getting Help

- **Dashboard Issues**: Check `/path/to/smart-ai-router/dashboard/backend/` logs
- **Smart-AI-Router Issues**: See https://github.com/gaaschk/smart-ai-router
- **GBrain Issues**: See https://github.com/garrytan/gbrain
- **Request Tracing**: All API errors include a `requestId` for debugging

