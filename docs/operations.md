# Service management (macOS)

The setup wizard installs a LaunchAgent in `~/Library/LaunchAgents/com.smart-ai-router.plist`. Manage it with:

```bash
# Restart
launchctl kickstart -k gui/$(id -u)/com.smart-ai-router

# Stop
launchctl kill SIGTERM gui/$(id -u)/com.smart-ai-router

# Unload (disable)
launchctl unload ~/Library/LaunchAgents/com.smart-ai-router.plist

# View logs
tail -f /path/to/smart-ai-router/logs/server.log
tail -f /path/to/smart-ai-router/logs/server.err
```

## Pull & Restart

The dashboard's **Pull & Restart** button fast-forwards to `origin/main`, reinstalls
dependencies, and restarts the server. It finds its own launchd job by reading the
installed plists and matching the program against this interpreter — so a job named
anything at all is found, and `SMART_ROUTER_LABEL` is only needed for an install
launchd knows about but no plist describes (e.g. `launchctl submit`).

Restarting itself takes whichever of two routes the install allows:

| Install | Route |
|---|---|
| LaunchAgent (`gui/<uid>`) | `launchctl kickstart -k` — immediate |
| LaunchDaemon (`system`), or a kickstart that fails | exit, and let launchd's `KeepAlive` start the new code |

The second route is what makes a root-owned `/Library/LaunchDaemons` install
restartable without `sudo`. It only fires when launchd reports *this* pid as the
job's, which is the proof that something will start a replacement — a job without
`KeepAlive`, or a hand-started `python -m smart_ai_router`, reports `ok: false` with
the exact command to run instead rather than exiting into an outage.

