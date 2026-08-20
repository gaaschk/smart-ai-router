"""Tests for the self-update path (git pull + daemon restart).

These cover the failure modes observed on the live deployment, where the
"Pull & Restart" button reported success and changed nothing:

  1. `git merge --ff-only` refused because an untracked file would be
     overwritten — git reports that on *stdout*, which the old code dropped.
  2. The configured daemon label didn't exist in launchd.
  3. The daemon was a root-owned system LaunchDaemon, so an unprivileged
     kickstart could never work.

In all three the old implementation returned ok=True, "restarting…" because it
fired Popen() and never looked at the result. Each must now be ok=False with an
actionable detail.

(2) and (3) are also the live host's actual configuration, so there is a second
group here: the app finds its own job by reading the installed plists rather
than trusting a label, and restarts a job it can't kickstart by exiting into
launchd's KeepAlive. Every test in that group pins the interlock as much as the
feature — a self-exit that fires when launchd is *not* supervising this process
is an outage, and the suite itself is one of the processes it must not kill.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from smart_ai_router import updates


class _Proc:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_git(responses):
    """Route git calls by subcommand; unlisted ones succeed with empty output."""
    def _git(*args):
        for key, proc in responses.items():
            if key in args:
                return proc
        return _Proc()
    return _git


# ── Merge failures must be reported verbatim ─────────────────────────────────

def test_merge_blocked_by_untracked_file_reports_git_message(monkeypatch):
    """The real production blocker. git puts this on stdout, not stderr."""
    stdout = (
        "error: The following untracked working tree files would be "
        "overwritten by merge:\n\tscripts/bakeoff_classifier.py\n"
        "Please move or remove them before you merge.\nAborting"
    )
    monkeypatch.setattr(
        updates, "_git",
        _fake_git({"merge": _Proc(returncode=1, stdout=stdout, stderr="")}),
    )
    out = updates.apply_source_update()
    assert out["ok"] is False
    # The actionable part — which file to delete — must survive.
    assert "bakeoff_classifier.py" in out["detail"]


def test_merge_failure_on_stderr_also_reported(monkeypatch):
    monkeypatch.setattr(
        updates, "_git",
        _fake_git({"merge": _Proc(returncode=1, stderr="fatal: not possible to fast-forward")}),
    )
    out = updates.apply_source_update()
    assert out["ok"] is False
    assert "fast-forward" in out["detail"]


def test_fetch_failure_short_circuits(monkeypatch):
    monkeypatch.setattr(
        updates, "_git",
        _fake_git({"fetch": _Proc(returncode=1, stderr="could not resolve host")}),
    )
    out = updates.apply_source_update()
    assert out["ok"] is False and "could not resolve host" in out["detail"]


# ── Restart-target resolution ────────────────────────────────────────────────

def _no_pip(monkeypatch):
    """Neutralize the uv dep-reinstall so tests don't shell out."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())


def _no_job(monkeypatch):
    """No launchd job runs this app — the plist scan reads real directories, and
    on the deployment host it finds a real job, so say so explicitly."""
    monkeypatch.setattr(updates, "_own_job", lambda: None)


def test_missing_launchd_job_is_reported_not_silently_ok(monkeypatch):
    monkeypatch.setattr(updates, "_git", _fake_git({}))
    monkeypatch.setattr(updates, "_job_exists", lambda domain, label: False)
    _no_job(monkeypatch)
    _no_pip(monkeypatch)

    out = updates.apply_source_update()
    assert out["ok"] is False
    assert "could not restart" in out["detail"].lower()
    # Code did land on disk — the message must not imply the pull was lost.
    assert "next restart" in out["detail"]


def test_system_daemon_needs_sudo_and_gets_a_command(monkeypatch):
    """A root-owned LaunchDaemon can't be kickstarted by this process."""
    monkeypatch.setattr(updates, "_git", _fake_git({}))
    monkeypatch.setattr(
        updates, "_job_exists",
        lambda domain, label: domain == "system",
    )
    _no_job(monkeypatch)
    _no_pip(monkeypatch)
    monkeypatch.setattr(updates, "APP_DAEMON_LABEL", "com.example.router")

    out = updates.apply_source_update()
    assert out["ok"] is False
    assert "root" in out["detail"].lower()
    # The hint must be runnable as-is.
    assert out["hint"] == "sudo launchctl kickstart -k system/com.example.router"


