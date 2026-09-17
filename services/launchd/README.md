# Launchd Service for macOS

This directory contains the macOS launchd service configuration for
smart-ai-router, so it runs as a background process and auto-starts on boot.

(The unified dashboard that used to live alongside this has been retired —
its features are now part of the built-in web UI at `http://localhost:8001`.)

## Quick Start

### One-Command Installation

```bash
cd services/launchd
./install.sh
```

This will:
- Copy `com.smart-ai-router.plist` to `~/Library/LaunchAgents/`
- Load the service
- Verify it's running
- Test connectivity on port 8001

### One-Command Uninstall

```bash
./install.sh --uninstall
```

## What Gets Installed

| Service | Label | Port | Purpose |
|---------|-------|------|---------|
| Smart-AI-Router | `com.smart-ai-router` | 8001 | Python Uvicorn LLM router + web UI |

## Configuration

Before installation, edit `com.smart-ai-router.plist` to:

1. **Update paths**: Replace `/Users/kevingaasch/ProjectHome/smart-ai-router`
   with your actual path.
2. **Add API keys**: provider credentials are normally set via
   `smart-ai-router setup` and stored in the local SQLite store, but the
   plist's `EnvironmentVariables` section can also carry secrets like
   `OPENROUTER_API_KEY` if needed.

## Management

```bash
# View status
launchctl list | grep smart-ai-router

# Start / stop / restart
launchctl start com.smart-ai-router
launchctl stop com.smart-ai-router
launchctl stop com.smart-ai-router && launchctl start com.smart-ai-router

# View logs
tail -f ~/Library/Logs/dashboard/smartrouter.log
```

## Troubleshooting

### Service won't start

Check for a port conflict:
```bash
lsof -i :8001
```

Note: a separate system-level LaunchDaemon
(`/Library/LaunchDaemons/com.kevingaasch.smart-ai-router.plist`) may also
manage this process on some machines (with `KeepAlive: true`), independent
of the per-user LaunchAgent here — check both if the service seems to be
respawning old code after a deploy.

### Service not auto-starting on boot

Verify the file is installed:
```bash
ls -la ~/Library/LaunchAgents/ | grep smart-ai-router
```

If missing, run `./install.sh` again.

## See Also

- [GBrain deployment](../../docs/gbrain-deployment.md) — how GBrain is set up alongside this service
- [Service management](../../docs/operations.md) — the macOS LaunchAgent, Pull & Restart
