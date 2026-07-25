"""Rasterize scanned/image-only PDFs to page images for vision models.

A born-digital PDF has a text layer that extract.py pulls out cheaply. A
scanned PDF is really a stack of images with no text — extract yields nothing.
For those, the only way to read the content is to render each page to an image
and hand it to a vision-capable model.

This is deliberately separate from extract.py (which produces text): rendering
produces *images*, needs pymupdf, and is only worth doing lazily at request
time — once we know the file is actually being used, and after the text path
has come up empty.

pymupdf is pip-only (no system packages), so this stays install-clean. If it
isn't importable, rendering is a no-op ([]) and the caller degrades to a clear
"couldn't read" path rather than crashing.
"""
from __future__ import annotations

import os

# Default page ceiling — a scanned book shouldn't explode one request into
# hundreds of images. Override with SMART_ROUTER_OCR_MAX_PAGES.
_DEFAULT_MAX_PAGES = 10
# Render resolution. 150 DPI is a good legibility/size trade for vision OCR.
_DEFAULT_DPI = 150


def ocr_max_pages() -> int:
    try:
        n = int(os.environ.get("SMART_ROUTER_OCR_MAX_PAGES", _DEFAULT_MAX_PAGES))
    except ValueError:
        n = _DEFAULT_MAX_PAGES
    return max(1, n)


def ocr_dpi() -> int:
    try:
        d = int(os.environ.get("SMART_ROUTER_OCR_DPI", _DEFAULT_DPI))
    except ValueError:
        d = _DEFAULT_DPI
    return max(72, d)


def available() -> bool:
    """True if the rasterizer backend (pymupdf) can be imported."""
    try:
        import fitz  # noqa: F401  (pymupdf)
        return True
    except ImportError:
        return False


def is_scanned_pdf(mime: str, extracted_text: str) -> bool:
    """A PDF with no usable text layer — the case OCR-by-vision exists for."""
    return (mime or "").lower() == "application/pdf" and not (extracted_text or "").strip()


def rasterize_pdf(data: bytes, *, max_pages: int | None = None, dpi: int | None = None) -> list[bytes]:
    """Render PDF pages to PNG bytes (one per page), capped at max_pages.

    Best-effort: returns [] if pymupdf is unavailable or the PDF can't be
    opened, and skips any individual page that fails to render. Never raises —
    a bad document must not break the request.
    """
    try:
        import fitz  # pymupdf
    except ImportError:
        return []

    cap = max_pages if max_pages is not None else ocr_max_pages()
    resolution = dpi if dpi is not None else ocr_dpi()
    out: list[bytes] = []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:  # noqa: BLE001 — malformed PDF → nothing to render
        return []
    try:
        for page in doc:
            if len(out) >= cap:
                break
            try:
                pix = page.get_pixmap(dpi=resolution)
                out.append(pix.tobytes("png"))
            except Exception:  # noqa: BLE001 — one bad page shouldn't fail all
                continue
    finally:
        doc.close()
    return out


def page_count(data: bytes) -> int:
    """Total pages in a PDF (0 if unreadable/unavailable). For truncation notes."""
    try:
        import fitz
    except ImportError:
        return 0
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:  # noqa: BLE001
        return 0
    try:
        return doc.page_count
    finally:
        doc.close()
