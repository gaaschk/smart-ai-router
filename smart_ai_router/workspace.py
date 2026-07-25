"""Per-user filesystem workspaces for the agent (read/write/bash) tools.

Every authenticated identity gets its own directory under a configurable root
(SMART_ROUTER_WORKSPACE_DIR, default ~/.smart_ai_router_workspaces). The agent's
filesystem tools operate *only* inside that directory — this module's whole job
is to turn a model-supplied relative path into an absolute path that is proven
to stay inside the user's jail, or refuse it.

Why a jail at all: this deployment sits behind a public tunnel and is
multi-user. Without a jail, a `read_file("../../.env")` from the model would
hand out the admin/OpenRouter keys, and one user could read another's files.
The jail is the security boundary for read/write; bash needs the *additional*
OS-level sandbox (see sandbox.py) because a shell can do far more than open
files.

Path resolution defends against traversal (`..`), absolute-path escapes
(`/etc/passwd`), and symlink escapes (a link inside the workspace pointing
out): the final resolved real path must live under the resolved workspace root,
checked with a boundary test that can't be fooled by a shared name prefix.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_DEFAULT_ROOT = "~/.smart_ai_router_workspaces"

# A user identity becomes a directory name; keep it filesystem-safe and prevent
# an identity from itself being a traversal payload. Anything outside this set
# collapses to "_", and the whole thing is length-capped.
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]")

# The admin identity ("" pre-auth, or "admin") shares one workspace. Empty
# string (open/no-auth mode) also lands here so a keyless local run still works.
_ADMIN_SLUG = "admin"


class WorkspaceError(Exception):
    """A path escaped its workspace, or the workspace can't be resolved."""


def workspace_root() -> Path:
    """Root holding every per-user workspace dir (created on first use)."""
    raw = os.environ.get("SMART_ROUTER_WORKSPACE_DIR", _DEFAULT_ROOT)
    root = Path(raw).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _slug(user: str) -> str:
    """Filesystem-safe directory name for an identity.

    Admin and open (no-auth) requests collapse to a single shared "admin"
    workspace; everyone else gets a sanitized, length-capped slug of their name.
    """
    name = (user or "").strip()
    if not name or name == _ADMIN_SLUG:
        return _ADMIN_SLUG
    slug = _SAFE_SEGMENT.sub("_", name)[:64]
    # Guard the degenerate cases where sanitizing emptied it or left only dots
    # (".", "..") — either would be a traversal or collision hazard.
    return slug if slug.strip(".") else _ADMIN_SLUG


def user_workspace(user: str) -> Path:
    """Absolute path to a user's workspace directory (created on first use)."""
    ws = workspace_root() / _slug(user)
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def resolve_in_workspace(user: str, rel_path: str) -> Path:
    """Resolve a model-supplied path to an absolute path inside the user's jail.

    `rel_path` is treated as relative to the workspace root even if it looks
    absolute (a leading "/" means "workspace root", not the real filesystem
    root) — so the model can't reach outside by passing "/etc/passwd". After
    resolving symlinks, the real path must sit inside the resolved workspace, or
    WorkspaceError is raised.
    """
    ws = user_workspace(user).resolve()

    candidate = (rel_path or "").strip()
    # Strip leading slashes so an "absolute"-looking path is re-rooted at the
    # workspace rather than the real FS root.
    candidate = candidate.lstrip("/")
    if not candidate:
        return ws  # empty / "/" refers to the workspace root itself

    target = (ws / candidate).resolve()
    if not _is_within(target, ws):
        raise WorkspaceError(f"path escapes workspace: {rel_path!r}")
    return target


def _is_within(path: Path, root: Path) -> bool:
    """True if `path` is `root` or lives under it (boundary-safe).

    Uses path-part containment rather than string prefixing, so a sibling like
    ".../admin-evil" is NOT considered inside ".../admin".
    """
    if path == root:
        return True
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
