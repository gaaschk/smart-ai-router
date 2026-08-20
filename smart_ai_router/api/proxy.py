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
import json
import re
import sys
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from smart_ai_router import helper_models as _helpers
from smart_ai_router import overhead as _overhead
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
    """
    record = getattr(request.state, "api_key", None)
    if record is None:
        return None
    scope = parse_scope(record.scope_models, record.max_tier)
    return scope if scope.is_restricted else None


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
    cost_usd = 0.0
    try:
        if prompt_tokens or completion_tokens:
            cost_usd = cr.cost_for(routed_model, prompt_tokens, completion_tokens) or 0.0
    except Exception:  # noqa: BLE001 — pricing lookup must not break logging
        cost_usd = 0.0
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


# ── endpoint ──────────────────────────────────────────────────────────────────

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
    #                      (wants a file produced / filesystem work) AND a
    #                      tool-capable model is in scope. Otherwise fall back
    #                      silently to plain chat — auto must never lock a user
    #                      out or needlessly escalate a plain question.
    tools_available = cr.capabilities(scope=scope).tools
    agent_auto = isinstance(agent_flag, str) and agent_flag.lower() == "auto"
    if agent_flag is True:
        if not tools_available:
            raise HTTPException(
                status_code=422,
                detail="Agent (filesystem) mode needs a tool-capable model, but "
                       "none is available for your key. Register a model that "
                       "supports function calling (via ollama or openrouter).",
            )
        agent_mode = True
    elif agent_auto:
        agent_mode = tools_available and is_actionable(prompt_text)
    else:
        agent_mode = False

    if agent_auto and agent_mode:
        print(f"[proxy] agent auto-detected for actionable prompt",
              file=sys.stderr, flush=True)

    # 2. Route
    needs_tools = bool(body.get("tools")) or agent_mode
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

    mode = "orchestrator" if is_orchestrator else profile.describe()
    print(f"[proxy] {mode} ({classifier_used}) → {routed_model} (real: {real_model})"
          f"{' [ESCALATED]' if claude_tier else ''}"
          f"{' [UNDERQUALIFIED]' if underqualified else ''}",
          file=sys.stderr, flush=True)
    print(f"[proxy] why: {decision.explain()}", file=sys.stderr, flush=True)

    forward_body = {**body, "model": real_model}
    # Apply a generous output-token default when the caller omits one, so
    # reasoning models have budget for thinking + answer instead of truncating.
    if not forward_body.get("max_tokens"):
        forward_body["max_tokens"] = _default_max_tokens()
    url = f"{base_url}/chat/completions"
    routing_headers = {
        "X-Routed-Model": routed_model,
        "X-Domain": domain,
        "X-Complexity": complexity,
        "X-Escalated": "true" if escalated else "false",
        "X-Classifier": classifier_used,
        "X-User": getattr(request.state, "user", "") or "",
        "X-Prompt-Profile": _header_safe(profile.describe()),
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
                fwd["max_tokens"] = _default_max_tokens()
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
                body={**body, "model": real_model},
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
