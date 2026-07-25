"""Unit tests for file-reference resolution in chat messages.

Covers: image ids → base64 data: URIs, document ids → injected text, owner
scoping (a caller can't expand another user's file), and pass-through of
already-inline content.
"""
import base64

import pytest

from smart_ai_router import fileref
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.store.sqlite_store import SqliteStore


@pytest.fixture
def cr(tmp_path, monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_FILES_DIR", str(tmp_path / "blobs"))
    return CapabilityRouter(store=SqliteStore(":memory:"))


def _img_msg(file_id):
    return [{"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": file_id}},
    ]}]


def test_image_ref_expands_to_base64_data_uri(cr):
    rec = cr.upload_file(b"\x89PNGdata", filename="p.png", mime="image/png", user="alice")
    out = fileref.resolve_file_refs(_img_msg(rec.id), cr, user="alice")
    part = out[0]["content"][1]
    assert part["type"] == "image_url"
    expected = base64.b64encode(b"\x89PNGdata").decode("ascii")
    assert part["image_url"]["url"] == f"data:image/png;base64,{expected}"


def test_document_ref_expands_to_text(cr):
    rec = cr.upload_file(b"hello document", filename="d.txt", mime="text/plain", user="alice")
    msgs = [{"role": "user", "content": [
        {"type": "file", "file": {"file_id": rec.id}},
    ]}]
    out = fileref.resolve_file_refs(msgs, cr, user="alice")
    part = out[0]["content"][0]
    assert part["type"] == "text"
    assert "hello document" in part["text"]
    assert "d.txt" in part["text"]


def test_inline_content_passes_through(cr):
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]}]
    out = fileref.resolve_file_refs(msgs, cr, user="alice")
    assert out == msgs  # untouched


def test_string_content_passes_through(cr):
    msgs = [{"role": "user", "content": "plain string"}]
    assert fileref.resolve_file_refs(msgs, cr, user="alice") == msgs


def test_unknown_id_raises(cr):
    with pytest.raises(fileref.FileRefError):
        fileref.resolve_file_refs(_img_msg("file-deadbeef"), cr, user="alice")


def test_cannot_expand_another_users_file(cr):
    rec = cr.upload_file(b"secret", filename="s.txt", mime="text/plain", user="bob")
    with pytest.raises(fileref.FileRefError):
        fileref.resolve_file_refs(_img_msg(rec.id), cr, user="alice")


def test_admin_can_expand_any_file(cr):
    rec = cr.upload_file(b"x", filename="s.png", mime="image/png", user="bob")
    out = fileref.resolve_file_refs(_img_msg(rec.id), cr, user="admin", is_admin=True)
    assert out[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_contains_image_detects_resolved_image(cr):
    rec = cr.upload_file(b"x", filename="s.png", mime="image/png", user="alice")
    out = fileref.resolve_file_refs(_img_msg(rec.id), cr, user="alice")
    assert fileref.contains_image(out) is True
    assert fileref.contains_image([{"role": "user", "content": "no image"}]) is False
