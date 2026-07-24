"""Integration tests for per-user API-key auth and key management endpoints.

Deployment model under test: the operator configures an admin key via
SMART_ROUTER_API_KEYS, then mints per-user keys (stored in the DB) through the
admin-only /api/keys endpoints. Per-user keys authenticate requests but cannot
manage other keys.
"""
import warnings

import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.store.sqlite_store import SqliteStore

_ADMIN = "admin-secret"


def _client(cr) -> TestClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # starlette/httpx deprecation noise
        return TestClient(create_app(cr))


@pytest.fixture
def admin_client(monkeypatch):
    """Client with an admin env key configured and a fresh in-memory store."""
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    return _client(cr), cr


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_open_when_no_keys_configured(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    client = _client(cr)
    # No env keys, no DB keys → router stays open (first-run behavior).
    assert client.get("/api/models").status_code == 200
    # And the first key can be minted in this bootstrap state.
    assert client.post("/api/keys", json={"user": "alice"}).status_code == 201


def test_admin_env_key_authenticates(admin_client):
    client, _ = admin_client
    assert client.get("/api/models").status_code == 401
    assert client.get("/api/models", headers=_auth(_ADMIN)).status_code == 200


def test_create_key_returns_plaintext_once_then_never(admin_client):
    client, _ = admin_client
    resp = client.post("/api/keys", json={"user": "alice"}, headers=_auth(_ADMIN))
    assert resp.status_code == 201
    body = resp.json()
    assert body["user"] == "alice"
    assert body["key"].startswith("sk-smart-")
    assert body["enabled"] is True
    prefix = body["key_prefix"]

    # Listing never re-exposes the secret.
    listed = client.get("/api/keys", headers=_auth(_ADMIN)).json()
    assert len(listed) == 1
    assert "key" not in listed[0]
    assert listed[0]["key_prefix"] == prefix


def test_db_key_authenticates_but_cannot_manage_keys(admin_client):
    client, _ = admin_client
    key = client.post("/api/keys", json={"user": "bob"}, headers=_auth(_ADMIN)).json()["key"]

    # A wrong token is rejected.
    assert client.get("/api/models", headers=_auth("wrong")).status_code == 401
    # The real per-user key works for normal endpoints.
    assert client.get("/api/models", headers=_auth(key)).status_code == 200
    # ...but must not be able to enumerate or mint keys.
    assert client.get("/api/keys", headers=_auth(key)).status_code == 403
    assert client.post("/api/keys", json={"user": "x"}, headers=_auth(key)).status_code == 403


def test_disabled_key_is_rejected(admin_client):
    client, _ = admin_client
    created = client.post("/api/keys", json={"user": "carol"}, headers=_auth(_ADMIN)).json()
    key, prefix = created["key"], created["key_prefix"]
    assert client.get("/api/models", headers=_auth(key)).status_code == 200

    # Revoke and confirm it stops working — no redeploy.
    r = client.put(f"/api/keys/{prefix}/enabled", json={"enabled": False}, headers=_auth(_ADMIN))
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert client.get("/api/models", headers=_auth(key)).status_code == 401


def test_delete_key_revokes_access(admin_client):
    client, _ = admin_client
    created = client.post("/api/keys", json={"user": "dave"}, headers=_auth(_ADMIN)).json()
    key, prefix = created["key"], created["key_prefix"]
    assert client.delete(f"/api/keys/{prefix}", headers=_auth(_ADMIN)).status_code == 204
    assert client.get("/api/models", headers=_auth(key)).status_code == 401
    assert client.delete(f"/api/keys/{prefix}", headers=_auth(_ADMIN)).status_code == 404


def test_empty_user_rejected(admin_client):
    client, _ = admin_client
    assert client.post("/api/keys", json={"user": "  "}, headers=_auth(_ADMIN)).status_code == 422


def test_using_a_key_updates_last_used(admin_client):
    client, cr = admin_client
    created = client.post("/api/keys", json={"user": "erin"}, headers=_auth(_ADMIN)).json()
    key, prefix = created["key"], created["key_prefix"]
    assert created["last_used_at"] == ""
    client.get("/api/models", headers=_auth(key))
    match = next(k for k in cr.all_api_keys() if k.key_prefix == prefix)
    assert match.last_used_at != ""
