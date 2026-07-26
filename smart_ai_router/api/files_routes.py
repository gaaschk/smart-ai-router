"""OpenAI-compatible Files API (`/v1/files`).

Bytes are stored on disk (see smart_ai_router.files); the database holds only
metadata. IDs and object shapes follow OpenAI's convention so OpenAI-compatible
clients — and claudish downstream of them — work unchanged.

Files are scoped to the calling identity (request.state.user, set by the auth
middleware). The admin identity may see and manage every file; a per-user key
sees only its own. In open (no-auth) mode every request shares the "" owner.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, Form

from smart_ai_router import extract
from smart_ai_router.api.schemas import (
    FileDeletedResponse,
    FileListResponse,
    FileResponse,
)
from smart_ai_router.models import FileRecord

files_router = APIRouter()


def _router_instance(request: Request):
    return request.app.state.capability_router


def _caller(request: Request) -> str:
    return getattr(request.state, "user", "") or ""


def _is_admin(request: Request) -> bool:
    return _caller(request) == "admin"


def _created_unix(iso: str) -> int:
    """Convert a stored ISO-8601 timestamp to a Unix second count (OpenAI shape)."""
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return 0


def _to_response(rec: FileRecord) -> FileResponse:
    return FileResponse(
        id=rec.id,
        bytes=rec.bytes,
        created_at=_created_unix(rec.created_at),
        filename=rec.filename,
        purpose=rec.purpose,
    )


def _owned_or_404(request: Request, file_id: str) -> FileRecord:
    """Fetch a file the caller is allowed to see, else 404.

    A 404 (not 403) for someone else's file avoids leaking that the id exists.
    """
    cr = _router_instance(request)
    rec = cr.get_file(file_id)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"No such file: {file_id!r}")
    if not _is_admin(request) and rec.user != _caller(request):
        raise HTTPException(status_code=404, detail=f"No such file: {file_id!r}")
    return rec


@files_router.post("/v1/files", response_model=FileResponse)
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    purpose: str = Form("assistants"),
):
    cr = _router_instance(request)
    mime = file.content_type or "application/octet-stream"
    filename = file.filename or ""

    # Refuse files we can neither read as text nor hand to a vision model, so
    # the user learns immediately instead of getting a confused answer after
    # sending (the model would otherwise receive a "couldn't read" placeholder).
    # Images are exempt: they're not "extractable" here but are inlined for
    # vision at request time. octet-stream is allowed through — extraction
    # falls back to a text decode for code files that arrive untyped.
    is_image = mime.lower().startswith("image/")
    is_octet = mime.lower() in ("", "application/octet-stream")
    if not is_image and not is_octet and not extract.is_extractable(mime):
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported file type for {filename or 'upload'!r} ({mime}). "
                "Supported: PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx), "
                "images, and plain-text/code files. Legacy formats like .doc/.ppt/"
                ".xls are not supported — save as the modern (OpenXML) format."
            ),
        )

    data = await file.read()
    try:
        rec = cr.upload_file(
            data,
            filename=filename,
            mime=mime,
            purpose=purpose,
            user=_caller(request),
        )
    except ValueError as exc:
        # Payload over the configured size ceiling.
        raise HTTPException(status_code=413, detail=str(exc))
    return _to_response(rec)


@files_router.get("/v1/files", response_model=FileListResponse)
def list_files(request: Request):
    cr = _router_instance(request)
    # Admin sees everything; a per-user key sees only its own files.
    scope = None if _is_admin(request) else _caller(request)
    records = cr.list_files(scope)
    return FileListResponse(data=[_to_response(r) for r in records])


@files_router.get("/v1/files/{file_id}", response_model=FileResponse)
def get_file(file_id: str, request: Request):
    return _to_response(_owned_or_404(request, file_id))


@files_router.get("/v1/files/{file_id}/content")
def get_file_content(file_id: str, request: Request):
    from fastapi.responses import Response

    rec = _owned_or_404(request, file_id)
    try:
        data = _router_instance(request).read_file_bytes(file_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail=f"No such file: {file_id!r}")
    headers = {}
    if rec.filename:
        headers["Content-Disposition"] = f'attachment; filename="{rec.filename}"'
    return Response(content=data, media_type=rec.mime, headers=headers)


@files_router.delete("/v1/files/{file_id}", response_model=FileDeletedResponse)
def delete_file(file_id: str, request: Request):
    # Ownership check first (404 for a file the caller can't see).
    _owned_or_404(request, file_id)
    deleted = _router_instance(request).delete_file(file_id)
    return FileDeletedResponse(id=file_id, deleted=deleted)
