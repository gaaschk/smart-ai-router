"""Measured tool-loop health: the hole the benchmarked `agentic` index left open.

`agentic` covers only models somebody benchmarked, and **no local model is among
them**. Because 0.0 means "never measured, therefore exempt", the tool-loop filter
removed measured-weak cloud models and then handed the turn to an unmeasured local
one because it was free — so the models most likely to fail a tool loop were
precisely the ones it could not see. The observed failure: a local 30B handed a
planning prompt returned 52 and 82 tokens, twice, two minutes each, while the
client's tool-call handshake timed out.

These tests pin the properties that make measuring it from live traffic safe:

  - a stall is a turn that produced *nothing*, not one that answered in prose —
    prose is the right reply to a question that merely had tools attached;
  - a consistently stalling model must actually reach the floor, which is why the
    prior is optimistic rather than the first sample being adopted outright;
  - it survives a catalog sync, because the catalog cannot know it;
  - unmeasured is exempt, and the floor is off until an operator sets one.
"""
from __future__ import annotations

import warnings
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec, UsageRecord
from smart_ai_router.router import select
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.taxonomy import DomainNeed, PromptProfile


def _store(*specs):
    store = SqliteStore(":memory:")
    for s in specs:
        store.upsert_model(s)
    return store


def _turn(store, model, *, tools=True, stalled=False, status=200, kind="proxy"):
    """One recorded turn. `stalled` is the observation, already reduced to a bit
    by the proxy — these tests cover what the store does with it."""
    store.record_usage(UsageRecord(
        user="u", routed_model=model, kind=kind, status=status,
        completion_tokens=200, latency_ms=1000,
        tools_offered=tools, tool_stalled=stalled,
    ))


def _health(store, model):
    return next(s.observed_tool_health for s in store.all_models()
                if s.value == model)


def _coder(name, cost=0, **kw):
    return ModelSpec(name, cost=cost, reliability=1.0, tools=True,
                     profile={"software_engineering": 0.95}, **kw)


# ── The measurement ───────────────────────────────────────────────────────────

def test_unmeasured_reads_as_zero():
    store = _store(_coder("m"))
    assert _health(store, "m") == 0.0


def test_a_clean_turn_reports_full_health():
    store = _store(_coder("m"))
    _turn(store, "m", stalled=False)
    assert _health(store, "m") == 1.0


def test_a_stall_blends_down_from_an_optimistic_prior():
    # NOT adopted outright the way tokens/sec is. One bit of evidence adopted
    # outright would land on exactly 0.0 — which this column defines as "never
    # measured" — so the model would read as unmeasured and stay exempt.
    store = _store(_coder("m"))
    _turn(store, "m", stalled=True)
    assert _health(store, "m") == 0.8


def test_a_model_that_always_stalls_actually_reaches_a_floor():
    """The regression that matters: it must not park at 0.0 and read as exempt."""
    store = _store(_coder("m"))
    for _ in range(8):
        _turn(store, "m", stalled=True)
    health = _health(store, "m")
    assert 0.0 < health < 0.25, health


def test_answering_in_prose_is_not_a_stall():
    # A model handed tools for a prompt that turned out to be a question answers
    # it. Counting that as failure would punish correct behavior — which is why
    # the proxy judges on "produced nothing", not "emitted no tool call".
    store = _store(_coder("m"))
    for _ in range(5):
        _turn(store, "m", stalled=False)
    assert _health(store, "m") == 1.0


def test_a_turn_without_tools_is_not_a_sample():
    # Otherwise ordinary chat traffic inflates the number until it says nothing
    # about tool loops — exactly how a local model serving mostly chat would earn
    # a clean record it never tested.
    store = _store(_coder("m"))
    _turn(store, "m", tools=False, stalled=False)
    assert _health(store, "m") == 0.0


def test_a_failed_call_is_not_the_model_stalling():
    # A 500 from the provider is not a model failing to hold a loop, and blaming
    # it would route around a network problem by demoting whatever was unlucky.
    store = _store(_coder("m"))
    _turn(store, "m", stalled=True, status=500)
    assert _health(store, "m") == 0.0


def test_the_routers_own_overhead_calls_do_not_count():
    store = _store(_coder("m"))
    _turn(store, "m", stalled=True, kind="classify")
    assert _health(store, "m") == 0.0


def test_it_survives_a_catalog_sync():
    # The catalog cannot know this: it is how the model behaves against *these*
    # clients. A sync that reset it would discard the only data we can't refetch.
    store = _store(_coder("m"))
    _turn(store, "m", stalled=True)
    store.upsert_model(_coder("m"))          # as a sync would
    assert _health(store, "m") == 0.8


# ── The floor ─────────────────────────────────────────────────────────────────

def _route(store, monkeypatch, floor=0, stale_days=30):
    monkeypatch.setenv("SMART_ROUTER_MIN_TOOL_HEALTH", str(floor))
    monkeypatch.setenv("SMART_ROUTER_TPS_STALENESS_DAYS", str(stale_days))
    return select(
        store, needs_tools=True,
        profile=PromptProfile(
            domains=(DomainNeed("software_engineering", "practitioner"),)),
    )


