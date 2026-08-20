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

Restarting has two routes, because kickstart only covers one kind of install:

  1. `launchctl kickstart` the job — clean and immediate, but only possible for
     a job in this user's gui/user domain.
  2. Exit, and let launchd's own KeepAlive start a new process on the new code.
     This is the route that works for a root-owned system LaunchDaemon, which
     an unprivileged kickstart can never touch. Guarded by an interlock: launchd
     must report *this* pid as the job's, which is both proof that the job is
     supervising us and what keeps the route from firing in a test run or in a
     hand-started process, where exiting would just take the app down.
"""
from __future__ import annotations

import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parent.parent
APP_DAEMON_LABEL = os.environ.get("SMART_ROUTER_LABEL", "com.smart-ai-router")

# launchd domains to probe, in the order a self-restart would prefer. A job in
# gui/user domains can be kickstarted by this process; one registered in the
# `system` domain belongs to root and cannot (see _restart_target).
_USER_DOMAINS = ("gui", "user")

# Where launchd reads job definitions from, and the domain each directory
# implies. Scanned so the app can find its own job by what it *runs* rather than
# by a configured label — a label that doesn't match the installed plist is the
# exact reason the live deployment pulled fine and never restarted.
_PLIST_DIRS = (
    (Path("/Library/LaunchDaemons"), "system"),
    (Path.home() / "Library" / "LaunchAgents", "gui"),
    (Path("/Library/LaunchAgents"), "gui"),
)


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


def _job_print(domain: str, label: str) -> str | None:
    """launchd's description of `domain/label`, or None if it has no such job.

    Readable without privilege even for a root-owned system daemon, which is what
    makes both the existence check and the pid interlock available here.
    """
    proc = subprocess.run(
        ["launchctl", "print", f"{domain}/{label}"],
        capture_output=True, text=True,
    )
    return proc.stdout if proc.returncode == 0 else None


def _job_exists(domain: str, label: str) -> bool:
    """True if launchd has `label` registered in `domain`."""
    return _job_print(domain, label) is not None


def _job_pid(domain: str, label: str) -> int | None:
    """The pid launchd currently has for `domain/label`, or None.

    None also covers a job that is registered but not running, which is why this
    is a stronger signal than existence for "is that job *this* process".
    """
    printed = _job_print(domain, label)
    if printed is None:
        return None
    for line in printed.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "pid":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def _relaunches_on_exit(keep_alive) -> bool:
    """Whether launchd would start a new process if this one ended now.

    Only the unconditional form is trusted. The dictionary form is a set of
    conditions (network state, other jobs, exit status), and `SuccessfulExit:
    false` is the one that clearly covers dying on a signal; anything else is
    read as "don't gamble the app on an assumption" and reported instead.
    """
    if keep_alive is True:
        return True
    if isinstance(keep_alive, dict):
        return keep_alive.get("SuccessfulExit") is False
    return False


@dataclass(frozen=True)
class _Job:
    """A launchd job that runs this application."""
    label: str
    domain: str
    relaunch_on_exit: bool
    path: Path


def _runs_this_app(exe: str, args: list[str]) -> bool:
    """Whether a plist's program is this app rather than something adjacent.

    Keyed on the executable living in this interpreter's own bin directory,
    which covers both `python -m smart_ai_router` and the console script while
    excluding jobs that merely mention the app — e.g. the cloudflared tunnel
    plist on the live host, which names it in its config path.
    """
    if not exe:
        return False
    try:
        if Path(exe).parent.resolve() != Path(sys.executable).parent.resolve():
            return False
    except OSError:
        return False
    return any("smart_ai_router" in a or "smart-ai-router" in a for a in (exe, *args))


def _own_job() -> _Job | None:
    """The launchd job that runs this app, found by reading the installed plists."""
    for directory, kind in _PLIST_DIRS:
        try:
            paths = sorted(directory.glob("*.plist"))
        except OSError:
            continue
        for path in paths:
            try:
                data = plistlib.loads(path.read_bytes())
            except Exception:
                continue  # unreadable or malformed: not ours to reason about
            if not isinstance(data, dict):
                continue
            args = [str(a) for a in (data.get("ProgramArguments") or [])]
            exe = str(data.get("Program") or (args[0] if args else ""))
            label = str(data.get("Label") or "")
            if not label or not _runs_this_app(exe, args):
                continue
            domain = kind if kind == "system" else f"{kind}/{os.getuid()}"
            return _Job(
                label=label,
                domain=domain,
                relaunch_on_exit=_relaunches_on_exit(data.get("KeepAlive")),
                path=path,
            )
    return None


def _exit_for_relaunch(delay: float = 0.5, grace: float = 15.0) -> None:
    """End this process so launchd starts a new one, once the response is out.

    SIGTERM rather than a hard exit, so uvicorn drains what's in flight — but a
    long-lived stream must not be able to hold the restart open forever, hence
    the escalation. KeepAlive relaunches either way.
    """
    def _stop() -> None:
        time.sleep(delay)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(grace)
        os._exit(1)

    threading.Thread(target=_stop, daemon=True, name="restart").start()


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
    return _restart(pulled)


def _restart(pulled: str) -> dict:
    """Restart the app by whichever route this install actually allows."""
    job = _own_job()
    # The configured label first, so an explicit SMART_ROUTER_LABEL still wins;
    # then the label read off the installed plist, which is what makes this work
    # on a host whose daemon is named something else entirely.
    target = _restart_target()
    if target is None and job is not None:
        target = _restart_target(job.label)

    if target is not None and target[0] != "system":
        # gui/user domain — we own this job and can restart it. Note this kills
        # the current process, so the HTTP response may never reach the client;
        # the UI treats a dropped connection here as success and polls for the
        # app's return.
        domain, label = target
        kick = subprocess.run(
            ["launchctl", "kickstart", "-k", f"{domain}/{label}"],
            capture_output=True, text=True,
        )
        if kick.returncode == 0:
            return {"ok": True, "detail": f"Pulled {pulled} and restarting…"}
        err = (kick.stderr or kick.stdout or "").strip()[:200]
        blocked = f"restart failed: {err or 'kickstart failed'}"
        hint = f"launchctl kickstart -k {domain}/{label}"
    elif target is not None:
        # A root-owned LaunchDaemon: kickstart from this process is denied.
        blocked = (
            f"restart needs root — {target[1]} is a system LaunchDaemon"
        )
        hint = f"sudo launchctl kickstart -k system/{target[1]}"
    else:
        blocked = f"no launchd job named {APP_DAEMON_LABEL!r} was found"
        hint = "Set SMART_ROUTER_LABEL to the daemon's label, then restart it."

    # Nothing kickstartable, so ask launchd for a new process the other way: end
    # this one and let KeepAlive replace it. Only when launchd confirms this pid
    # is the job's — otherwise exiting is just an outage.
    if job is not None and job.relaunch_on_exit and _job_pid(job.domain, job.label) == os.getpid():
        _exit_for_relaunch()
        return {
            "ok": True,
            "detail": (
                f"Pulled {pulled} and restarting… (exiting so launchd relaunches "
                f"{job.label} on the new code)"
            ),
        }

    return {
        "ok": False,
        "detail": (
            f"Pulled {pulled}, but the app could not restart itself: {blocked}. "
            "New code is on disk and takes effect on next restart."
        ),
        "hint": hint,
    }