def test_user_domain_job_is_kickstarted_and_reports_success(monkeypatch):
    monkeypatch.setattr(updates, "_git", _fake_git({"rev-parse": _Proc(stdout="abcdef1234\n")}))
    monkeypatch.setattr(updates, "_job_exists", lambda domain, label: domain.startswith("gui/"))
    _no_job(monkeypatch)
    monkeypatch.setattr(updates, "APP_DAEMON_LABEL", "com.example.router")

    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        return _Proc()
    monkeypatch.setattr(subprocess, "run", _run)

    out = updates.apply_source_update()
    assert out["ok"] is True
    kick = [c for c in calls if c and c[0] == "launchctl"]
    assert kick, "never attempted a restart"
    assert kick[0][:3] == ["launchctl", "kickstart", "-k"]
    assert kick[0][3].startswith("gui/")
    assert kick[0][3].endswith("/com.example.router")
    assert "abcdef1" in out["detail"]  # pulled revision surfaced


def test_kickstart_failure_is_surfaced(monkeypatch):
    """A restart that errors must not be reported as a restart."""
    monkeypatch.setattr(updates, "_git", _fake_git({}))
    monkeypatch.setattr(updates, "_job_exists", lambda domain, label: domain.startswith("gui/"))
    _no_job(monkeypatch)

    def _run(cmd, **kwargs):
        if cmd and cmd[0] == "launchctl":
            return _Proc(returncode=1, stderr="Could not find service")
        return _Proc()
    monkeypatch.setattr(subprocess, "run", _run)

    out = updates.apply_source_update()
    assert out["ok"] is False
    assert "Could not find service" in out["detail"]
    assert out["hint"].startswith("launchctl kickstart")


def test_user_domain_preferred_over_system(monkeypatch):
    """When a job exists in both, restart the one we can actually control."""
    monkeypatch.setattr(updates, "_job_exists", lambda domain, label: True)
    domain, _ = updates._restart_target("com.example.router")
    assert domain.startswith("gui/")


# ── Finding our own job by what it runs ──────────────────────────────────────

