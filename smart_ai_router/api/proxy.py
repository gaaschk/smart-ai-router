"""
OpenAI-compatible proxy endpoint.

Every POST /v1/chat/completions is:
  1. Classified (domain + complexity) from the last user message.
  2. Routed to the cheapest-qualifying model via CapabilityRouter.
  3. Forwarded to the real provider, streaming the response back verbatim.

Supported provider prefixes in the model value:
  openrouter/<vendor>/<model>  -> https://openrouter.ai/api/v1
  ollama/<model>               -> OLLAMA_BASE_URL (default http://localhost:11434)
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from smart_ai_router.classifier import classify

proxy_router = APIRouter()

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"


# ── helpers ───────────────────────────────────────────────────────────────────

def _extract_prompt(messages: list[dict]) -> str:
    """Return the last user-role message content as a string."""
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
    """Return (base_url, api_key, real_model_id) for the given routed model."""
    if model_value.startswith("openrouter/"):
        real_model = model_value[len("openrouter/"):]
        # Look for stored OpenRouter provider key
        api_key = ""
        for p in cr.all_providers():
            if p.kind == "openrouter" and p.api_key:
                api_key = p.api_key
                break
        return _OPENROUTER_BASE, api_key, real_model

    if model_value.startswith("ollama/"):
        real_model = model_value[len("ollama/"):]
        base_url = "http://localhost:11434"
        for p in cr.all_providers():
            if p.kind == "ollama" and p.base_url:
                base_url = p.base_url.rstrip("/")
                break
        return f"{base_url}/v1", "", real_model

    # Unknown prefix — try to forward as-is to OpenRouter
    for p in cr.all_providers():
        if p.kind == "openrouter" and p.api_key:
            return _OPENROUTER_BASE, p.api_key, model_value
    raise HTTPException(status_code=422, detail=f"Cannot resolve provider for model {model_value!r}")


def _build_request(base_url: str, api_key: str, body: dict) -> urllib.request.Request:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    headers["HTTP-Referer"] = "https://github.com/gaaschk/smart-ai-router"
    return urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )


# ── endpoint ──────────────────────────────────────────────────────────────────

@proxy_router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body: dict[str, Any] = await request.json()
    cr = request.app.state.capability_router

    messages = body.get("messages", [])
    stream = body.get("stream", False)

    # 1. Classify the prompt
    prompt_text = _extract_prompt(messages)
    domain, complexity = classify(prompt_text) if prompt_text else ("general", "trivial")

    # 2. Route to the best model
    try:
        routed_model = cr.route(
            domain,
            complexity,
            needs_tools=bool(body.get("tools")),
            est_tokens=sum(len(str(m.get("content", ""))) // 4 for m in messages),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # 3. Resolve provider URL + credentials
    base_url, api_key, real_model = _resolve_provider(routed_model, cr)

    # 4. Forward request with real model id
    forward_body = {**body, "model": real_model}
    req = _build_request(base_url, api_key, forward_body)

    # Log routing decision to stderr (visible in service logs)
    import sys
    print(
        f"[proxy] {domain}/{complexity} → {routed_model} (real: {real_model})",
        file=sys.stderr, flush=True,
    )

    # 5. Stream or return response
    try:
        if stream:
            def _generate():
                try:
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        while True:
                            chunk = resp.read(4096)
                            if not chunk:
                                break
                            yield chunk
                except urllib.error.HTTPError as e:
                    error_body = e.read().decode(errors="replace")
                    yield f"data: {json.dumps({'error': error_body})}\n\n".encode()

            return StreamingResponse(
                _generate(),
                media_type="text/event-stream",
                headers={
                    "X-Routed-Model": routed_model,
                    "X-Domain": domain,
                    "X-Complexity": complexity,
                },
            )
        else:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.load(resp)
            from fastapi.responses import JSONResponse
            return JSONResponse(
                content=data,
                headers={
                    "X-Routed-Model": routed_model,
                    "X-Domain": domain,
                    "X-Complexity": complexity,
                },
            )

    except urllib.error.HTTPError as e:
        error_body = e.read().decode(errors="replace")
        raise HTTPException(status_code=e.code, detail=error_body)
    except urllib.error.URLError as e:
        raise HTTPException(status_code=502, detail=f"Provider unreachable: {e.reason}")
