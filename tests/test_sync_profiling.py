"""Sync-triggered LLM profiling — which models a sync hands to the rater.

A new model starts taking traffic the moment sync stores it, with a profile
shaped by a cue table reading a marketing blurb. Waiting for a human to press
Refine means routing on a known-bad shape in the meantime, so sync profiles what
it introduces.

The load-bearing question is *which* models, and the answer is deliberately
narrow: new arrivals and rewritten descriptions, never a model that merely
re-priced. These tests pin that boundary, because the failure mode on the wrong
side of it is paying for the same judgment on every sync.

Offline throughout — the rating call is monkeypatched and no test registers an
enabled provider that would fetch a catalog.
"""
from __future__ import annotations

import asyncio
import dataclasses
import warnings

from fastapi.testclient import TestClient

from smart_ai_router import llm_profiler
from smart_ai_router.api.app import create_app
from smart_ai_router.api.routes import _profile_new_models
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec, ProviderConfig
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.sync import SyncResult, _apply_spec, sync_from_providers
from smart_ai_router.taxonomy import FIELD_KEYS

_ADMIN = "admin-secret"


# ── Which models sync flags for profiling ─────────────────────────────────────

def test_a_first_sync_flags_every_new_model():
    store = SqliteStore(":memory:")
    r = sync_from_providers(store, bedrock_key="x")
    assert r.added == 3
    assert sorted(r.needs_profiling) == sorted(s.value for s in store.all_models())


def test_an_unchanged_catalog_flags_nothing():
    store = SqliteStore(":memory:")
    sync_from_providers(store, bedrock_key="x")
    assert sync_from_providers(store, bedrock_key="x").needs_profiling == []


def test_a_model_that_only_changed_level_is_not_re_profiled():
    """The whole reason ratings are stored as ratings: a new price or benchmark
    index re-levels the profile for free, so re-rating would buy nothing and be
    billed for it on every sync."""
    store = SqliteStore(":memory:")
    sync_from_providers(store, bedrock_key="x")
    haiku = next(s for s in store.all_models() if "haiku" in s.value)
    store.upsert_model(dataclasses.replace(haiku, ctx_k=haiku.ctx_k + 1, cost=99))

    r = sync_from_providers(store, bedrock_key="x")
    assert r.updated == 1              # it did change...
    assert r.needs_profiling == []     # ...but not in a way the rater can see


def test_a_rewritten_description_is_re_profiled():
    """The description is the only shape evidence sync has, so a rewrite means
    the stored judgment rests on evidence that no longer stands."""
    store = SqliteStore(":memory:")
    prior = ModelSpec(value="m", profile={f: 0.8 for f in FIELD_KEYS},
                      description="general assistant")
    store.upsert_model(prior)
    existing = {s.value: s for s in store.all_models()}

    result = SyncResult()
    _apply_spec(store, dataclasses.replace(prior, description="agentic coding model"),
                existing, result)
    assert result.needs_profiling == ["m"]


def test_a_description_appearing_where_there_was_none_is_profiled():
    store = SqliteStore(":memory:")
    prior = ModelSpec(value="m", profile={f: 0.8 for f in FIELD_KEYS})
    store.upsert_model(prior)
    existing = {s.value: s for s in store.all_models()}

    result = SyncResult()
    _apply_spec(store, dataclasses.replace(prior, description="coding model"),
                existing, result)
    assert result.needs_profiling == ["m"]


def test_a_description_going_missing_is_not_profiled():
    """A catalog that drops a blurb gives the rater strictly less to work with;
    re-rating on that would replace a judgment with a worse one."""
    store = SqliteStore(":memory:")
    prior = ModelSpec(value="m", profile={f: 0.8 for f in FIELD_KEYS},
                      description="agentic coding model")
    store.upsert_model(prior)
    existing = {s.value: s for s in store.all_models()}

    result = SyncResult()
    _apply_spec(store, dataclasses.replace(prior, description=""), existing, result)
    assert result.needs_profiling == []


# ── The endpoint ──────────────────────────────────────────────────────────────

def _client(cr) -> TestClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return TestClient(create_app(cr))


