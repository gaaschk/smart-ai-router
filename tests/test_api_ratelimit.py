"""Integration test: per-user quota returns 429 before forwarding."""
import warnings

import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.app import create_app
from smart_ai_router.apikeys import display_prefix, generate_key, hash_key
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ApiKey, ModelSpec, UsageRecord
from smart_ai_router.store.sqlite_store import SqliteStore

_ADMIN = "admin-secret"


def _client(cr) -> TestClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return TestClient(create_app(cr))


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    store = SqliteStore(":memory:")
    store.upsert_model(ModelSpec(
        "ollama/llama3.1:8b", provider="ollama", cost=0,
        reliability=1.0, tools=True, competence={"general": 0.85, "coding": 0.85},
    ))
    cr = CapabilityRouter(store=store)
    return _client(cr), cr


def _mint_key(cr, *, rl_window_s=0, rl_max_req=0, rl_max_tokens=0):
    plaintext = generate_key()
    cr.create_api_key(ApiKey(
        key_hash=hash_key(plaintext), user="quota-user",
        key_prefix=display_prefix(plaintext),
        rl_window_s=rl_window_s, rl_max_req=rl_max_req, rl_max_tokens=rl_max_tokens,
    ))
    return plaintext


def test_request_quota_returns_429(setup):
    client, cr = setup
    key = _mint_key(cr, rl_window_s=3600, rl_max_req=1)
    # Pre-seed one request in the window → the next one is over the cap.
    cr.record_usage(UsageRecord(user="quota-user", routed_model="ollama/llama3.1:8b"))

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "smart-worker",
              "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(key),
    )
    assert resp.status_code == 429
    assert "request quota" in resp.json()["detail"]
    assert resp.headers.get("Retry-After") == "3600"


def test_no_limits_key_is_not_rate_checked(setup):
    client, cr = setup
    key = _mint_key(cr)  # no window/caps
    cr.record_usage(UsageRecord(user="quota-user"))
    # Should NOT be a 429 — a live upstream isn't reachable in tests, so we only
    # assert the quota gate didn't fire (429 would come before forwarding).
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "smart-worker",
              "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(key),
    )
    assert resp.status_code != 429
