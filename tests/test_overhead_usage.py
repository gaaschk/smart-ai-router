"""Router overhead in the usage log — the spend nobody requested.

The usage page used to report only proxied requests, so a deployment whose
classifier escalates to a paid refine model on every consequential prompt saw a
number smaller than its bill. Overhead calls are now rows of their own, tagged by
kind, and these tests pin the two properties that make that safe:

  - overhead is *visible*: every classify / refine / profile call lands in the log
    with its model, tokens and cost;
  - overhead is *separate*: it never leaks into the user-traffic totals, the
    per-user breakdown, or the rate limiter's counter.

Offline throughout — every provider call goes through a fake transport.
"""
from __future__ import annotations

import asyncio
import json
import warnings

import httpx
import pytest
from fastapi.testclient import TestClient

from smart_ai_router import overhead as _overhead
from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.llm_classifier import classify_profile_llm
from smart_ai_router.models import ModelSpec, ProviderConfig, UsageRecord
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.taxonomy import FIELD_KEYS

_PROFILE_JSON = (
    '{"domains":[{"field":"law_regulatory","depth":"specialist"},'
    '{"field":"natural_science","depth":"specialist"}],'
    '"demands":["factual_precision"],"stakes":"high"}'
)


# ── The sink ──────────────────────────────────────────────────────────────────

def test_note_outside_a_collect_block_is_a_no_op():
    """The classifier is used by the bakeoff script and by tests with no store at
    all, so announcing spend when nobody is listening must be free and silent."""
    _overhead.note(_overhead.CLASSIFY, model="m", usage={"prompt_tokens": 5})


def test_a_classifier_call_is_noted_with_its_model_and_tokens(monkeypatch):
    import smart_ai_router.llm_classifier as lc

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": _PROFILE_JSON}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        })

    real = httpx.AsyncClient
    monkeypatch.setattr(
        lc.httpx, "AsyncClient",
        lambda *a, **k: real(transport=httpx.MockTransport(handler)),
    )

    with _overhead.collect() as calls:
        asyncio.run(classify_profile_llm(
            "48 jurisdictions", base_url="http://x/v1", model="qwen-test"
        ))
    assert len(calls) == 1
    assert calls[0].kind == _overhead.CLASSIFY
    assert calls[0].model == "qwen-test"
    assert (calls[0].prompt_tokens, calls[0].completion_tokens) == (120, 30)


def test_a_call_whose_reply_carries_no_usage_is_still_recorded(monkeypatch):
    """A provider that reports no token block still charged for the call. The
    count is the honest part even when the size isn't."""
    import smart_ai_router.llm_classifier as lc

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": _PROFILE_JSON}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        lc.httpx, "AsyncClient",
        lambda *a, **k: real(transport=httpx.MockTransport(handler)),
    )

    with _overhead.collect() as calls:
        asyncio.run(classify_profile_llm("x", base_url="http://x/v1", model="m"))
    assert len(calls) == 1
    assert (calls[0].prompt_tokens, calls[0].completion_tokens) == (0, 0)


def test_record_prices_a_bare_model_id_against_the_catalog():
    """Overhead callers hold the provider's id ("openai/gpt-5.6-luna"); the store
    keys the same model as "openrouter/openai/gpt-5.6-luna". Without the lookup,
    every overhead row would be free and the whole feature would report $0."""
    store = SqliteStore(":memory:")
    store.upsert_model(ModelSpec(
        value="openrouter/openai/gpt-5.6-luna", provider="openrouter",
        cost_input=1.0, cost_output=4.0,
    ))
    cr = CapabilityRouter(store=store)

    _overhead.record(cr, [_overhead.OverheadCall(
        kind=_overhead.CLASSIFY_REFINE, model="openai/gpt-5.6-luna",
        prompt_tokens=1_000_000, completion_tokens=1_000_000,
    )], user="kevin")

    row = store.usage_summary()["overhead"]["by_model"][0]
    assert row["key"] == "openrouter/openai/gpt-5.6-luna"
    assert row["cost_usd"] == pytest.approx(5.0)


