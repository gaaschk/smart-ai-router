"""Unit tests for filesystem blob storage, text extraction, and the store layer.

Covers the pieces that back the Files API: OpenAI-style id generation with a
path-traversal guard, on-disk blob read/write/delete, best-effort text
extraction, and the SQLite metadata table.
"""
import pytest

from smart_ai_router import extract, files
from smart_ai_router.models import FileRecord
from smart_ai_router.store.sqlite_store import SqliteStore


# ── Blob storage + id safety ───────────────────────────────────────────────────

@pytest.fixture
def files_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_FILES_DIR", str(tmp_path / "blobs"))
    return tmp_path / "blobs"


def test_generate_id_is_openai_shaped():
    fid = files.generate_file_id()
    assert fid.startswith("file-")
    # token is lowercase hex
    token = fid[len("file-"):]
    assert token and all(c in "0123456789abcdef" for c in token)


def test_write_read_delete_roundtrip(files_root):
    fid = files.generate_file_id()
    path = files.write_blob(fid, b"hello bytes")
    assert path.exists()
    assert files.read_blob(fid) == b"hello bytes"
    assert files.delete_blob(fid) is True
    assert files.delete_blob(fid) is False  # already gone


@pytest.mark.parametrize("bad", ["../etc/passwd", "file-../x", "nope", "file-XYZ", "file-"])
def test_bad_ids_rejected(bad):
    with pytest.raises(ValueError):
        files.blob_path(bad)


def test_max_file_bytes_env_override(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_MAX_FILE_MB", "2")
    assert files.max_file_bytes() == 2 * 1024 * 1024


# ── Text extraction ────────────────────────────────────────────────────────────

def test_extract_plain_text():
    assert extract.extract_text(b"line one\nline two", "text/plain") == "line one\nline two"


def test_extract_json_type():
    assert extract.is_extractable("application/json")
    assert extract.extract_text(b'{"a":1}', "application/json") == '{"a":1}'


def test_extract_octet_stream_falls_back_to_decode():
    # Many code files arrive as octet-stream; we still try a text decode.
    assert extract.extract_text(b"print('hi')", "application/octet-stream") == "print('hi')"


def test_extract_image_yields_nothing():
    assert extract.is_extractable("image/png") is False
    assert extract.extract_text(b"\x89PNG\r\n", "image/png") == ""


def test_extract_malformed_pdf_never_raises():
    # Not a real PDF — extraction is best-effort and must return "" not crash.
    assert extract.extract_text(b"%PDF-broken", "application/pdf") == ""


# ── Store metadata ─────────────────────────────────────────────────────────────

def test_create_get_list_delete_file_record():
    store = SqliteStore(":memory:")
    rec = store.create_file(FileRecord(
        id="file-abc123", user="alice", filename="a.txt",
        mime="text/plain", bytes=5, path="/tmp/x",
    ))
    assert rec.created_at  # stamped on insert

    got = store.get_file("file-abc123")
    assert got is not None and got.user == "alice" and got.filename == "a.txt"

    assert store.delete_file("file-abc123") is True
    assert store.get_file("file-abc123") is None
    assert store.delete_file("file-abc123") is False


def test_list_files_scoped_by_user():
    store = SqliteStore(":memory:")
    store.create_file(FileRecord(id="file-a1", user="alice", filename="a"))
    store.create_file(FileRecord(id="file-b1", user="bob", filename="b"))

    assert {r.id for r in store.list_files()} == {"file-a1", "file-b1"}
    assert [r.id for r in store.list_files("alice")] == ["file-a1"]
    assert [r.id for r in store.list_files("bob")] == ["file-b1"]
