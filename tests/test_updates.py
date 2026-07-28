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
"""
from __future__ import annotations

import subprocess

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


def test_missing_launchd_job_is_reported_not_silently_ok(monkeypatch):
    monkeypatch.setattr(updates, "_git", _fake_git({}))
    monkeypatch.setattr(updates, "_job_exists", lambda domain, label: False)
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
