"""Find out which models can really call a tool, by asking them to.

A model that can't leaks the call as prose in a family-specific syntax
(<function=Read>, <|tool_call>call:x::y{}, <|python_tag|>) and the client prints
it instead of running it. Waiting to see that on a live turn costs a user's
request per model; one cheap probe at sync time costs a fraction of a cent.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx

from smart_ai_router import settings as _settings

_TOOL = {"type": "function", "function": {
    "name": "get_weather", "description": "Get the current weather for a city.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "required": ["city"]}}}
_PROBE_BODY = {
    "messages": [{"role": "user",
                  "content": "What's the weather in Paris right now? Use the tool."}],
    "tools": [_TOOL], "max_tokens": 400, "temperature": 0,
}
_REPROBE_DAYS = 7  # providers change what a model id serves; verdicts expire
_CONCURRENCY = 8


def classify(reply: dict) -> tuple[str, str]:
    """(verdict, note) for a chat-completion reply to the probe."""
    from smart_ai_router.api.proxy import _leaks_tool_call  # avoid import cycle

    msg = ((reply.get("choices") or [{}])[0] or {}).get("message") or {}
    calls = msg.get("tool_calls") or []
    content = msg.get("content") or ""
    if calls:
        try:
            json.loads(calls[0]["function"]["arguments"] or "{}")
            return "ok", ""
        except (KeyError, TypeError, ValueError):
            return "text", f"unparseable arguments: {calls[0]!r}"[:300]
    if _leaks_tool_call(content):
        return "text", content[:300]
    return "none", content[:300]


async def _probe(client, sem, cr, spec) -> str:
    from smart_ai_router.api.proxy import _headers, _resolve_provider

    base_url, api_key, real_model = _resolve_provider(spec.value, cr)
    async with sem:
        try:
            r = await client.post(
                f"{base_url}/chat/completions", headers=_headers(api_key),
                json={**_PROBE_BODY, "model": real_model},
            )
        except httpx.RequestError:
            return "error"
    if r.status_code == 200:
        verdict, note = classify(r.json())
    elif r.status_code == 404 and "tool" in r.text.lower():
        verdict, note = "unsupported", r.text[:300]
    else:
        return "error"  # unknown, not a verdict: left unprobed to retry next time
    cr.set_tool_probe(spec.value, verdict, note)
    return verdict


async def probe_models(cr, *, limit: int = 60, retest: bool = False) -> dict:
    """Probe OpenRouter models that have no fresh verdict. Returns counts by
    verdict plus `remaining` (still unprobed after this call's `limit`)."""
    max_cost = _settings.get_int("tool_probe_max_cost")
    if max_cost <= 0:
        return {"enabled": False}
    stale = (datetime.now(timezone.utc) - timedelta(days=_REPROBE_DAYS)).isoformat()
    todo = [
        s for s in cr.all_models()
        if s.provider == "openrouter" and s.tools and s.cost_output <= max_cost
        and (retest or not s.tool_probe_at or s.tool_probe_at < stale)
    ]
    batch = todo[:limit]
    sem = asyncio.Semaphore(_CONCURRENCY)
    async with httpx.AsyncClient(timeout=60) as client:
        verdicts = await asyncio.gather(*(_probe(client, sem, cr, s) for s in batch))
    out: dict = {v: verdicts.count(v)
                 for v in ("ok", "text", "none", "unsupported", "error")}
    out["remaining"] = len(todo) - len(batch)
    return out
