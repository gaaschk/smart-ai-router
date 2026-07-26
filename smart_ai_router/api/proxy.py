"""
OpenAI-compatible proxy endpoint.

Every POST /v1/chat/completions is:
  1. Classified (domain + complexity) from the last user message.
  2. Routed to the cheapest-qualifying model via CapabilityRouter.
  3. Forwarded to the real provider with async httpx, streaming back verbatim.

Routing modes (selected by the incoming `model` name):
  smart-orchestrator  -> force a Claude model (reliable skill/workflow tool-calling)
  smart-worker / *    -> classify + route to cheapest capable model, Claude fallback

Supported provider prefixes in the routed model value:
  openrouter/<vendor>/<model>  -> https://openrouter.ai/api/v1
  ollama/<model>               -> stored ollama base_url (default http://localhost:11434)
  bedrock/<model>              -> https://bedrock-runtime.{region}.amazonaws.com/v1
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from smart_ai_router.agent_loop import run_agent_loop
from smart_ai_router.classifier import classify, is_actionable
from smart_ai_router.fileref import FileRefError, contains_image, resolve_file_refs
from smart_ai_router.llm_classifier import (
    ClassifierTarget,
    classifier_fallback_model,
    classifier_model,
    classify_chain,
)
from smart_ai_router.models import UsageRecord
from smart_ai_router.ratelimit import check_rate_limit, window_start_for
from smart_ai_router.scope import ModelScope, parse_scope
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

# Default output-token ceiling applied when a caller omits max_tokens.
# max_tokens caps *output* only (for reasoning models: thinking + answer), so
# a stingy default silently truncates responses — the common empty-content /
# finish_reason:"length" failure. OpenAI/ChatGPT treat max_tokens as optional
# (unbounded up to context), but many providers here default low, so we set a
# generous floor that leaves room for a reasoning budget plus a real answer.
_DEFAULT_MAX_TOKENS = 4096

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
    `interval` seconds of silence so a slow round never looks like a hang."""
    ait = gen.__aiter__()
    while True:
        nxt = asyncio.ensure_future(ait.__anext__())
        while True:
            try:
                chunk = await asyncio.wait_for(asyncio.shield(nxt), timeout=interval)
            except asyncio.TimeoutError:
                yield b": keepalive\n\n"
                continue
            except StopAsyncIteration:
                return
            break
        yield chunk


# ── helpers ───────────────────────────────────────────────────────────────────

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


# Minimum general competence for a Claude model to drive the orchestration
# loop reliably. Old/weak Claude models (e.g. claude-3-haiku ≈ 0.78) fall
# below this and are skipped in favor of modern Haiku/Sonnet (≥ 0.80).
_ORCHESTRATOR_MIN_COMPETENCE = 0.80


