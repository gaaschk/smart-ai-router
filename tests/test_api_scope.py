"""Integration tests for per-user scope enforcement in the proxy."""
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


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    store = SqliteStore(":memory:")
    # A cheap Claude on bedrock (orchestrator target) + a local model.
    store.upsert_model(ModelSpec(
        "bedrock/anthropic/claude-haiku", provider="bedrock", cost=3,
        reliability=1.0, tools=True,
        competence={"general": 0.85, "coding": 0.85},
    ))
    store.upsert_model(ModelSpec(
        "ollama/llama3.1:8b", provider="ollama", cost=0,
        reliability=1.0, tools=True,
        competence={"general": 0.80, "coding": 0.80},
    ))
    cr = CapabilityRouter(store=store)
    return _client(cr), cr


def _mint(client, **scope):
    body = {"user": "scoped", **scope}
    return client.post("/api/keys", json=body, headers=_auth(_ADMIN)).json()["key"]


def test_orchestrator_denied_when_claude_out_of_scope(setup):
    client, _ = setup
    # This key may only reach ollama models — orchestrator forces Claude → 403.
    key = _mint(client, scope_models='{"allow": ["ollama/"]}')
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "smart-orchestrator",
              "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(key),
    )
    assert resp.status_code == 403
    assert "scope" in resp.json()["detail"].lower()


def test_worker_route_stays_within_scope(setup):
    client, cr = setup
    # max_tier=0 unrestricted allow, but deny bedrock → worker must pick ollama.
    key = _mint(client, scope_models='{"deny": ["bedrock"]}')
    # We assert on the routing decision directly (no upstream needed): the proxy
    # builds scope from the key and hands it to the router. Emulate that here to
    # confirm the wiring — the dedicated proxy path is covered by the 403 test.
    from smart_ai_router.scope import parse_scope
    record = next(k for k in cr.all_api_keys() if k.user == "scoped")
    scope = parse_scope(record.scope_models, record.max_tier)
    chosen = cr.route("coding", "moderate", needs_tools=True, scope=scope)
    assert chosen == "ollama/llama3.1:8b"


def test_admin_key_is_unscoped(setup):
    client, cr = setup
    # Admin (env) key carries no api_key record → unrestricted; orchestrator
    # resolves the Claude model without a 403. We can't reach a live provider,
    # but a 403 would fire before forwarding — so anything other than 403 means
    # scope did not block it.
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "smart-orchestrator",
              "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(_ADMIN),
    )
    assert resp.status_code != 403
