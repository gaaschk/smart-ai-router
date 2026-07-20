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

import json
import sys
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from smart_ai_router.classifier import classify

proxy_router = APIRouter()

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Model-name markers that force the orchestrator (Claude) path.
_ORCHESTRATOR_MARKERS = ("smart-orchestrator", "orchestrator")


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


def _headers(api_key: str) -> dict[str, str]:
    h = {"Content-Type": "application/json",
         "HTTP-Referer": "https://github.com/gaaschk/smart-ai-router"}
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

    # 1. Classify
    prompt_text = _extract_prompt(messages)
    domain, complexity = classify(prompt_text) if prompt_text else ("general", "trivial")

    # Detect image content in any message
    needs_vision = any(
        isinstance(m.get("content"), list) and
        any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
        for m in messages
    )

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
    else:
        try:
            routed_model = cr.route(
                domain,
                complexity,
                needs_tools=bool(body.get("tools")),
                needs_vision=needs_vision,
                est_tokens=sum(len(str(m.get("content", ""))) // 4 for m in messages),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    # Worker path escalated to Claude — no cheaper model cleared the quality bar.
    # Claude is the most expensive tier, so surface a note to the user.
    escalated = (not is_orchestrator) and ("claude" in routed_model.lower())

    # 3. Resolve provider
    base_url, api_key, real_model = _resolve_provider(routed_model, cr)

    mode = "orchestrator" if is_orchestrator else f"{domain}/{complexity}"
    print(f"[proxy] {mode} → {routed_model} (real: {real_model})"
          f"{' [ESCALATED]' if escalated else ''}",
          file=sys.stderr, flush=True)

    forward_body = {**body, "model": real_model}
    url = f"{base_url}/chat/completions"
    routing_headers = {
        "X-Routed-Model": routed_model,
        "X-Domain": domain,
        "X-Complexity": complexity,
        "X-Escalated": "true" if escalated else "false",
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
                        async for chunk in resp.aiter_bytes(4096):
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
        return JSONResponse(content=data, headers=routing_headers)
