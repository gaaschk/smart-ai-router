# Manual Launchd Installation

`./install.sh` (see [README.md](./README.md)) handles this automatically;
this doc is for doing it by hand or understanding what the script does.

## Prerequisites

- macOS 12+
- smart-ai-router already set up (`smart-ai-router setup` has been run at
  least once, so provider credentials exist in the local SQLite store)

## Steps

1. Edit `com.smart-ai-router.plist` in this directory to match your actual
   repo path if it differs from `/Users/kevingaasch/ProjectHome/smart-ai-router`.

2. Copy it into place and load it:
   ```bash
   cp com.smart-ai-router.plist ~/Library/LaunchAgents/
   chmod 644 ~/Library/LaunchAgents/com.smart-ai-router.plist
   launchctl load ~/Library/LaunchAgents/com.smart-ai-router.plist
   launchctl list | grep smart-ai-router
   ```

3. Wait a few seconds, then test:
   ```bash
   sleep 5
   curl http://localhost:8001/health
   ```

4. Logs:
   ```bash
   tail -f ~/Library/Logs/dashboard/smartrouter.log
   ```

## Managing the service

```bash
launchctl start com.smart-ai-router
launchctl stop com.smart-ai-router
launchctl stop com.smart-ai-router && launchctl start com.smart-ai-router   # restart
```

## Removing it

```bash
launchctl stop com.smart-ai-router
launchctl unload ~/Library/LaunchAgents/com.smart-ai-router.plist
rm ~/Library/LaunchAgents/com.smart-ai-router.plist
```

Or just run `./install.sh --uninstall`.

## Troubleshooting

- **Port 8001 in use**: `lsof -i :8001`, then `kill -9 <PID>` if it's a stray
  process. Also check for the system-level LaunchDaemon
  (`/Library/LaunchDaemons/com.kevingaasch.smart-ai-router.plist`), which
  runs independently of this per-user LaunchAgent and will respawn the
  process if it's the one managing it.
- **Path errors**: double-check the paths inside the `.plist` file.
- **Permissions**: make sure the log directory
  (`~/Library/Logs/dashboard/`) is writable.

## See Also

- [GBrain deployment](../../docs/gbrain-deployment.md)
- [Service management](../../docs/operations.md)
