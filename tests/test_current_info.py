"""Answers that depend on the state of the world, rather than on knowledge.

The bug: asked how many teams are in the WNBA, a model answered from training and
called a 2024 season "current" — in September 2026. Nothing about that reply looked
wrong from the inside, which is the whole problem: a model has no clock and cannot
tell a fact it learned from a fact that is still true.

Two independent fixes, so two groups of tests:

  * the model is told today's date, unconditionally and for free
  * a prompt whose answer moves gets searched before the model answers it

The second is provider-side (OpenRouter's web plugin), so the tests assert on the
forwarded request body — what the provider was actually asked to do — rather than
on a mocked search result, which would only prove the mock works.
"""
import datetime
import warnings

import httpx
import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api import proxy as _proxy
from smart_ai_router.api.app import create_app
from smart_ai_router.classifier import classify_profile
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.taxonomy import DEMANDS, DomainNeed, PromptProfile

_REPLY = {
    "id": "cmpl-1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "answer."},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

# The reported prompt, and a question whose answer was settled before any model
# was trained. The point is that these two must not be treated the same.
_MOVES = "How many teams are in the WNBA currently?"
_SETTLED = "Why do objects fall at the same rate in a vacuum?"

_UI = {"X-Smart-Router-Client": "ui"}


def _profile(score=0.95):
    return {"software_engineering": score, "law_regulatory": score,
            "medicine_health": score, "general_knowledge": score,
            "creative_writing": score, "technical_writing": score}


def _spec(value, **kw):
    return ModelSpec(
        value=value, provider=value.split("/")[0], cost=kw.pop("cost", 1),
        ctx_k=200, reliability=1.0, tools=True, profile=_profile(),
        competence={"coding": 0.95, "reasoning": 0.95, "docs": 0.95, "general": 0.95},
        **kw,
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    monkeypatch.delenv("SMART_ROUTER_MODEL_DENYLIST", raising=False)
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_FALLBACK", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_REFINE_MODEL", "")
    monkeypatch.setenv("SMART_ROUTER_WEB_SEARCH", "true")
    monkeypatch.setenv("SMART_ROUTER_WEB_SEARCH_MAX_RESULTS", "5")

    sent: list[dict] = []

    async def fake_post(self, url, **kwargs):
        sent.append(kwargs.get("json") or {})
        return httpx.Response(200, json=_REPLY, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    store = SqliteStore(":memory:")
    store.upsert_model(_spec("openrouter/generalist"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = TestClient(create_app(CapabilityRouter(store=store)))
    c.sent = sent
    c.store = store
    return c


def _chat(client, prompt, *, headers=None, **body):
    payload = {"model": "auto", "messages": [{"role": "user", "content": prompt}],
               "stream": False}
    payload.update(body)
    return client.post("/v1/chat/completions", json=payload, headers=headers or {})


# ── Telling the model what day it is ────────────────────────────────────────────

def test_the_page_tells_the_model_todays_date(client):
    """The cheap half of the fix: no search, no cost, no routing change.

    Without it "this year" is unresolvable and "current" means whenever training
    stopped — which is how a 2024 season got called current in 2026.
    """
    _chat(client, _MOVES, headers=_UI)
    system = " ".join(
        m["content"] for m in client.sent[-1]["messages"] if m["role"] == "system"
    )
    assert datetime.date.today().isoformat() in system


def test_an_agent_request_is_told_the_date_too(client):
    """Unlike the rendering note, this isn't suppressed when tools are present.

    An agent writing a report dates it, and a tool loop reasoning about "recent"
    needs the same anchor a chat reply does. It's a fact, not a style suggestion,
    so there's no version of a tool loop where it counts as noise.
    """
    _chat(client, _MOVES, headers=_UI, tools=[{
        "type": "function",
        "function": {"name": "noop", "parameters": {"type": "object", "properties": {}}},
    }])
    system = " ".join(
        m["content"] for m in client.sent[-1]["messages"] if m["role"] == "system"
    )
    assert datetime.date.today().isoformat() in system


def test_the_date_note_asks_for_a_hedge_not_just_a_date(client):
    """Knowing the date is only useful if the model does something with it.

    The reply that started this was wrong *and* certain. A model that knows time
    has passed can say so; one that is only handed a date can still assert.
    """
    note = _proxy._todays_date_note()
    assert "out of date" in note


# ── Searching when the answer moves ─────────────────────────────────────────────

def _fresh():
    return PromptProfile(
        domains=(DomainNeed(field="general_knowledge", depth="surface"),),
        demands=frozenset({"current_info"}),
    )


def test_a_time_sensitive_prompt_is_searched(client):
    r = _chat(client, _MOVES, headers=_UI)
    assert r.status_code == 200
    assert client.sent[-1]["plugins"] == [{"id": "web", "max_results": 5}]
    assert r.headers["X-Web-Search"] == "true"


def test_a_settled_question_is_not_searched(client):
    """Search costs about $0.007 a request, so it has to be conditional.

    Physics did not change since training. Searching every prompt would be the
    same mistake as one output ceiling for every prompt — paying everywhere for a
    problem that exists somewhere.
    """
    r = _chat(client, _SETTLED, headers=_UI)
    assert "plugins" not in client.sent[-1]
    assert r.headers["X-Web-Search"] == "false"


def test_the_demand_is_what_triggers_the_search(client):
    """The trigger is the classification, not a second keyword list in the proxy.

    If these could disagree, X-Prompt-Profile would say one thing and X-Web-Search
    another and there'd be no way to tell which to fix.
    """
    assert classify_profile(_MOVES).needs_current_info() is True
    assert classify_profile(_SETTLED).needs_current_info() is False


def test_an_operator_can_turn_search_off(monkeypatch, client):
    """It spends money per request, so it must be switchable without a deploy."""
    monkeypatch.setenv("SMART_ROUTER_WEB_SEARCH", "false")
    r = _chat(client, _MOVES, headers=_UI)
    assert "plugins" not in client.sent[-1]
    assert r.headers["X-Web-Search"] == "false"


def test_result_count_follows_the_setting(monkeypatch, client):
    monkeypatch.setenv("SMART_ROUTER_WEB_SEARCH_MAX_RESULTS", "9")
    _chat(client, _MOVES, headers=_UI)
    assert client.sent[-1]["plugins"][0]["max_results"] == 9


def test_a_non_openrouter_model_answers_unsearched_and_says_so(monkeypatch, client):
    """`plugins` is an OpenRouter feature; Ollama and Bedrock ignore the field.

    Sending it anyway would be worse than not searching: the header would claim a
    check that never happened. Answering unsearched is the right fallback — the
    model can still partly answer — but only if the caller can tell, which is what
    X-Web-Search is for.
    """
    client.store.delete_model("openrouter/generalist")
    client.store.upsert_model(_spec("ollama/local", cost=0))
    r = _chat(client, _MOVES, headers=_UI)
    assert r.status_code == 200
    assert r.headers["X-Routed-Model"] == "ollama/local"
    assert "plugins" not in client.sent[-1]
    assert r.headers["X-Web-Search"] == "false"


def test_an_api_client_gets_searched_too(client):
    """Unlike the display notes, this isn't a UI concern.

    A program asking a question about now has the same stale-answer problem, and
    `plugins` doesn't alter the caller's prompt or the reply schema — the results
    reach the model provider-side. Suppressing it for /v1 would mean the API is
    quietly less correct than the page.
    """
    r = _chat(client, _MOVES)
    assert client.sent[-1]["plugins"] == [{"id": "web", "max_results": 5}]
    assert r.headers["X-Web-Search"] == "true"


# ── The demand itself ───────────────────────────────────────────────────────────

def test_needing_fresh_facts_does_not_raise_the_capability_bar():
    """A better model has the same cutoff. Bumping the bar would buy a more
    expensive stale answer, which is the one outcome worse than a cheap one."""
    assert DEMANDS["current_info"][0] == 0.0
    plain = PromptProfile(domains=(DomainNeed(field="general_knowledge", depth="surface"),))
    assert _fresh().requirements() == plain.requirements()


@pytest.mark.parametrize("prompt", [
    "What is the latest version of Python?",
    "Who won the election?",
    "What is the current price of a gallon of gas?",
    "How many teams are in the NBA now?",
    "What are the NFL standings?",
    "Summarize this week's news about AI regulation",
    "Is Bard still called Bard?",
    "What changed in the 2026 tax brackets?",
])
def test_prompts_whose_answers_move_are_detected_without_an_llm(prompt):
    """These run through the keyword fallback — the path with no such cue at all,
    which is why the bug reproduced even with the LLM classifier down."""
    assert classify_profile(prompt).needs_current_info() is True


@pytest.mark.parametrize("prompt", [
    "Write me a poem about the sea",
    "Refactor this function to use a dict comprehension",
    "Explain how a binary search works",
    "What did Plutarch write about Alcibiades?",
])
def test_settled_prompts_are_not_searched(prompt):
    """Search is cheap but not free, and a search result in the context of a poem
    request is a distraction. The cue set is broad on purpose — a needless search
    costs a fraction of a cent, a missed one costs a wrong answer — so this is the
    side that has to be pinned down.
    """
    assert classify_profile(prompt).needs_current_info() is False