def _stall_until_unhealthy(store, model, times=8):
    for _ in range(times):
        _turn(store, model, stalled=True)


def test_the_floor_is_off_by_default(monkeypatch):
    # Measure first, route later: the operator watches the number in Models before
    # letting it change picks.
    store = _store(_coder("cheap", cost=0), _coder("dear", cost=5))
    _stall_until_unhealthy(store, "cheap")
    assert _route(store, monkeypatch, floor=0).model == "cheap"


def test_a_stalling_model_loses_once_a_floor_is_set(monkeypatch):
    store = _store(_coder("cheap", cost=0), _coder("dear", cost=5))
    _stall_until_unhealthy(store, "cheap")
    decision = _route(store, monkeypatch, floor=70)
    assert decision.model == "dear"
    assert decision.agentic_excluded == 1


def test_an_unmeasured_model_is_exempt_not_condemned(monkeypatch):
    # The chicken-and-egg guard: a model with no reading must stay reachable, or
    # nothing new can ever be measured.
    store = _store(_coder("fresh", cost=0), _coder("dear", cost=5))
    assert _route(store, monkeypatch, floor=70).model == "fresh"


def test_a_stale_reading_stops_counting(monkeypatch):
    # Same escape from the one-way ratchet as throughput: an excluded model gets
    # no tool traffic, so nothing would ever redeem it.
    store = _store(_coder("cheap", cost=0), _coder("dear", cost=5))
    _stall_until_unhealthy(store, "cheap")
    when = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    store._conn.execute(
        "UPDATE models SET observed_tool_health_at = ? WHERE value = ?",
        (when, "cheap"))
    store._conn.commit()
    assert _route(store, monkeypatch, floor=70).model == "cheap"


def test_a_benchmarked_weak_model_is_still_excluded_on_its_own(monkeypatch):
    # The two readings are independent: the `agentic` index must keep working with
    # no tool-health floor set at all.
    store = _store(_coder("weak", cost=0, agentic=0.10),
                   _coder("dear", cost=5))
    assert _route(store, monkeypatch, floor=0).model == "dear"


# ── What the proxy actually observes ──────────────────────────────────────────
# The tests above take the stall bit as given. These pin where it comes from,
# which is the part that decides whether any of the rest measures the right thing.

_TOOLS = [{
    "type": "function",
    "function": {"name": "enter_plan_mode", "description": "switch to planning",
                 "parameters": {"type": "object", "properties": {}}},
}]


def _reply(*, content="", tool_calls=None, completion_tokens=200):
    msg: dict = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": "cmpl-1",
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 24000,
                  "completion_tokens": completion_tokens,
                  "total_tokens": 24000 + completion_tokens},
    }


def _proxy(reply, monkeypatch, **body):
    """Drive one non-streaming request through the real proxy and hand back the
    store, so the recorded observation can be read off it."""
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    monkeypatch.delenv("SMART_ROUTER_MODEL_DENYLIST", raising=False)
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_FALLBACK", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_REFINE_MODEL", "")

    async def fake_post(self, url, **kwargs):
        return httpx.Response(200, json=reply, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    store = SqliteStore(":memory:")
    store.upsert_model(_coder("openrouter/test/coder"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = TestClient(create_app(CapabilityRouter(store=store)))
    r = client.post("/v1/chat/completions", json={
        "model": "smart-auto", "stream": False,
        "messages": [{"role": "user", "content": "write the plan"}],
        **body,
    })
    assert r.status_code == 200, r.text
    return store


def _recorded(store):
    """The last proxy row's observation, read straight from the table so the
    assertion doesn't depend on which identity open mode assigned the request."""
    row = store._conn.execute(
        "SELECT tools_offered, tool_stalled FROM usage_log "
        "WHERE kind = 'proxy' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return bool(row["tools_offered"]), bool(row["tool_stalled"])


@pytest.mark.parametrize("reply,expected,why", [
    # The observed failure: tools offered, no call, 82 tokens of nothing.
    (_reply(content="I'll switch to plan mode.", completion_tokens=82),
     (True, True), "short reply with no tool call is a stall"),
    # A real tool call is health, however short the prose around it.
    (_reply(tool_calls=[{"id": "c1", "type": "function",
                         "function": {"name": "enter_plan_mode",
                                      "arguments": "{}"}}],
            completion_tokens=12),
     (True, False), "a tool call is never a stall, even a terse one"),
    # A substantive prose answer is a legitimate reply to a question.
    (_reply(content="x" * 400, completion_tokens=900),
     (True, False), "a real answer is not a stall"),
])
def test_the_proxy_derives_the_stall_bit(reply, expected, why, monkeypatch):
    assert _recorded(_proxy(reply, monkeypatch, tools=_TOOLS)) == expected, why


def test_a_request_with_no_tools_records_no_sample(monkeypatch):
    store = _proxy(_reply(content="hi", completion_tokens=2), monkeypatch)
    assert _recorded(store) == (False, False)
