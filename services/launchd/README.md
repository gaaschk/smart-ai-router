# Launchd Services for macOS

This directory contains macOS launchd service configuration files to run the Unified Dashboard services as background processes that auto-start on boot.

## Quick Start

### One-Command Installation

```bash
cd services/launchd
./install.sh
```

This will:
- Copy service files to `~/Library/LaunchAgents/`
- Load all three services
- Verify they're running
- Display next steps

### One-Command Uninstall

```bash
./install.sh --uninstall
```

## What Gets Installed

| Service | Label | Port | Purpose |
|---------|-------|------|---------|
| Dashboard Backend | `com.dashboard.backend` | 5050 | Node.js Express API |
| Dashboard Frontend | `com.dashboard.frontend` | 5173 | React SPA (served) |
| Smart-AI-Router | `com.smartrouter.api` | 8001 | Python Uvicorn LLM router |

## Service Files

- **`com.dashboard.backend.plist`** — Backend configuration
  - Runs Node.js dashboard backend
  - Auto-restarts on crash
  - Logs to `/var/log/dashboard-backend.log`

- **`com.dashboard.frontend.plist`** — Frontend configuration
  - Serves production-built React app
  - Requires `npm install -g serve`
  - Logs to `/var/log/dashboard-frontend.log`

- **`com.smartrouter.api.plist`** — Smart-AI-Router configuration
  - Runs Python smart-ai-router API
  - Auto-restarts on crash
  - Logs to `/var/log/smartrouter-api.log`

## Installation Methods

### Method 1: Automated Script (Recommended)

```bash
./install.sh
```

### Method 2: Manual Installation

See [INSTALL.md](./INSTALL.md) for step-by-step instructions.

## Configuration

Before installation, edit the `.plist` files to:

1. **Update Paths**: Replace `/Users/kevingaasch/ProjectHome/smart-ai-router` with your actual path
   ```xml
   <string>/your/actual/path/to/smart-ai-router</string>
   ```

2. **Set Secrets**: Replace placeholder values
   ```xml
   <key>AUTH_JWT_SECRET</key>
   <string>CHANGE_ME_IN_PRODUCTION</string>
   ```

3. **Add API Keys** (for smart-ai-router):
   ```xml
   <key>OPENAI_API_KEY</key>
   <string>sk-your-actual-key</string>
   ```

Or use `./install.sh` which will preserve your edits.

## Management

### View Status
```bash
launchctl list | grep -E 'dashboard|smartrouter'
```

### Start a Service
```bash
launchctl start com.dashboard.backend
```

### Stop a Service
```bash
launchctl stop com.dashboard.backend
```

### Restart a Service
```bash
launchctl stop com.dashboard.backend && launchctl start com.dashboard.backend
```

### View Logs
```bash
tail -f /var/log/dashboard-backend.log
tail -f /var/log/dashboard-backend-error.log
tail -f /var/log/dashboard-frontend.log
tail -f /var/log/smartrouter-api.log
```

## Troubleshooting

### Services Won't Start

Check permissions on log directory:
```bash
sudo mkdir -p /var/log
sudo chmod 755 /var/log
```

Check for port conflicts:
```bash
lsof -i :5050  # Backend
lsof -i :5173  # Frontend
lsof -i :8001  # Smart-AI-Router
```

### Database Connection Failed

Ensure PostgreSQL is running:
```bash
brew services start postgresql@16
psql -d dashboard -c "SELECT 1"  # Test connection
```

### Services Not Auto-Starting on Boot

Verify files are installed correctly:
```bash
ls -la ~/Library/LaunchAgents/ | grep dashboard
```

If missing, run `./install.sh` again.

## Production Notes

⚠️ **Before deploying to production:**

1. **Change secrets** in all `.plist` files:
   - `AUTH_JWT_SECRET` — Use a strong random value
   - API keys — Use production keys, not dev keys

2. **Update paths** to match your deployment location

3. **Review log rotation** — Set up logrotate or similar for `/var/log/*.log`

4. **Use environment-specific config** — Create separate plist files for staging/prod

5. **Monitor services** — Set up monitoring/alerting for process restarts

## Advanced Usage

### Custom Plist Creation

To create a custom service, copy an existing `.plist` and modify:

```xml
<key>Label</key>
<string>com.custom.service</string>
<key>ProgramArguments</key>
<array>
    <string>/path/to/executable</string>
    <string>arg1</string>
    <string>arg2</string>
</array>
```

Then load it:
```bash
launchctl load ~/Library/LaunchAgents/com.custom.service.plist
```

### Environment Variables in Launchd

Use the `<key>EnvironmentVariables</key>` section:

```xml
<key>EnvironmentVariables</key>
<dict>
    <key>VAR_NAME</key>
    <string>value</string>
</dict>
```

### Changing Log Levels

Edit the `EnvironmentVariables` section of any `.plist`:

```xml
<key>LOG_LEVEL</key>
<string>debug</string>  <!-- or info, warn, error -->
```

## See Also

- [DEPLOYMENT_GUIDE.md](../../DEPLOYMENT_GUIDE.md) — Full deployment instructions
- [INSTALL.md](./INSTALL.md) — Detailed manual installation steps
- [QUICK_START.md](../../QUICK_START.md) — Quick reference

---

**Questions?** Check the logs or see the troubleshooting section in INSTALL.md.
