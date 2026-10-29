"""Provider-independent orchestrator routing and canary integration tests."""

import warnings
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.app import create_app
from smart_ai_router.api.proxy import (
    _orchestrator_capable,
    _pinned_pool,
    _pool_pin,
)
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec
from smart_ai_router.store.sqlite_store import SqliteStore

_REPLY = {
    "id": "cmpl-1",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "answer."},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

# A weak-but-cheap Claude and a strong-but-dear one, so a routing choice between
# them is visible. Scores are shaped like the real matrix: haiku-4.5 sits well
# below the 0.80 floor the old code used, which is exactly why it was unreachable.
_HAIKU = ModelSpec(
    "openrouter/anthropic/claude-haiku-4.5",
    provider="openrouter",
    cost=2,
    ctx_k=200,
    tools=True,
    reliability=1.0,
    competence={"coding": 0.70, "docs": 0.70, "reasoning": 0.62, "general": 0.68},
    profile={
        "software_engineering": 0.70,
        "law_regulatory": 0.55,
        "general_knowledge": 0.68,
    },
)
_OPUS = ModelSpec(
    "openrouter/anthropic/claude-opus-4.8",
    provider="openrouter",
    cost=9,
    ctx_k=200,
    tools=True,
    reliability=1.0,
    competence={"coding": 0.95, "docs": 0.93, "reasoning": 0.95, "general": 0.93},
    profile={
        "software_engineering": 0.95,
        "law_regulatory": 0.90,
        "general_knowledge": 0.93,
    },
)
# Cheap, capable fixture; individual tests vary its tool eligibility.
_LOCAL = ModelSpec(
    "ollama/qwen3-coder:30b",
    provider="ollama",
    cost=0,
    ctx_k=200,
    tools=True,
    reliability=1.0,
    competence={"coding": 0.90, "docs": 0.85, "reasoning": 0.85, "general": 0.88},
    profile={
        "software_engineering": 0.90,
        "law_regulatory": 0.80,
        "general_knowledge": 0.88,
    },
)


_STREAM_CHUNKS = [
    b'data: {"choices":[{"index":0,"delta":{"content":"answer."}}]}\n\n',
    b"data: [DONE]\n\n",
]


class _FakeStream:
    """Minimal stand-in for httpx's streaming response context manager."""

    status_code = 200
    headers: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_raw(self):
        for chunk in _STREAM_CHUNKS:
            yield chunk


def _client(*specs, monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    monkeypatch.delenv("SMART_ROUTER_MODEL_DENYLIST", raising=False)
    # Empty classifier chain → the keyword profiler runs, so no network.
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_FALLBACK", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_REFINE_MODEL", "")

    async def fake_post(self, url, **kwargs):
        return httpx.Response(200, json=_REPLY, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(
        httpx.AsyncClient, "stream", lambda self, *a, **kw: _FakeStream()
    )

    store = SqliteStore(":memory:")
    for spec in specs:
        store.upsert_model(spec)
        if spec.tool_probe:
            store.set_tool_probe(spec.value, spec.tool_probe)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return TestClient(create_app(CapabilityRouter(store=store)))


@pytest.fixture
def client(monkeypatch):
    return _client(
        _HAIKU,
        _OPUS,
        replace(_LOCAL, tools=False),
        replace(_OPUS, value="openrouter/openai/gpt-5", cost=10),
        monkeypatch=monkeypatch,
    )


@pytest.fixture
def weak_only(monkeypatch):
    """Nothing here clears a hard prompt, so every route is under-qualified —
    the condition the escalation note exists for."""
    return _client(_HAIKU, monkeypatch=monkeypatch)


def _chat(client, prompt, *, model="smart-orchestrator", **extra):
    return client.post(
        "/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            **extra,
        },
    )


_NOTE_PHRASE = "no available model clears that bar"

_TRIVIAL = "say hi"
_HARD = (
    "Analyze the statutory and regulatory liability of an autonomous reactor "
    "control system under the licensing regimes of 48 jurisdictions, citing the "
    "controlling provisions for each."
)


# ── The pool ──────────────────────────────────────────────────────────────────


def test_orchestrator_requires_tools_even_without_tools_array(client):
    """The local model is cheaper and clears the bar, so the worker path takes
    it. Orchestrator mode must reject it when tool support is absent."""
    assert (
        _chat(client, _TRIVIAL, model="smart-worker").headers["X-Routed-Model"]
        == "ollama/qwen3-coder:30b"
    )
    assert "claude" in _chat(client, _TRIVIAL).headers["X-Routed-Model"]