def test_an_uncatalogued_overhead_model_is_still_logged():
    """A local classifier is genuinely free and a model missing from the catalog
    is genuinely unpriceable; neither is a reason to lose the call."""
    store = SqliteStore(":memory:")
    cr = CapabilityRouter(store=store)

    _overhead.record(cr, [_overhead.OverheadCall(
        kind=_overhead.CLASSIFY, model="qwen2.5:3b-instruct",
        prompt_tokens=100, completion_tokens=20,
    )])

    oh = store.usage_summary()["overhead"]
    assert oh["totals"]["requests"] == 1
    assert oh["totals"]["cost_usd"] == 0.0
    assert oh["by_model"][0]["key"] == "qwen2.5:3b-instruct"


# ── The store ─────────────────────────────────────────────────────────────────

def _seed_mixed(store: SqliteStore) -> None:
    store.record_usage(UsageRecord(
        user="kevin", routed_model="openrouter/cheap", domain="coding",
        complexity="moderate", prompt_tokens=100, completion_tokens=50,
        cost_usd=0.01, ts="2026-08-19T00:00:00+00:00",
    ))
    store.record_usage(UsageRecord(
        kind=_overhead.CLASSIFY, user="kevin", routed_model="ollama/qwen",
        prompt_tokens=200, completion_tokens=20, cost_usd=0.0,
        ts="2026-08-19T00:00:01+00:00",
    ))
    store.record_usage(UsageRecord(
        kind=_overhead.CLASSIFY_REFINE, user="kevin", routed_model="openrouter/luna",
        prompt_tokens=300, completion_tokens=40, cost_usd=0.02,
        ts="2026-08-19T00:00:02+00:00",
    ))
    store.record_usage(UsageRecord(
        kind=_overhead.PROFILE, user="admin", routed_model="openrouter/luna",
        prompt_tokens=400, completion_tokens=60, cost_usd=0.03,
        ts="2026-08-19T00:00:03+00:00",
    ))


def test_user_totals_count_requests_only():
    store = SqliteStore(":memory:")
    _seed_mixed(store)
    s = store.usage_summary()

    assert s["totals"]["requests"] == 1
    assert s["totals"]["cost_usd"] == pytest.approx(0.01)
    assert [r["key"] for r in s["by_model"]] == ["openrouter/cheap"]
    assert [r["key"] for r in s["by_domain"]] == ["coding/moderate"]
    assert sum(r["requests"] for r in s["by_day"]) == 1
    # The admin's profiling run must not invent a "user" who sent requests.
    assert [r["key"] for r in s["by_user"]] == ["kevin"]
    assert s["by_user"][0]["requests"] == 1


def test_overhead_is_broken_out_by_kind_and_model():
    store = SqliteStore(":memory:")
    _seed_mixed(store)
    oh = store.usage_summary()["overhead"]

    assert oh["totals"]["requests"] == 3
    assert oh["totals"]["cost_usd"] == pytest.approx(0.05)
    by_kind = {r["key"]: r for r in oh["by_kind"]}
    assert set(by_kind) == {"classify", "classify-refine", "profile"}
    assert by_kind["classify"]["prompt_tokens"] == 200
    by_model = {r["key"]: r for r in oh["by_model"]}
    # Both LLM passes on the same model roll up together.
    assert by_model["openrouter/luna"]["requests"] == 2
    assert by_model["openrouter/luna"]["cost_usd"] == pytest.approx(0.05)


def test_a_users_own_view_shows_only_their_overhead():
    store = SqliteStore(":memory:")
    _seed_mixed(store)
    oh = store.usage_summary(user="kevin")["overhead"]
    # kevin's prompts were classified and refined; the admin's profiling run is
    # not his to see or to answer for.
    assert {r["key"] for r in oh["by_kind"]} == {"classify", "classify-refine"}