def _orchestrator_model(cr) -> str | None:
    """Pick the cheapest *capable* Claude model for the orchestration layer.

    Orchestration needs a Claude model that reliably follows Claude Code's
    skill/workflow tool-calling conventions, so we require a competence floor
    and then pick the cheapest that clears it. Prefers bedrock over openrouter
    at equal cost (bedrock claude models carry higher seeded competence).
    Returns the model value string, or None if no capable Claude model exists.
    """
    claude = [
        s for s in cr.all_models()
        if "claude" in s.value.lower() and s.reliability >= 0.5
    ]
    if not claude:
        return None

    capable = [
        s for s in claude
        if s.competence.get("general", 0.0) >= _ORCHESTRATOR_MIN_COMPETENCE
    ]
    pool = capable or claude  # if none clear the floor, fall back to any claude

    # Cheapest first, then highest general competence
    pool.sort(key=lambda s: (s.cost, -s.competence.get("general", 0.0)))
    return pool[0].value


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
               complexity: str, usage: dict | None, status: int) -> None:
    """Attribute a proxied request to its user in the usage log (best-effort).

    Never raises — usage accounting must not break a request that already
    succeeded. `usage` is the provider's OpenAI-style token block, absent for
    streaming responses (tokens aren't tallied until a later phase).
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

    # 1. Classify — LLM classifier is primary; the deterministic keyword
    # classifier is the fallback whenever the LLM path is disabled or fails
    # (network error, timeout, malformed output). Classification never blocks
    # or fails the request.
    prompt_text = _extract_prompt(messages)
    if not prompt_text:
        domain, complexity = "general", "trivial"
        classifier_used = "default"
    else:
        chain_result = await classify_chain(prompt_text, _classifier_targets(cr))
        if chain_result is not None:
            domain, complexity, classifier_used = chain_result
        else:
            domain, complexity = classify(prompt_text)
            classifier_used = "keyword"

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
    if is_orchestrator:
        # Orchestration layer: force a Claude model for reliable skill/workflow
        # tool-calling. Prefer bedrock; fall back to any openrouter claude model.
        routed_model = _orchestrator_model(cr)
        if routed_model is None:
            raise HTTPException(
                status_code=422,
                detail="Orchestrator mode requires a Claude model. Configure a "
                       "'bedrock' provider or ensure an anthropic/claude model is synced.",
            )
        # A scoped user who can't reach the forced Claude model can't orchestrate.
        if scope is not None:
            spec = cr.get_model(routed_model)
            if spec is not None and not scope.permits(spec):
                raise HTTPException(
                    status_code=403,
                    detail="Your key's scope does not permit the Claude model "
                           "required for orchestrator mode.",
                )
    else:
        try:
            routed_model = cr.route(
                domain,
                complexity,
                needs_tools=bool(body.get("tools")) or agent_mode,
                needs_vision=needs_vision,
                est_tokens=sum(len(str(m.get("content", ""))) // 4 for m in messages),
                scope=scope,
                agent_mode=agent_mode,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    # Worker path escalated to Claude — no cheaper model cleared the quality bar.
    # Claude is the most expensive tier, so surface a note to the user.
    escalated = (not is_orchestrator) and ("claude" in routed_model.lower())

    # 3. Resolve provider
    base_url, api_key, real_model = _resolve_provider(routed_model, cr)

    mode = "orchestrator" if is_orchestrator else f"{domain}/{complexity}"
    print(f"[proxy] {mode} ({classifier_used}) → {routed_model} (real: {real_model})"
          f"{' [ESCALATED]' if escalated else ''}",
          file=sys.stderr, flush=True)

    forward_body = {**body, "model": real_model}
    # Apply a generous output-token default when the caller omits one, so
    # reasoning models have budget for thinking + answer instead of truncating.
    if not forward_body.get("max_tokens"):
        forward_body["max_tokens"] = _DEFAULT_MAX_TOKENS
    url = f"{base_url}/chat/completions"
    routing_headers = {
        "X-Routed-Model": routed_model,
        "X-Domain": domain,
        "X-Complexity": complexity,
        "X-Escalated": "true" if escalated else "false",
        "X-Classifier": classifier_used,
        "X-User": getattr(request.state, "user", "") or "",
    }

    _ESCALATION_NOTE = (
        f"> _[smart-ai-router] This {domain}/{complexity} task exceeded the "
        f"capability of every available lower-cost model, so it was escalated "
        f"to {routed_model} — the most capable (and most expensive) tier. "
        f"Escalation happens only when necessary._\n\n"
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
                fwd["max_tokens"] = _DEFAULT_MAX_TOKENS
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
                   complexity=complexity, usage=None, status=200)

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
            if escalated:
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
        async def _stream_generator() -> AsyncIterator[bytes]:
            # Emit an SSE comment immediately so the client sees the stream is
            # alive while we wait for the upstream provider's first token.
            yield b": smart-ai-router connected\n\n"
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
                            return
                        # Record the request for attribution + request-count
                        # quotas. Token counts aren't tallied for streams yet
                        # (usage isn't emitted mid-stream), so they log as 0.
                        _log_usage(
                            cr, request,
                            routed_model=routed_model, domain=domain,
                            complexity=complexity, usage=None,
                            status=resp.status_code,
                        )
                        # Prepend escalation note as a synthetic first delta chunk
                        if escalated:
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
                        # the socket, so tokens reach the browser immediately.
                        async for chunk in resp.aiter_raw():
                            yield chunk
            except httpx.RequestError as exc:
                yield f"data: {json.dumps({'error': f'proxy upstream error: {exc}'})}\n\n".encode()
                yield b"data: [DONE]\n\n"

        return StreamingResponse(
            _stream_generator(),
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
        if escalated:
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
        )
        return JSONResponse(content=data, headers=routing_headers)
