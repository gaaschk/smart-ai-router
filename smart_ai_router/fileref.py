"""Resolve uploaded-file references in chat messages into inline content.

Clients attach an uploaded file (from /v1/files) by referencing its id in a
message content part. Backends (ollama/openrouter) don't know our file ids, so
the proxy must expand each reference *before* forwarding:

  - image files  → an OpenAI `image_url` part with an inline data: URI
                   (base64), which every OpenAI-dialect vision model accepts —
                   the same shape claudish already emits for inline images.
  - other files  → a `text` part carrying the server-extracted text, so the
                   document works with *any* model, not just vision ones.

Reference forms accepted (both OpenAI-shaped):
  {"type": "file", "file": {"file_id": "file-..."}}          # documents/images
  {"type": "image_url", "image_url": {"url": "file-..."}}    # image by id

Inline parts (a real data: URI or http URL in image_url, plain text) are passed
through untouched — claudish already inlines its images this way.

References are owner-scoped: a caller can only expand its own files (admin may
expand any). An unknown or forbidden id raises FileRefError so the proxy can
fail the request clearly instead of silently dropping the attachment.
"""
from __future__ import annotations

import base64

from smart_ai_router import ocr as _ocr

_FILE_ID_PREFIX = "file-"
_IMAGE_MIME_PREFIX = "image/"


class FileRefError(Exception):
    """A message referenced a file that can't be resolved (missing/forbidden)."""


def _looks_like_file_id(value) -> bool:
    return isinstance(value, str) and value.startswith(_FILE_ID_PREFIX)


def _referenced_file_id(part: dict) -> str | None:
    """Return the file id a content part references, or None if it inlines content."""
    ptype = part.get("type")
    if ptype == "file":
        ref = part.get("file")
        if isinstance(ref, dict) and _looks_like_file_id(ref.get("file_id")):
            return ref["file_id"]
        return None
    if ptype == "image_url":
        ref = part.get("image_url")
        url = ref.get("url") if isinstance(ref, dict) else None
        # A bare file id is a reference; a data:/http URL is already inline.
        return url if _looks_like_file_id(url) else None
    return None


def _image_part(mime: str, data: bytes) -> dict:
    b64 = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _expand(cr, rec) -> list[dict]:
    """Turn a resolved FileRecord into one or more inline content parts.

    - image → a single image_url part (base64 data URI)
    - scanned PDF (no text layer) → a text preamble + one image_url part per
      rendered page, so a vision model can read it (OCR-by-vision)
    - anything else → a single text part with the extracted text
    """
    if rec.mime.startswith(_IMAGE_MIME_PREFIX):
        return [_image_part(rec.mime, cr.read_file_bytes(rec.id))]

    label = rec.filename or rec.id

    # Scanned/image-only PDF: no text was extracted at upload. Rasterize its
    # pages to images so a vision model can read it. Falls back to the text
    # path (a clear "couldn't read" note) if pymupdf is unavailable or the
    # render yields nothing.
    if _ocr.is_scanned_pdf(rec.mime, rec.extracted_text):
        pages = _ocr.rasterize_pdf(cr.read_file_bytes(rec.id))
        if pages:
            total = _ocr.page_count(cr.read_file_bytes(rec.id))
            shown = len(pages)
            preamble = f"[File: {label} — scanned PDF, rendered {shown} page(s) as images"
            preamble += f" of {total}]" if total and total > shown else "]"
            parts: list[dict] = [{"type": "text", "text": preamble}]
            parts.extend(_image_part("image/png", p) for p in pages)
            return parts

    text = rec.extracted_text or f"[Attached file {label} could not be read as text.]"
    return [{"type": "text", "text": f"[File: {label}]\n{text}"}]


def _fetch_owned(cr, file_id: str, *, user: str, is_admin: bool):
    """Fetch a file the caller may use, else raise FileRefError.

    Mirrors the Files API scoping: not-found and not-owned are the same error to
    the caller, so a wrong id can't probe another user's files.
    """
    rec = cr.get_file(file_id)
    if rec is None or (not is_admin and rec.user != user):
        raise FileRefError(f"No such file: {file_id!r}")
    return rec


def resolve_file_refs(
    messages: list[dict],
    cr,
    *,
    user: str = "",
    is_admin: bool = False,
) -> list[dict]:
    """Return messages with every file reference expanded to inline content.

    Messages without list content, and parts that already inline their content,
    are returned unchanged. Raises FileRefError on an unresolvable reference.
    """
    resolved: list[dict] = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            resolved.append(msg)
            continue
        new_parts = []
        for part in content:
            if not isinstance(part, dict):
                new_parts.append(part)
                continue
            file_id = _referenced_file_id(part)
            if file_id is None:
                new_parts.append(part)
                continue
            rec = _fetch_owned(cr, file_id, user=user, is_admin=is_admin)
            new_parts.extend(_expand(cr, rec))
        resolved.append({**msg, "content": new_parts})
    return resolved


def contains_image(messages: list[dict]) -> bool:
    """True if any message carries an image content part (post-resolution)."""
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                return True
    return False
