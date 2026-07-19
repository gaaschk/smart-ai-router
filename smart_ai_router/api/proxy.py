"""
OpenAI-compatible proxy endpoint.

Every POST /v1/chat/completions is:
  1. Classified (domain + complexity) from the last user message.
  2. Routed to the cheapest-qualifying model via CapabilityRouter.
  3. Forwarded to the real provider with async httpx, streaming back verbatim.

Supported provider prefixes in the model value:
  openrouter/<vendor>/<model>  -> https://openrouter.ai/api/v1
  ollama/<model>               -> stored ollama base_url (default http://localhost:11434)
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


def _resolve_provider(model_value: str, cr) -> tuple[str, str, str]:
    """Return (base_url, api_key, real_model_id)."""
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

    # 1. Classify
    prompt_text = _extract_prompt(messages)
    domain, complexity = classify(prompt_text) if prompt_text else ("general", "trivial")

    # 2. Route
    try:
        routed_model = cr.route(
            domain,
            complexity,
            needs_tools=bool(body.get("tools")),
            est_tokens=sum(len(str(m.get("content", ""))) // 4 for m in messages),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # 3. Resolve provider
    base_url, api_key, real_model = _resolve_provider(routed_model, cr)

    print(f"[proxy] {domain}/{complexity} → {routed_model} (real: {real_model})",
          file=sys.stderr, flush=True)

    forward_body = {**body, "model": real_model}
    url = f"{base_url}/chat/completions"
    routing_headers = {
        "X-Routed-Model": routed_model,
        "X-Domain": domain,
        "X-Complexity": complexity,
    }

    # 4. Forward with async httpx
    if stream:
        async def _stream_generator() -> AsyncIterator[bytes]:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST", url,
                    headers=_headers(api_key),
                    json=forward_body,
                ) as resp:
                    if resp.status_code >= 400:
                        error = await resp.aread()
                        yield f"data: {json.dumps({'error': error.decode(errors='replace')})}\n\n".encode()
                        return
                    async for chunk in resp.aiter_bytes(4096):
                        yield chunk

        return StreamingResponse(
            _stream_generator(),
            media_type="text/event-stream",
            headers=routing_headers,
        )
    else:
        async with httpx.AsyncClient(timeout=120) as client:
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

        return JSONResponse(content=resp.json(), headers=routing_headers)
