"""Bad-response reports (`/api/reports`).

A router that picks the model for you takes the blame when the answer is wrong,
and the answer alone never says why: the pick, the profile it was picked on, and
the turns leading up to it are what make a bad reply diagnosable. So a report
carries the whole conversation plus the routing decision, and the user only has
to write the part a machine can't infer — what was wrong with it.

Anyone may file one, anonymous visitors included: they are the callers most
likely to meet a bad reply and the least likely to have another way to say so.
Reading and clearing them is admin-only — a report is someone else's chat.

The transcript arrives from the client rather than being read back out of the
store. That is deliberate: an unsaved thread, an anonymous visitor's chat, and an
API caller with no conversation at all would each otherwise be unreportable.
Sizes are capped here because that makes the body untrusted input.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, Request

from smart_ai_router.api.schemas import (
    ReportCreateRequest,
    ReportDeletedResponse,
    ReportListResponse,
    ReportResponse,
)
from smart_ai_router.api.routes import _is_admin
from smart_ai_router.models import Report

reports_router = APIRouter()

_MAX_DESCRIPTION = 4_000       # a paragraph or three; not a log dump
_MAX_MSG_CHARS = 32_000        # one turn's worth of JSON
_MAX_TRANSCRIPT_CHARS = 256_000
_MAX_META_CHARS = 4_000


def _router_instance(request: Request):
    return request.app.state.capability_router


def _admin_only(request: Request) -> None:
    """Reports are other people's conversations, so reading them is the operator's
    privilege alone. `_is_admin` is borrowed from the main route module rather than
    re-derived here: it already knows that a first-run install with no keys at all
    is the operator (or nobody could ever read a report on it), and that neither an
    anonymous visitor nor a self-minted key counts, whatever the key situation is."""
    if not _is_admin(request):
        raise HTTPException(
            status_code=403, detail="Reading reports requires an admin key"
        )


def _capped(msg: object) -> object:
    """One turn, shortened if it is enormous. A pasted logfile or a base64 image
    shouldn't cost the rest of the conversation its place in the report."""
    blob = json.dumps(msg)
    if len(blob) <= _MAX_MSG_CHARS:
        return msg
    role = msg.get("role", "?") if isinstance(msg, dict) else "?"
    return {"role": role, "content": blob[:_MAX_MSG_CHARS] + " …[truncated]"}


def _fit_transcript(transcript: list[object]) -> str:
    """JSON for the transcript, trimmed to the cap from the *front*.

    The reported reply is at the end, so when something has to go it is the oldest
    turns. Per-turn capping runs first, which is what guarantees this terminates
    with something rather than eating the conversation down to nothing.
    """
    msgs = [_capped(m) for m in transcript]
    while len(msgs) > 1 and len(json.dumps(msgs)) > _MAX_TRANSCRIPT_CHARS:
        msgs.pop(0)
    return json.dumps(msgs)


def _to_response(rec: Report) -> ReportResponse:
    def _load(blob: str, fallback):
        try:
            value = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            return fallback
        return value if isinstance(value, type(fallback)) else fallback

    return ReportResponse(
        id=rec.id,
        ts=rec.ts,
        user=rec.user,
        conversation_id=rec.conversation_id,
        description=rec.description,
        transcript=_load(rec.transcript_json, []),
        meta=_load(rec.meta_json, {}),
    )


@reports_router.post("/reports", response_model=ReportResponse)
def create_report(request: Request, body: ReportCreateRequest):
    description = (body.description or "").strip()
    if not description:
        raise HTTPException(status_code=422, detail="say what went wrong")
    if len(description) > _MAX_DESCRIPTION:
        raise HTTPException(
            status_code=422, detail=f"description too long (max {_MAX_DESCRIPTION} chars)"
        )

    meta_json = json.dumps(body.meta or {})
    if len(meta_json) > _MAX_META_CHARS:
        raise HTTPException(status_code=422, detail="meta too large")

    rec = Report(
        description=description,
        user=getattr(request.state, "user", "") or "",
        conversation_id=(body.conversation_id or "").strip(),
        transcript_json=_fit_transcript(body.transcript or []),
        meta_json=meta_json,
    )
    return _to_response(_router_instance(request).create_report(rec))


@reports_router.get("/reports", response_model=ReportListResponse)
def list_reports(
    request: Request,
    limit: int = Query(100, ge=1, le=500, description="Most recent N reports"),
):
    _admin_only(request)
    reports = _router_instance(request).list_reports(limit)
    return ReportListResponse(data=[_to_response(r) for r in reports])


@reports_router.delete("/reports/{report_id}", response_model=ReportDeletedResponse)
def delete_report(report_id: int, request: Request):
    _admin_only(request)
    return ReportDeletedResponse(
        id=report_id, deleted=_router_instance(request).delete_report(report_id)
    )
