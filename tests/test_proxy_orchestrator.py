"""Integration tests for orchestrator-mode routing.

Orchestrator mode exists because only Claude models reliably drive Claude Code's
own loop — skills, Workflow/Agent tool calls, the harness's tool-use conventions.
It used to enforce that by taking the cheapest Claude above a general-competence
floor, ignoring the prompt profile it had just paid to compute. These tests pin
the replacement: the pool is Claude-only, and *within* that pool the prompt
decides the tier, so a mechanical turn is not billed at Opus rates.
"""
import warnings

import httpx
import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.app import create_app
from smart_ai_router.api.proxy import _orchestrator_capable
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec
from smart_ai_router.store.sqlite_store import SqliteStore

_REPLY = {
    "id": "cmpl-1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "answer."},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

# A weak-but-cheap Claude and a strong-but-dear one, so a routing choice between
# them is visible. Scores are shaped like the real matrix: haiku-4.5 sits well
# below the 0.80 floor the old code used, which is exactly why it was unreachable.
_HAIKU = ModelSpec(
    "openrouter/anthropic/claude-haiku-4.5", provider="openrouter", cost=2,
    ctx_k=200, tools=True, reliability=1.0,
    competence={"coding": 0.70, "docs": 0.70, "reasoning": 0.62, "general": 0.68},
    profile={"software_engineering": 0.70, "law_regulatory": 0.55,
             "general_knowledge": 0.68},
)
_OPUS = ModelSpec(
    "openrouter/anthropic/claude-opus-4.8", provider="openrouter", cost=9,
    ctx_k=200, tools=True, reliability=1.0,
    competence={"coding": 0.95, "docs": 0.93, "reasoning": 0.95, "general": 0.93},
    profile={"software_engineering": 0.95, "law_regulatory": 0.90,
             "general_knowledge": 0.93},
)
# Cheaper than either Claude and strong enough to win on the worker path — its
# presence is what proves the pool is actually being narrowed.
_LOCAL = ModelSpec(
    "ollama/qwen3-coder:30b", provider="ollama", cost=0, ctx_k=200,
    tools=True, reliability=1.0,
    competence={"coding": 0.90, "docs": 0.85, "reasoning": 0.85, "general": 0.88},
    profile={"software_engineering": 0.90, "law_regulatory": 0.80,
             "general_knowledge": 0.88},
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
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return TestClient(create_app(CapabilityRouter(store=store)))


@pytest.fixture
def client(monkeypatch):
    return _client(_HAIKU, _OPUS, _LOCAL, monkeypatch=monkeypatch)


@pytest.fixture
def weak_only(monkeypatch):
    """Nothing here clears a hard prompt, so every route is under-qualified —
    the condition the escalation note exists for."""
    return _client(_HAIKU, monkeypatch=monkeypatch)


def _chat(client, prompt, *, model="smart-orchestrator", **extra):
    return client.post("/v1/chat/completions", json={
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        **extra,
    })


_NOTE_PHRASE = "no available model clears that bar"

_TRIVIAL = "say hi"
_HARD = (
    "Analyze the statutory and regulatory liability of an autonomous reactor "
    "control system under the licensing regimes of 48 jurisdictions, citing the "
    "controlling provisions for each."
)


# ── The pool ──────────────────────────────────────────────────────────────────

def test_orchestrator_never_leaves_claude(client):
    """The local model is cheaper and clears the bar, so the worker path takes
    it. Orchestrator mode must not, however trivial the prompt."""
    assert _chat(client, _TRIVIAL, model="smart-worker").headers[
        "X-Routed-Model"] == "ollama/qwen3-coder:30b"
    assert "claude" in _chat(client, _TRIVIAL).headers["X-Routed-Model"]


def test_orchestrator_422s_when_no_claude_is_configured(monkeypatch):
    client = _client(_LOCAL, monkeypatch=monkeypatch)
    resp = _chat(client, _TRIVIAL)
    assert resp.status_code == 422
    assert "claude" in resp.json()["detail"].lower()


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
    """Narrowing the pool must not turn "the best Claude is not good enough" into
    a failure — the client cannot fall back to a non-Claude model, so the closest
    miss is the only useful answer. It is flagged, not refused."""
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
        weak_only, _HARD, model="smart-worker",
        tools=[{"type": "function",
                "function": {"name": "noop", "parameters": {"type": "object"}}}],
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


# ── Eligibility ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    # Modern ids name the family after "claude-", not the generation.
    "openrouter/anthropic/claude-sonnet-5",
    "openrouter/anthropic/claude-haiku-4.5",
    "openrouter/anthropic/claude-sonnet-4.5",
    "bedrock/anthropic.claude-opus-4-8-v1:0",
    # A "-latest" alias names no generation at all; excluding it would drop a
    # live model for lacking a digit, so unknown reads as eligible.
    "openrouter/anthropic/claude-haiku-latest",
    # Legacy but loop-capable: both of these drove Claude Code itself. Excluding
    # them would 422 a deployment whose only Claude is a 3.x Sonnet.
    "openrouter/anthropic/claude-3-5-sonnet",
    "bedrock/anthropic.claude-3-7-sonnet-20250219-v1:0",
])
def test_capable_models(value):
    assert _orchestrator_capable(ModelSpec(value))


@pytest.mark.parametrize("value", [
    "ollama/qwen3-coder:30b",
    "openrouter/openai/gpt-5",
    "openrouter/google/gemini-2.5-pro",
    # Original March-2024 generation: it can emit a tool call but loses the thread
    # over a long loop, and nothing in the prompt profile scores loop stamina.
    "openrouter/anthropic/claude-3-haiku",
    "openrouter/anthropic/claude-3-opus",
    # Older still — no tool use at all.
    "bedrock/anthropic.claude-v2:1",
    "openrouter/anthropic/claude-instant-1.2",
])
def test_incapable_models(value):
    assert not _orchestrator_capable(ModelSpec(value))