def test_orchestrator_422s_when_no_tool_model_is_configured(monkeypatch):
    client = _client(replace(_LOCAL, tools=False), monkeypatch=monkeypatch)
    resp = _chat(client, _TRIVIAL)
    assert resp.status_code == 422
    assert "tool" in resp.json()["detail"].lower()


# ── The tier, within the pool ─────────────────────────────────────────────────


def test_a_trivial_turn_gets_the_cheap_claude(client):
    """The regression this change is for: haiku-4.5's general score (0.68) is
    below the old 0.80 floor, so every orchestrator request — including this one
    — used to be billed at Opus rates."""
    r = _chat(client, _TRIVIAL)
    assert r.headers["X-Routed-Model"] == "openrouter/anthropic/claude-haiku-4.5"


def test_a_hard_turn_escalates_within_the_pool(client):
    r = _chat(client, _HARD)
    assert r.headers["X-Routed-Model"] == "openrouter/anthropic/claude-opus-4.8"


def test_orchestrator_reports_why_it_picked(client):
    """Orchestrator mode used to skip selection entirely and so had no reason to
    report. Now it routes like any other request, so the headers must say so."""
    r = _chat(client, _TRIVIAL)
    assert r.headers["X-Routing-Why"]
    assert r.headers["X-Qualified"] == "true"


def test_orchestrator_still_answers_when_no_claude_qualifies(weak_only):
    """A competence shortfall returns the closest eligible model, flagged in headers."""
    r = _chat(weak_only, _HARD)
    assert r.status_code == 200
    assert r.headers["X-Routed-Model"] == "openrouter/anthropic/claude-haiku-4.5"
    assert r.headers["X-Qualified"] == "false"


# ── The escalation note ───────────────────────────────────────────────────────


def test_no_prose_is_injected_into_an_orchestrator_answer(weak_only):
    """An under-qualified pick earns a human reader a caveat, but orchestrator
    mode is a tool loop: prose in the assistant turn reads as the model's answer
    mid-loop, and becomes history the client re-sends (and re-pays for) forever.
    The header still tells the truth."""
    r = _chat(weak_only, _HARD)
    assert r.headers["X-Escalated"] == "true"
    assert r.json()["choices"][0]["message"]["content"] == "answer."


def test_no_prose_is_injected_when_the_client_sends_tools(weak_only):
    """Same reasoning, reached the other way: any client shipping tool
    definitions is driving a loop, whatever model name it asked for."""
    r = _chat(
        weak_only,
        _HARD,
        model="smart-worker",
        tools=[
            {
                "type": "function",
                "function": {"name": "noop", "parameters": {"type": "object"}},
            }
        ],
    )
    assert r.headers["X-Escalated"] == "true"
    assert r.json()["choices"][0]["message"]["content"] == "answer."


def test_no_prose_is_injected_into_an_orchestrator_stream(weak_only):
    """The path Claude Code actually takes. A synthetic first delta carrying prose
    is worse here than in a buffered response: the client renders it as the start
    of the model's turn before any real token arrives."""
    r = _chat(weak_only, _HARD, stream=True)
    assert r.status_code == 200
    assert r.headers["X-Escalated"] == "true"
    # Not just "smart-ai-router" — the SSE preamble is a comment by that name.
    assert _NOTE_PHRASE not in r.text


def test_a_human_stream_still_gets_the_caveat(weak_only):
    r = _chat(weak_only, _HARD, model="smart-worker", stream=True)
    assert _NOTE_PHRASE in r.text


def test_a_human_client_still_gets_the_caveat(weak_only):
    """The control for the two above: no tools, no orchestrator → note injected.
    Guards against suppressing it for everyone."""
    r = _chat(weak_only, _HARD, model="smart-worker")
    assert r.headers["X-Escalated"] == "true"
    assert _NOTE_PHRASE in r.json()["choices"][0]["message"]["content"]


# ── The canary ────────────────────────────────────────────────────────────────
# A named challenger must qualify; otherwise normal pool selection applies.


