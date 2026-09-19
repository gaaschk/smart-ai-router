"""Measured throughput: the one axis no catalog can supply.

A model's tokens/sec is not a property of the model — it is a property of the
model *on this host, under this provider's current load*. So unlike every other
number the router sorts on, it cannot be imported; it has to be observed from
real traffic. These tests pin the three properties that make observing it safe:

  - it's earned, not asserted: a row only moves the average when there were
    enough completion tokens for tokens/sec to mean anything;
  - it survives a catalog sync, because the catalog doesn't own it;
  - an unmeasured model is *exempt*, not slow — otherwise the floor is a
    chicken-and-egg trap where nothing new can ever be measured.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from smart_ai_router.models import ModelSpec, UsageRecord
from smart_ai_router.router import select
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.taxonomy import DomainNeed, PromptProfile


def _store(*specs):
    store = SqliteStore(":memory:")
    for s in specs:
        store.upsert_model(s)
    return store


def _call(store, model, *, completion_tokens=200, latency_ms=1000, status=200):
    store.record_usage(UsageRecord(
        user="u", routed_model=model, completion_tokens=completion_tokens,
        latency_ms=latency_ms, status=status,
    ))


def _last(store):
    return store.recent_usage("u", "1970-01-01T00:00:00")[-1]


def _tps(store, model):
    return next(s.observed_tps for s in store.all_models() if s.value == model)


def _age(store, model, *, days):
    """Backdate a model's measurement, so expiry is testable without sleeping."""
    when = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    store._conn.execute(
        "UPDATE models SET observed_tps_at = ? WHERE value = ?", (when, model))
    store._conn.commit()


def _coder(name, cost=0, **kw):
    return ModelSpec(name, cost=cost, reliability=1.0,
                     profile={"software_engineering": 0.95}, **kw)


# ── The measurement ───────────────────────────────────────────────────────────

def test_first_measurement_is_adopted_outright():
    # Not blended toward 0: an EWMA seeded at zero would spend its first several
    # calls reporting a speed the model never ran at, and the floor would act on
    # that number.
    store = _store(_coder("m"))
    _call(store, "m", completion_tokens=200, latency_ms=1000)
    assert _tps(store, "m") == 200.0


def test_later_calls_blend_toward_the_new_rate():
    store = _store(_coder("m"))
    _call(store, "m", completion_tokens=200, latency_ms=1000)   # 200 tps
    _call(store, "m", completion_tokens=100, latency_ms=1000)   # 100 tps
    # alpha=0.2 → 0.8*200 + 0.2*100. Moves, but one bad minute can't condemn it.
    assert _tps(store, "m") == 180.0


def test_a_tiny_reply_does_not_count():
    # A 5-token answer is almost entirely time-to-first-token, so its tokens/sec
    # measures the queue, not the model.
    store = _store(_coder("m"))
    _call(store, "m", completion_tokens=5, latency_ms=800)
    assert _tps(store, "m") == 0.0


def test_a_failed_call_does_not_count():
    store = _store(_coder("m"))
    _call(store, "m", completion_tokens=200, latency_ms=50, status=500)
    assert _tps(store, "m") == 0.0


def test_an_unmeasured_row_records_no_latency():
    # Rows written before the column, and calls that died before dispatch.
    store = _store(_coder("m"))
    _call(store, "m", latency_ms=0)
    assert _tps(store, "m") == 0.0
    assert _last(store).latency_ms == 0


def test_latency_is_kept_per_row_too():
    # The running average can't answer "was it slow *then*" — a provider having a
    # bad hour and a permanently slow model look identical once collapsed.
    store = _store(_coder("m"))
    _call(store, "m", latency_ms=1234)
    assert _last(store).latency_ms == 1234


def test_a_catalog_sync_does_not_erase_the_measurement():
    # The whole point: this is the one column sync doesn't own. If upsert_model
    # reset it, every nightly sync would throw away the only data we can't refetch.
    store = _store(_coder("m"))
    _call(store, "m", completion_tokens=200, latency_ms=1000)
    store.upsert_model(_coder("m"))          # as a sync would
    assert _tps(store, "m") == 200.0


