"""
Update monitoring — decoupled from any CI runner.

The running app checks on demand whether updates are available from origin/main,
and can deliberately apply a source update (git pull + restart) when asked via
the UI. No push-based CI runner required.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
APP_DAEMON_LABEL = "com.kevingaasch.smart-ai-router"


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def source_update_status(fetch: bool = True) -> dict:
    """Compare local HEAD against origin/main."""
    if fetch:
        f = _git("fetch", "origin", "main")
        if f.returncode != 0:
            return {"ok": False, "detail": f"git fetch failed: {f.stderr.strip()[:200]}"}

    local = _git("rev-parse", "HEAD").stdout.strip()
    remote = _git("rev-parse", "origin/main").stdout.strip()
    if not local or not remote:
        return {"ok": False, "detail": "could not resolve HEAD / origin/main"}

    counts = _git("rev-list", "--left-right", "--count", "HEAD...origin/main").stdout.strip()
    ahead = behind = 0
    if counts:
        parts = counts.split()
        if len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])

    return {
        "ok": True,
        "local": local[:7],
        "remote": remote[:7],
        "behind": behind,
        "ahead": ahead,
        "update_available": behind > 0,
        "detail": "up to date" if behind == 0 else f"{behind} commit(s) behind origin/main",
    }


def apply_source_update() -> dict:
    """Pull latest source and restart the app daemon. Deliberate, on-demand."""
    f = _git("fetch", "origin", "main")
    if f.returncode != 0:
        return {"ok": False, "detail": f"fetch failed: {f.stderr.strip()[:200]}"}

    merge = _git("merge", "--ff-only", "origin/main")
    if merge.returncode != 0:
        return {"ok": False, "detail": "cannot fast-forward (local diverged from origin/main)"}

    # Reinstall deps in case pyproject.toml changed.
    uv = str(Path.home() / ".local/bin/uv")
    subprocess.run(
        [uv, "pip", "install", "-e", str(ROOT)],
        capture_output=True, text=True,
    )

    # Restart via scoped passwordless-sudo rule (see scripts/sudoers-setup.sh).
    # This kills the current process — the HTTP response may not return; that's expected.
    subprocess.Popen(
        ["sudo", "-n", "/bin/launchctl", "kickstart", "-k", f"system/{APP_DAEMON_LABEL}"]
    )
    return {"ok": True, "detail": "Pulled latest and restarting…"}
