"""Integration tests for POST /api/models/profile — the Refine endpoint.

Offline throughout: the rating call itself is monkeypatched, so what's under test
is the endpoint's guards, its wiring to the enrichment + audit, and the shape of
what it hands the UI.
"""
from __future__ import annotations

import dataclasses
import warnings

import pytest
from fastapi.testclient import TestClient

from smart_ai_router import llm_profiler
from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec, ProviderConfig, UsageRecord
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.taxonomy import FIELD_KEYS

_ADMIN = "admin-secret"


def _client(cr) -> TestClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return TestClient(create_app(cr))


def _profile(**scores) -> dict[str, float]:
    out = {f: 0.80 for f in FIELD_KEYS}
    out.update(scores)
    return out


def _seed(store: SqliteStore, *, with_key: bool = True) -> None:
    store.upsert_provider(ProviderConfig(
        name="openrouter", kind="openrouter",
        api_key="sk-or-test" if with_key else "",
    ))
    store.upsert_model(ModelSpec(
        value="openrouter/cheap-coder", provider="openrouter", cost=1,
        reliability=1.0, profile=_profile(law_regulatory=0.90),
        description="Agentic coding model",
    ))
    # The frontier model doubles as the rater the profiler routes to: profiling
    # is a routed helper call now, so a run needs a model that clears the
    # profiler's bar (general_knowledge @ specialist) and honors a json_schema.
    store.upsert_model(ModelSpec(
        value="openrouter/pricey-frontier", provider="openrouter", cost=12,
        reliability=1.0, structured_outputs=True,
        profile=_profile(law_regulatory=0.95, general_knowledge=0.95),
    ))
    # One logged law prompt that the cheap coder currently wins — the audit has
    # to be able to see this move.
    store.record_usage(UsageRecord(
        user="kevin", routed_model="openrouter/cheap-coder",
        domain="reasoning", complexity="hard",
        profile={"domains": [{"field": "law_regulatory", "depth": "specialist"}],
                 "demands": [], "stakes": "low"},
        ts="2026-08-19T00:00:00+00:00",
    ))


def _fake_rating(monkeypatch, ratings, note="coder"):
    async def rate(spec, **kwargs):
        if "coder" not in spec.value:
            return {}, "general model"
        return dict(ratings), note

    monkeypatch.setattr(llm_profiler, "rate_model", rate)


def _post(client, **body):
    return client.post(
        "/api/models/profile", json=body,
        headers={"Authorization": f"Bearer {_ADMIN}"},
    )


