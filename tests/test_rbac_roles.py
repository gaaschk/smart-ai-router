"""Three-role access matrix: guest / user / admin.

Pins the backend gates that match the UI's data-admin / data-user tab
hiding (see smart_ai_router/api/ui/index.html):

  Guest  — chat + report write (+ whoami / conversations / signup)
  User   — guest surfaces + own usage + own GBrain memory (search/pages/remember)
  Admin  — everything, including providers / models catalog / sync / updates /
           keys / settings / reports list / brain-wide GBrain (stats/skills/jobs)
"""
from __future__ import annotations

import warnings
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.store.sqlite_store import SqliteStore

_ADMIN = "admin-secret-rbac"


def _client(cr) -> TestClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return TestClient(create_app(cr))


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    return _client(cr), cr


@pytest.fixture
def user_key(admin_client):
    client, _ = admin_client
    return client.post(
        "/api/keys", json={"user": "alice"}, headers=_auth(_ADMIN)
    ).json()["key"]


# ── Admin ────────────────────────────────────────────────────────────────────

def test_admin_reaches_operator_surfaces(admin_client):
    client, _ = admin_client
    h = _auth(_ADMIN)
    assert client.get("/api/models", headers=h).status_code == 200
    assert client.get("/api/providers", headers=h).status_code == 200
    assert client.get("/api/keys", headers=h).status_code == 200
    assert client.get("/api/settings", headers=h).status_code == 200
    assert client.get("/api/updates?fetch=false", headers=h).status_code == 200
    assert client.get("/api/usage", headers=h).status_code == 200


# ── User (operator-issued per-user key) ──────────────────────────────────────

def test_user_can_reach_usage_and_whoami(admin_client, user_key):
    client, _ = admin_client
    h = _auth(user_key)
    me = client.get("/api/whoami", headers=h).json()
    assert me["kind"] == "user"
    assert me["is_admin"] is False
    assert me["user"] == "alice"
    assert client.get("/api/usage", headers=h).status_code == 200


def test_user_blocked_from_admin_operator_surfaces(admin_client, user_key):
    client, _ = admin_client
    h = _auth(user_key)
    for path, method in (
        ("/api/models", "get"),
        ("/api/providers", "get"),
        ("/api/keys", "get"),
        ("/api/settings", "get"),
        ("/api/updates?fetch=false", "get"),
        ("/api/reports", "get"),
        ("/api/sync", "post"),
    ):
        if method == "get":
            r = client.get(path, headers=h)
        else:
            r = client.post(path, headers=h, json={})
        assert r.status_code == 403, f"{method.upper()} {path} -> {r.status_code}"


def test_user_blocked_from_brain_wide_gbrain(admin_client, user_key):
    """Skills / stats / jobs are shared-brain ops — not per-user source scoped."""
    client, _ = admin_client
    h = _auth(user_key)
    for path in (
        "/api/gbrain/stats",
        "/api/gbrain/health",
        "/api/gbrain/integrations",
        "/api/gbrain/jobs",
        "/api/gbrain/jobs/catalog",
    ):
        assert client.get(path, headers=h).status_code == 403, path
    assert client.post(
        "/api/gbrain/jobs?name=embed", headers=h, json={}
    ).status_code == 403


def test_user_can_reach_own_gbrain_memory(admin_client, user_key):
    """Memory endpoints stay open to authenticated users; source isolation
    (source_id_for_user) scopes the data — see test_gbrain_source_isolation.py.
    """
    client, _ = admin_client
    h = _auth(user_key)
    fake = MagicMock()
    fake.list_pages.return_value = []
    fake.search.return_value = []
    fake.hybrid_query.return_value = []
    fake.ensure_source.return_value = True
    fake.remember.return_value = {"ok": True}

    with patch("smart_ai_router.api.gbrain_routes.get_gbrain", return_value=fake):
        assert client.get("/api/gbrain/pages", headers=h).status_code == 200
        assert client.get("/api/gbrain/search?q=hello", headers=h).status_code == 200
        assert client.post(
            "/api/gbrain/remember?title=t&content=c", headers=h
        ).status_code == 200


# ── Guest / open mode still works for bootstrap ──────────────────────────────

def test_open_mode_still_allows_first_key_mint(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    client = _client(cr)
    assert client.get("/api/models").status_code == 200
    assert client.post("/api/keys", json={"user": "alice"}).status_code == 201
