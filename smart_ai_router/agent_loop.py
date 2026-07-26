"""The agent tool-calling loop for filesystem-enabled chat.

When a chat request opts into agent mode, the proxy advertises the filesystem
tools (tools.py) to a tool-capable model and then runs this loop:

  1. Send messages (+ tool schemas) to the provider.
  2. If the model returns tool_calls, execute each against the caller's
     workspace, append the results as `tool` messages, and go to 1.
  3. When the model answers without tool calls, that's the final response.

This is the classic OpenAI function-calling loop (option 1 in the Claude API
skill's taxonomy — a manual loop we own), specialized to our proxy: it reuses
the same provider-forwarding path as a normal completion, so routing, keys, and
timeouts behave identically. It is capped at a max number of rounds so a model
that loops forever can't wedge a request.

The loop streams *narration* of tool activity to the UI as SSE deltas (so the
user sees "running: read_file(...)"), then streams the model's final answer.
Intermediate model turns that only call tools are not shown verbatim — only
their tool activity is surfaced.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Callable

from smart_ai_router import tools as _tools

# Hard ceiling on tool-call rounds per request. Generous enough for real
# multi-step tasks, low enough to bound cost and stop runaway loops.
_MAX_ROUNDS = 12


def _sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


def _content_delta(text: str) -> bytes:
    """An OpenAI-style streaming chunk carrying assistant content."""
    return _sse({"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})


def _tool_args(call: dict) -> dict:
    """Parse a tool call's JSON arguments, tolerating malformed/empty input."""
    raw = (call.get("function") or {}).get("arguments") or "{}"
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


async def run_agent_loop(
    *,
    user: str,
    body: dict[str, Any],
    tool_schemas: list[dict],
    call_model: Callable[[dict], Any],
    narrate: bool = True,
) -> AsyncIterator[bytes]:
    """Drive the tool-calling loop, yielding SSE bytes for the client.

    Args:
        user: caller identity — scopes every tool to their workspace.
        body: the OpenAI chat request (messages, model, etc.). Copied, not
              mutated.
        tool_schemas: tool definitions to advertise (from tools.tool_schemas()).
        call_model: async callable taking a request body and returning the
                    provider's parsed JSON response (non-streaming). Supplied by
                    the proxy so routing/keys/timeouts are reused.
        narrate: if True, emit human-readable tool-activity deltas to the UI.

    Yields SSE `data:` frames: tool-activity narration, then the final answer,
    then `[DONE]`.
    """
    messages = list(body.get("messages", []))
    req = {**body, "tools": tool_schemas, "stream": False}
    req.pop("tool_choice", None)

    for _round in range(_MAX_ROUNDS):
        req["messages"] = messages
        try:
            data = await call_model(req)
        except Exception as exc:  # noqa: BLE001 — surface provider errors to the client
            yield _sse({"error": f"agent loop provider error: {exc}"})
            yield b"data: [DONE]\n\n"
            return

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            # Final answer — stream its content and finish.
            content = message.get("content") or ""
            if content:
                yield _content_delta(content)
            yield _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            yield b"data: [DONE]\n\n"
            return

        # Record the assistant's tool-call turn, then execute each call.
        messages.append(message)
        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            args = _tool_args(call)
            if narrate:
                yield _content_delta(_narration(name, args))
            result = _tools.execute_tool(user, name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": result,
            })

    # Ran out of rounds without a final answer.
    yield _content_delta("\n\n_[agent stopped: reached the maximum number of tool-call rounds]_")
    yield _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
    yield b"data: [DONE]\n\n"


def _narration(name: str, args: dict) -> str:
    """A short, human-readable line describing a tool call, for the UI."""
    if name == "list_dir":
        return f"\n\n`📂 list_dir({args.get('path', '') or '.'})`\n\n"
    if name == "read_file":
        return f"\n\n`📄 read_file({args.get('path', '')})`\n\n"
    if name == "write_file":
        return f"\n\n`✏️ write_file({args.get('path', '')})`\n\n"
    if name == "edit_file":
        return f"\n\n`✏️ edit_file({args.get('path', '')})`\n\n"
    if name == "run_bash":
        return f"\n\n`⚡ run_bash: {args.get('command', '')}`\n\n"
    return f"\n\n`🔧 {name}(...)`\n\n"
