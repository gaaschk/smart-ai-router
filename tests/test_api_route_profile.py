"""Integration tests for POST /api/route — profile and legacy request shapes."""
import warnings

import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec
from smart_ai_router.store.sqlite_store import SqliteStore


def _client(cr) -> TestClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return TestClient(create_app(cr))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    monkeypatch.delenv("SMART_ROUTER_MODEL_DENYLIST", raising=False)
    store = SqliteStore(":memory:")
    # The pair the old router got wrong: both cleared one 0.88 coding bar, and
    # the cheap one won a prompt that also needed regulatory depth.
    store.upsert_model(ModelSpec(
        value="cheap-coder", provider="openrouter", cost=1, ctx_k=200,
        reliability=1.0,
        profile={"software_engineering": 0.95, "law_regulatory": 0.55,
                 "general_knowledge": 0.70},
        competence={"coding": 0.95, "reasoning": 0.55, "docs": 0.7, "general": 0.7},
    ))
    store.upsert_model(ModelSpec(
        value="pricey-frontier", provider="openrouter", cost=12, ctx_k=200,
        reliability=1.0,
        profile={"software_engineering": 0.94, "law_regulatory": 0.95,
                 "general_knowledge": 0.95},
        competence={"coding": 0.94, "reasoning": 0.95, "docs": 0.94, "general": 0.95},
    ))
    return _client(CapabilityRouter(store=store))


def test_profile_request_requires_every_field(client):
    r = client.post("/api/route", json={
        "domains": [
            {"field": "law_regulatory", "depth": "specialist"},
            {"field": "software_engineering", "depth": "specialist"},
        ],
    })
    assert r.status_code == 200
    d = r.json()
    assert d["model"] == "pricey-frontier"
    assert d["qualified"] is True
    assert set(d["requirements"]) == {"law_regulatory", "software_engineering"}
    assert "Law & regulatory" in d["profile"]
    assert d["why"]


def test_single_field_profile_takes_the_cheap_model(client):
    r = client.post("/api/route", json={
        "domains": [{"field": "software_engineering", "depth": "specialist"}],
    })
    assert r.json()["model"] == "cheap-coder"


def test_unqualified_pick_is_flagged(client):
    r = client.post("/api/route", json={
        "domains": [{"field": "law_regulatory", "depth": "frontier"}],
        "demands": ["factual_precision"],
        "stakes": "high",
    })
    d = r.json()
    assert r.status_code == 200
    assert d["qualified"] is False
    assert "clears every bar" in d["why"]


def test_legacy_label_request_still_works(client):
    # Callers that predate the profile send only (domain, complexity).
    r = client.post("/api/route", json={"domain": "coding", "complexity": "hard"})
    assert r.status_code == 200
    d = r.json()
    assert d["model"] == "cheap-coder"
    assert d["domain"] == "coding"
    assert d["complexity"] == "hard"


def test_legacy_labels_are_derived_from_the_profile(client):
    r = client.post("/api/route", json={
        "domains": [{"field": "law_regulatory", "depth": "frontier"}],
    })
    d = r.json()
    assert (d["domain"], d["complexity"]) == ("reasoning", "expert")


def test_unknown_field_is_a_422_not_a_silent_default(client):
    # Silently routing a prompt whose fields we didn't understand is how a
    # demanding request gets answered by a cheap model.
    r = client.post("/api/route", json={
        "domains": [{"field": "astrology", "depth": "frontier"}],
    })
    assert r.status_code == 422
    assert "law_regulatory" in r.json()["detail"]


def test_models_endpoint_exposes_the_profile(client):
    rows = client.get("/api/models").json()
    coder = next(m for m in rows if m["value"] == "cheap-coder")
    assert coder["profile"]["law_regulatory"] == 0.55