def _canary(monkeypatch, model, percent=100):
    monkeypatch.setenv("SMART_ROUTER_ORCHESTRATOR_CANARY_MODEL", model)
    monkeypatch.setenv("SMART_ROUTER_ORCHESTRATOR_CANARY_PERCENT", str(percent))


def test_no_canary_by_default(client, monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_ORCHESTRATOR_CANARY_MODEL", raising=False)
    monkeypatch.delenv("SMART_ROUTER_ORCHESTRATOR_CANARY_PERCENT", raising=False)
    r = _chat(client, _TRIVIAL)
    assert "claude" in r.headers["X-Routed-Model"]
    assert "X-Canary" not in r.headers


def test_a_named_canary_takes_the_turn(client, monkeypatch):
    _canary(monkeypatch, "openrouter/openai/gpt-5")
    r = _chat(client, _TRIVIAL)
    assert r.headers["X-Routed-Model"] == "openrouter/openai/gpt-5"
    assert r.headers["X-Canary"] == "true"


def test_a_zero_share_is_off_even_when_named(client, monkeypatch):
    _canary(monkeypatch, "ollama/qwen3-coder:30b", percent=0)
    assert "claude" in _chat(client, _TRIVIAL).headers["X-Routed-Model"]


def test_an_unknown_canary_falls_back_to_claude(client, monkeypatch):
    """A typo in a settings field must not take the lane down."""
    _canary(monkeypatch, "openrouter/does-not/exist")
    r = _chat(client, _TRIVIAL)
    assert "claude" in r.headers["X-Routed-Model"]
    assert "X-Canary" not in r.headers


def test_the_canary_loses_a_turn_it_does_not_qualify_for(monkeypatch):
    """The whole safety property: the canary is routed through the same
    select_from as the pool, so the prompt's own bar still applies. A cheap weak
    model gets the mechanical turns it can handle and none of the hard ones."""
    weak = ModelSpec(
        "ollama/tiny:1b",
        provider="ollama",
        cost=0,
        ctx_k=200,
        tools=True,
        reliability=1.0,
        competence={"coding": 0.55, "docs": 0.55, "reasoning": 0.5, "general": 0.6},
        profile={
            "software_engineering": 0.55,
            "law_regulatory": 0.45,
            "general_knowledge": 0.60,
        },
    )
    client = _client(_HAIKU, _OPUS, weak, monkeypatch=monkeypatch)
    _canary(monkeypatch, "ollama/tiny:1b")
    assert _chat(client, _TRIVIAL).headers["X-Canary"] == "true"

    hard = _chat(client, _HARD)
    assert hard.headers["X-Routed-Model"] == "openrouter/anthropic/claude-opus-4.8"
    assert "X-Canary" not in hard.headers


def test_the_canary_does_not_touch_the_worker_lane(client, monkeypatch):
    """The worker lane already considers every model in scope and picks on cost;
    a canary there would only override that with a fixed choice."""
    _canary(monkeypatch, "openrouter/anthropic/claude-opus-4.8")
    r = _chat(client, _TRIVIAL, model="smart-worker")
    assert r.headers["X-Routed-Model"] == _LOCAL.value
    assert "X-Canary" not in r.headers


def test_a_canary_turn_is_still_logged_and_answered(client, monkeypatch):
    _canary(monkeypatch, "openrouter/openai/gpt-5")
    r = _chat(client, _TRIVIAL)
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "answer."
    # Billed against the canary, not the Claude it replaced — otherwise the
    # Usage page could not answer "what did the experiment cost".
    by_model = client.get("/api/usage").json()["by_model"]
    assert [r["key"] for r in by_model] == ["openrouter/openai/gpt-5"]


# ── Eligibility ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "openrouter/openai/gpt-5",
        "openrouter/google/gemini-2.5-pro",
        "ollama/qwen3-coder:30b",
        _HAIKU.value,
    ],
)
def test_capable_models(value, monkeypatch):
    spec = replace(_LOCAL, value=value)
    assert _orchestrator_capable(spec)
    response = _chat(_client(spec, monkeypatch=monkeypatch), _TRIVIAL)
    assert response.status_code == 200
    assert response.headers["X-Routed-Model"] == value


@pytest.mark.parametrize(
    "changes",
    [dict(tools=False), dict(tool_probe="text"), dict(tool_probe="unsupported")],
)
def test_incapable_models(changes):
    assert not _orchestrator_capable(replace(_HAIKU, **changes))