# ── The floor ─────────────────────────────────────────────────────────────────

def _practitioner(store, floor, monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_MIN_TOKENS_PER_SECOND", str(floor))
    return select(
        store, needs_tools=False,
        profile=PromptProfile(
            domains=(DomainNeed("software_engineering", "practitioner"),)),
    )


def test_a_model_measured_slow_loses_to_a_faster_one(monkeypatch):
    store = _store(_coder("slow", cost=0), _coder("fast", cost=5))
    _call(store, "slow", completion_tokens=200, latency_ms=10_000)   # 20 tps
    _call(store, "fast", completion_tokens=200, latency_ms=1_000)    # 200 tps
    decision = _practitioner(store, 50, monkeypatch)
    assert decision.model == "fast"
    assert decision.slow_excluded == 1
    assert "too slow" in decision.explain()


def test_the_floor_names_itself_when_it_empties_the_pool(monkeypatch):
    # Otherwise the 422 reads "run sync()" for a catalog that synced fine, and the
    # operator goes looking in the wrong place for a floor they set themselves.
    store = _store(_coder("slow"))
    _call(store, "slow", completion_tokens=200, latency_ms=20_000)
    with pytest.raises(RuntimeError, match="1 excluded as slower than"):
        _practitioner(store, 50, monkeypatch)


def test_the_floor_is_off_by_default(monkeypatch):
    store = _store(_coder("slow", cost=0), _coder("fast", cost=5))
    _call(store, "slow", completion_tokens=200, latency_ms=10_000)
    decision = _practitioner(store, 0, monkeypatch)
    assert decision.model == "slow"
    assert decision.slow_excluded == 0


def test_an_unmeasured_model_is_exempt(monkeypatch):
    # The cold-start case, and the reason the floor is usable at all: a model
    # nobody has called yet has to stay reachable long enough to be measured.
    store = _store(_coder("never-tried"))
    decision = _practitioner(store, 100, monkeypatch)
    assert decision.model == "never-tried"
    assert decision.slow_excluded == 0


def test_overhead_rows_are_timed_but_do_not_set_the_average():
    # Triage runs in front of every request, so its latency is worth recording —
    # but a few dozen tokens of JSON under a 256-token cap is mostly cold load, and
    # letting that set the routing average would condemn the classifier's model.
    store = _store(_coder("m"))
    store.record_usage(UsageRecord(
        kind="classify", user="u", routed_model="m",
        completion_tokens=40, latency_ms=8_000,
    ))
    assert _tps(store, "m") == 0.0
    # Not via recent_usage(), which is the rate limiter's counter and excludes
    # overhead on purpose — the row is there, it just isn't user traffic.
    row = store._conn.execute(
        "SELECT latency_ms FROM usage_log WHERE kind = 'classify'").fetchone()
    assert row["latency_ms"] == 8_000


# ── Expiry: the floor must not be a one-way ratchet ───────────────────────────
# A model the floor excludes receives no traffic, so it can never re-measure
# itself. Without an expiry, one bad afternoon on a provider — or a figure from a
# machine you no longer own — condemns a model permanently.

def test_a_measurement_is_recorded_with_its_date():
    store = _store(_coder("m"))
    _call(store, "m", completion_tokens=200, latency_ms=1_000)
    at = next(s.observed_tps_at for s in store.all_models() if s.value == "m")
    assert at.startswith(str(datetime.now(timezone.utc).year))


def test_a_demoted_model_becomes_reachable_again_once_stale(monkeypatch):
    # The ratchet, broken: same slow measurement, only older, and the model is back
    # in the pool where traffic can prove it either way.
    store = _store(_coder("slow"), _coder("fast", cost=5))
    _call(store, "slow", completion_tokens=200, latency_ms=20_000)  # 10 tps
    assert _practitioner(store, 50, monkeypatch).model == "fast"
    _age(store, "slow", days=45)
    decision = _practitioner(store, 50, monkeypatch)
    assert decision.model == "slow"
    assert decision.slow_excluded == 0


def test_a_fresh_measurement_is_still_trusted(monkeypatch):
    store = _store(_coder("slow"), _coder("fast", cost=5))
    _call(store, "slow", completion_tokens=200, latency_ms=20_000)
    _age(store, "slow", days=3)
    assert _practitioner(store, 50, monkeypatch).model == "fast"


def test_expiry_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_TPS_STALENESS_DAYS", "0")
    store = _store(_coder("slow"), _coder("fast", cost=5))
    _call(store, "slow", completion_tokens=200, latency_ms=20_000)
    _age(store, "slow", days=4000)
    assert _practitioner(store, 50, monkeypatch).model == "fast"


def test_a_measurement_of_unknown_age_is_treated_as_stale(monkeypatch):
    # Rows written before the column. Re-measuring is cheap; routing for a month
    # around a figure of unknown vintage is not.
    store = _store(_coder("slow"))
    _call(store, "slow", completion_tokens=200, latency_ms=20_000)
    store._conn.execute("UPDATE models SET observed_tps_at = '' WHERE value = 'slow'")
    store._conn.commit()
    assert _practitioner(store, 50, monkeypatch).slow_excluded == 0


# ── The local prior ───────────────────────────────────────────────────────────
# "My hardware is the slow part" is knowledge the operator has and no catalog
# does, so they can assert it for unmeasured *local* models and skip paying for
# the first slow call. A measurement outranks it the moment one exists.

def test_the_local_assumption_can_exclude_a_never_called_model(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_ASSUMED_LOCAL_TPS", "15")
    store = _store(_coder("ollama/big", provider="ollama"),
                   _coder("openrouter/hosted", cost=5, provider="openrouter"))
    decision = _practitioner(store, 50, monkeypatch)
    assert decision.model == "openrouter/hosted"
    assert decision.slow_excluded == 1


def test_the_assumption_does_not_touch_hosted_models(monkeypatch):
    # The prior is about this machine, so it has no standing over a model that
    # doesn't run on it.
    monkeypatch.setenv("SMART_ROUTER_ASSUMED_LOCAL_TPS", "15")
    store = _store(_coder("openrouter/unmeasured", provider="openrouter"))
    assert _practitioner(store, 50, monkeypatch).model == "openrouter/unmeasured"


def test_a_real_measurement_beats_the_assumption(monkeypatch):
    # The assumption is pessimistic on purpose. It must not outlive the evidence —
    # otherwise a hardware upgrade could never show up in the routing.
    monkeypatch.setenv("SMART_ROUTER_ASSUMED_LOCAL_TPS", "15")
    store = _store(_coder("ollama/fast", provider="ollama"))
    _call(store, "ollama/fast", completion_tokens=200, latency_ms=1_000)  # 200 tps
    decision = _practitioner(store, 50, monkeypatch)
    assert decision.model == "ollama/fast"
    assert decision.slow_excluded == 0


def test_the_assumption_returns_when_the_measurement_ages_out(monkeypatch):
    # The hardware-change case. The old machine's figures expire, and the operator's
    # prior covers the gap until the new machine has produced its own.
    monkeypatch.setenv("SMART_ROUTER_ASSUMED_LOCAL_TPS", "15")
    store = _store(_coder("ollama/fast", provider="ollama"),
                   _coder("openrouter/hosted", cost=5, provider="openrouter"))
    _call(store, "ollama/fast", completion_tokens=200, latency_ms=1_000)
    assert _practitioner(store, 50, monkeypatch).model == "ollama/fast"
    _age(store, "ollama/fast", days=45)
    assert _practitioner(store, 50, monkeypatch).model == "openrouter/hosted"


def test_the_assumption_is_inert_without_a_floor(monkeypatch):
    # Two knobs, and neither does anything alone: this one only supplies a number
    # for the floor to compare against.
    monkeypatch.setenv("SMART_ROUTER_ASSUMED_LOCAL_TPS", "15")
    store = _store(_coder("ollama/big", provider="ollama"),
                   _coder("openrouter/hosted", cost=5, provider="openrouter"))
    assert _practitioner(store, 0, monkeypatch).model == "ollama/big"
