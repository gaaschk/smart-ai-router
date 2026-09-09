"""The caller's params vs. the model the caller never saw.

A client names a model *class* ("smart-worker", "auto") and never learns which
model answered, so every model-specific knob in its body is a guess about the
pick. The router used to forward the body verbatim, and a wrong guess is not
ignored — it is a provider 400 that turns a routed request into no answer:

    reasoning_effort + a coding prompt → ollama/qwen3-coder:30b
    → 400 '"qwen3-coder:30b" does not support thinking'

These tests pin the three ways that guess can be wrong, and which fix each one
gets: drop the param (a preference the answer survives without), clamp it (an
output ceiling), or *route* on it (a schema, where dropping is a silent failure).
"""
import warnings

import httpx
import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec
from smart_ai_router.store.sqlite_store import SqliteStore

_REPLY = {
    "id": "cmpl-1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "answer."},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

_CODING = {"software_engineering": 0.95, "general_knowledge": 0.75}


def _spec(value, **kw) -> ModelSpec:
    return ModelSpec(
        value=value, provider="openrouter", ctx_k=200, reliability=1.0,
        tools=True, profile=dict(_CODING),
        competence={"coding": 0.95, "reasoning": 0.8, "docs": 0.8, "general": 0.8},
        **kw,
    )


def _client(monkeypatch, *specs: ModelSpec) -> TestClient:
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    monkeypatch.delenv("SMART_ROUTER_MODEL_DENYLIST", raising=False)
    # No classifier models → keyword profiling only, so no test touches a network.
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_FALLBACK", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_REFINE_MODEL", "")

    sent: list[dict] = []

    async def fake_post(self, url, **kwargs):
        sent.append(kwargs.get("json") or {})
        return httpx.Response(200, json=_REPLY, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    store = SqliteStore(":memory:")
    for s in specs:
        store.upsert_model(s)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = TestClient(create_app(CapabilityRouter(store=store)))
    c.sent = sent       # forwarded bodies, for asserting what the provider saw
    return c


def _chat(client, **extra):
    return client.post("/v1/chat/completions", json={
        "model": "auto",
        "messages": [{"role": "user", "content": "Refactor this Python parser."}],
        "stream": False,
        **extra,
    })


# ── thinking knobs on a model that can't think ────────────────────────────────

def test_reasoning_effort_is_dropped_for_a_non_reasoning_model(monkeypatch):
    c = _client(monkeypatch, _spec("openrouter/plain-coder", cost=1, reasoning=False))
    r = _chat(c, reasoning_effort="high")
    assert r.status_code == 200
    assert "reasoning_effort" not in c.sent[-1]
    # …and the caller is told, so a knob that did nothing is visible rather than
    # mysterious. This header is the whole reason dropping is acceptable.
    assert r.headers["X-Dropped-Params"] == "reasoning_effort"


def test_every_spelling_of_the_thinking_knob_is_dropped(monkeypatch):
    # OpenAI, OpenRouter (object + legacy bool) and Anthropic all name it
    # differently, and a translation layer in front of the router forwards
    # whichever its client used.
    c = _client(monkeypatch, _spec("openrouter/plain-coder", cost=1, reasoning=False))
    r = _chat(
        c,
        reasoning_effort="high",
        reasoning={"effort": "high"},
        include_reasoning=True,
        thinking={"type": "adaptive"},
    )
    assert r.status_code == 200
    for p in ("reasoning_effort", "reasoning", "include_reasoning", "thinking"):
        assert p not in c.sent[-1]


def test_reasoning_effort_survives_to_a_reasoning_model(monkeypatch):
    # The filter is about the pick, not about the param: a model that can think
    # must still receive the caller's budget, or "drop what doesn't fit" quietly
    # becomes "never think".
    c = _client(monkeypatch, _spec("openrouter/thinker", cost=1, reasoning=True))
    r = _chat(c, reasoning_effort="high")
    assert c.sent[-1]["reasoning_effort"] == "high"
    assert r.headers["X-Dropped-Params"] == ""


# ── an output ceiling the caller couldn't have known ──────────────────────────

def test_caller_supplied_max_tokens_is_clamped_to_the_pick(monkeypatch):
    # Several providers *reject* a request over their output limit rather than
    # truncating the reply, so an un-clamped ask is no answer at all.
    c = _client(monkeypatch, _spec("openrouter/short-output", cost=1, max_output=4096))
    r = _chat(c, max_tokens=200_000)
    assert c.sent[-1]["max_tokens"] == 4096
    assert r.headers["X-Output-Limit"] == "4096"


def test_an_unknown_ceiling_leaves_max_tokens_alone(monkeypatch):
    # max_output == 0 means the catalog didn't say — which is every local model.
    # Guessing a ceiling there would truncate answers that were fine.
    c = _client(monkeypatch, _spec("openrouter/unknown-limit", cost=1, max_output=0))
    _chat(c, max_tokens=200_000)
    assert c.sent[-1]["max_tokens"] == 200_000


# ── a schema is routed on, not dropped ────────────────────────────────────────
# Dropping `response_format` would be the silent failure the router exists to
# prevent: the model answers in prose, the caller's parse finds nothing, and no
# error is raised anywhere. So the requirement moves into the pick instead.

_SCHEMA = {
    "type": "json_schema",
    "json_schema": {"name": "r", "schema": {"type": "object"}},
}


@pytest.fixture
def two_models(monkeypatch):
    """A cheaper model that ignores schemas, and a dearer one that honors them."""
    return _client(
        monkeypatch,
        _spec("openrouter/cheap-prose", cost=1, structured_outputs=False),
        _spec("openrouter/dear-schema", cost=2, cost_input=1.0, structured_outputs=True),
    )


def test_json_schema_request_routes_to_a_model_that_honors_it(two_models):
    r = _chat(two_models, response_format=_SCHEMA)
    assert r.status_code == 200
    assert r.headers["X-Routed-Model"] == "openrouter/dear-schema"
    assert two_models.sent[-1]["response_format"] == _SCHEMA


def test_without_a_schema_the_cheaper_model_still_wins(two_models):
    # Pins that the previous test measured the schema requirement and not just
    # this store's cheapest row.
    r = _chat(two_models)
    assert r.headers["X-Routed-Model"] == "openrouter/cheap-prose"


def test_a_plain_json_object_request_is_not_a_schema_requirement(two_models):
    # `json_object` asks for valid JSON, which any model can attempt; only
    # `json_schema` is a shape contract worth paying more for.
    r = _chat(two_models, response_format={"type": "json_object"})
    assert r.headers["X-Routed-Model"] == "openrouter/cheap-prose"


# ── the agent loop is the same routed model ───────────────────────────────────

def test_the_agent_loop_is_seeded_with_the_filtered_body(monkeypatch):
    """Every round of the agent loop hits the same pick, so it needs the same
    filtering. The loop used to be seeded from the raw request body, which handed
    a dropped param straight back to the model it was dropped for."""
    seen: list[dict] = []

    async def fake_loop(*, user, body, tool_schemas, stream_model, register_file):
        seen.append(body)
        yield b"data: [DONE]\n\n"

    monkeypatch.setattr("smart_ai_router.api.proxy.run_agent_loop", fake_loop)
    c = _client(monkeypatch, _spec("openrouter/plain-coder", cost=1, reasoning=False))
    r = _chat(c, agent=True, reasoning_effort="high", max_tokens=200_000)
    assert r.status_code == 200
    assert seen and "reasoning_effort" not in seen[0]
    assert seen[0]["model"] == "plain-coder"   # provider id, as the loop forwards it


def test_a_malformed_response_format_does_not_500(two_models):
    # Body is unvalidated client input: a string where a dict belongs must not
    # take the request down before it is even routed.
    r = _chat(two_models, response_format="json_schema")
    assert r.status_code == 200