def _write_plist(directory: Path, name: str, body: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.plist"
    path.write_bytes(plistlib.dumps(body))
    return path


def _app_plist(label="com.example.router", keep_alive=True) -> dict:
    """A plist shaped like the live host's: this venv's python, -m module."""
    return {
        "Label": label,
        "ProgramArguments": [sys.executable, "-m", "smart_ai_router"],
        "KeepAlive": keep_alive,
    }


def _scan(monkeypatch, *dirs):
    monkeypatch.setattr(updates, "_PLIST_DIRS", tuple(dirs))


def test_own_job_is_found_by_its_program_not_by_the_configured_label(monkeypatch, tmp_path):
    """The live failure: the plist is named com.kevingaasch.smart-ai-router while
    the code defaults to com.smart-ai-router, so a label lookup finds nothing and
    the pull silently never restarts. Reading the plist removes the guess."""
    daemons = tmp_path / "LaunchDaemons"
    _write_plist(daemons, "com.kevingaasch.smart-ai-router",
                 _app_plist(label="com.kevingaasch.smart-ai-router"))
    _scan(monkeypatch, (daemons, "system"))
    monkeypatch.setattr(updates, "APP_DAEMON_LABEL", "com.smart-ai-router")

    job = updates._own_job()
    assert job is not None
    assert job.label == "com.kevingaasch.smart-ai-router"
    assert job.domain == "system"
    assert job.relaunch_on_exit is True


def test_a_job_that_merely_mentions_the_app_is_not_ours(monkeypatch, tmp_path):
    """The cloudflared tunnel on the live host is called
    com.cloudflare.smart-ai-router and names the app in its config path.
    Kickstarting or killing *that* would take the tunnel down instead."""
    agents = tmp_path / "LaunchAgents"
    _write_plist(agents, "com.cloudflare.smart-ai-router", {
        "Label": "com.cloudflare.smart-ai-router",
        "ProgramArguments": ["/opt/homebrew/bin/cloudflared", "tunnel", "run",
                             "--config", "/etc/smart-ai-router.yml"],
        "KeepAlive": True,
    })
    _scan(monkeypatch, (agents, "gui"))
    assert updates._own_job() is None


def test_a_launch_agent_resolves_to_this_users_gui_domain(monkeypatch, tmp_path):
    agents = tmp_path / "LaunchAgents"
    _write_plist(agents, "com.example.router", _app_plist())
    _scan(monkeypatch, (agents, "gui"))
    job = updates._own_job()
    assert job is not None and job.domain == f"gui/{os.getuid()}"


def test_a_malformed_plist_is_skipped_not_fatal(monkeypatch, tmp_path):
    """launchd directories are shared with every other app on the machine."""
    daemons = tmp_path / "LaunchDaemons"
    daemons.mkdir()
    (daemons / "broken.plist").write_text("not a plist at all")
    _write_plist(daemons, "com.example.router", _app_plist())
    _scan(monkeypatch, (daemons, "system"))
    job = updates._own_job()
    assert job is not None and job.label == "com.example.router"


def test_keepalive_forms_that_do_and_do_not_guarantee_a_relaunch():
    assert updates._relaunches_on_exit(True) is True
    # Conditions launchd evaluates itself. Only "relaunch when it exits badly"
    # clearly covers dying on SIGTERM; the rest must not be gambled on.
    assert updates._relaunches_on_exit({"SuccessfulExit": False}) is True
    assert updates._relaunches_on_exit({"SuccessfulExit": True}) is False
    assert updates._relaunches_on_exit({"NetworkState": True}) is False
    assert updates._relaunches_on_exit(False) is False
    assert updates._relaunches_on_exit(None) is False


# ── Restarting by exiting into KeepAlive ─────────────────────────────────────

def _system_job(monkeypatch, *, keep_alive=True, pid=None):
    """A root-owned daemon that launchd reports as running under `pid`."""
    job = updates._Job(
        label="com.kevingaasch.smart-ai-router",
        domain="system",
        relaunch_on_exit=keep_alive,
        path=Path("/Library/LaunchDaemons/com.kevingaasch.smart-ai-router.plist"),
    )
    monkeypatch.setattr(updates, "_own_job", lambda: job)
    monkeypatch.setattr(
        updates, "_job_pid",
        lambda domain, label: os.getpid() if pid is None else pid,
    )
    return job


def _watch_exit(monkeypatch) -> list[bool]:
    exits: list[bool] = []
    monkeypatch.setattr(updates, "_exit_for_relaunch", lambda *a, **k: exits.append(True))
    return exits


def test_a_system_daemon_restarts_itself_by_exiting(monkeypatch):
    """The live host's configuration. An unprivileged kickstart is denied, but
    ending the process is not — launchd's KeepAlive starts the new code."""
    monkeypatch.setattr(updates, "_git", _fake_git({"rev-parse": _Proc(stdout="abcdef1234\n")}))
    monkeypatch.setattr(updates, "_job_exists", lambda domain, label: domain == "system")
    _no_pip(monkeypatch)
    _system_job(monkeypatch)
    exits = _watch_exit(monkeypatch)

    out = updates.apply_source_update()
    assert out["ok"] is True
    assert exits == [True]
    assert "abcdef1" in out["detail"]
    assert "com.kevingaasch.smart-ai-router" in out["detail"]


def test_self_exit_is_refused_when_launchd_is_not_supervising_this_process(monkeypatch):
    """The interlock. Without the pid check this path would end a hand-started
    process — or the test runner — and call it a restart."""
    monkeypatch.setattr(updates, "_git", _fake_git({}))
    monkeypatch.setattr(updates, "_job_exists", lambda domain, label: domain == "system")
    _no_pip(monkeypatch)
    _system_job(monkeypatch, pid=os.getpid() + 1000)
    exits = _watch_exit(monkeypatch)

    out = updates.apply_source_update()
    assert out["ok"] is False
    assert exits == []
    assert out["hint"].startswith("sudo launchctl kickstart")


def test_self_exit_is_refused_when_keepalive_would_not_relaunch(monkeypatch):
    """Exiting a job launchd won't restart is an outage, not an update."""
    monkeypatch.setattr(updates, "_git", _fake_git({}))
    monkeypatch.setattr(updates, "_job_exists", lambda domain, label: domain == "system")
    _no_pip(monkeypatch)
    _system_job(monkeypatch, keep_alive=False)
    exits = _watch_exit(monkeypatch)

    out = updates.apply_source_update()
    assert out["ok"] is False and exits == []
    assert "root" in out["detail"].lower()


def test_a_failed_kickstart_falls_back_to_exiting(monkeypatch):
    """A wrong label, a job booted out from under us — the second route doesn't
    depend on either being right."""
    monkeypatch.setattr(updates, "_git", _fake_git({}))
    monkeypatch.setattr(updates, "_job_exists", lambda domain, label: domain.startswith("gui/"))
    job = updates._Job(label="com.example.router", domain=f"gui/{os.getuid()}",
                       relaunch_on_exit=True, path=Path("/tmp/x.plist"))
    monkeypatch.setattr(updates, "_own_job", lambda: job)
    monkeypatch.setattr(updates, "_job_pid", lambda domain, label: os.getpid())
    exits = _watch_exit(monkeypatch)

    def _run(cmd, **kwargs):
        if cmd and cmd[0] == "launchctl":
            return _Proc(returncode=1, stderr="Could not find service")
        return _Proc()
    monkeypatch.setattr(subprocess, "run", _run)

    out = updates.apply_source_update()
    assert out["ok"] is True and exits == [True]


def test_a_kickstartable_job_is_not_restarted_by_exiting(monkeypatch):
    """Kickstart when it works: no throttle interval, no window where the app is
    simply down. Exiting is the fallback, not the default."""
    monkeypatch.setattr(updates, "_git", _fake_git({}))
    monkeypatch.setattr(updates, "_job_exists", lambda domain, label: domain.startswith("gui/"))
    _system_job(monkeypatch)  # supervised and KeepAlive — still must not be used
    exits = _watch_exit(monkeypatch)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())

    out = updates.apply_source_update()
    assert out["ok"] is True and exits == []