def _seed(store: SqliteStore, *, with_key: bool = True) -> None:
    """A bedrock provider (deterministic, no network) plus a *disabled*
    openrouter provider that supplies the profiler's key without its catalog
    being fetched."""
    store.upsert_provider(ProviderConfig(name="bedrock", kind="bedrock",
                                         api_key="aws", enabled=True))
    store.upsert_provider(ProviderConfig(
        name="openrouter", kind="openrouter",
        api_key="sk-or-test" if with_key else "", enabled=False,
    ))


def _fake_rating(monkeypatch, ratings=None, note="rated"):
    seen: list[str] = []

    async def rate(spec, **kwargs):
        seen.append(spec.value)
        return dict(ratings or {"medicine_health": "unsuited"}), note

    monkeypatch.setattr(llm_profiler, "rate_model", rate)
    return seen


def _sync(client, **body):
    return client.post("/api/sync", json=body,
                       headers={"Authorization": f"Bearer {_ADMIN}"})


def test_sync_profiles_the_models_it_just_added(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setattr("smart_ai_router.api.proxy._OPENROUTER_BASE", "http://fake/v1")
    seen = _fake_rating(monkeypatch)
    store = SqliteStore(":memory:")
    _seed(store)
    client = _client(CapabilityRouter(store=store))

    d = _sync(client).json()

    assert d["added"] == 3
    assert d["profiled"]["enrich"]["rated"] == 3
    assert d["profile_pending"] == 0
    assert len(seen) == 3
    # And the judgment landed, so the next request routes on it.
    spec = next(s for s in store.all_models() if "haiku" in s.value)
    assert spec.profile_ratings == {"medicine_health": "unsuited"}


def test_a_second_sync_profiles_nothing(monkeypatch):
    """Idempotence is the difference between this being free and it being a
    recurring bill."""
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setattr("smart_ai_router.api.proxy._OPENROUTER_BASE", "http://fake/v1")
    seen = _fake_rating(monkeypatch)
    store = SqliteStore(":memory:")
    _seed(store)
    client = _client(CapabilityRouter(store=store))

    _sync(client)
    seen.clear()
    d = _sync(client).json()

    assert d["unchanged"] == 3
    assert d["profiled"] is None
    assert seen == []


def test_the_setting_turns_it_off(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setenv("SMART_ROUTER_MODEL_PROFILER_ON_SYNC", "0")
    monkeypatch.setattr("smart_ai_router.api.proxy._OPENROUTER_BASE", "http://fake/v1")
    seen = _fake_rating(monkeypatch)
    store = SqliteStore(":memory:")
    _seed(store)
    client = _client(CapabilityRouter(store=store))

    d = _sync(client).json()
    assert d["added"] == 3
    assert d["profiled"] is None
    assert seen == []


def test_the_request_can_override_the_setting(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setenv("SMART_ROUTER_MODEL_PROFILER_ON_SYNC", "1")
    monkeypatch.setattr("smart_ai_router.api.proxy._OPENROUTER_BASE", "http://fake/v1")
    seen = _fake_rating(monkeypatch)
    store = SqliteStore(":memory:")
    _seed(store)
    client = _client(CapabilityRouter(store=store))

    assert _sync(client, profile=False).json()["profiled"] is None
    assert seen == []


def test_no_openrouter_key_reports_pending_rather_than_failing(monkeypatch):
    """An unconfigured profiler must not fail a sync that worked — but the models
    it couldn't reach have to be visible, not silently absent."""
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    store = SqliteStore(":memory:")
    _seed(store, with_key=False)
    client = _client(CapabilityRouter(store=store))

    r = _sync(client)
    assert r.status_code == 200
    d = r.json()
    assert d["added"] == 3
    assert d["profiled"] is None
    assert d["profile_pending"] == 3
    assert d["errors"] == []


def test_a_broken_profiler_does_not_fail_the_sync(monkeypatch):
    """The catalog is already correct by the time profiling runs; a crash in the
    optional pass must not turn a successful sync into a 500."""
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setattr("smart_ai_router.api.proxy._OPENROUTER_BASE", "http://fake/v1")

    async def boom(*a, **k):
        raise RuntimeError("rater exploded")

    monkeypatch.setattr(llm_profiler, "enrich_models", boom)
    store = SqliteStore(":memory:")
    _seed(store)
    client = _client(CapabilityRouter(store=store))

    r = _sync(client)
    assert r.status_code == 200
    d = r.json()
    assert d["added"] == 3                      # the catalog still landed
    assert d["profiled"] is None
    assert any("rater exploded" in e for e in d["errors"])
    assert d["profile_pending"] == 3


def test_a_non_admin_sync_does_not_spend_money(monkeypatch):
    """Refine is admin-only because it bills per model. A sync that quietly did
    the same thing would be a way around that gate."""
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setattr("smart_ai_router.api.proxy._OPENROUTER_BASE", "http://fake/v1")
    seen = _fake_rating(monkeypatch)
    store = SqliteStore(":memory:")
    _seed(store)
    client = _client(CapabilityRouter(store=store))
    key = client.post("/api/keys", json={"user": "alice"},
                      headers={"Authorization": f"Bearer {_ADMIN}"}).json()["key"]

    d = client.post("/api/sync", json={},
                    headers={"Authorization": f"Bearer {key}"}).json()
    assert d["added"] == 3            # the catalog sync itself still works
    assert d["profiled"] is None
    assert d["profile_pending"] == 3
    assert seen == []


def test_a_bounded_run_reports_what_it_left(monkeypatch):
    """No silent caps: a limit that skips models must say so, or the report reads
    as "everything is profiled"."""
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setenv("SMART_ROUTER_MODEL_PROFILER_LIMIT", "2")
    monkeypatch.setattr("smart_ai_router.api.proxy._OPENROUTER_BASE", "http://fake/v1")
    seen = _fake_rating(monkeypatch)
    store = SqliteStore(":memory:")
    _seed(store)
    client = _client(CapabilityRouter(store=store))

    d = _sync(client).json()
    assert d["profiled"]["enrich"]["rated"] == 2
    assert d["profile_pending"] == 1
    assert len(seen) == 2
    # Cheapest first, so the leftover is the priciest model.
    assert not any("opus" in v for v in seen)


def test_sync_profiling_re_rates_a_model_whose_description_changed(monkeypatch):
    """only_missing must not apply to a sync-selected pool: the point of flagging
    a rewritten description is to replace the judgment built on the old one."""
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setattr("smart_ai_router.api.proxy._OPENROUTER_BASE", "http://fake/v1")
    _fake_rating(monkeypatch, {"software_engineering": "specialty"}, note="recoded")
    store = SqliteStore(":memory:")
    _seed(store)
    store.upsert_model(ModelSpec(
        value="openrouter/m", provider="openrouter", cost=1, reliability=1.0,
        profile={f: 0.8 for f in FIELD_KEYS},
        profile_ratings={"medicine_health": "unsuited"},
        profile_note="stale judgment", description="old blurb",
    ))
    cr = CapabilityRouter(store=store)

    # Flagged exactly as a rewritten description would flag it.
    result = SyncResult(updated=1, needs_profiling=["openrouter/m"])
    report, pending = asyncio.run(
        _profile_new_models(cr, result, None)
    )

    assert pending == 0
    assert report.enrich["rated"] == 1
    got = store.get("openrouter/m")
    assert got.profile_ratings == {"software_engineering": "specialty"}
    assert got.profile_note == "recoded"


def test_the_audit_replays_against_the_whole_catalog(monkeypatch):
    """only_values narrows *rating*, not the replay: a model's rating only means
    anything against the full matrix it competes in."""
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setattr("smart_ai_router.api.proxy._OPENROUTER_BASE", "http://fake/v1")
    _fake_rating(monkeypatch, {"law_regulatory": "unsuited"})
    store = SqliteStore(":memory:")
    _seed(store)
    # A cheap model already in the catalog, unrated and untouched by this sync.
    store.upsert_model(ModelSpec(
        value="openrouter/cheap", provider="openrouter", cost=1, reliability=1.0,
        profile={f: 0.9 for f in FIELD_KEYS},
    ))
    from smart_ai_router.models import UsageRecord

    store.record_usage(UsageRecord(
        user="u", routed_model="openrouter/cheap",
        profile={"domains": [{"field": "law_regulatory", "depth": "specialist"}],
                 "demands": [], "stakes": "low"},
        ts="2026-08-19T00:00:00+00:00",
    ))
    client = _client(CapabilityRouter(store=store))

    d = _sync(client).json()
    audit = d["profiled"]["audit"]
    # The recorded prompt was replayed even though its model wasn't rated, and it
    # still routes to the cheap model — nothing about it changed.
    assert audit["profiles"] == 1
    assert audit["flipped"] == 0
