"""Integration tests for GET /api/usage — the dashboard rollup endpoint.

Scoped like conversations: the admin identity sees all users (with a by_user
breakdown); a per-user key sees only its own rows and no by_user.
"""
import warnings

import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import UsageRecord
from smart_ai_router.store.sqlite_store import SqliteStore

_ADMIN = "admin-secret"


def _client(cr) -> TestClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return TestClient(create_app(cr))


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _rec(user, model, ts, **kw):
    return UsageRecord(
        user=user, key_prefix=user[:4], routed_model=model,
        domain=kw.get("domain", "coding"), complexity=kw.get("complexity", "moderate"),
        prompt_tokens=kw.get("pt", 100), completion_tokens=kw.get("ct", 50),
        cost_usd=kw.get("cost", 0.01), status=200, ts=ts,
    )


@pytest.fixture
def store():
    s = SqliteStore(":memory:")
    # Use a recent-ish timestamp so it falls within the default 30-day window is
    # NOT reliable across time; instead widen the query window in tests below.
    s.record_usage(_rec("alice", "openrouter/gpt-4", "2099-01-01T00:00:00+00:00"))
    s.record_usage(_rec("bob", "ollama/llama3", "2099-01-01T00:00:00+00:00", cost=0.0))
    return s


@pytest.fixture
def admin_client(monkeypatch, store):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    return _client(CapabilityRouter(store=store))


def test_admin_sees_all_users_and_by_user(admin_client):
    # days=365000 → window easily covers the 2099 rows.
    body = admin_client.get(
        "/api/usage?days=365000", headers=_auth(_ADMIN)
    ).json()
    assert body["totals"]["requests"] == 2
    assert body["by_user"] is not None
    users = {r["key"] for r in body["by_user"]}
    assert users == {"alice", "bob"}


def test_per_user_key_scoped_and_no_by_user(admin_client):
    created = admin_client.post(
        "/api/keys", json={"user": "alice"}, headers=_auth(_ADMIN)
    ).json()
    body = admin_client.get(
        "/api/usage?days=365000", headers=_auth(created["key"])
    ).json()
    assert body["totals"]["requests"] == 1  # only alice's row
    assert body["by_user"] is None
    models = {r["key"] for r in body["by_model"]}
    assert models == {"openrouter/gpt-4"}


def test_days_window_bounds_which_rows_count(monkeypatch):
    # A row 60 days old falls inside a 90-day window but outside the default
    # 30-day one; a 10-day-old row is inside both. Timestamps are computed
    # relative to now so the assertions don't drift with the calendar.
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    ten_days = (now - timedelta(days=10)).isoformat()
    sixty_days = (now - timedelta(days=60)).isoformat()

    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    s = SqliteStore(":memory:")
    s.record_usage(_rec("alice", "m", ten_days))
    s.record_usage(_rec("alice", "m", sixty_days))
    client = _client(CapabilityRouter(store=s))

    default_30 = client.get("/api/usage", headers=_auth(_ADMIN)).json()
    assert default_30["totals"]["requests"] == 1  # only the 10-day-old row

    ninety = client.get("/api/usage?days=90", headers=_auth(_ADMIN)).json()
    assert ninety["totals"]["requests"] == 2  # both rows


def test_usage_requires_auth_when_keys_configured(admin_client):
    assert admin_client.get("/api/usage").status_code == 401