@pytest.mark.parametrize(
    "changes",
    [
        dict(agentic=0.01),
        dict(tool_probe="text"),
        dict(tools=False),
        dict(reliability=0.1),
    ],
)
def test_orchestrator_filters_incapable_openai(changes, monkeypatch):
    spec = replace(_LOCAL, value="openrouter/openai/gpt-5", **changes)
    response = _chat(_client(_HAIKU, spec, monkeypatch=monkeypatch), _TRIVIAL)
    assert response.headers["X-Routed-Model"] == _HAIKU.value


def test_openai_can_win_against_claude_without_canary(monkeypatch):
    spec = replace(_LOCAL, value="openrouter/openai/gpt-5")
    response = _chat(_client(_HAIKU, _OPUS, spec, monkeypatch=monkeypatch), _TRIVIAL)
    assert response.headers["X-Routed-Model"] == spec.value
    assert "X-Canary" not in response.headers


def test_orchestrator_applies_agent_denylist_without_tools_array(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_AGENT_DENYLIST", "gpt-5")
    spec = replace(_LOCAL, value="openrouter/openai/gpt-5")
    response = _chat(_client(_HAIKU, spec, monkeypatch=monkeypatch), _TRIVIAL)
    assert response.headers["X-Routed-Model"] == _HAIKU.value


def test_canary_cannot_bypass_tool_requirement(client, monkeypatch):
    _canary(monkeypatch, _LOCAL.value)
    response = _chat(client, _TRIVIAL)
    assert response.headers["X-Routed-Model"] == _HAIKU.value
    assert "X-Canary" not in response.headers


# ── vendor pins ───────────────────────────────────────────────────────────────
# Vendor pins restrict the pool while retaining prompt-based selection.

_CATALOG = [
    ModelSpec("openrouter/anthropic/claude-sonnet-5", provider="openrouter"),
    ModelSpec("openrouter/openai/gpt-5.6-luna", provider="openrouter"),
    ModelSpec("openrouter/~openai/gpt-5.6-sol", provider="openrouter"),
    ModelSpec("openrouter/x-ai/grok-4.6", provider="openrouter"),
    ModelSpec("ollama/qwen3-coder:30b", provider="ollama"),
]


@pytest.mark.parametrize(
    "requested,pin",
    [
        ("smart-openai", "openai"),
        ("smart-grok", "grok"),
        # Claudish prefixes a provider role it strips itself; only the tail routes.
        ("ll@smart-anthropic", "anthropic"),
        ("SMART-OpenAI", "openai"),
        # Open-pool synonyms and anything not named smart-* pin nothing.
        ("smart-auto", ""),
        ("smart-all", ""),
        ("smart-router", ""),
        ("smart-worker", ""),
        ("gpt-4", ""),
        ("", ""),
    ],
)
def test_pool_pin_parses(requested, pin):
    assert _pool_pin(requested) == pin


@pytest.mark.parametrize(
    "pin,expected",
    [
        # Vendor segment, with OpenRouter's ~vendor variant folded into the vendor.
        (
            "openai",
            ["openrouter/openai/gpt-5.6-luna", "openrouter/~openai/gpt-5.6-sol"],
        ),
        ("anthropic", ["openrouter/anthropic/claude-sonnet-5"]),
        # Provider, for one-segment ids that have no vendor of their own.
        ("ollama", ["ollama/qwen3-coder:30b"]),
        # Family names are what people actually type, and neither is a vendor:
        # grok ships under x-ai, sonnet under anthropic.
        ("grok", ["openrouter/x-ai/grok-4.6"]),
        ("sonnet", ["openrouter/anthropic/claude-sonnet-5"]),
        # A typo matches nothing, so the caller gets a 422 instead of the silent
        # every-vendor pool that would be indistinguishable from a working pin.
        ("opnai", []),
    ],
)
def test_pinned_pool(pin, expected):
    assert [s.value for s in _pinned_pool(pin, _CATALOG)] == expected


def test_vendor_pin_beats_coincidental_substring():
    """An exact vendor must not lose to a substring hit elsewhere in the catalog."""
    catalog = _CATALOG + [
        ModelSpec("openrouter/acme/openai-clone-v2", provider="openrouter")
    ]
    assert "openrouter/acme/openai-clone-v2" not in [
        s.value for s in _pinned_pool("openai", catalog)
    ]
