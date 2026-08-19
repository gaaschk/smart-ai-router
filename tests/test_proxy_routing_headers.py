"""Integration tests for the routing headers and caveat the proxy emits.

The router now knows something the old one couldn't: that *nothing available*
clears the bar a prompt sets. That knowledge is worthless unless it reaches the
caller, so these tests pin the wire contract — the profile, the reason, the
qualified flag, and the caveat prepended to an under-qualified answer.
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


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    monkeypatch.delenv("SMART_ROUTER_MODEL_DENYLIST", raising=False)
    # No classifier models configured → the chain is empty and the keyword
    # profiler runs, so these tests never touch a network classifier.
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_FALLBACK", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_REFINE_MODEL", "")

    sent: list[dict] = []

    async def fake_post(self, url, **kwargs):
        sent.append(kwargs.get("json") or {})
        return httpx.Response(200, json=_REPLY, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    store = SqliteStore(":memory:")
    store.upsert_model(ModelSpec(
        value="openrouter/cheap-coder", provider="openrouter", cost=1, ctx_k=200,
        reliability=1.0, tools=True,
        profile={"software_engineering": 0.95, "law_regulatory": 0.55,
                 "medicine_health": 0.50, "general_knowledge": 0.70},
        competence={"coding": 0.95, "reasoning": 0.55, "docs": 0.7, "general": 0.7},
    ))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = TestClient(create_app(CapabilityRouter(store=store)))
    c.sent = sent  # forwarded bodies, for asserting what the provider saw
    c.store = store  # for asserting what landed in the usage log
    return c


def _chat(client, prompt):
    return client.post("/v1/chat/completions", json={
        "model": "auto",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    })


def test_headers_carry_the_profile_and_the_reason(client):
    r = _chat(client, "Write a Python function to reverse a linked list.")
    assert r.status_code == 200
    assert r.headers["X-Routed-Model"] == "openrouter/cheap-coder"
    assert r.headers["X-Qualified"] == "true"
    assert r.headers["X-Prompt-Profile"]          # human-readable profile
    assert r.headers["X-Routing-Why"]             # the binding constraint
    assert r.headers["X-Classifier"] == "keyword"
    # Legacy headers still populated, derived from the profile.
    assert r.headers["X-Domain"] in {"coding", "docs", "reasoning", "general"}
    assert r.headers["X-Complexity"] in {"trivial", "moderate", "hard", "expert"}


def test_qualified_answer_carries_no_caveat(client):
    r = _chat(client, "Write a Python function to reverse a linked list.")
    assert r.headers["X-Escalated"] == "false"
    assert r.json()["choices"][0]["message"]["content"] == "answer."


def test_underqualified_answer_is_flagged_and_caveated(client):
    # The prompt from the transcript that started this: high-stakes regulatory
    # depth, answered confidently by a model with a 0.55 law score.
    r = _chat(
        client,
        "Analyze the statutory and regulatory liability of an autonomous reactor "
        "control system under the licensing regimes of 48 jurisdictions, citing "
        "the controlling provisions for each.",
    )
    assert r.status_code == 200
    assert r.headers["X-Qualified"] == "false"
    assert r.headers["X-Escalated"] == "true"

    content = r.json()["choices"][0]["message"]["content"]
    assert content.endswith("answer.")
    assert "no available model clears that bar" in content
    assert "unverified" in content


def test_headers_survive_an_unencodable_profile(client):
    # Header values must be latin-1; the profile string is built from field
    # labels, so a non-ascii label would 500 every response.
    r = _chat(client, "Explain the GDPR joint-controller test for a data broker.")
    assert r.status_code == 200
    r.headers["X-Prompt-Profile"].encode("latin-1")


def test_forwarded_body_keeps_the_provider_model_id(client):
    _chat(client, "Write a Python function to reverse a linked list.")
    assert client.sent[-1]["model"] == "cheap-coder"   # provider prefix stripped
    assert client.sent[-1]["max_tokens"] > 0           # generous default applied


def test_usage_log_records_the_profile_that_routed(client):
    """The profile is what chose the model, so it is what has to be recorded:
    (domain, complexity) is too lossy to replay a routing decision from, and
    replaying real decisions is how a profiling change gets judged."""
    _chat(
        client,
        "Analyze the regulatory and safety implications of an autonomous reactor "
        "control system under the licensing regimes of 48 jurisdictions.",
    )
    rows = client.store.usage_profiles()
    assert len(rows) == 1
    recorded = rows[0]["profile"]
    assert recorded["stakes"] in {"low", "medium", "high"}
    assert recorded["domains"]  # at least one named field, with its depth
    assert all({"field", "depth"} == set(d) for d in recorded["domains"])

    # And it round-trips into the same requirements the router actually used.
    from smart_ai_router.taxonomy import normalize_profile

    replayed = normalize_profile(recorded)
    assert replayed is not None
    assert replayed.requirements()


def test_usage_log_profile_matches_the_header_the_caller_saw(client):
    """The recorded profile and the X-Prompt-Profile header must describe the
    same decision, or the audit replays something the user was never told."""
    r = _chat(client, "Write a Python function to reverse a linked list.")
    from smart_ai_router.taxonomy import normalize_profile

    recorded = normalize_profile(client.store.usage_profiles()[0]["profile"])
    assert recorded.describe() == r.headers["X-Prompt-Profile"]
