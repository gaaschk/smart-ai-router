"""Tests for scanned-PDF OCR-by-vision: rasterization + fileref integration.

A "scanned" PDF here is one with no text layer (extracted_text == ""). We build
such PDFs with pymupdf so the tests are hermetic and don't need fixtures.
"""
import base64

import pytest

from smart_ai_router import fileref, ocr
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.store.sqlite_store import SqliteStore

fitz = pytest.importorskip("fitz")  # pymupdf; skip if the backend isn't installed


def _image_only_pdf(pages: int = 2) -> bytes:
    """A PDF whose pages carry only a drawn rectangle — no extractable text."""
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=200, height=200)
        page.draw_rect(fitz.Rect(20, 20, 180, 180), fill=(0.2, 0.4, 0.8))
    data = doc.tobytes()
    doc.close()
    return data


def _text_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hello born-digital text")
    data = doc.tobytes()
    doc.close()
    return data


# ── ocr module ─────────────────────────────────────────────────────────────────

def test_available_true_when_pymupdf_installed():
    assert ocr.available() is True


def test_is_scanned_pdf_only_for_empty_text_pdf():
    assert ocr.is_scanned_pdf("application/pdf", "") is True
    assert ocr.is_scanned_pdf("application/pdf", "   ") is True
    assert ocr.is_scanned_pdf("application/pdf", "has text") is False
    assert ocr.is_scanned_pdf("text/plain", "") is False


def test_rasterize_yields_one_png_per_page():
    pages = ocr.rasterize_pdf(_image_only_pdf(3))
    assert len(pages) == 3
    assert all(p[:8] == b"\x89PNG\r\n\x1a\n" for p in pages)  # PNG signature


def test_rasterize_respects_page_cap():
    pages = ocr.rasterize_pdf(_image_only_pdf(5), max_pages=2)
    assert len(pages) == 2


def test_rasterize_malformed_pdf_returns_empty():
    assert ocr.rasterize_pdf(b"not a pdf") == []


def test_page_count():
    assert ocr.page_count(_image_only_pdf(4)) == 4
    assert ocr.page_count(b"garbage") == 0


def test_max_pages_env_override(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_OCR_MAX_PAGES", "3")
    assert ocr.ocr_max_pages() == 3


# ── fileref integration ─────────────────────────────────────────────────────────

@pytest.fixture
def cr(tmp_path, monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_FILES_DIR", str(tmp_path / "blobs"))
    return CapabilityRouter(store=SqliteStore(":memory:"))


def _ref_msg(file_id):
    return [{"role": "user", "content": [{"type": "file", "file": {"file_id": file_id}}]}]


def test_scanned_pdf_expands_to_page_images(cr):
    # A born-digital text PDF extracts text at upload; an image-only one doesn't.
    rec = cr.upload_file(_image_only_pdf(2), filename="scan.pdf",
                         mime="application/pdf", user="alice")
    assert rec.extracted_text.strip() == ""  # no text layer → OCR path

    out = fileref.resolve_file_refs(_ref_msg(rec.id), cr, user="alice")
    parts = out[0]["content"]
    # A text preamble followed by one image_url per rendered page.
    assert parts[0]["type"] == "text" and "scanned PDF" in parts[0]["text"]
    images = [p for p in parts if p["type"] == "image_url"]
    assert len(images) == 2
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    # And the request now looks like it needs vision.
    assert fileref.contains_image(out) is True


def test_scanned_pdf_respects_page_cap(cr, monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_OCR_MAX_PAGES", "1")
    rec = cr.upload_file(_image_only_pdf(4), filename="scan.pdf",
                         mime="application/pdf", user="alice")
    out = fileref.resolve_file_refs(_ref_msg(rec.id), cr, user="alice")
    parts = out[0]["content"]
    images = [p for p in parts if p["type"] == "image_url"]
    assert len(images) == 1
    # Preamble notes the truncation (1 of 4).
    assert "of 4" in parts[0]["text"]


def test_born_digital_pdf_stays_text(cr):
    rec = cr.upload_file(_text_pdf(), filename="doc.pdf",
                         mime="application/pdf", user="alice")
    assert "born-digital" in rec.extracted_text
    out = fileref.resolve_file_refs(_ref_msg(rec.id), cr, user="alice")
    parts = out[0]["content"]
    # Text path — no images, extracted text injected.
    assert all(p["type"] == "text" for p in parts)
    assert fileref.contains_image(out) is False
