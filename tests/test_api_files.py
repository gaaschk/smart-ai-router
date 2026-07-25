"""Integration tests for the OpenAI-compatible /v1/files endpoints.

Covers upload → list → get → download → delete, the OpenAI object shape, the
size ceiling (413), and per-user scoping (a per-user key sees only its own
files; admin sees all).
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
def blobs(tmp_path, monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_FILES_DIR", str(tmp_path / "blobs"))
    return tmp_path


@pytest.fixture
def open_client(blobs, monkeypatch):
    """Open (no-auth) router — simplest surface for shape/roundtrip tests."""
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    return _client(cr)


def _upload(client, name, content, *, headers=None, purpose="assistants"):
    return client.post(
        "/v1/files",
        files={"file": (name, content, "text/plain")},
        data={"purpose": purpose},
        headers=headers or {},
    )


def test_upload_returns_openai_shape(open_client):
    resp = _upload(open_client, "notes.txt", b"hello world")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"].startswith("file-")
    assert body["object"] == "file"
    assert body["filename"] == "notes.txt"
    assert body["bytes"] == 11
    assert body["purpose"] == "assistants"
    assert isinstance(body["created_at"], int)


def test_upload_list_get_download_delete_roundtrip(open_client):
    fid = _upload(open_client, "doc.txt", b"body text").json()["id"]

    listed = open_client.get("/v1/files").json()
    assert listed["object"] == "list"
    assert [f["id"] for f in listed["data"]] == [fid]

    got = open_client.get(f"/v1/files/{fid}").json()
    assert got["id"] == fid

    content = open_client.get(f"/v1/files/{fid}/content")
    assert content.status_code == 200
    assert content.content == b"body text"

    deleted = open_client.delete(f"/v1/files/{fid}").json()
    assert deleted == {"id": fid, "object": "file", "deleted": True}
    assert open_client.get(f"/v1/files/{fid}").status_code == 404


def test_missing_file_is_404(open_client):
    assert open_client.get("/v1/files/file-deadbeef").status_code == 404
    assert open_client.get("/v1/files/file-deadbeef/content").status_code == 404


def test_oversize_upload_rejected(blobs, monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    monkeypatch.setenv("SMART_ROUTER_MAX_FILE_MB", "1")
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    client = _client(cr)
    big = b"x" * (1024 * 1024 + 1)
    resp = client.post("/v1/files", files={"file": ("big.bin", big, "application/octet-stream")})
    assert resp.status_code == 413


# ── Per-user scoping ───────────────────────────────────────────────────────────

@pytest.fixture
def scoped(blobs, monkeypatch):
    """Admin env key + two per-user keys minted through the admin endpoint."""
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    client = _client(cr)
    alice = client.post("/api/keys", json={"user": "alice"}, headers=_auth(_ADMIN)).json()["key"]
    bob = client.post("/api/keys", json={"user": "bob"}, headers=_auth(_ADMIN)).json()["key"]
    return client, alice, bob


def test_user_sees_only_own_files(scoped):
    client, alice, bob = scoped
    a_id = _upload(client, "a.txt", b"alice", headers=_auth(alice)).json()["id"]
    b_id = _upload(client, "b.txt", b"bob", headers=_auth(bob)).json()["id"]

    a_list = [f["id"] for f in client.get("/v1/files", headers=_auth(alice)).json()["data"]]
    assert a_list == [a_id]

    # Alice cannot see or fetch Bob's file (404, not 403 — don't leak existence).
    assert client.get(f"/v1/files/{b_id}", headers=_auth(alice)).status_code == 404
    assert client.get(f"/v1/files/{b_id}/content", headers=_auth(alice)).status_code == 404
    assert client.delete(f"/v1/files/{b_id}", headers=_auth(alice)).status_code == 404


def test_admin_sees_all_files(scoped):
    client, alice, bob = scoped
    a_id = _upload(client, "a.txt", b"alice", headers=_auth(alice)).json()["id"]
    b_id = _upload(client, "b.txt", b"bob", headers=_auth(bob)).json()["id"]

    all_ids = {f["id"] for f in client.get("/v1/files", headers=_auth(_ADMIN)).json()["data"]}
    assert all_ids == {a_id, b_id}
