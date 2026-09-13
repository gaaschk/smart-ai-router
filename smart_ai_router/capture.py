"""Sampled capture of real tool-loop turns, so a bakeoff can replay them.

Why this exists: nothing in the store keeps a prompt. usage_log records tokens,
cost and the routed model (store/sqlite_store.py:95); chat_messages holds web-UI
conversations, which carry no tool calls. So the only corpus available for
judging whether a non-Claude model can hold a tool loop was one written by hand
(scripts/bakeoff_orchestrator.py), and a synthetic corpus cannot settle the
question it was invented for — the first run of it scored all three Claude
incumbents as failing a case they had answered correctly.

Three limits, each load-bearing rather than cautious:

  * **Admin only, and not as a setting.** A captured turn is the prompt verbatim
    — file contents, paths, whatever was pasted. `wanted()` takes the user and
    refuses anyone else, so no percent typed into the Settings page can start
    recording someone else's prompts.
  * **Tool-bearing turns only.** A request that offers no tools cannot exercise
    the thing being measured, and the requests that offer none are the chat page
    — ordinary conversation. Enforced at the call site, which is the only place
    that knows.
  * **The reference is the tool calls, not the answer.** A replay needs to know
    what the incumbent *did*: the tool it chose and the arguments it passed. The
    prose is not stored, only its length, so this stays a corpus rather than a
    transcript archive.

JSONL beside the DB rather than a table: the only consumer is a script, rows are
append-only and nothing in the UI queries them, and one rotation at MAX_BYTES
bounds the disk with no schema migration.
"""
from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import Any

from smart_ai_router import settings as _settings

# The only user whose prompts are ever written. Deliberately a constant — see
# the module docstring.
CAPTURE_USER = "admin"

# Rotate at this size, keeping one previous generation. Two files bound the disk;
# a turn with a large pasted file in it is a few hundred KB, so this is a few
# hundred turns of history.
MAX_BYTES = 8 * 1024 * 1024


def capture_path() -> Path:
    """Where captures are appended.

    Env-only (SMART_ROUTER_CAPTURE_FILE), like SMART_ROUTER_FILES_DIR: a
    filesystem path is intrinsic to the machine, not application policy.
    """
    raw = os.environ.get(
        "SMART_ROUTER_CAPTURE_FILE", "~/.smart_ai_router_captures.jsonl"
    )
    return Path(raw).expanduser()


def percent() -> int:
    """Sample rate, 0-100. UI-managed; 0 (the default) means capture nothing."""
    return max(0, min(100, _settings.get_int("capture_turn_percent")))


def wanted(user: str) -> bool:
    """Whether to capture this request. Cheap, and checked before the response is
    inspected, so an uncaptured turn costs a settings read and nothing else."""
    pct = percent()
    if pct <= 0 or user != CAPTURE_USER:
        return False
    return pct >= 100 or random.random() * 100 < pct


def record(
    *,
    lane: str,
    routed_model: str,
    messages: list[dict],
    tools: list[dict] | None,
    tool_calls: list[dict],
    content_len: int,
) -> bool:
    """Append one turn; return whether it was written.

    Never raises. This is instrumentation on the response path, and a full disk
    must cost a sample, not the reply.
    """
    row: dict[str, Any] = {
        "lane": lane,
        "routed_model": routed_model,
        "messages": messages,
        "tools": tools or [],
        # The incumbent's behavior, in the shape the response gave it, so the
        # bakeoff scores a replay with the same function it scores a live call.
        "reference": {"tool_calls": tool_calls, "content_len": content_len},
    }
    try:
        path = capture_path()
        if path.exists() and path.stat().st_size > MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


def load(path: Path | None = None) -> list[dict]:
    """Every captured turn, oldest first.

    A malformed line is skipped rather than failing the corpus: a truncated last
    line is what a rotation mid-write looks like, and one bad row should not cost
    the other four hundred.
    """
    p = path or capture_path()
    if not p.exists():
        return []
    rows: list[dict] = []
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("messages"):
                rows.append(row)
    return rows
