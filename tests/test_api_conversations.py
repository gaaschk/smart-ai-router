"""Integration tests for the /api/conversations chat-history endpoints.

Covers create → list → get → rename → delete, message append + retrieval,
structured-content round-trip, tag (grouping) normalization and limits, the
admin's `?user=` owner filter, and per-user scoping (a per-user key sees only
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


# ── Tags (grouping) ────────────────────────────────────────────────────────────

def test_tags_survive_create_list_and_get(open_client):
    conv = open_client.post(
        "/api/conversations", json={"title": "t", "tags": ["work", "cost"]}
    ).json()
    assert sorted(conv["tags"]) == ["cost", "work"]
    assert sorted(open_client.get("/api/conversations").json()["data"][0]["tags"]) == ["cost", "work"]
    assert sorted(open_client.get(f"/api/conversations/{conv['id']}").json()["tags"]) == ["cost", "work"]


def test_tags_are_normalized(open_client):
    conv = open_client.post(
        "/api/conversations", json={"title": "t", "tags": ["  Work ", "WORK", "", "  ", "Cost"]}
    ).json()
    # Lowercased, trimmed, blanks dropped, deduped case-insensitively, order kept.
    assert conv["tags"] == ["work", "cost"]


def test_patch_replaces_tags_without_touching_title(open_client):
    cid = open_client.post("/api/conversations", json={"title": "keep", "tags": ["a"]}).json()["id"]
    r = open_client.patch(f"/api/conversations/{cid}", json={"tags": ["b", "c"]})
    assert r.status_code == 200
    assert r.json()["title"] == "keep"
    assert r.json()["tags"] == ["b", "c"]


def test_patch_can_clear_tags(open_client):
    cid = open_client.post("/api/conversations", json={"title": "t", "tags": ["a"]}).json()["id"]
    assert open_client.patch(f"/api/conversations/{cid}", json={"tags": []}).json()["tags"] == []


def test_patch_renames_without_touching_tags(open_client):
    cid = open_client.post("/api/conversations", json={"title": "old", "tags": ["a"]}).json()["id"]
    r = open_client.patch(f"/api/conversations/{cid}", json={"title": "new"})
    assert (r.json()["title"], r.json()["tags"]) == ("new", ["a"])


def test_patch_title_and_tags_together(open_client):
    cid = open_client.post("/api/conversations", json={"title": "old"}).json()["id"]
    r = open_client.patch(f"/api/conversations/{cid}", json={"title": "new", "tags": ["x"]})
    assert (r.json()["title"], r.json()["tags"]) == ("new", ["x"])


def test_patch_with_no_fields_is_422(open_client):
    cid = open_client.post("/api/conversations", json={"title": "t"}).json()["id"]
    assert open_client.patch(f"/api/conversations/{cid}", json={}).status_code == 422


def test_rejected_tags_are_422(open_client):
    cid = open_client.post("/api/conversations", json={"title": "t"}).json()["id"]
    for bad in (["a" * 25], ["needs,split"], [f"t{n}" for n in range(13)]):
        assert open_client.patch(f"/api/conversations/{cid}", json={"tags": bad}).status_code == 422
    # None of the rejected sets were applied.
    assert open_client.get(f"/api/conversations/{cid}").json()["tags"] == []


def test_list_filters_by_tag(open_client):
    open_client.post("/api/conversations", json={"title": "A", "tags": ["work"]})
    open_client.post("/api/conversations", json={"title": "B", "tags": ["home"]})
    titles = [c["title"] for c in open_client.get("/api/conversations?tag=WORK").json()["data"]]
    assert titles == ["A"]   # the filter is case-folded like the tags themselves


def test_deleting_a_conversation_drops_it_from_tag_filters(open_client):
    cid = open_client.post("/api/conversations", json={"title": "A", "tags": ["work"]}).json()["id"]
    open_client.delete(f"/api/conversations/{cid}")
    assert open_client.get("/api/conversations?tag=work").json()["data"] == []


# ── Admin owner filter ─────────────────────────────────────────────────────────

def test_admin_can_filter_by_user(scoped):
    client, alice, bob = scoped
    client.post("/api/conversations", json={"title": "A"}, headers=_auth(alice))
    client.post("/api/conversations", json={"title": "B"}, headers=_auth(bob))

    body = client.get("/api/conversations?user=alice", headers=_auth(_ADMIN)).json()
    assert [c["title"] for c in body["data"]] == ["A"]
    assert [c["user"] for c in body["data"]] == ["alice"]
    # The owner options list everyone with history, not just the filtered owner,
    # so the picker can always get back out of the filter.
    assert body["users"] == ["alice", "bob"]


def test_admin_filter_on_unknown_user_is_empty(scoped):
    client, alice, _ = scoped
    client.post("/api/conversations", json={"title": "A"}, headers=_auth(alice))
    assert client.get("/api/conversations?user=nobody", headers=_auth(_ADMIN)).json()["data"] == []


def test_user_may_not_filter_by_another_user(scoped):
    client, alice, bob = scoped
    client.post("/api/conversations", json={"title": "B"}, headers=_auth(bob))
    r = client.get("/api/conversations?user=bob", headers=_auth(alice))
    assert r.status_code == 403
    # Asking for itself is fine, and gets no owner options to snoop through.
    own = client.get("/api/conversations?user=alice", headers=_auth(alice))
    assert own.status_code == 200
    assert own.json()["users"] == []