def test_a_row_written_before_the_kind_column_counts_as_a_request():
    """The migration defaults existing rows to 'proxy', and the SQL coalesces on
    top of that: a pre-upgrade deployment's history must not silently move from
    'requests' to 'overhead'."""
    store = SqliteStore(":memory:")
    _seed_mixed(store)
    with store._lock:
        store._conn.execute("UPDATE usage_log SET kind = NULL WHERE kind = 'proxy'")
        store._conn.commit()

    s = store.usage_summary()
    assert s["totals"]["requests"] == 1
    assert s["overhead"]["totals"]["requests"] == 3


def test_the_rate_limiter_does_not_count_overhead():
    """One prompt is one request against a key's quota, however many calls the
    router made to route it — otherwise tightening the classifier config would
    quietly shrink every user's allowance."""
    store = SqliteStore(":memory:")
    _seed_mixed(store)
    rows = store.recent_usage("kevin", "")
    assert [r.kind for r in rows] == ["proxy"]


# ── End to end: a proxied request ─────────────────────────────────────────────

_REPLY = {
    "id": "cmpl-1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "answer."},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}


@pytest.fixture
def client(monkeypatch):
    """A deployment with a local classifier *and* a paid refine model configured,
    so one chat request produces triage + refine + the answer itself."""
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    monkeypatch.delenv("SMART_ROUTER_MODEL_DENYLIST", raising=False)
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "qwen-test")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_FALLBACK", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_REFINE_MODEL", "luna-test")

    async def fake_post(self, url, **kwargs):
        body = kwargs.get("json") or {}
        model = body.get("model", "")
        if model in {"qwen-test", "luna-test"}:
            payload = {
                "choices": [{"message": {"content": _PROFILE_JSON}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30},
            }
        else:
            payload = _REPLY
        return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    store = SqliteStore(":memory:")
    store.upsert_provider(ProviderConfig(
        name="openrouter", kind="openrouter", api_key="sk-or-test",
    ))
    store.upsert_model(ModelSpec(
        value="openrouter/frontier", provider="openrouter", cost=12, ctx_k=200,
        reliability=1.0, cost_input=1.0, cost_output=4.0,
        profile={f: 0.95 for f in FIELD_KEYS},
    ))
    # The two overhead models, priced so the log can show what routing cost.
    store.upsert_model(ModelSpec(value="ollama/qwen-test", provider="ollama"))
    store.upsert_model(ModelSpec(
        value="openrouter/luna-test", provider="openrouter",
        cost_input=1.0, cost_output=4.0,
    ))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = TestClient(create_app(CapabilityRouter(store=store)))
    c.store = store
    return c


def _chat(client, prompt="Liability of a reactor control system in 48 jurisdictions"):
    return client.post("/v1/chat/completions", json={
        "model": "auto", "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    })


def test_one_chat_request_logs_its_routing_overhead(client):
    assert _chat(client).status_code == 200

    s = client.store.usage_summary()
    assert s["totals"]["requests"] == 1          # one request, as before
    by_kind = {r["key"]: r for r in s["overhead"]["by_kind"]}
    # Triage escalated (high stakes) so both passes ran, and both are on the bill.
    assert set(by_kind) == {"classify", "classify-refine"}
    assert by_kind["classify-refine"]["cost_usd"] > 0    # the paid one
    assert by_kind["classify"]["cost_usd"] == 0.0        # the local one


def test_overhead_is_attributed_to_the_requesting_user(client):
    """Whose prompts are expensive to *route* is only answerable if the classifier
    rows carry an identity — and an open-mode row carries "" like any other."""
    _chat(client)
    kinds = {r.kind for r in client.store.recent_usage("", "")}
    assert kinds == {"proxy"}                    # rate limiting is unaffected
    rows = [r for r in client.store.usage_summary(user="")["overhead"]["by_kind"]]
    assert {r["key"] for r in rows} == {"classify", "classify-refine"}


def test_the_usage_endpoint_exposes_the_overhead_block(client):
    _chat(client)
    d = client.get("/api/usage").json()
    assert d["overhead"]["totals"]["requests"] == 2
    assert d["totals"]["requests"] == 1
    assert {r["key"] for r in d["overhead"]["by_kind"]} == {"classify", "classify-refine"}


# ── End to end: a Refine run ─────────────────────────────────────────────────

def test_refine_logs_one_overhead_row_per_model_rated(monkeypatch):
    """The largest self-directed burst the router makes — one call per model —
    and previously the only trace it left was the ratings themselves."""
    import smart_ai_router.llm_profiler as lp

    monkeypatch.setenv("SMART_ROUTER_API_KEYS", "admin-secret")
    monkeypatch.setenv("SMART_ROUTER_MODEL_PROFILER_MODEL", "luna-test")
    ratings = json.dumps({**{f: "capable" for f in FIELD_KEYS}, "note": "ordinary"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": ratings}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 100},
        })

    real = httpx.AsyncClient
    monkeypatch.setattr(
        lp.httpx, "AsyncClient",
        lambda *a, **k: real(transport=httpx.MockTransport(handler)),
    )

    store = SqliteStore(":memory:")
    store.upsert_provider(ProviderConfig(
        name="openrouter", kind="openrouter", api_key="sk-or-test",
    ))
    for name in ("a", "b"):
        store.upsert_model(ModelSpec(
            value=f"openrouter/{name}", provider="openrouter", cost=1,
            profile={f: 0.8 for f in FIELD_KEYS},
        ))
    store.upsert_model(ModelSpec(
        value="openrouter/luna-test", provider="openrouter",
        cost_input=1.0, cost_output=4.0,
    ))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = TestClient(create_app(CapabilityRouter(store=store)))

    r = client.post("/api/models/profile", json={"limit": 2},
                    headers={"Authorization": "Bearer admin-secret"})
    assert r.status_code == 200
    assert r.json()["enrich"]["rated"] == 2

    oh = store.usage_summary()["overhead"]
    assert oh["totals"]["requests"] == 2
    assert oh["by_kind"][0]["key"] == _overhead.PROFILE
    assert oh["totals"]["cost_usd"] > 0
    # Charged to whoever pressed the button, not to a nameless "system".
    assert store.usage_summary(user="admin")["overhead"]["totals"]["requests"] == 2


def test_a_dry_run_is_billed_too(monkeypatch):
    """dry_run writes no model rows, but it makes exactly the same calls. Leaving
    it out of the log would make previewing look free."""
    import smart_ai_router.llm_profiler as lp

    monkeypatch.setenv("SMART_ROUTER_API_KEYS", "admin-secret")
    monkeypatch.setenv("SMART_ROUTER_MODEL_PROFILER_MODEL", "luna-test")
    ratings = json.dumps({**{f: "capable" for f in FIELD_KEYS}, "note": "ordinary"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": ratings}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 100},
        })

    real = httpx.AsyncClient
    monkeypatch.setattr(
        lp.httpx, "AsyncClient",
        lambda *a, **k: real(transport=httpx.MockTransport(handler)),
    )

    store = SqliteStore(":memory:")
    store.upsert_provider(ProviderConfig(
        name="openrouter", kind="openrouter", api_key="sk-or-test",
    ))
    store.upsert_model(ModelSpec(
        value="openrouter/a", provider="openrouter", cost=1,
        profile={f: 0.8 for f in FIELD_KEYS},
    ))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = TestClient(create_app(CapabilityRouter(store=store)))

    client.post("/api/models/profile", json={"dry_run": True},
                headers={"Authorization": "Bearer admin-secret"})

    assert store.usage_summary()["overhead"]["totals"]["requests"] == 1
    assert store.get("openrouter/a").profile_ratings == {}   # nothing written
