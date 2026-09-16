# macOS Launchd Service Installation

This guide shows how to install and manage the Unified Dashboard services as background processes on macOS, so they auto-start when your Mac boots.

## Prerequisites

- macOS 12+ on Mac Mini
- Services already installed and working (see [DEPLOYMENT_GUIDE.md](../../DEPLOYMENT_GUIDE.md))
- All services tested and running successfully
- API keys configured for smart-ai-router

## Installation Steps

### 1. Prepare the Service Files

The service files are in this directory:
- `com.dashboard.backend.plist` — Dashboard backend (Node.js Express)
- `com.dashboard.frontend.plist` — Dashboard frontend (React + serve)
- `com.smartrouter.api.plist` — Smart-AI-Router (Python Uvicorn)

**Important**: Edit each `.plist` file to match your actual paths:

Replace:
- `/Users/kevingaasch/ProjectHome/smart-ai-router` → your actual path
- `AUTH_JWT_SECRET=CHANGE_ME_IN_PRODUCTION` → real secret key
- API keys in `com.smartrouter.api.plist` with real keys

### 2. Install Backend Service

```bash
# Copy to user's LaunchAgents
cp com.dashboard.backend.plist ~/Library/LaunchAgents/

# Set permissions
chmod 644 ~/Library/LaunchAgents/com.dashboard.backend.plist

# Load the service
launchctl load ~/Library/LaunchAgents/com.dashboard.backend.plist

# Verify it loaded
launchctl list | grep dashboard.backend
```

### 3. Install Frontend Service

```bash
# Install 'serve' globally (static file server)
npm install -g serve

# Copy service file
cp com.dashboard.frontend.plist ~/Library/LaunchAgents/

# Set permissions
chmod 644 ~/Library/LaunchAgents/com.dashboard.frontend.plist

# Load the service
launchctl load ~/Library/LaunchAgents/com.dashboard.frontend.plist

# Verify it loaded
launchctl list | grep dashboard.frontend
```

### 4. Install Smart-AI-Router Service

```bash
# Copy service file
cp com.smartrouter.api.plist ~/Library/LaunchAgents/

# Set permissions
chmod 644 ~/Library/LaunchAgents/com.smartrouter.api.plist

# Load the service
launchctl load ~/Library/LaunchAgents/com.smartrouter.api.plist

# Verify it loaded
launchctl list | grep smartrouter.api
```

### 5. Verify All Services Are Running

```bash
# Check status
launchctl list | grep -E 'dashboard|smartrouter'

# Or individually:
launchctl list com.dashboard.backend
launchctl list com.dashboard.frontend
launchctl list com.smartrouter.api
```

You should see output like:
```
	0	com.dashboard.backend
	0	com.dashboard.frontend
	0	com.smartrouter.api
```

The first number is the process ID (0 = not running yet, wait a few seconds).

### 6. Test Services

```bash
# Wait a few seconds for services to start
sleep 5

# Test smart-ai-router
curl http://localhost:8001/health

# Test dashboard backend
curl http://localhost:5050/health

# Test dashboard frontend
curl http://localhost:5173/
```

All should respond successfully.

### 7. Verify Logs

Service output is logged to:
- Backend: `/var/log/dashboard-backend.log`
- Frontend: `/var/log/dashboard-frontend.log`
- Smart-AI-Router: `/var/log/smartrouter-api.log`

View logs:
```bash
# Real-time backend logs
tail -f /var/log/dashboard-backend.log

# Real-time error logs
tail -f /var/log/dashboard-backend-error.log
```

---

## Managing Services

### Start a Service

```bash
launchctl start com.dashboard.backend
launchctl start com.dashboard.frontend
launchctl start com.smartrouter.api
```

### Stop a Service

```bash
launchctl stop com.dashboard.backend
launchctl stop com.dashboard.frontend
launchctl stop com.smartrouter.api
```

### Restart a Service

```bash
launchctl stop com.dashboard.backend && launchctl start com.dashboard.backend
```

### Unload (Disable) a Service

```bash
launchctl unload ~/Library/LaunchAgents/com.dashboard.backend.plist
launchctl unload ~/Library/LaunchAgents/com.dashboard.frontend.plist
launchctl unload ~/Library/LaunchAgents/com.smartrouter.api.plist
```

### Remove a Service Completely

```bash
# Stop it first
launchctl stop com.dashboard.backend

# Unload it
launchctl unload ~/Library/LaunchAgents/com.dashboard.backend.plist

# Delete the file
rm ~/Library/LaunchAgents/com.dashboard.backend.plist
```

### View All Running Services

```bash
launchctl list | grep -E 'dashboard|smartrouter'
```

---

## Troubleshooting

### Service Not Starting

Check the error log:
```bash
tail -20 /var/log/dashboard-backend-error.log
```

Common issues:
- **Port in use**: Another service is using 5050, 5173, or 8001
  ```bash
  lsof -i :5050
  # Kill the process if needed: kill -9 <PID>
  ```
- **Database not running**: PostgreSQL must be running
  ```bash
  brew services start postgresql@16
  ```
- **Path errors**: Check that all paths in `.plist` files are correct
- **Permissions**: Make sure log directory is writable
  ```bash
  sudo mkdir -p /var/log
  sudo chmod 755 /var/log
  ```

### Service Keeps Crashing

The launchd config has `KeepAlive=true` and `StartInterval=5`, so it will auto-restart every 5 seconds if it crashes.

To diagnose:
```bash
# Stop it so it doesn't auto-restart
launchctl stop com.dashboard.backend

# Run it manually to see errors
cd /path/to/dashboard/backend
npm start
```

### Viewing the UI After Auto-Start

Once services are running:
- **Frontend**: http://localhost:5173
- **Backend API**: http://localhost:5050
- **Smart-AI-Router**: http://localhost:8001

---

## Optional: Automated Installation Script

Here's a one-liner to install all services:

```bash
cd /path/to/smart-ai-router/services/launchd && \
cp com.dashboard.backend.plist com.dashboard.frontend.plist com.smartrouter.api.plist ~/Library/LaunchAgents/ && \
chmod 644 ~/Library/LaunchAgents/com.dashboard.*.plist ~/Library/LaunchAgents/com.smartrouter.*.plist && \
launchctl load ~/Library/LaunchAgents/com.dashboard.backend.plist && \
launchctl load ~/Library/LaunchAgents/com.dashboard.frontend.plist && \
launchctl load ~/Library/LaunchAgents/com.smartrouter.api.plist && \
echo "✅ All services loaded! Check status with: launchctl list | grep -E 'dashboard|smartrouter'"
```

---

## Auto-Start on Boot

Once services are loaded with `launchctl load`, they will:
- ✅ Start automatically when the Mac boots
- ✅ Auto-restart if they crash (every 5 seconds)
- ✅ Log output to `/var/log/*.log`
- ✅ Run with `RunAtLoad=true` in the plist files

No additional configuration needed!

---

## Next Steps

1. Build the dashboard frontend for production:
   ```bash
   cd dashboard/frontend
   npm run build
   ```

2. Build the dashboard backend:
   ```bash
   cd dashboard/backend
   npm run build
   ```

3. Follow the installation steps above to load all services

4. Reboot to verify auto-start:
   ```bash
   sudo reboot
   # After reboot, check: launchctl list | grep -E 'dashboard|smartrouter'
   ```

5. Access the UI at http://localhost:5173

---

**Your dashboard is now running 24/7!** 🎉
