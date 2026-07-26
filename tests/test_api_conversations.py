"""Integration tests for the /api/conversations chat-history endpoints.

Covers create → list → get → rename → delete, message append + retrieval,
structured-content round-trip, and per-user scoping (a per-user key sees only
its own conversations; admin sees all; someone else's id is a 404, not a 403).
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
def open_client(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    return _client(CapabilityRouter(store=SqliteStore(":memory:")))


@pytest.fixture
def scoped(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    client = _client(CapabilityRouter(store=SqliteStore(":memory:")))
    alice = client.post("/api/keys", json={"user": "alice"}, headers=_auth(_ADMIN)).json()["key"]
    bob = client.post("/api/keys", json={"user": "bob"}, headers=_auth(_ADMIN)).json()["key"]
    return client, alice, bob


def test_create_list_get_roundtrip(open_client):
    r = open_client.post("/api/conversations", json={"title": "Resume help"})
    assert r.status_code == 200
    conv = r.json()
    assert conv["id"].startswith("conv-")
    assert conv["title"] == "Resume help"

    listed = open_client.get("/api/conversations").json()
    assert listed["object"] == "list"
    assert [c["id"] for c in listed["data"]] == [conv["id"]]

    detail = open_client.get(f"/api/conversations/{conv['id']}").json()
    assert detail["title"] == "Resume help"
    assert detail["messages"] == []


def test_default_title_when_blank(open_client):
    conv = open_client.post("/api/conversations", json={"title": "   "}).json()
    assert conv["title"] == "New chat"


def test_append_and_retrieve_messages_in_order(open_client):
    cid = open_client.post("/api/conversations", json={"title": "t"}).json()["id"]
    open_client.post(f"/api/conversations/{cid}/messages", json={"role": "user", "content": "hi"})
    open_client.post(f"/api/conversations/{cid}/messages", json={"role": "assistant", "content": "hello"})

    msgs = open_client.get(f"/api/conversations/{cid}").json()["messages"]
    assert [(m["role"], m["content"]) for m in msgs] == [("user", "hi"), ("assistant", "hello")]


def test_structured_content_round_trips(open_client):
    cid = open_client.post("/api/conversations", json={"title": "t"}).json()["id"]
    parts = [{"type": "text", "text": "look"}, {"type": "file", "file": {"file_id": "file-x"}}]
    open_client.post(f"/api/conversations/{cid}/messages", json={"role": "user", "content": parts})

    msg = open_client.get(f"/api/conversations/{cid}").json()["messages"][0]
    assert msg["content"] == parts  # decoded back to the exact array


def test_bad_role_is_422(open_client):
    cid = open_client.post("/api/conversations", json={"title": "t"}).json()["id"]
    r = open_client.post(f"/api/conversations/{cid}/messages", json={"role": "boss", "content": "x"})
    assert r.status_code == 422


def test_rename(open_client):
    cid = open_client.post("/api/conversations", json={"title": "old"}).json()["id"]
    r = open_client.patch(f"/api/conversations/{cid}", json={"title": "new"})
    assert r.status_code == 200
    assert r.json()["title"] == "new"
    assert open_client.get(f"/api/conversations/{cid}").json()["title"] == "new"


def test_rename_empty_is_422(open_client):
    cid = open_client.post("/api/conversations", json={"title": "old"}).json()["id"]
    assert open_client.patch(f"/api/conversations/{cid}", json={"title": "  "}).status_code == 422


def test_delete_removes_conversation_and_messages(open_client):
    cid = open_client.post("/api/conversations", json={"title": "t"}).json()["id"]
    open_client.post(f"/api/conversations/{cid}/messages", json={"role": "user", "content": "x"})

    r = open_client.delete(f"/api/conversations/{cid}")
    assert r.json()["deleted"] is True
    assert open_client.get(f"/api/conversations/{cid}").status_code == 404
    assert [c["id"] for c in open_client.get("/api/conversations").json()["data"]] == []


def test_missing_conversation_is_404(open_client):
    assert open_client.get("/api/conversations/conv-deadbeef").status_code == 404
    assert open_client.delete("/api/conversations/conv-deadbeef").status_code == 404
    assert open_client.patch("/api/conversations/conv-deadbeef", json={"title": "x"}).status_code == 404


# ── Per-user scoping ───────────────────────────────────────────────────────────

def test_user_sees_only_own_conversations(scoped):
    client, alice, bob = scoped
    a_id = client.post("/api/conversations", json={"title": "A"}, headers=_auth(alice)).json()["id"]
    b_id = client.post("/api/conversations", json={"title": "B"}, headers=_auth(bob)).json()["id"]

    a_list = [c["id"] for c in client.get("/api/conversations", headers=_auth(alice)).json()["data"]]
    assert a_list == [a_id]

    # Alice cannot see, rename, delete, or post to Bob's conversation (404).
    assert client.get(f"/api/conversations/{b_id}", headers=_auth(alice)).status_code == 404
    assert client.patch(f"/api/conversations/{b_id}", json={"title": "x"}, headers=_auth(alice)).status_code == 404
    assert client.delete(f"/api/conversations/{b_id}", headers=_auth(alice)).status_code == 404
    assert client.post(f"/api/conversations/{b_id}/messages",
                       json={"role": "user", "content": "x"}, headers=_auth(alice)).status_code == 404


def test_admin_sees_all_conversations(scoped):
    client, alice, bob = scoped
    client.post("/api/conversations", json={"title": "A"}, headers=_auth(alice))
    client.post("/api/conversations", json={"title": "B"}, headers=_auth(bob))
    titles = {c["title"] for c in client.get("/api/conversations", headers=_auth(_ADMIN)).json()["data"]}
    assert titles == {"A", "B"}
