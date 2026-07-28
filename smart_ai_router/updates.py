"""
Update monitoring — decoupled from any CI runner.

The running app checks on demand whether updates are available from origin/main,
and can deliberately apply a source update (git pull + restart) when asked via
the UI. No push-based CI runner required.

Honesty contract: apply_source_update() reports ok=True only when the pull
*and* the restart request both actually succeeded. Every failure path returns
ok=False with a `detail` the UI can show, and `hint` carrying a copy-pasteable
command when the fix needs a human (e.g. sudo). Previously this fired the
restart blind and always claimed success, so a blocked merge or a failed
kickstart both looked like "Restarting…" and the update banner simply came
back with no explanation.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
APP_DAEMON_LABEL = os.environ.get("SMART_ROUTER_LABEL", "com.smart-ai-router")

# launchd domains to probe, in the order a self-restart would prefer. A job in
# gui/user domains can be kickstarted by this process; one registered in the
# `system` domain belongs to root and cannot (see _restart_target).
_USER_DOMAINS = ("gui", "user")


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)


def _git_error(proc: subprocess.CompletedProcess, limit: int = 300) -> str:
    """The most informative message from a failed git call.

    git writes some failures to stdout and others to stderr (a merge blocked by
    untracked files puts the useful part on stdout), so prefer whichever is
    non-empty rather than assuming stderr.
    """
    for stream in (proc.stderr, proc.stdout):
        text = (stream or "").strip()
        if text:
            return text[:limit]
    return f"git exited {proc.returncode}"


def source_update_status(fetch: bool = True) -> dict:
    """Compare local HEAD against origin/main."""
    if fetch:
        f = _git("fetch", "origin", "main")
        if f.returncode != 0:
            return {"ok": False, "detail": f"git fetch failed: {_git_error(f, 200)}"}

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


def _job_exists(domain: str, label: str) -> bool:
    """True if launchd has `label` registered in `domain`."""
    proc = subprocess.run(
        ["launchctl", "print", f"{domain}/{label}"],
        capture_output=True, text=True,
    )
    return proc.returncode == 0


def _restart_target(label: str = "") -> tuple[str, str] | None:
    """Locate the launchd job for this app as (domain, label), or None.

    The label is configurable because deployments name their daemon whatever
    they like (SMART_ROUTER_LABEL). The *domain* has to be discovered: the same
    app may be installed as a per-user LaunchAgent (gui/user domain, restartable
    by this process) or as a system LaunchDaemon (root-owned, not restartable
    without sudo), and kickstarting the wrong domain fails with a bare
    "Could not find service".
    """
    lbl = label or APP_DAEMON_LABEL
    if not lbl:
        return None
    uid = os.getuid()
    for domain in _USER_DOMAINS:
        if _job_exists(f"{domain}/{uid}", lbl):
            return f"{domain}/{uid}", lbl
    if _job_exists("system", lbl):
        return "system", lbl
    return None


def apply_source_update() -> dict:
    """Pull latest source and restart the app daemon. Deliberate, on-demand.

    Returns {ok, detail} and, when a human has to finish the job, `hint` with
    the exact command to run.
    """
    f = _git("fetch", "origin", "main")
    if f.returncode != 0:
        return {"ok": False, "detail": f"fetch failed: {_git_error(f, 200)}"}

    merge = _git("merge", "--ff-only", "origin/main")
    if merge.returncode != 0:
        # Surface git's own words. The common real-world causes are a diverged
        # branch and untracked files that the incoming commit would overwrite —
        # indistinguishable under the old generic message, and the second one
        # is fixed by simply deleting the named files.
        return {
            "ok": False,
            "detail": f"git merge --ff-only failed: {_git_error(merge)}",
        }

    # Reinstall deps in case pyproject.toml changed.
    uv = str(Path.home() / ".local/bin/uv")
    subprocess.run([uv, "pip", "install", "-e", str(ROOT)], capture_output=True, text=True)

    pulled = _git("rev-parse", "HEAD").stdout.strip()[:7]
    target = _restart_target()
    if target is None:
        return {
            "ok": False,
            "detail": (
                f"Pulled {pulled}, but no launchd job named "
                f"{APP_DAEMON_LABEL!r} was found, so the app could not restart "
                "itself. New code is on disk and takes effect on next restart."
            ),
            "hint": "Set SMART_ROUTER_LABEL to the daemon's label, then restart it.",
        }

    domain, label = target
    if domain == "system":
        # A root-owned LaunchDaemon. This process runs unprivileged, so
        # kickstart would fail with EPERM; say so plainly instead of pretending.
        return {
            "ok": False,
            "detail": (
                f"Pulled {pulled}. Restart needs root: {label} is a system "
                "LaunchDaemon, so the app cannot restart itself. New code is "
                "on disk and takes effect on next restart."
            ),
            "hint": f"sudo launchctl kickstart -k system/{label}",
        }

    # gui/user domain — we own this job and can restart it. Note this kills the
    # current process, so the HTTP response may never reach the client; the UI
    # treats a dropped connection here as success and polls for the app's
    # return. Any *error* we can still report means the restart did not happen.
    kick = subprocess.run(
        ["launchctl", "kickstart", "-k", f"{domain}/{label}"],
        capture_output=True, text=True,
    )
    if kick.returncode != 0:
        err = (kick.stderr or kick.stdout or "").strip()[:200]
        return {
            "ok": False,
            "detail": f"Pulled {pulled} but restart failed: {err or 'kickstart failed'}",
            "hint": f"launchctl kickstart -k {domain}/{label}",
        }
    return {"ok": True, "detail": f"Pulled {pulled} and restarting…"}
