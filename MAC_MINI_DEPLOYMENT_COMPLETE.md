# Mac Mini Deployment Complete ✅

## Quick Start

All services are now deployed and running on the Mac Mini at **kevins-mac-mini.local**

### Access the Dashboard

🌐 **http://kevins-mac-mini.local:5173**

### Admin Credentials
- **Email:** `admin@dashboard.local`
- **Password:** `ChangeMe2026!Secure` (⚠️ change after first login)

---

## Service Status

All three services are **fully operational**:

| Service | Port | Status | Access |
|---|---|---|---|
| Dashboard Frontend (React) | 5173 | ✅ Running | http://kevins-mac-mini.local:5173 |
| Dashboard Backend (Node.js) | 5050 | ✅ Running | http://kevins-mac-mini.local:5050/health |
| Smart-AI-Router (Python) | 8001 | ✅ Running | http://kevins-mac-mini.local:8001/v1/models |

**Database:** PostgreSQL (local connection, user: `kevingaasch`)

---

## Starting Services

### Option 1: Manual Startup Script (Recommended for now)

```bash
ssh kevins-mac-mini.local
bash ~/ProjectHome/smart-ai-router/start-services.sh
```

This will:
- Start all three services in the background
- Display logs in `/tmp/`
- Show access URLs and credentials

### Option 2: Individual Commands

**Terminal 1 - Smart-AI-Router:**
```bash
cd ~/ProjectHome/smart-ai-router
source .venv/bin/activate
python -m smart_ai_router
```

**Terminal 2 - Dashboard Backend:**
```bash
cd ~/ProjectHome/smart-ai-router/dashboard/backend
npm start
```

**Terminal 3 - Dashboard Frontend:**
```bash
cd ~/ProjectHome/smart-ai-router/dashboard/frontend
serve -s dist -l 5173
```

---

## Stopping Services

```bash
killall python node serve npm
```

---

## Launchd Auto-Start (WIP)

⚠️ **launchd auto-start is not yet working** due to a macOS domain/permission issue with the current user's LaunchAgents. This is a known limitation when launchctl refuses to load user agents without clear error messages.

**Workaround:** Add `start-services.sh` to your login items:
1. System Settings → General → Login Items → "Allow in the Login Items"
2. Or use macOS Task Scheduler alternatives (e.g., `cron`, `nohup`, screen sessions)

---

## Logs

| Service | Log File |
|---|---|
| Smart-AI-Router | `/tmp/smart-router.log` |
| Dashboard Backend | `/tmp/dashboard-backend.log` |
| Dashboard Frontend | `/tmp/dashboard-frontend.log` |

View logs:
```bash
tail -f /tmp/dashboard-backend.log
```

---

## Features

### Dashboard
- **Cost Analytics:** View routing costs, by-model breakdown, by-domain filtering
- **Chat Interface:** Send messages through smart-ai-router with per-user conversation history
- **User Management:** Admin can manage users and API keys
- **JWT Authentication:** Secure session management

### Smart-AI-Router
- **LLM Routing:** Classify prompts, route to optimal models
- **Provider Management:** Configure OpenAI, Anthropic, Google, etc.
- **Usage Tracking:** Per-user cost and usage logs
- **OpenAI-Compatible API:** Standard `/v1/` endpoint format

---

## Architecture

```
Browser (http://kevins-mac-mini.local:5173)
    ↓
[Dashboard Frontend] (React, port 5173)
    ↓ HTTP/Socket.IO
[Dashboard Backend] (Express, port 5050)
    ↓ PostgreSQL
[Local Database] (PostgreSQL, localhost:5432)

[Dashboard Backend] 
    ↓ HTTP
[Smart-AI-Router] (Python/FastAPI, port 8001)
    ↓
[External LLM Providers] (OpenAI, Anthropic, Google, etc.)
```

---

## Next Steps

1. **Change the admin password** immediately after first login
2. **Configure your LLM provider API keys** in the Smart-AI-Router settings
3. **(Optional) Debug & fix launchd auto-start** if you want services to auto-launch on reboot
4. **(Optional) Integrate GBrain** if you want knowledge management features

---

## Troubleshooting

### Services not responding
```bash
# Check if they're running
ps aux | grep -E "python|node|serve"

# Restart
killall python node serve npm
bash ~/ProjectHome/smart-ai-router/start-services.sh
```

### Database connection errors
```bash
# Verify PostgreSQL is running
psql -U kevingaasch -d dashboard -c "SELECT 1"

# If not running, start it
/opt/homebrew/opt/postgresql@16/bin/pg_ctl -D /opt/homebrew/var/postgresql@16 start
```

### Port already in use
```bash
# Find what's using the port
lsof -iTCP:5050 -sTCP:LISTEN

# Kill the process
kill -9 <PID>
```

---

## Deployment Notes

- **Python:** Using .venv at `/Users/kevingaasch/ProjectHome/smart-ai-router/.venv`
- **Node:** Homebrew-installed node at `/opt/homebrew/bin/node` (v26.8.2)
- **PostgreSQL:** Homebrew-installed postgresql@16, data at `/opt/homebrew/var/postgresql@16`
- **Repository:** `/Users/kevingaasch/ProjectHome/smart-ai-router` (main branch)
- **Build outputs:**
  - Backend: `dashboard/backend/dist/server.js`
  - Frontend: `dashboard/frontend/dist/` (built with Vite)

---

## Support

For issues, check:
- Service logs: `/tmp/*.log`
- Database connection: `psql -U kevingaasch -d dashboard -c "SELECT 1"`
- Port availability: `lsof -i :5050` (or :8001, :5173)
- Node/Python/PostgreSQL are installed: `which node python3 psql`

