"""Document text extraction for uploaded files.

Turns a document into plain text that can be injected into any model's context —
so documents work with every model, not just vision ones. Images are NOT handled
here: they're inlined as base64 for vision models at request time.

Supported:
  - PDF (born-digital text layer) via pypdf
  - text/* and common code/data types (utf-8 decode)

A scanned/image-only PDF yields little or no text here; that case is handled
later by rasterizing pages to images and routing to a vision model.
"""
from __future__ import annotations

import io

# MIME prefixes/types we treat as decodable plain text.
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_EXACT = {
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "application/javascript",
    "application/x-sh",
    "application/toml",
    "application/x-python",
}


def is_extractable(mime: str) -> bool:
    """True if we can pull text out of this MIME type here (not images)."""
    mime = (mime or "").lower()
    if mime == "application/pdf":
        return True
    if any(mime.startswith(p) for p in _TEXT_MIME_PREFIXES):
        return True
    return mime in _TEXT_MIME_EXACT


def extract_text(data: bytes, mime: str, *, filename: str = "") -> str:
    """Best-effort text extraction. Returns "" when nothing can be extracted.

    Never raises on malformed input — extraction is best-effort and must not
    break an upload.
    """
    mime = (mime or "").lower()
    if mime == "application/pdf":
        return _extract_pdf(data)
    if is_extractable(mime):
        return _decode_text(data)
    # Unknown type: try a text decode as a last resort (many code files arrive
    # as application/octet-stream); give up quietly if it isn't text.
    if not mime or mime == "application/octet-stream":
        return _decode_text(data)
    return ""


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return ""


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 — one bad page shouldn't fail all
                continue
        return "\n\n".join(p for p in parts if p).strip()
    except Exception:  # noqa: BLE001 — malformed PDF → no text, not a crash
        return ""
