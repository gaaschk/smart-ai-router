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

import asyncio
import json
from typing import Any, AsyncIterator, Callable

from smart_ai_router import tools as _tools

# Hard ceiling on tool-call rounds per request. Generous enough for real
# multi-step tasks, low enough to bound cost and stop runaway loops.
_MAX_ROUNDS = 12

# Shown when a model round returns no content and no tool calls. Emitting
# *something* matters: a blank assistant bubble is indistinguishable from a
# crashed stream, and the empty turn used to be appended to the conversation
# history, degrading every later turn.
_EMPTY_ANSWER_NOTE = (
    "_[no answer returned — the model produced no output. This usually means "
    "the output token budget was exhausted (often by a reasoning model's "
    "thinking tokens). Try again, or raise max_tokens.]_"
)


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


def _accumulate_tool_call(slots: dict[int, dict], frag: dict) -> None:
    """Fold one streamed tool_call fragment into the per-index accumulator.

    OpenAI-style streaming splits a tool call across chunks: the first fragment
    carries the id and function name, later fragments append `arguments` text.
    We key by `index` and concatenate.
    """
    idx = frag.get("index", 0)
    slot = slots.setdefault(
        idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
    )
    if frag.get("id"):
        slot["id"] = frag["id"]
    if frag.get("type"):
        slot["type"] = frag["type"]
    fn = frag.get("function") or {}
    if fn.get("name"):
        slot["function"]["name"] = fn["name"]
    if fn.get("arguments"):
        slot["function"]["arguments"] += fn["arguments"]


async def run_agent_loop(
    *,
    user: str,
    body: dict[str, Any],
    tool_schemas: list[dict],
    call_model: Callable[[dict], Any] | None = None,
    stream_model: Callable[[dict], AsyncIterator[dict]] | None = None,
    narrate: bool = True,
    register_file: Callable[[bytes, str, str], str] | None = None,
) -> AsyncIterator[bytes]:
    """Drive the tool-calling loop, yielding SSE bytes for the client.

    Args:
        user: caller identity — scopes every tool to their workspace.
        body: the OpenAI chat request (messages, model, etc.). Copied, not
              mutated.
        tool_schemas: tool definitions to advertise (from tools.tool_schemas()).
        stream_model: preferred. Async callable taking a request body and
                    yielding the provider's streamed `choices[0].delta` dicts.
                    Lets the model's tokens reach the user as they generate, so
                    agent mode feels like plain chat when no tool fires.
        call_model: fallback. Async callable returning the provider's parsed
                    (non-streaming) JSON response. Used only when stream_model
                    is not supplied. One of the two is required.
        narrate: if True, emit human-readable tool-activity deltas to the UI.
        register_file: optional callback (data, filename, mime) -> file_id, so
                    create_document can register its output for download.

    Yields SSE `data:` frames: streamed answer/narration, then `[DONE]`.
    """
    messages = list(body.get("messages", []))
    req = {**body, "tools": tool_schemas}
    req.pop("tool_choice", None)

    for _round in range(_MAX_ROUNDS):
        req["messages"] = messages
        # holder[0] receives the reassembled assistant message for this round.
        holder: list[dict] = [{}]
        streamed_any = False
        try:
            if stream_model is not None:
                async for out in _stream_round(stream_model, req, holder):
                    # Content bytes pass straight through to the client; the
                    # first content byte flips streamed_any so we don't re-emit
                    # the buffered answer later.
                    streamed_any = True
                    yield out
                message = holder[0]
            else:
                data = await call_model(req)
                message = (data.get("choices") or [{}])[0].get("message") or {}
        except Exception as exc:  # noqa: BLE001 — surface provider errors to the client
            yield _sse({"error": f"agent loop provider error: {exc}"})
            yield b"data: [DONE]\n\n"
            return

        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            # Final answer. If we streamed it live, tokens already went out;
            # otherwise emit the buffered content now.
            if not streamed_any:
                content = message.get("content") or ""
                if content:
                    yield _content_delta(content)
                else:
                    # The round produced neither tool calls nor text. Left
                    # silent this renders as an empty bubble that looks like the
                    # chat died; say so instead. Most common cause is the output
                    # budget being consumed by a reasoning model's thinking
                    # tokens (finish_reason "length").
                    yield _content_delta(_EMPTY_ANSWER_NOTE)
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
            # execute_tool is synchronous and can block for a long time
            # (run_bash shells out with a wall-clock timeout; file tools do real
            # disk IO). Calling it directly would stall the whole event loop —
            # freezing every other user's stream *and* preventing the SSE
            # heartbeat from firing, which reads to the client as a hang. Run it
            # on a worker thread so the loop stays responsive.
            result = await asyncio.to_thread(
                _tools.execute_tool, user, name, args, register_file=register_file
            )
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "content": result,
            })

    # Ran out of rounds without a final answer.
    yield _content_delta("\n\n_[agent stopped: reached the maximum number of tool-call rounds]_")
    yield _sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]})
    yield b"data: [DONE]\n\n"


async def _stream_round(
    stream_model: Callable[[dict], AsyncIterator[dict]],
    req: dict,
    holder: list[dict],
) -> AsyncIterator[bytes]:
    """Run one streaming model round.

    Yields SSE content-delta bytes as the model emits text (passed straight to
    the client), while reassembling the full assistant message — content plus
    any fragmented tool_calls — into holder[0] for the loop to act on.
    """
    content_parts: list[str] = []
    tool_slots: dict[int, dict] = {}

    async for delta in stream_model(req):
        if not isinstance(delta, dict):
            continue
        text = delta.get("content")
        if text:
            content_parts.append(text)
            yield _content_delta(text)
        for frag in delta.get("tool_calls") or []:
            _accumulate_tool_call(tool_slots, frag)

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts) or None,
    }
    if tool_slots:
        message["tool_calls"] = [tool_slots[i] for i in sorted(tool_slots)]
    holder[0] = message


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
    if name == "create_document":
        return f"\n\n`📝 create_document({args.get('path', '')})`\n\n"
    if name == "run_bash":
        return f"\n\n`⚡ run_bash: {args.get('command', '')}`\n\n"
    return f"\n\n`🔧 {name}(...)`\n\n"
