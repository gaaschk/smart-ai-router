"""Chat history API (`/api/conversations`).

Persists chat threads server-side so history survives browser reloads and
service restarts. Conversations are scoped to the calling identity
(request.state.user, set by the auth middleware): the admin identity sees and
manages every conversation; a per-user key sees only its own. In open (no-auth)
mode every request shares the "" owner — mirrors the Files API convention.

A turn's `content` may be a plain string or an OpenAI content-parts array (text
plus file/image refs). Structured content is JSON-encoded on the way into the
store (content_json=True) and decoded on the way out, so it round-trips to the
exact shape the client sent.

Threads carry free-form `tags` for grouping, and the list endpoint takes `?user=`
(admin only — a per-user key may only ask for itself) and `?tag=` filters.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, Request

from smart_ai_router.api.schemas import (
    ChatMessageCreateRequest,
    ChatMessageResponse,
    ConversationCreateRequest,
    ConversationDeletedResponse,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationResponse,
    ConversationUpdateRequest,
)
from smart_ai_router.models import ChatMessage, Conversation, generate_conversation_id

conversations_router = APIRouter()

# Grouping labels are meant to be glanceable in a narrow sidebar, so they are
# short and few. A comma is the UI's separator in the tag editor, so a tag can't
# contain one and survive the round trip.
_MAX_TAG_LEN = 24
_MAX_TAGS = 12


def _router_instance(request: Request):
    return request.app.state.capability_router


def _caller(request: Request) -> str:
    return getattr(request.state, "user", "") or ""


def _is_admin(request: Request) -> bool:
    return _caller(request) == "admin"


def _normalize_tags(tags: list[str]) -> list[str]:
    """Lowercase, trim, drop blanks, and dedupe while keeping the caller's order.

    Case-folding is what makes the sidebar's chip row usable: "Work" and "work"
    are one group, not two.
    """
    out: list[str] = []
    for raw in tags:
        if not isinstance(raw, str):
            raise HTTPException(status_code=422, detail="tags must be strings")
        tag = raw.strip().lower()
        if not tag:
            continue
        if "," in tag:
            raise HTTPException(status_code=422, detail="a tag may not contain a comma")
        if len(tag) > _MAX_TAG_LEN:
            raise HTTPException(
                status_code=422, detail=f"tag too long (max {_MAX_TAG_LEN} chars): {tag!r}"
            )
        if tag not in out:
            out.append(tag)
    if len(out) > _MAX_TAGS:
        raise HTTPException(status_code=422, detail=f"at most {_MAX_TAGS} tags per conversation")
    return out


def _to_response(conv: Conversation) -> ConversationResponse:
    return ConversationResponse(
        id=conv.id,
        title=conv.title,
        created_at=conv.created_at,
        updated_at=conv.updated_at,
        tags=list(conv.tags),
        user=conv.user,
    )


def _msg_to_response(msg: ChatMessage) -> ChatMessageResponse:
    content: object = msg.content
    if msg.content_json:
        try:
            content = json.loads(msg.content)
        except (json.JSONDecodeError, TypeError):
            content = msg.content  # fall back to raw text if somehow corrupt
    return ChatMessageResponse(role=msg.role, content=content, ts=msg.ts)


def _owned_or_404(request: Request, conversation_id: str) -> Conversation:
    """Fetch a conversation the caller may see, else 404.

    A 404 (not 403) for someone else's conversation avoids leaking that the id
    exists.
    """
    cr = _router_instance(request)
    conv = cr.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail=f"No such conversation: {conversation_id!r}")
    if not _is_admin(request) and conv.user != _caller(request):
        raise HTTPException(status_code=404, detail=f"No such conversation: {conversation_id!r}")
    return conv


@conversations_router.get("/conversations", response_model=ConversationListResponse)
def list_conversations(
    request: Request,
    user: str | None = Query(None, description="Filter by owner (admin only)"),
    tag: str | None = Query(None, description="Filter to one grouping label"),
):
    cr = _router_instance(request)
    is_admin = _is_admin(request)
    caller = _caller(request)

    # Admin sees everything and may narrow to one owner; a per-user key sees only
    # its own conversations, so asking for someone else's is a 403 (the scope, not
    # the id, is what's being refused — nothing leaks about what exists).
    if is_admin:
        scope = user
    else:
        if user is not None and user != caller:
            raise HTTPException(
                status_code=403, detail="Filtering by user requires an admin key"
            )
        scope = caller

    convs = cr.list_conversations(scope, tag=(tag.strip().lower() if tag else None))
    # Owner options for the admin's filter come from who actually has history.
    users = cr.list_conversation_users() if is_admin else []
    return ConversationListResponse(
        data=[_to_response(c) for c in convs], users=users
    )


@conversations_router.post("/conversations", response_model=ConversationResponse)
def create_conversation(request: Request, body: ConversationCreateRequest):
    cr = _router_instance(request)
    conv = Conversation(
        id=generate_conversation_id(),
        user=_caller(request),
        title=(body.title or "New chat").strip() or "New chat",
        tags=_normalize_tags(body.tags or []),
    )
    return _to_response(cr.create_conversation(conv))


@conversations_router.get(
    "/conversations/{conversation_id}", response_model=ConversationDetailResponse
)
def get_conversation(conversation_id: str, request: Request):
    conv = _owned_or_404(request, conversation_id)
    cr = _router_instance(request)
    msgs = cr.list_chat_messages(conversation_id)
    return ConversationDetailResponse(
        **_to_response(conv).model_dump(),
        messages=[_msg_to_response(m) for m in msgs],
    )


@conversations_router.patch(
    "/conversations/{conversation_id}", response_model=ConversationResponse
)
def update_conversation(
    conversation_id: str, request: Request, body: ConversationUpdateRequest
):
    """Rename a thread, retag it, or both — whichever fields the client sent."""
    _owned_or_404(request, conversation_id)
    cr = _router_instance(request)

    if body.title is None and body.tags is None:
        raise HTTPException(status_code=422, detail="send a title and/or tags")

    title = None
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="title must not be empty")

    tags = None if body.tags is None else _normalize_tags(body.tags)

    cr.update_conversation(conversation_id, title=title, tags=tags)
    # Return the fresh record (a rename also bumps updated_at).
    conv = cr.get_conversation(conversation_id)
    return _to_response(conv)


@conversations_router.delete(
    "/conversations/{conversation_id}", response_model=ConversationDeletedResponse
)
def delete_conversation(conversation_id: str, request: Request):
    _owned_or_404(request, conversation_id)
    deleted = _router_instance(request).delete_conversation(conversation_id)
    return ConversationDeletedResponse(id=conversation_id, deleted=deleted)


@conversations_router.post(
    "/conversations/{conversation_id}/messages", response_model=ChatMessageResponse
)
def add_message(
    conversation_id: str, request: Request, body: ChatMessageCreateRequest
):
    _owned_or_404(request, conversation_id)
    cr = _router_instance(request)
    if body.role not in ("user", "assistant", "system"):
        raise HTTPException(status_code=422, detail="role must be user|assistant|system")

    # A string stores as text; anything else (content-parts array) is JSON-encoded.
    if isinstance(body.content, str):
        content, content_json = body.content, False
    else:
        content, content_json = json.dumps(body.content), True

    msg = cr.add_chat_message(ChatMessage(
        conversation_id=conversation_id,
        role=body.role,
        content=content,
        content_json=content_json,
    ))
    return _msg_to_response(msg)