def test_refine_writes_ratings_and_reports_the_routing_flip(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setattr(
        "smart_ai_router.api.proxy._OPENROUTER_BASE", "http://fake/v1"
    )
    _fake_rating(monkeypatch, {"law_regulatory": "unsuited"})
    store = SqliteStore(":memory:")
    _seed(store)
    client = _client(CapabilityRouter(store=store))

    d = _post(client, audit_days=3650).json()

    assert d["enrich"]["rated"] == 2
    assert d["enrich"]["changed"] == 1
    assert d["enrich"]["written"] == 2
    assert store.get("openrouter/cheap-coder").profile_ratings == {
        "law_regulatory": "unsuited"
    }
    # The logged law prompt now routes to the frontier model — the point of the
    # whole exercise, stated in terms of real traffic.
    audit = d["audit"]
    assert audit["flipped_requests"] == 1
    flip = audit["flips"][0]
    assert flip["before"]["model"] == "openrouter/cheap-coder"
    assert flip["after"]["model"] == "openrouter/pricey-frontier"
    assert flip["direction"] == "pricier"


def test_dry_run_previews_the_flip_without_writing(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setattr(
        "smart_ai_router.api.proxy._OPENROUTER_BASE", "http://fake/v1"
    )
    _fake_rating(monkeypatch, {"law_regulatory": "unsuited"})
    store = SqliteStore(":memory:")
    _seed(store)
    client = _client(CapabilityRouter(store=store))

    d = _post(client, dry_run=True, audit_days=3650).json()

    assert d["enrich"]["written"] == 0
    assert d["enrich"]["dry_run"] is True
    assert d["audit"]["flipped_requests"] == 1   # still predicts the flip
    assert store.get("openrouter/cheap-coder").profile_ratings == {}
    assert store.get("openrouter/cheap-coder").profile["law_regulatory"] == 0.90
    change = d["enrich"]["changes"][0]
    assert change["model"] == "openrouter/cheap-coder"
    assert change["note"] == "coder"
    assert change["shifts"]["law_regulatory"] == [0.9, 0.7]


def test_the_report_names_the_routed_rater_and_why(monkeypatch):
    """The rater is chosen by routing now, so the run has to say which model
    reshaped the catalog and on what grounds — otherwise the next admin reading a
    report of shifted profiles has no way to judge it."""
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setattr(
        "smart_ai_router.api.proxy._OPENROUTER_BASE", "http://fake/v1"
    )
    _fake_rating(monkeypatch, {"law_regulatory": "weak"})
    store = SqliteStore(":memory:")
    _seed(store)
    client = _client(CapabilityRouter(store=store))

    enrich = _post(client).json()["enrich"]
    # The provider-side id, i.e. what was actually sent as `model`.
    assert enrich["rater"] == "pricey-frontier"
    assert "judge what a catalog model" in enrich["rater_why"]
    assert "general_knowledge" in enrich["rater_why"]


def test_a_pinned_rater_is_reported_as_pinned(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setattr(
        "smart_ai_router.api.proxy._OPENROUTER_BASE", "http://fake/v1"
    )
    _fake_rating(monkeypatch, {"law_regulatory": "weak"})
    store = SqliteStore(":memory:")
    _seed(store)
    client = _client(CapabilityRouter(store=store))

    enrich = _post(client, model="vendor/hand-picked").json()["enrich"]
    assert enrich["rater"] == "vendor/hand-picked"
    assert enrich["rater_why"] == "requested for this run"


def test_refine_requires_admin(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    store = SqliteStore(":memory:")
    _seed(store)
    client = _client(CapabilityRouter(store=store))
    r = client.post("/api/models/profile", json={},
                    headers={"Authorization": "Bearer not-the-admin"})
    assert r.status_code in (401, 403)


def test_refine_422s_when_the_raters_provider_has_no_key(monkeypatch):
    """A missing provider key is a fixable setup problem, so say so rather than
    firing one doomed call per model and reporting a run that rated nothing."""
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    store = SqliteStore(":memory:")
    _seed(store, with_key=False)
    client = _client(CapabilityRouter(store=store))
    r = _post(client)
    assert r.status_code == 422
    assert "No API key" in r.json()["detail"]


def test_refine_422s_when_no_model_qualifies_to_rate(monkeypatch):
    """With the rater chosen by routing, "nothing available can do this" is the
    new shape of an unconfigured profiler — and a 422 that names the two ways out
    beats a 200 reporting a run that never happened."""
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    store = SqliteStore(":memory:")
    _seed(store)
    # Strip the one model that honors a json_schema: the rest of the catalog is
    # ineligible, exactly as a deployment with only small local models would be.
    store.upsert_model(dataclasses.replace(
        store.get("openrouter/pricey-frontier"), structured_outputs=False
    ))
    client = _client(CapabilityRouter(store=store))
    r = _post(client)
    assert r.status_code == 422
    assert "No model available to profile with" in r.json()["detail"]


def test_models_endpoint_exposes_ratings_and_note(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    store = SqliteStore(":memory:")
    _seed(store)
    store.upsert_model(ModelSpec(
        value="openrouter/rated", provider="openrouter", cost=2, reliability=1.0,
        profile=_profile(), profile_ratings={"medicine_health": "unsuited"},
        profile_note="code-tuned; keep away from clinical questions",
    ))
    client = _client(CapabilityRouter(store=store))
    models = {m["value"]: m for m in client.get("/api/models").json()}
    rated = models["openrouter/rated"]
    assert rated["profile_ratings"] == {"medicine_health": "unsuited"}
    assert "clinical" in rated["profile_note"]
    assert rated["profile"]["medicine_health"] == pytest.approx(0.60)
    # An unrated model reports no ratings, so the UI can tell the two apart.
    assert models["openrouter/pricey-frontier"]["profile_ratings"] == {}


def test_only_missing_makes_a_second_run_a_no_op(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setattr(
        "smart_ai_router.api.proxy._OPENROUTER_BASE", "http://fake/v1"
    )
    _fake_rating(monkeypatch, {"law_regulatory": "weak"})
    store = SqliteStore(":memory:")
    _seed(store)
    client = _client(CapabilityRouter(store=store))

    assert _post(client).json()["enrich"]["rated"] == 2
    second = _post(client).json()["enrich"]
    assert second["considered"] == 0
    assert second["rated"] == 0
    # ...and asking again explicitly does re-rate.
    assert _post(client, only_missing=False).json()["enrich"]["rated"] == 2