def test_job_pid_is_read_from_launchctl_print(monkeypatch):
    """Readable without privilege even for a system daemon, which is the whole
    reason the interlock is available to an unprivileged process."""
    printed = (
        "system/com.example.router = {\n\tactive count = 1\n\tstate = running\n"
        "\truns = 64\n\tpid = 4242\n\tproperties = keepalive | runatload\n}"
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _Proc(stdout=printed),
    )
    assert updates._job_pid("system", "com.example.router") == 4242


def test_job_pid_is_none_when_launchd_does_not_know_the_job(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _Proc(returncode=1, stderr="Could not find service"),
    )
    assert updates._job_pid("system", "nope") is None


# ── Status reporting ─────────────────────────────────────────────────────────

def test_status_reports_behind_count(monkeypatch):
    monkeypatch.setattr(
        updates, "_git",
        _fake_git({
            "rev-parse": _Proc(stdout="1111111aaaa\n"),
            "rev-list": _Proc(stdout="0\t3\n"),
        }),
    )
    out = updates.source_update_status()
    assert out["ok"] is True
    assert out["behind"] == 3 and out["update_available"] is True


def test_status_fetch_failure_reports_git_output(monkeypatch):
    monkeypatch.setattr(
        updates, "_git",
        _fake_git({"fetch": _Proc(returncode=1, stderr="network unreachable")}),
    )
    out = updates.source_update_status()
    assert out["ok"] is False and "network unreachable" in out["detail"]
