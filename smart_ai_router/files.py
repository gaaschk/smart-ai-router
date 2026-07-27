"""Filesystem-backed file storage for uploads.

Blobs live on disk under a configurable root (SMART_ROUTER_FILES_DIR, default
~/.smart_ai_router_files); the database holds only metadata (see FileRecord).
Disk is cheap and this keeps large uploads out of the DB.

IDs follow OpenAI's convention ("file-<token>") so OpenAI-compatible clients —
and claudish downstream of them — work unchanged.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from smart_ai_router import settings as _settings

_FILE_PREFIX = "file-"


def files_dir() -> Path:
    """Root directory for stored blobs (created on first use).

    Env-only (SMART_ROUTER_FILES_DIR): a filesystem path is intrinsic to the
    machine, not application policy, so it stays out of the UI settings.
    """
    raw = os.environ.get("SMART_ROUTER_FILES_DIR", "~/.smart_ai_router_files")
    d = Path(raw).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def max_file_bytes() -> int:
    """Upload size ceiling in bytes. UI-managed (Settings page) with
    SMART_ROUTER_MAX_FILE_MB as env fallback (default 512)."""
    return max(1, _settings.get_int("max_file_mb")) * 1024 * 1024


def generate_file_id() -> str:
    """OpenAI-style opaque file id, e.g. 'file-9f3a...'."""
    return f"{_FILE_PREFIX}{secrets.token_hex(16)}"


def _is_valid_id(file_id: str) -> bool:
    """Guard against path traversal — ids are prefix + lowercase hex only."""
    if not file_id.startswith(_FILE_PREFIX):
        return False
    token = file_id[len(_FILE_PREFIX):]
    return bool(token) and all(c in "0123456789abcdef" for c in token)


def blob_path(file_id: str) -> Path:
    """Absolute on-disk path for a file id. Raises on a malformed id."""
    if not _is_valid_id(file_id):
        raise ValueError(f"invalid file id: {file_id!r}")
    return files_dir() / file_id


def write_blob(file_id: str, data: bytes) -> Path:
    """Persist bytes for a file id and return the path."""
    path = blob_path(file_id)
    path.write_bytes(data)
    return path


def read_blob(file_id: str) -> bytes:
    """Read the stored bytes for a file id."""
    return blob_path(file_id).read_bytes()


def delete_blob(file_id: str) -> bool:
    """Remove the on-disk blob. Returns False if it was already gone."""
    path = blob_path(file_id)
    if path.exists():
        path.unlink()
        return True
    return False
