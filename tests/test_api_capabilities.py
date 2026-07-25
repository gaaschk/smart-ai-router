"""Integration test for the /api/capabilities endpoint."""
import warnings

import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec
from smart_ai_router.store.sqlite_store import SqliteStore

_ADMIN = "admin-secret"


def _client(cr) -> TestClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return TestClient(create_app(cr))


def _seed(store):
    store.upsert_model(ModelSpec(
        value="ollama/llava", provider="ollama", vision=True, ctx_k=8,
        reliability=1.0, competence={"coding": 0.8},
    ))
    store.upsert_model(ModelSpec(
        value="openrouter/text", provider="openrouter", tools=True, ctx_k=200,
        reliability=1.0, competence={"coding": 0.9},
    ))


def test_capabilities_reflects_matrix(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    store = SqliteStore(":memory:")
    _seed(store)
    client = _client(CapabilityRouter(store=store))

    d = client.get("/api/capabilities").json()
    assert d["vision"] is True
    assert d["tools"] is True
    assert d["max_context_k"] == 200
    assert d["model_count"] == 2
    assert sorted(d["providers"]) == ["ollama", "openrouter"]


def test_capabilities_narrowed_by_key_scope(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    store = SqliteStore(":memory:")
    _seed(store)
    cr = CapabilityRouter(store=store)
    client = _client(cr)

    # Mint a key scoped to openrouter/ only — it can't reach the vision model.
    created = client.post(
        "/api/keys",
        json={"user": "scoped", "scope_models": '{"allow": ["openrouter/"]}'},
        headers={"Authorization": f"Bearer {_ADMIN}"},
    ).json()
    key = created["key"]

    d = client.get(
        "/api/capabilities", headers={"Authorization": f"Bearer {key}"}
    ).json()
    assert d["vision"] is False   # vision model is out of scope
    assert d["tools"] is True
    assert d["providers"] == ["openrouter"]
