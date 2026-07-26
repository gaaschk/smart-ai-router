"""Integration tests for /api/whoami — the identity behind the current key.

The endpoint lets the UI show who you're signed in as (root/admin, a per-user
label, or open mode) without ever exposing the secret. It mirrors the identity
the auth middleware attaches to each request.
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
        warnings.simplefilter("ignore")
        return TestClient(create_app(cr))


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    return _client(CapabilityRouter(store=SqliteStore(":memory:")))


def test_admin_env_key_reports_admin(admin_client):
    me = admin_client.get("/api/whoami", headers=_auth(_ADMIN)).json()
    assert me["authenticated"] is True
    assert me["kind"] == "admin"
    assert me["user"] == "admin"
    assert me["is_admin"] is True


def test_per_user_key_reports_user_and_prefix(admin_client):
    created = admin_client.post(
        "/api/keys", json={"user": "alice"}, headers=_auth(_ADMIN)
    ).json()
    me = admin_client.get("/api/whoami", headers=_auth(created["key"])).json()
    assert me["kind"] == "user"
    assert me["user"] == "alice"
    assert me["is_admin"] is False
    # Prefix is the safe display slice, and it must match the key's real prefix.
    assert me["key_prefix"] and created["key"].startswith(me["key_prefix"])


def test_whoami_never_leaks_the_secret(admin_client):
    created = admin_client.post(
        "/api/keys", json={"user": "bob"}, headers=_auth(_ADMIN)
    ).json()
    body = admin_client.get("/api/whoami", headers=_auth(created["key"])).text
    assert created["key"] not in body


def test_missing_key_is_401_when_keys_configured(admin_client):
    # whoami sits behind the same auth wall as the rest of /api.
    assert admin_client.get("/api/whoami").status_code == 401


def test_open_mode_reports_open_and_first_run_admin(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    client = _client(CapabilityRouter(store=SqliteStore(":memory:")))
    me = client.get("/api/whoami").json()
    assert me["authenticated"] is False
    assert me["kind"] == "open"
    # First-run (no keys anywhere) → the UI may still offer key management.
    assert me["is_admin"] is True
