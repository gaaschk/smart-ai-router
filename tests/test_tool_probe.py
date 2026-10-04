"""The router asks each model for a real tool call, and routes around the ones
that answer in prose."""
import asyncio

import httpx

from smart_ai_router import tool_probe
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec
from smart_ai_router.store.sqlite_store import SqliteStore


def _reply(**msg):
    return {"choices": [{"message": {"role": "assistant", **msg}}]}


def test_classify():
    call = {"function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}
    assert tool_probe.classify(_reply(tool_calls=[call]))[0] == "ok"
    bad = {"function": {"name": "get_weather", "arguments": "{city:"}}
    assert tool_probe.classify(_reply(tool_calls=[bad]))[0] == "text"
    verdict, note = tool_probe.classify(
        _reply(content='<|tool_call>call:x::get_weather{city:"Paris"}<tool_call|>'))
    assert verdict == "text" and "get_weather" in note
    assert tool_probe.classify(_reply(content="It is sunny."))[0] == "none"


def test_probe_stores_verdicts_and_routing_skips_text_models(monkeypatch):
    def spec(v, cost):
        return ModelSpec(value=v, provider="openrouter", ctx_k=200, tools=True,
                         cost=cost, cost_output=1.0,
                         profile={"software_engineering": 0.95})
    store = SqliteStore(":memory:")
    for s in (spec("openrouter/x/leaky", 1), spec("openrouter/x/solid", 5)):
        store.upsert_model(s)
    cr = CapabilityRouter(store=store)

    async def fake_post(self, url, **kw):
        body = (_reply(content="<function=get_weather>{}") if "leaky" in kw["json"]["model"]
                else _reply(tool_calls=[{"function": {"name": "get_weather",
                                                      "arguments": "{}"}}]))
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    out = asyncio.run(tool_probe.probe_models(cr))
    assert (out["ok"], out["text"], out["remaining"]) == (1, 1, 0)
    assert cr.get_model("openrouter/x/leaky").tool_probe == "text"
    # a second call has nothing left to do: verdicts are fresh
    assert asyncio.run(tool_probe.probe_models(cr))["ok"] == 0

    from smart_ai_router.taxonomy import profile_from_labels
    prof = profile_from_labels("coding", "moderate")
    d = cr.select(prof, needs_tools=True)
    assert d.model == "openrouter/x/solid"


def test_a_live_leak_marks_the_model_and_negative_prices_are_not_rates(monkeypatch):
    from smart_ai_router import pricing
    from smart_ai_router.api.proxy import _log_usage

    store = SqliteStore(":memory:")
    store.upsert_model(ModelSpec(value="openrouter/x/leaky", provider="openrouter",
                                 tools=True, cost_input=1.0, cost_output=1.0))
    cr = CapabilityRouter(store=store)

    class _Req:  # _log_usage only reads request.state
        class state:
            user, key_prefix = "admin", ""
    _log_usage(cr, _Req, routed_model="openrouter/x/leaky", domain="d",
               complexity="c", usage={"completion_tokens": 300}, status=200,
               tools_offered=True, tool_calls=[],
               content="I'll look.\n<|tool_call>call:x::ls{}<tool_call|>")
    m = cr.get_model("openrouter/x/leaky")
    assert m.tool_probe == "text" and "tool_call" in m.tool_probe_note

    neg = ModelSpec(value="v", provider="openrouter", cost_input=-1e6, cost_output=-1e6)
    assert pricing.cost_for(neg, 1000, 1000) is None
