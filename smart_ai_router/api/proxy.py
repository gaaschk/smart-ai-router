"""
OpenAI-compatible proxy endpoint.

Every POST /v1/chat/completions is:
  1. Classified (domain + complexity) from the last user message.
  2. Routed to the cheapest-qualifying model via CapabilityRouter.
  3. Forwarded to the real provider with async httpx, streaming back verbatim.

Routing modes (selected by the incoming `model` name). Both classify the prompt
and route on the resulting profile; they differ only in the candidate pool:
  smart-orchestrator  -> Claude models only (reliable skill/workflow tool-calling)
  smart-worker / *    -> every model in scope, cheapest that qualifies

Supported provider prefixes in the routed model value:
  openrouter/<vendor>/<model>  -> https://openrouter.ai/api/v1
  ollama/<model>               -> stored ollama base_url (default http://localhost:11434)
  bedrock/<model>              -> https://bedrock-runtime.{region}.amazonaws.com/v1
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import re
import sys
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from smart_ai_router import helper_models as _helpers
from smart_ai_router import overhead as _overhead
from smart_ai_router import public_access as _public
from smart_ai_router import self_signup as _signup
from smart_ai_router import settings as _settings
from smart_ai_router.agent_loop import run_agent_loop
from smart_ai_router.classifier import classify_profile, is_actionable
from smart_ai_router.fileref import FileRefError, contains_image, resolve_file_refs
from smart_ai_router.llm_classifier import (
    ClassifierTarget,
    classifier_fallback_model,
    classifier_model,
    classify_profile_two_speed,
)
from smart_ai_router.models import ModelSpec, UsageRecord
from smart_ai_router.ratelimit import check_rate_limit, window_start_for
from smart_ai_router.scope import ModelScope, parse_scope
from smart_ai_router.taxonomy import DomainNeed, PromptProfile
from smart_ai_router.tools import tool_schemas as _tool_schemas

proxy_router = APIRouter()


def _agent_tool_schemas() -> list[dict]:
    """Tools advertised to the model in agent mode.

    Read + write are always offered; bash is included only when the OS sandbox
    is actually available (tools.tool_schemas gates it on sandbox.available()).
    """
    return _tool_schemas(allow_write=True, allow_bash=None)

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Model-name markers that force the orchestrator (Claude) path.
_ORCHESTRATOR_MARKERS = ("smart-orchestrator", "orchestrator")

def _default_max_tokens() -> int:
    """Output-token ceiling applied when a caller omits max_tokens.

    max_tokens caps *output* only (for reasoning models: thinking + answer), so
    a stingy default silently truncates responses — the common empty-content /
    finish_reason:"length" failure. OpenAI/ChatGPT treat max_tokens as optional
    (unbounded up to context), but many providers here default low, so we set a
    generous floor that leaves room for a reasoning budget plus a real answer.

    UI-managed (Settings page) with SMART_ROUTER_DEFAULT_MAX_TOKENS as env
    fallback, so a deployment hitting truncation can raise it without a deploy.
    """
    return max(1, _settings.get_int("default_max_tokens"))


# Header the chat page sends on its own requests. The distinction matters because
# the two kinds of caller want opposite things from us: a browser wants a rendered
# document, while a program driving `/v1` wants exactly the messages it sent and
# nothing else — an injected system turn changes its output, and because the turn
# becomes conversation history it keeps changing it for the rest of the session.
# So the capability note below is browser-only, and this is how we can tell.
_UI_CLIENT_HEADER = "x-smart-router-client"
_UI_CLIENT_VALUE = "ui"


def _is_ui_client(request) -> bool:
    return (request.headers.get(_UI_CLIENT_HEADER) or "").strip().lower() \
        == _UI_CLIENT_VALUE


def _rich_output_preamble() -> str:
    """A system turn telling the model what the chat page can actually render.

    Without it a model has no way to know, and defaults to plain prose — which is
    why asking for something visual produced no visuals. Nothing here asks for
    decoration: it says what the surface supports and leaves the judgement of
    whether a diagram helps to the model, because a mandatory chart on a prompt
    that didn't want one is worse than no chart.

    UI-managed so the wording is tunable without a deploy, and blankable — an
    operator who wants the model unprompted sets it to empty.
    """
    return (_settings.get("chat_rich_output_prompt") or "").strip()


def _todays_date_note() -> str:
    """A system turn stating today's date.

    A model has no clock. Asked what is current it answers from training and calls
    a 2024 season "current" in 2026, with no way to notice — the reported bug, and
    half of it is fixed by this one line, no search required. Knowing the date is
    also what lets a model say "my information may be out of date" instead of
    asserting a stale fact, and what makes "this year" resolvable at all.

    Server-side rather than in the tunable preamble because it has to be computed
    per request, and unconditional because a wrong date is never the better input.
    """
    return (
        f"Today's date is {_dt.date.today().isoformat()}. Your training data has a "
        "cutoff before this. For anything that can change over time — who holds a "
        "position, prices, counts, standings, what is 'current' or 'latest' — say "
        "plainly that your information may be out of date, and give the date your "
        "figure refers to rather than calling it current."
    )


def _web_search_plugin(profile, model_value: str) -> list[dict] | None:
    """OpenRouter's web plugin, when the prompt needs facts newer than the model.

    Search runs provider-side: OpenRouter does the retrieval, puts the results in
    front of the model, and returns citations as message annotations. That is the
    reason this is a body field and not a tool in tools.py — a `web_search` tool
    would mean an agent loop, several round trips, and a search API key of our own,
    to arrive at the same place.

    Returns None when it can't or shouldn't run, and the caller reports which:
    OpenRouter-only, since a local Ollama model and Bedrock's OpenAI-compatible
    endpoint both ignore `plugins`. Answering unsearched is the right fallback —
    the alternative is refusing a question the model can still partly answer — but
    it is only honest if the caller can tell, hence X-Web-Search.
    """
    if not _settings.get_bool("web_search_enabled"):
        return None
    if profile is None or not profile.needs_current_info():
        return None
    if not model_value.startswith("openrouter/"):
        return None
    return [{
        "id": "web",
        "max_results": max(1, _settings.get_int("web_search_max_results")),
    }]


def _output_budget(profile, spec=None) -> int:
    """Output ceiling for a caller who didn't name one, given what they asked for.

    One number cannot serve both "what's the capital of France" and "write me a
    short story". Sized for the first, the second comes back a paragraph long and
    cut mid-sentence; sized for the second, every trivial question carries a
    budget it will never use — which costs nothing directly, since billing follows
    the tokens actually written, but on a reasoning model an oversized budget is an
    invitation to think for a while.

    So the prompt profile decides. It already knows the answer is a document (see
    `PromptProfile.is_long_form`), and it knew it *before* this function existed —
    the information was there and simply wasn't being used. Never lowers the
    ordinary default: an operator who raised that meant it.

    Then the model's own ceiling clamps whatever we arrived at, because asking for
    more than a model can emit is not a harmless overshoot — several providers
    reject the call, which turns a long answer into no answer at all. Output limits
    on the live catalog run from 2048 to 1.8M, so this is the difference between a
    generous setting and a broken one. `max_output == 0` means the catalog didn't
    say; we send the budget unclamped, which is right for local models (Ollama
    treats max_tokens as num_predict and just stops).
    """
    base = _default_max_tokens()
    want = base
    if profile is not None and profile.is_long_form():
        want = max(base, _settings.get_int("long_form_max_tokens"))
    limit = int(getattr(spec, "max_output", 0) or 0)
    if limit > 0:
        want = min(want, limit)
    return max(1, want)


# Thinking knobs, in every spelling a client might use: OpenAI's
# `reasoning_effort`, OpenRouter's `reasoning` config object and its legacy
# `include_reasoning` bool, and Anthropic's `thinking` (which is what a
# translation layer in front of the router forwards).
_REASONING_PARAMS = ("reasoning", "reasoning_effort", "include_reasoning", "thinking")


def _drop_unsupported(forward_body: dict, spec) -> list[str]:
    """Remove params the *routed* model can't take. Returns what was removed.

    The caller names a model class ("smart-worker") and never learns which model
    answered, so every model-specific knob in its body is a guess about a pick it
    can't see — and a wrong guess is not ignored, it is a provider 400 that turns
    a routed request into no answer at all:

        reasoning_effort + a coding prompt → ollama/qwen3-coder:30b
        → 400 '"qwen3-coder:30b" does not support thinking'

    Only the flags the catalog actually tracks can be filtered honestly, so this
    is deliberately narrow — a knob we have no capability bit for is left alone
    rather than guessed at. An unknown spec (`None`) is treated as incapable:
    dropping a preference degrades the answer, a 400 removes it.
    """
    if getattr(spec, "reasoning", False):
        return []
    dropped = [p for p in _REASONING_PARAMS if p in forward_body]
    for p in dropped:
        forward_body.pop(p)
    return dropped


# ── prompt caching ────────────────────────────────────────────────────────────
# The single largest cost lever this router has, and for a long time the one it
# wasn't pulling. Measured from the live usage log: 92% of lifetime spend was one
# five-minute coding session — 52 requests, median 69,571 prompt tokens each,
# every one of them re-sending the same growing prefix, `cached_tokens: 0`
# throughout. Routing had nothing left to give there (the pick was already Haiku,
# the cheapest Claude), so the money was never in *which* model answered. It was
# in being billed full price for the same 60k tokens 52 times.
#
# Anthropic caches only what you mark; OpenRouter passes `cache_control` through
# to it. A client that sets its own breakpoints loses them to the Anthropic→OpenAI
# translation layer in front of the router (LiteLLM strips every one), so by the
# time a body arrives here the intent is gone and no client can restore it. The
# router is the last place that can.

# Minimum prompt Anthropic will cache at all: 1024 tokens on Sonnet/Opus, 2048 on
# Haiku. Below it a breakpoint is ignored rather than an error, so this guard is
# about not writing pointless markers — and it takes the larger number because
# cheapest-qualified-wins routes to Haiku often.
_CACHE_MIN_TOKENS = 2048


def _supports_cache_control(routed_model: str) -> bool:
    """Whether an explicit cache breakpoint does anything for this model.

    Claude is the family that *requires* one: OpenAI, Grok and DeepSeek cache long
    prefixes automatically on OpenRouter, and Ollama has no prompt cache to mark.
    So the set of models a breakpoint helps is exactly the Claude family — which
    its id names precisely.

    OpenRouter only. Bedrock also serves Claude and also has prompt caching, but
    through its own `cachePoint` shape, and whether our OpenAI-compatible path to
    it honors `cache_control` is unverified — same call as `structured_outputs` in
    sync.py, where the safe direction is to not claim a capability rather than to
    trust one.

    ponytail: an id substring, not a stored capability. The honest signal is
    OpenRouter's per-model `pricing.input_cache_read`, which sync doesn't keep —
    add a column when a second family needs explicit breakpoints.
    """
    m = routed_model.lower()
    return m.startswith("openrouter/") and "claude" in m


def _cache_marked(msg: dict) -> dict | None:
    """`msg` with a cache breakpoint on its last content block, or None if it has
    no content to hang one on (an assistant turn that is nothing but tool_calls).

    Copies rather than mutating: these dicts come straight from the request body
    and are shared with it.
    """
    content = msg.get("content")
    if isinstance(content, str):
        if not content.strip():
            return None
        return {**msg, "content": [{
            "type": "text", "text": content,
            "cache_control": {"type": "ephemeral"},
        }]}
    if isinstance(content, list):
        for i in range(len(content) - 1, -1, -1):
            block = content[i]
            if isinstance(block, dict) and block.get("type"):
                blocks = list(content)
                blocks[i] = {**block, "cache_control": {"type": "ephemeral"}}
                return {**msg, "content": blocks}
    return None


def _inject_cache_breakpoints(
    forward_body: dict, routed_model: str, est_tokens: int, *, loops: bool = False
) -> int:
    """Mark this request's stable prefix as cacheable. Returns breakpoints set.

    Two of them, which is the standard shape for a growing conversation:

    1. **End of the system block.** Anthropic's cache prefix is ordered
       tools → system → messages, so a breakpoint here also covers the tool
       definitions — which is where the tokens actually are. (Measured on a real
       Claude Code request: 146,296 of 153,507 bytes were tool schemas, ~40k
       tokens, against ~1.9k tokens of conversation.) That matters because a
       breakpoint on `tools` itself isn't expressible in the OpenAI wire shape,
       and this makes one unnecessary.
    2. **End of the history.** Rolling: what this turn writes, the next turn
       reads, since each turn's prefix contains the last one's.

    Three guards, each of which is a way injecting could *cost* money or break:

    - Second turn onward only. A cache write is billed at 1.25×, a read at 0.10×,
      so a marker nobody comes back to read is a 25% surcharge. An assistant turn
      in the history is proof the client re-sends its prefix, which is the thing
      that makes a write pay for itself. `loops=True` is the same proof arrived at
      differently: agent mode re-sends the whole prefix on every round of its tool
      loop, so even a first turn is guaranteed to read back what it writes — and
      that loop is precisely the traffic shape the burst was made of.
    - Above `_CACHE_MIN_TOKENS`, below which Anthropic ignores the marker anyway.
      `est_tokens` counts messages only, so tools are excluded and the estimate
      errs low — the guard is conservative in the harmless direction.
    - Never over a caller that set its own breakpoints. It knows its prefix better
      than this heuristic does; the only reason to guess is that its intent
      usually doesn't survive translation.
    """
    if not _settings.get_bool("prompt_caching"):
        return 0
    if not _supports_cache_control(routed_model) or est_tokens < _CACHE_MIN_TOKENS:
        return 0
    msgs = list(forward_body.get("messages") or [])
    if not loops and not any(m.get("role") == "assistant" for m in msgs):
        return 0
    if any(
        isinstance(b, dict) and "cache_control" in b
        for m in msgs
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
    ):
        return 0

    marked: set[int] = set()
    system_i = max(
        (i for i, m in enumerate(msgs) if m.get("role") == "system"), default=-1
    )
    if system_i >= 0:
        stamped = _cache_marked(msgs[system_i])
        if stamped is not None:
            msgs[system_i] = stamped
            marked.add(system_i)
    # Backwards, because the final message can be an assistant turn carrying only
    # tool_calls — nothing to mark, but the turn before it will have content.
    for i in range(len(msgs) - 1, -1, -1):
        if i in marked:
            break
        stamped = _cache_marked(msgs[i])
        if stamped is not None:
            msgs[i] = stamped
            marked.add(i)
            break

    if marked:
        forward_body["messages"] = msgs
    return len(marked)


# Seconds of silence in an SSE stream before we emit a keepalive comment. A
# model round (especially the first token of a slow reasoning model) can take
# many seconds during which the agent loop yields nothing; without a heartbeat
# the client's bubble looks frozen. An SSE comment line (":\n\n") is ignored by
# the OpenAI wire format but proves the connection is alive.
_HEARTBEAT_SECS = 10.0


async def _with_heartbeat(
    gen: AsyncIterator[bytes], interval: float = _HEARTBEAT_SECS
) -> AsyncIterator[bytes]:
    """Yield from `gen`, injecting an SSE keepalive comment after every
    `interval` seconds of silence so a slow round never looks like a hang.

    Implementation note — why a pump task and a queue instead of the obvious
    `wait_for(shield(anext(...)))`:

    `asyncio.shield` deliberately leaves the inner `__anext__()` task *running*
    when the outer `wait_for` times out (that's the point — we don't want a
    heartbeat tick to cancel an in-flight provider read). But an async
    generator may not be closed while one of its `__anext__()` calls is still
    in flight: when the client disconnects mid-stream, Starlette calls
    `aclose()` on us, that propagates to the shielded generator, and CPython
    raises

        RuntimeError: aclose(): asynchronous generator is already running

    The generator then never unwinds, so the underlying httpx stream is never
    released and the response wedges — the client's bubble stops updating and
    the request hangs until the read timeout. (Observed in production as
    "Task exception was never retrieved ... aclose(): asynchronous generator is
    already running".)

    Instead we drain `gen` in a dedicated task that owns it exclusively, and
    communicate over a queue. Only the pump ever calls `__anext__`, so a
    heartbeat tick can never overlap iteration, and cancellation has exactly
    one owner: on client disconnect our `finally` cancels the pump and awaits
    it, which closes `gen` from the same task that was iterating it.
    """
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=1)

    async def _pump() -> None:
        """Drain `gen` into the queue, then post a terminal sentinel."""
        try:
            async for chunk in gen:
                await queue.put(("chunk", chunk))
            await queue.put(("end", None))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — forwarded to the consumer
            await queue.put(("error", exc))

    pump = asyncio.ensure_future(_pump())
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                # Silence from the provider — prove the connection is alive.
                yield b": keepalive\n\n"
                continue
            if kind == "chunk":
                yield payload
            elif kind == "end":
                return
            else:
                raise payload
    finally:
        # Client disconnect, downstream error, or normal completion: make sure
        # the pump (and therefore `gen`) is torn down before we return, so the
        # provider connection is released rather than leaked.
        if not pump.done():
            pump.cancel()
        try:
            await pump
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await gen.aclose()


# ── helpers ───────────────────────────────────────────────────────────────────

def _estimate_tokens(text: str) -> int:
    """Rough token count from character length (~4 chars/token), the same
    heuristic used for est_tokens routing below. Used as a fallback when a
    streaming provider doesn't return a usage block."""
    return max(0, len(str(text)) // 4)


class _StreamUsageScanner:
    """Incrementally scans an OpenAI SSE byte stream (fed a copy of the raw
    chunks) to recover token usage without disturbing the forwarded bytes.

    OpenAI-compatible backends, when sent stream_options.include_usage, emit a
    final data chunk carrying a top-level `usage` block (with choices: []). We
    remember it. Providers that ignore the flag emit none, so we also accumulate
    streamed delta content length as an estimate fallback.
    """

    def __init__(self) -> None:
        self._buf = ""
        self.usage: dict | None = None
        self._content_len = 0

    def feed(self, chunk: bytes) -> None:
        """Feed one raw network chunk. Parses only whole lines; a partial
        trailing line is retained until the rest arrives."""
        self._buf += chunk.decode("utf-8", errors="replace")
        # Keep the last (possibly partial) line in the buffer.
        *lines, self._buf = self._buf.split("\n")
        for line in lines:
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                u = obj.get("usage")
                if isinstance(u, dict):
                    self.usage = u
                for choice in obj.get("choices") or []:
                    if isinstance(choice, dict):
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if isinstance(piece, str):
                            self._content_len += len(piece)

    def resolve(self, messages: list[dict]) -> tuple[dict, bool]:
        """Return (usage_block, tokens_estimated). Prefers the provider's real
        usage; otherwise estimates prompt tokens from the request messages and
        completion tokens from accumulated delta content."""
        if self.usage is not None:
            return self.usage, False
        prompt_tokens = sum(
            len(str(m.get("content", ""))) // 4 for m in messages
        )
        return (
            {"prompt_tokens": prompt_tokens,
             "completion_tokens": max(0, self._content_len // 4)},
            True,
        )


def _extract_prompt(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(
                    part.get("text", "") for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
    return ""


def _ollama_base(cr) -> str:
    """OpenAI-compatible base URL for the stored ollama provider (or default)."""
    base_url = next(
        (p.base_url.rstrip("/") for p in cr.all_providers() if p.kind == "ollama" and p.base_url),
        "http://localhost:11434",
    )
    return f"{base_url}/v1"


def _openrouter_key(cr) -> str:
    """API key for the stored openrouter provider, or "" if none configured."""
    return next(
        (p.api_key for p in cr.all_providers() if p.kind == "openrouter" and p.api_key),
        "",
    )


def _classifier_targets(cr) -> list[ClassifierTarget]:
    """Build the ordered classifier fallback chain from config + providers.

    1. Local Ollama model (fast, private, no rate limit) — primary.
    2. A free OpenRouter model — resilience backstop, only if a key is stored.
    Either step is skipped when its model is unset or its provider unavailable.
    """
    targets: list[ClassifierTarget] = []
    local = classifier_model()
    if local:
        targets.append(ClassifierTarget(model=local, base_url=_ollama_base(cr), label="llm"))
    fallback = classifier_fallback_model()
    or_key = _openrouter_key(cr)
    if fallback and or_key:
        targets.append(
            ClassifierTarget(
                model=fallback, base_url=_OPENROUTER_BASE, api_key=or_key, label="llm-free"
            )
        )
    return targets


def _header_safe(text: str, limit: int = 400) -> str:
    """Make a human string safe to put in an HTTP header value.

    Header values must be latin-1 encodable and single-line, and a non-encodable
    character raises inside the response layer — which would turn an explanatory
    header into a 500. Field labels are ASCII today, so this is a guard against
    a future label (or a model-supplied string) rather than a live problem.
    """
    collapsed = " ".join(text.split())
    return collapsed.encode("ascii", "replace").decode("ascii")[:limit]


def _refine_target(cr) -> ClassifierTarget | None:
    """The second-pass profiler, or None when it isn't available.

    Which model this is comes from helper_models.resolve(), so by default it is
    the cheapest model that clears frontier depth and honors a JSON schema rather
    than a name someone typed. Called lazily — only for a prompt that actually
    escalates — because resolving it reads the model catalog.

    Absent an available model or a provider to reach it through, the two-speed
    chain simply routes on the local triage profile: a degraded routing decision,
    never a failed request. That is why the HTTPException _resolve_provider raises
    is swallowed here — for a user request it is a fixable 422, but for an
    optional second opinion it is just a reason to skip.
    """
    choice = _helpers.resolve(_helpers.REFINE, cr)
    if choice is None:
        return None
    try:
        base_url, api_key, real_model = _resolve_provider(choice.model, cr)
    except HTTPException:
        return None
    return ClassifierTarget(
        model=real_model,
        base_url=base_url,
        api_key=api_key,
        label=_helpers.REFINE.label,
    )


def _bedrock_base(cr) -> tuple[str, str] | None:
    """Return (base_url, api_key) for the stored bedrock provider, or None."""
    for p in cr.all_providers():
        if p.kind == "bedrock" and p.api_key:
            region = p.base_url.strip() or "us-east-1"
            return f"https://bedrock-runtime.{region}.amazonaws.com/v1", p.api_key
    return None


def _resolve_provider(model_value: str, cr) -> tuple[str, str, str]:
    """Return (base_url, api_key, real_model_id)."""
    if model_value.startswith("bedrock/"):
        real_model = model_value[len("bedrock/"):]
        bedrock = _bedrock_base(cr)
        if not bedrock:
            raise HTTPException(status_code=422, detail="No bedrock provider configured")
        base_url, api_key = bedrock
        return base_url, api_key, real_model

    if model_value.startswith("openrouter/"):
        real_model = model_value[len("openrouter/"):]
        api_key = next(
            (p.api_key for p in cr.all_providers() if p.kind == "openrouter" and p.api_key),
            "",
        )
        return _OPENROUTER_BASE, api_key, real_model

    if model_value.startswith("ollama/"):
        real_model = model_value[len("ollama/"):]
        base_url = next(
            (p.base_url.rstrip("/") for p in cr.all_providers() if p.kind == "ollama" and p.base_url),
            "http://localhost:11434",
        )
        return f"{base_url}/v1", "", real_model

    # Unknown prefix — fall through to OpenRouter
    api_key = next(
        (p.api_key for p in cr.all_providers() if p.kind == "openrouter" and p.api_key),
        "",
    )
    if not api_key:
        raise HTTPException(status_code=422, detail=f"Cannot resolve provider for model {model_value!r}")
    return _OPENROUTER_BASE, api_key, model_value


# Orchestration drives Claude Code's own loop: recognizing skills, emitting
# Workflow/Agent tool calls, following the tool-use conventions its harness
# parses. Only Claude models do that reliably, so the candidate pool is forced
# no matter what the prompt itself needs.
#
# Eligibility is a *generation* test, not a competence one, because those are
# different questions: whether a model can drive the harness at all, versus
# whether it is strong enough for a given prompt. Conflating them into a single
# general-competence floor excluded claude-haiku-4.5 (general ≈ 0.68) — the model
# Claude Code itself ships as its small-fast default — and so pinned every
# orchestrator request, however mechanical, to Sonnet or above. The prompt
# profile picks the tier now; this decides only who is allowed in the room.
#
# The line is 3.5: Anthropic's tool-use API arrived with Claude 3, but the
# original March-2024 generation (claude-3-opus/sonnet/haiku) loses the thread
# over a long agentic loop, while 3.5 and 3.7 Sonnet drove Claude Code itself.
# Nothing the prompt profile scores captures loop stamina, so it has to live here.
_ORCHESTRATOR_MIN_GENERATION = (3, 5)

# Legacy ids put the generation immediately after "claude-" (claude-3-haiku,
# claude-3-5-sonnet, bedrock's claude-v2:1). Current ones put the *family* there
# instead (claude-sonnet-5, claude-haiku-4.5, claude-opus-4-8), so "no match"
# means "not a legacy id" — i.e. modern, and eligible — rather than "unknown".
_LEGACY_CLAUDE_GENERATION = re.compile(r"claude-v?(\d+)(?:[-.](\d+))?")


def _orchestrator_capable(spec: ModelSpec) -> bool:
    """Whether `spec` can drive the orchestration loop at all.

    Deliberately not a quality judgment — see _ORCHESTRATOR_MIN_GENERATION.
    """
    name = spec.value.lower()
    if "claude" not in name:
        return False
    # claude-instant-* predates tool use entirely, and names no generation where
    # the pattern looks for one, so it needs saying outright.
    if "instant" in name:
        return False
    gen = _LEGACY_CLAUDE_GENERATION.search(name)
    if gen is None:
        return True
    return (int(gen.group(1)), int(gen.group(2) or 0)) >= _ORCHESTRATOR_MIN_GENERATION


def _enforce_rate_limit(cr, request: Request) -> None:
    """Raise 429 if the authenticated key is over its request/token quota.

    No-op for admin/open requests (no key record). Uses the usage log as the
    counter, so a key with no limits configured is never queried.
    """
    record = getattr(request.state, "api_key", None)
    if record is None or not record.rl_window_s:
        return
    if not record.rl_max_req and not record.rl_max_tokens:
        return
    recent = cr.recent_usage(record.user, window_start_for(record))
    status = check_rate_limit(record, recent)
    if not status.allowed:
        raise HTTPException(
            status_code=429,
            detail=status.reason,
            headers={"Retry-After": str(status.retry_after_s)},
        )


def _request_scope(request: Request) -> ModelScope | None:
    """Per-user model scope for the authenticated key, or None if unrestricted.

    Admin/env keys and open (no-auth) requests carry no `api_key` record, so
    they are unscoped. A per-user key's scope is built from its stored
    `scope_models` JSON + `max_tier`.

    Anonymous visitors are the exception: their ceiling comes from the public
    access settings and the day's spend, not from a stored key, and it is never
    None — an anonymous request is always scoped.

    Self-issued keys are the same exception wearing a key. Their ceiling also has
    to move with the day's spend, so it can't live in the row; it is read live and
    *tightened onto* whatever the row already said, never loosening it.
    """
    if getattr(request.state, "is_anon", False):
        return _public.anon_scope(request.app.state.capability_router)
    record = getattr(request.state, "api_key", None)
    if record is None:
        return None
    scope = parse_scope(record.scope_models, record.max_tier)
    if _signup.is_signup_user(record.user):
        scope = scope.capped_at(
            _signup.tier_ceiling(request.app.state.capability_router, record.user)
        )
    return scope if scope.is_restricted else None


def _cached_tokens(usage: dict | None) -> int:
    """Prompt tokens the provider served from its cache, 0 if it didn't say.

    Two spellings: OpenAI's `prompt_tokens_details.cached_tokens`, which is what
    OpenRouter normalizes to, and Anthropic's own `cache_read_input_tokens` in
    case a provider leaks it through an OpenAI-shaped reply. Reading both is a
    one-line hedge against the failure that would otherwise be silent — billing
    a cache read at full price looks exactly like caching not working.
    """
    u = usage or {}
    details = u.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens"):
        return int(details["cached_tokens"] or 0)
    return int(u.get("cache_read_input_tokens", 0) or 0)


def _billable_prompt(prompt_tokens: int, cached_tokens: int) -> int:
    """Prompt tokens priced at the full input rate, cache reads discounted.

    A cache read costs 10% of the input rate, so it can't be counted like a fresh
    token or the Usage page reports a bill nobody was sent.

    ponytail: a cache *write* costs 1.25× and the OpenAI usage shape doesn't
    separate it out, so a turn that writes is under-counted by a quarter of the
    written portion. Off in the harmless direction (the provider's own dashboard
    remains the source of truth), and fixable only when a provider reports writes.
    """
    if cached_tokens <= 0:
        return prompt_tokens
    fresh = max(0, prompt_tokens - cached_tokens)
    return fresh + int(cached_tokens * 0.1)


def _log_usage(cr, request: Request, *, routed_model: str, domain: str,
               complexity: str, usage: dict | None, status: int,
               tokens_estimated: bool = False,
               profile: PromptProfile | None = None,
               classifier: str = "") -> None:
    """Attribute a proxied request to its user in the usage log (best-effort).

    Never raises — usage accounting must not break a request that already
    succeeded. `usage` is an OpenAI-style token block: for non-streaming calls
    it's the provider's; for streams it's either the provider's trailing
    include_usage chunk or a locally estimated one (tokens_estimated=True).

    `profile` is the full prompt profile that chose the model, recorded alongside
    the lossy (domain, complexity) summary. It is what lets a later profiling
    change be judged against traffic that really happened — see
    profile_audit.py — so a routing change can be evaluated on its effect rather
    than on how its score diff reads.

    `classifier` is which profiler produced it, stored for the same reason it is
    reported in X-Classifier: the chain degrades silently, so the only way to
    notice that every request is being profiled by the keyword fallback is to
    count.
    """
    user = getattr(request.state, "user", "") or ""
    key_prefix = getattr(request.state, "key_prefix", "") or ""
    # No identity and no usage → nothing worth recording (e.g. open/no-auth mode).
    if not user and not key_prefix and not usage:
        return
    prompt_tokens = int((usage or {}).get("prompt_tokens", 0) or 0)
    completion_tokens = int((usage or {}).get("completion_tokens", 0) or 0)
    cached_tokens = _cached_tokens(usage)
    cost_usd = 0.0
    try:
        if prompt_tokens or completion_tokens:
            cost_usd = cr.cost_for(
                routed_model, _billable_prompt(prompt_tokens, cached_tokens),
                completion_tokens,
            ) or 0.0
    except Exception:  # noqa: BLE001 — pricing lookup must not break logging
        cost_usd = 0.0
    if cached_tokens:
        # The only visible proof that caching is working, since the count itself
        # isn't persisted (that would be a column and a migration; the corrected
        # cost is what the Usage page actually sums).
        print(f"[proxy] cache hit: {cached_tokens} of {prompt_tokens} prompt "
              f"tokens read at 10% on {routed_model}", file=sys.stderr, flush=True)
    try:
        cr.record_usage(UsageRecord(
            user=user, key_prefix=key_prefix, routed_model=routed_model,
            domain=domain, complexity=complexity,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            cost_usd=cost_usd, status=status,
            tokens_estimated=tokens_estimated,
            profile=profile.to_dict() if profile is not None else None,
            classifier=classifier,
        ))
    except Exception:  # noqa: BLE001 — logging is best-effort
        pass


def _headers(api_key: str) -> dict[str, str]:
    h = {"Content-Type": "application/json",
         # Ask the upstream not to compress: the streaming path forwards raw
         # socket bytes verbatim (aiter_raw) for immediate token delivery, and
         # raw bytes must be uncompressed or the browser would see garbage.
         "Accept-Encoding": "identity",
         "HTTP-Referer": "https://github.com/smart-ai-router/smart-ai-router"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


# ── endpoints ─────────────────────────────────────────────────────────────────

@proxy_router.get("/v1/models")
def list_models():
    """The model names a client may send — which is the two routing modes, not
    the catalog.

    An editor that only speaks OpenAI (Cursor, Continue, Zed, aider) asks here
    before it will let you pick anything, and a 404 reads as a broken endpoint.
    But listing the catalog would be a lie: `model` in a completions body never
    selects a model, it is overwritten with the router's pick, and the only part
    of it that changes anything is whether it says "orchestrator". So this lists
    exactly what a caller can decide. /api/models still serves the real catalog,
    with the capability flags and prices this shape has nowhere to put.
    """
    return {
        "object": "list",
        "data": [
            # created: a constant. OpenAI's typed clients require the field, and
            # a real timestamp would be invented — the modes ship with the code.
            {"id": name, "object": "model", "created": 0,
             "owned_by": "smart-ai-router"}
            for name in ("smart-worker", _ORCHESTRATOR_MARKERS[0])
        ],
    }


@proxy_router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body: dict[str, Any] = await request.json()
    cr = request.app.state.capability_router

    messages = body.get("messages", [])
    stream = bool(body.get("stream", False))
    requested_model = str(body.get("model", ""))
    is_orchestrator = any(m in requested_model for m in _ORCHESTRATOR_MARKERS)

    # Agent mode: the client asks the assistant to use the filesystem tools
    # (read/write/bash over its per-user workspace). Signaled by a non-standard
    # `agent` flag in the body; stripped before forwarding so providers never
    # see it. The flag is tri-state:
    #   True / False  — explicit opt-in / opt-out (honored as given)
    #   "auto" / absent — let the classifier decide (default)
    # Resolved to a concrete bool below, once prompt text and scope are known.
    agent_flag = body.pop("agent", "auto")

    # Expand any uploaded-file references (file-… ids) into inline content the
    # backend understands: images → base64 data: URIs, documents → their
    # server-extracted text. Owner-scoped, so a key can only attach its own
    # files. Done before classification/routing so document text informs the
    # classifier and image parts drive the vision decision below.
    user = getattr(request.state, "user", "") or ""
    try:
        messages = resolve_file_refs(
            messages, cr, user=user, is_admin=(user == "admin")
        )
    except FileRefError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    body = {**body, "messages": messages}

    # 1. Profile the prompt — which fields it needs and how deep into each (see
    # taxonomy.py). The two-speed LLM classifier is primary, escalating to a
    # stronger profiler only for consequential prompts; the deterministic keyword
    # classifier is the fallback whenever the LLM path is disabled or fails
    # (network error, timeout, malformed output). Profiling never blocks or fails
    # the request.
    prompt_text = _extract_prompt(messages)
    if not prompt_text:
        profile = PromptProfile(domains=(DomainNeed("general_knowledge", "surface"),))
        classifier_used = "default"
    else:
        # Classification is billable work the user didn't ask for by name, so the
        # calls it makes are collected and logged as overhead rows against this
        # request's identity. Recorded after the fact rather than inside the
        # classifier so the classifier stays store-free — see overhead.py.
        with _overhead.collect() as overhead_calls:
            chain_result = await classify_profile_two_speed(
                prompt_text,
                _classifier_targets(cr),
                # Passed unevaluated: resolving the refine model routes, and the
                # pass fires on a small minority of prompts. See
                # classify_profile_two_speed().
                lambda: _refine_target(cr),
            )
        _overhead.record(
            cr, overhead_calls,
            user=getattr(request.state, "user", "") or "",
            key_prefix=getattr(request.state, "key_prefix", "") or "",
        )
        if chain_result is not None:
            profile, classifier_used = chain_result
        else:
            profile = classify_profile(prompt_text)
            classifier_used = "keyword"

    # Legacy labels for the usage log, the X- headers, and the dashboard. Always
    # derived from the profile that actually routed, so the two can't disagree.
    domain, complexity = profile.legacy_labels()

    # Detect image content in any message (after file-ref resolution, so an
    # image attached by file id counts too).
    needs_vision = contains_image(messages)

    # Enforce per-user quota before doing any routing/forwarding work.
    _enforce_rate_limit(cr, request)

    # Per-user model scope (None for admin/open requests).
    scope = _request_scope(request)

    # Capability guard: if the request needs vision but no reachable model
    # (within this key's scope) accepts images, fail clearly rather than
    # silently dropping the image and returning a confused answer. This is the
    # locked no-vision-model behavior — better than claudish's silent strip.
    if needs_vision and not cr.capabilities(scope=scope).vision:
        raise HTTPException(
            status_code=422,
            detail="This request includes an image, but no image-capable "
                   "(vision) model is available for your key. Register a "
                   "vision-capable model (via ollama or openrouter) or remove "
                   "the image.",
        )

    # Resolve the tri-state agent flag into a concrete decision.
    #
    #   explicit True    → agent mode; hard-fail (422) if no tool-capable model,
    #                      because the caller asked for it and a silent downgrade
    #                      would be surprising.
    #   explicit False   → never agent mode.
    #   "auto" / absent  → enter agent mode only if the prompt is *actionable*
    #                      (wants a file produced / filesystem work), a
    #                      tool-capable model is in scope, AND the caller sent no
    #                      tools of its own. Otherwise fall back silently to
    #                      plain chat — auto must never lock a user out or
    #                      needlessly escalate a plain question.
    #
    # That last condition is the one that isn't obvious. A client that supplies
    # `tools` runs its own tool loop by definition — a coding agent (Claude Code
    # through claudish, Codex, anything speaking the OpenAI tool protocol) sends
    # its editor, shell and search tools expecting `tool_calls` back to execute
    # itself. Its prompts are maximally actionable, so "auto" fired on every one
    # of them and the router answered with a server-side filesystem loop over
    # *its own* workspace instead: the caller's tools were never called, its
    # sandbox was never touched, and the reply described work done somewhere the
    # user could not see. Nothing errored, which is what made it hard to spot.
    # Only an explicit `agent: true` overrides this, because then the caller has
    # asked for the router's loop by name.
    # Anonymous visitors never get agent mode, whatever they ask for. The tools
    # are read/write/bash over a workspace on the operator's own machine, so
    # this is the difference between a public chat page and a public shell. A
    # flat refusal (not a silent downgrade) so an explicit `agent: true` from a
    # stranger is never quietly answered as if it had worked.
    is_anon = getattr(request.state, "is_anon", False)
    # A self-issued key is a stranger who clicked a button, not someone the
    # operator decided to trust, so the same refusal applies — and here it is the
    # load-bearing one. Handing a public shell to anyone who can complete a POST
    # would be worse than anonymous chat, not better, because the key also works
    # from outside the browser.
    is_self_serve = _signup.is_signup_user(getattr(request.state, "user", "") or "")
    if (is_anon or is_self_serve) and agent_flag is True:
        raise HTTPException(
            status_code=403,
            detail=(
                "Agent (filesystem) mode is not available for anonymous use. "
                "Sign in with an API key to use it."
                if is_anon else
                "Agent (filesystem) mode is not available on a self-serve account. "
                "It needs an API key issued by this deployment's operator."
            ),
        )

    tools_available = cr.capabilities(scope=scope).tools
    agent_auto = isinstance(agent_flag, str) and agent_flag.lower() == "auto"
    if is_anon or is_self_serve:
        agent_mode = False          # settled above; auto must not re-enable it
    elif agent_flag is True:
        if not tools_available:
            raise HTTPException(
                status_code=422,
                detail="Agent (filesystem) mode needs a tool-capable model, but "
                       "none is available for your key. Register a model that "
                       "supports function calling (via ollama or openrouter).",
            )
        agent_mode = True
    elif agent_auto:
        client_brought_tools = bool(body.get("tools"))
        agent_mode = (
            not client_brought_tools
            and tools_available
            and is_actionable(prompt_text)
        )
    else:
        agent_mode = False

    if agent_auto and agent_mode:
        print(f"[proxy] agent auto-detected for actionable prompt",
              file=sys.stderr, flush=True)

    # 2. Route
    needs_tools = bool(body.get("tools")) or agent_mode
    # A caller asking for a json_schema reply needs a model that honors the
    # *schema*, not merely one that emits JSON. This is a routing constraint and
    # not a dropped param for the same reason vision is: a model that ignores the
    # schema answers the prompt in prose, the caller's parse finds nothing, and
    # nothing anywhere reports an error. Filtering the pick is the only place the
    # requirement can be met — see ModelSpec.structured_outputs.
    _rf = body.get("response_format")
    needs_structured = isinstance(_rf, dict) and _rf.get("type") == "json_schema"
    est_tokens = sum(len(str(m.get("content", ""))) // 4 for m in messages)

    if is_orchestrator:
        # Orchestration narrows the pool to Claude, then routes on the profile
        # like any other request — so a mechanical turn (acknowledge a tool
        # result, small edit) can run on Haiku while a genuinely hard one
        # escalates to Opus. Previously this branch ignored the profile and took
        # the cheapest Claude clearing a competence floor, which meant every
        # orchestrator request paid for a classification it then discarded.
        pool = [s for s in cr.all_models() if _orchestrator_capable(s)]
        if not pool:
            raise HTTPException(
                status_code=422,
                detail="Orchestrator mode requires a Claude model of generation "
                       f"{'.'.join(str(n) for n in _ORCHESTRATOR_MIN_GENERATION)}"
                       " or newer. Configure a 'bedrock' provider or sync an "
                       "anthropic/claude model.",
            )
        # A scoped key that can reach none of them cannot orchestrate. Checked
        # against the pool rather than after the pick, so the error names the
        # real cause instead of surfacing as a generic "no eligible model".
        if scope is not None and not any(scope.permits(s) for s in pool):
            raise HTTPException(
                status_code=403,
                detail="Your key's scope does not permit the Claude model "
                       "required for orchestrator mode.",
            )
        candidates = pool
    else:
        candidates = None

    try:
        if candidates is None:
            decision = cr.select(
                profile,
                needs_tools=needs_tools,
                needs_vision=needs_vision,
                needs_structured=needs_structured,
                est_tokens=est_tokens,
                scope=scope,
                agent_mode=agent_mode,
            )
        else:
            decision = cr.select_from(
                candidates,
                profile,
                needs_tools=needs_tools,
                needs_vision=needs_vision,
                needs_structured=needs_structured,
                est_tokens=est_tokens,
                scope=scope,
                agent_mode=agent_mode,
            )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    routed_model = decision.model

    # Worker path escalated to Claude — no cheaper model cleared the quality bar.
    # Claude is the most expensive tier, so surface a note to the user.
    claude_tier = (not is_orchestrator) and ("claude" in routed_model.lower())

    # Nothing available cleared every bar this prompt sets, so the pick is the
    # closest miss rather than a qualified model. This is the case the old router
    # could not even detect, and the case the fabricated-regulatory-answer came
    # from: the honest move is to say so up front, not to answer confidently.
    underqualified = not decision.qualified

    # Either condition earns the user a prepended caveat: one is about cost, the
    # other about how far to trust the answer.
    escalated = claude_tier or underqualified

    # …but only a *human* reader. A client that ships its own tool definitions is
    # a program driving a tool loop, and prose injected into the assistant turn
    # derails it — mid-loop it reads as the model's answer, and because it becomes
    # conversation history the client re-sends it on every later turn, so one note
    # is billed for the rest of the session. Orchestrator mode is always such a
    # client. The response headers still report the truth either way, which is
    # where a program should be reading it from.
    inject_note = escalated and not (is_orchestrator or bool(body.get("tools")))

    # 3. Resolve provider
    base_url, api_key, real_model = _resolve_provider(routed_model, cr)
    # The chosen model's spec, for its output ceiling. One store read, and only
    # this: everything else about the decision is already in `decision`.
    routed_spec = cr.get_model(routed_model)

    mode = "orchestrator" if is_orchestrator else profile.describe()
    print(f"[proxy] {mode} ({classifier_used}) → {routed_model} (real: {real_model})"
          f"{' [ESCALATED]' if claude_tier else ''}"
          f"{' [UNDERQUALIFIED]' if underqualified else ''}",
          file=sys.stderr, flush=True)
    print(f"[proxy] why: {decision.explain()}", file=sys.stderr, flush=True)

    forward_body = {**body, "model": real_model}
    # The caller chose its params for a model it never saw; the pick may not take
    # them. Done before anything else touches the body so nothing downstream has
    # to reason about a param that isn't going to survive.
    dropped_params = _drop_unsupported(forward_body, routed_spec)
    if dropped_params:
        print(f"[proxy] dropped for {routed_model}: {', '.join(dropped_params)}",
              file=sys.stderr, flush=True)
    # Tell the model what the chat page can render — but only when the caller *is*
    # the chat page, and only after classification, so the note never influences
    # the routing profile it isn't part of. Prepended rather than merged into an
    # existing system turn: the caller's own instructions stay verbatim, and a
    # later turn wins any disagreement, which is the right precedence for a note
    # about the display surface.
    # The date comes first and is not conditional on `tools`: an agent loop needs to
    # know what day it is as much as a chat reply does, and unlike the rendering note
    # it is a fact rather than a suggestion, so it is never noise.
    if _is_ui_client(request):
        notes = [_todays_date_note()]
        if not forward_body.get("tools"):
            notes.append(_rich_output_preamble())
        forward_body["messages"] = (
            [{"role": "system", "content": n} for n in notes if n]
            + list(forward_body.get("messages") or [])
        )
    # Search the web when the prompt turns on facts that move. Set on forward_body
    # before the agent branch reads it, so an agent round searches too.
    search_plugin = _web_search_plugin(profile, routed_model)
    if search_plugin:
        forward_body["plugins"] = search_plugin
    # Apply a generous output-token default when the caller omits one, so
    # reasoning models have budget for thinking + answer instead of truncating —
    # and a document-sized one when the profile says the answer is a document.
    if not forward_body.get("max_tokens"):
        forward_body["max_tokens"] = _output_budget(profile, routed_spec)
    # A caller who *named* a max_tokens gets clamped to the pick's ceiling too:
    # `_output_budget` only clamps the number it computed itself, so a client
    # sizing its request for the model it thinks it's talking to could still ask
    # for more than the routed model can emit — which several providers reject
    # outright rather than truncate (see ModelSpec.max_output). 0 = the catalog
    # didn't say, so send it unclamped.
    model_ceiling = int(getattr(routed_spec, "max_output", 0) or 0)
    try:
        asked_output = int(forward_body["max_tokens"])
    except (TypeError, ValueError):
        # Not a number at all. Nothing to clamp, and inventing one would hide a
        # malformed body — leave it for the provider to reject.
        asked_output = 0
    if model_ceiling and asked_output:
        forward_body["max_tokens"] = min(asked_output, model_ceiling)
    # Callers the operator never vetted — anonymous visitors and self-issued keys
    # — get a hard output ceiling, applied after the default and over anything they
    # asked for. This is what bounds the damage while the spend cap is blind: a
    # call's cost isn't known until it returns, so the protection has to be a limit
    # on how expensive one call can possibly be.
    output_cap = 0
    if is_anon:
        output_cap = _public.max_output_tokens()
    elif is_self_serve:
        output_cap = _signup.max_output_tokens()
    if output_cap:
        forward_body["max_tokens"] = min(
            int(forward_body["max_tokens"] or output_cap), output_cap
        )
    # Last thing done to the body, because the breakpoints have to land on the
    # messages actually sent — including the system notes prepended above, which
    # sit at the front of the prefix and belong inside the cached region.
    cache_breakpoints = _inject_cache_breakpoints(
        forward_body, routed_model, est_tokens, loops=agent_mode
    )
    url = f"{base_url}/chat/completions"
    routing_headers = {
        "X-Routed-Model": routed_model,
        "X-Domain": domain,
        "X-Complexity": complexity,
        "X-Escalated": "true" if escalated else "false",
        "X-Classifier": classifier_used,
        "X-User": getattr(request.state, "user", "") or "",
        "X-Prompt-Profile": _header_safe(profile.describe()),
        # The ceiling this reply had to fit in. Reported so a truncated answer can
        # say *what* cut it off: "the model was terse" and "the model was stopped
        # at 1024 tokens" look identical on screen, and only one of them is
        # something the reader can do anything about.
        "X-Output-Limit": str(forward_body.get("max_tokens") or 0),
        # Whether this answer was checked against the live web. Reported for every
        # request, not just searched ones: "searched and found nothing newer" and
        # "answered from 2024 training data" read identically on screen, and the
        # reader's trust in a date-sensitive fact should differ between them.
        "X-Web-Search": "true" if search_plugin else "false",
        # Params the pick couldn't take, so a client can see why the knob it set
        # did nothing. Silence here is the whole failure mode being fixed: the
        # caller tuned a model it never learns the name of.
        "X-Dropped-Params": ",".join(dropped_params),
        # How many cache breakpoints this request was marked with. 0 is the
        # common, correct answer (a local model, a first turn, a short prompt) —
        # it's the *always* 0 that was the bug, and only a per-request number
        # distinguishes those.
        "X-Cache-Breakpoints": str(cache_breakpoints),
    }
    routing_headers["X-Routing-Why"] = _header_safe(decision.explain())
    routing_headers["X-Qualified"] = "false" if underqualified else "true"

    if underqualified:
        _ESCALATION_NOTE = (
            f"> _[smart-ai-router] This request looks like **{profile.describe()}**, "
            f"and no available model clears that bar — {decision.explain()}. "
            f"It was sent to {routed_model}, the closest available. Treat "
            f"specifics (citations, standards, figures) as unverified._\n\n"
        )
    else:
        _ESCALATION_NOTE = (
            f"> _[smart-ai-router] This request looks like **{profile.describe()}**, "
            f"which exceeded the capability of every available lower-cost model, "
            f"so it was escalated to {routed_model} — the most capable (and most "
            f"expensive) tier. Escalation happens only when necessary._\n\n"
        )

    # Generous timeout: reasoning models can take minutes to first token.
    # connect short, read/write/pool long — the read budget covers slow
    # time-to-first-token and long generations.
    _timeout = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=600.0)

    # 4a. Agent mode: run the tool-calling loop server-side, executing the
    # filesystem tools against the caller's workspace and streaming tool
    # activity + the final answer back as SSE. The loop reuses this same
    # provider/keys via a non-streaming call_model closure.
    if agent_mode:
        async def _stream_model(req_body: dict) -> AsyncIterator[dict]:
            """Stream one model round from the provider, yielding each
            choices[0].delta dict. The agent loop passes content through live
            and reassembles tool calls from the fragments."""
            fwd = {**req_body, "model": real_model, "stream": True}
            if not fwd.get("max_tokens"):
                # Per *round*, not per request — the loop may take several. The
                # profile-aware budget still applies: an agent writing a document
                # to a file needs room for the document.
                fwd["max_tokens"] = _output_budget(profile, routed_spec)
            async with httpx.AsyncClient(timeout=_timeout) as client:
                async with client.stream(
                    "POST", url, headers=_headers(api_key), json=fwd,
                ) as resp:
                    if resp.status_code >= 400:
                        err = await resp.aread()
                        raise RuntimeError(
                            f"provider {resp.status_code}: {err.decode(errors='replace')[:500]}"
                        )
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            obj = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        choices = obj.get("choices") or []
                        if choices and isinstance(choices[0], dict):
                            delta = choices[0].get("delta")
                            if isinstance(delta, dict):
                                yield delta

        _log_usage(cr, request, routed_model=routed_model, domain=domain,
                   complexity=complexity, usage=None, status=200,
                   profile=profile, classifier=classifier_used)

        def _register_file(data: bytes, filename: str, mime: str) -> str:
            """Register an agent-created file in the Files API, owned by the
            caller, so it downloads from the chat and shows in the Files tab."""
            rec = cr.upload_file(
                data, filename=filename, mime=mime,
                purpose="assistants", user=user,
            )
            return rec.id

        async def _agent_generator() -> AsyncIterator[bytes]:
            yield b": smart-ai-router connected\n\n"
            if inject_note:
                yield f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': _ESCALATION_NOTE}, 'finish_reason': None}]})}\n\n".encode()
            async for chunk in run_agent_loop(
                user=user,
                # forward_body, not the raw body: every round of the loop hits the
                # same routed model, so it needs the same param filtering, output
                # ceiling and system notes a single forwarded request gets. Seeding
                # from `body` here meant a dropped param came straight back.
                body=forward_body,
                tool_schemas=_agent_tool_schemas(),
                stream_model=_stream_model,
                register_file=_register_file,
            ):
                yield chunk

        return StreamingResponse(
            _with_heartbeat(_agent_generator()),
            media_type="text/event-stream",
            headers={
                **routing_headers,
                "X-Agent": "true",
                "X-Agent-Auto": "true" if agent_auto else "false",
            },
        )

    # 4. Forward with async httpx
    if stream:
        # Ask OpenAI-compatible backends to emit a trailing usage chunk so
        # streamed requests can be token-accounted. Providers that ignore this
        # fall back to a local estimate (see _StreamUsageScanner).
        forward_body["stream_options"] = {"include_usage": True}

        async def _stream_generator() -> AsyncIterator[bytes]:
            # Emit an SSE comment immediately so the client sees the stream is
            # alive while we wait for the upstream provider's first token.
            yield b": smart-ai-router connected\n\n"
            scanner = _StreamUsageScanner()
            logged = False  # guard: log usage exactly once (drain or error)

            def _record(status: int) -> None:
                nonlocal logged
                if logged:
                    return
                logged = True
                usage, estimated = scanner.resolve(messages)
                _log_usage(
                    cr, request,
                    routed_model=routed_model, domain=domain,
                    complexity=complexity, usage=usage,
                    status=status, tokens_estimated=estimated,
                    profile=profile, classifier=classifier_used,
                )

            try:
                async with httpx.AsyncClient(timeout=_timeout) as client:
                    async with client.stream(
                        "POST", url,
                        headers=_headers(api_key),
                        json=forward_body,
                    ) as resp:
                        if resp.status_code >= 400:
                            error = await resp.aread()
                            yield f"data: {json.dumps({'error': error.decode(errors='replace')})}\n\n".encode()
                            # Record the failed attempt for attribution/quotas
                            # (no tokens, but the request count matters).
                            _record(resp.status_code)
                            return
                        # Prepend escalation note as a synthetic first delta chunk
                        if inject_note:
                            note_chunk = {
                                "choices": [{
                                    "index": 0,
                                    "delta": {"role": "assistant", "content": _ESCALATION_NOTE},
                                    "finish_reason": None,
                                }],
                            }
                            yield f"data: {json.dumps(note_chunk)}\n\n".encode()
                        # Forward each network chunk the instant it arrives.
                        # aiter_bytes(4096) *buffers* until 4 KB accumulates
                        # before yielding, which stalls SSE token-by-token
                        # streaming into visible ~4 KB bursts ("a line every few
                        # seconds"). aiter_raw() hands us bytes as they land on
                        # the socket, so tokens reach the browser immediately. We
                        # forward each chunk verbatim and feed a copy to the
                        # scanner to recover the trailing usage block.
                        async for chunk in resp.aiter_raw():
                            scanner.feed(chunk)
                            yield chunk
                        _record(resp.status_code)
            except httpx.RequestError as exc:
                yield f"data: {json.dumps({'error': f'proxy upstream error: {exc}'})}\n\n".encode()
                yield b"data: [DONE]\n\n"
                _record(502)
            finally:
                # Client disconnect / cancellation mid-drain still records what
                # streamed (no-ops if _record already ran on drain/error).
                _record(200)

        return StreamingResponse(
            # Same keepalive treatment as agent mode: a slow time-to-first-token
            # on a reasoning model can otherwise leave the bubble silent long
            # enough to look like a hang.
            _with_heartbeat(_stream_generator()),
            media_type="text/event-stream",
            headers=routing_headers,
        )
    else:
        async with httpx.AsyncClient(timeout=_timeout) as client:
            try:
                resp = await client.post(
                    url,
                    headers=_headers(api_key),
                    json=forward_body,
                )
            except httpx.RequestError as exc:
                raise HTTPException(status_code=502, detail=f"Provider unreachable: {exc}")

        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)

        data = resp.json()
        if inject_note:
            try:
                msg = data["choices"][0]["message"]
                msg["content"] = _ESCALATION_NOTE + (msg.get("content") or "")
            except (KeyError, IndexError, TypeError):
                pass  # unexpected shape — return provider response unmodified
        _log_usage(
            cr, request,
            routed_model=routed_model, domain=domain, complexity=complexity,
            usage=data.get("usage") if isinstance(data, dict) else None,
            status=resp.status_code,
            profile=profile, classifier=classifier_used,
        )
        return JSONResponse(content=data, headers=routing_headers)
