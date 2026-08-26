"""Self-serve account creation (`/api/signup`).

Two endpoints, one of which exists only so the UI can avoid offering a button
that would fail:

* ``GET /api/signup`` — may I? Reports availability and, when not, a reason that
  is safe to show a stranger.
* ``POST /api/signup`` — mints a key and returns it once. No request body: there
  is nothing to collect.

**This endpoint has to be reachable by someone with no credential at all**, which
is the whole feature, and it must work whether or not anonymous chat is on — an
operator may well want accounts *instead* of open access. So the middleware lets
it through on its own terms; see ``api/app.py``.

What keeps it from being an open faucet is layered, and no single layer is the
answer: the same-origin check turns away bare scanners, the per-IP limiter bounds
how fast one address can mint keys, ``self_signup_max_accounts`` bounds how many
can exist, and — the one that actually protects the bill — the pooled daily budget
means a thousand accounts cost the same as one. See ``self_signup.py``.

An anonymous visitor who signs up brings their chats with them. That is the point
of doing it here rather than making them start over: the conversations that
convinced them to create an account are the ones they would most hate to lose.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from smart_ai_router import public_access as _public
from smart_ai_router import self_signup as _signup
from smart_ai_router.apikeys import display_prefix, generate_key, hash_key
from smart_ai_router.api.schemas import SignupResponse, SignupStatusResponse

signup_router = APIRouter()


def _require_enabled() -> None:
    """404 when self-serve signup is off — the endpoint genuinely doesn't exist."""
    if not _signup.enabled():
        raise HTTPException(status_code=404, detail="Self-serve accounts are not enabled")


@signup_router.get("/signup", response_model=SignupStatusResponse)
def signup_status(request: Request):
    """Whether a POST here would succeed right now.

    Answers rather than 404s when signup is off, so the UI has one call that
    always tells it what to render instead of having to treat a 404 as data.
    """
    if not _signup.enabled():
        return SignupStatusResponse(available=False, reason="")
    reason = _signup.lockout_reason(request.app.state.capability_router)
    return SignupStatusResponse(available=not reason, reason=reason)


@signup_router.post("/signup", response_model=SignupResponse, status_code=201)
def create_self_account(request: Request):
    """Mint a self-issued API key for the caller.

    The plaintext is in the response and nowhere else, ever — same rule as an
    admin-created key, but with more riding on it: there is no operator who can
    look this account up, because there is nothing on file to look it up by.
    """
    _require_enabled()
    cr = request.app.state.capability_router

    caller = getattr(request.state, "user", "") or ""
    if caller and not _public.is_anon_user(caller):
        # Already holding a key (or the admin identity). Minting a second one from
        # the first would be a way to launder an existing identity into a fresh
        # quota, and there is no reason a signed-in caller needs this route.
        raise HTTPException(
            status_code=409,
            detail="This request is already authenticated. Sign out first if you "
                   "want a separate self-serve account.",
        )

    reason = _signup.lockout_reason(cr)
    if reason:
        raise HTTPException(status_code=503, detail=reason)

    plaintext = generate_key()
    record = cr.create_api_key(_signup.new_account(
        cr, key_hash=hash_key(plaintext), key_prefix=display_prefix(plaintext),
    ))

    carried = 0
    if _public.is_anon_user(caller):
        try:
            carried = cr.reassign_conversations(from_user=caller, to_user=record.user)
        except Exception:  # noqa: BLE001
            # Best-effort: the key is already real, and failing the whole signup
            # over the history transfer would leave the caller with neither. They
            # still have the anonymous cookie, so the chats are not lost — they are
            # just still under the old identity.
            carried = 0

    return SignupResponse(
        key=plaintext,
        user=record.user,
        key_prefix=record.key_prefix,
        carried_over=carried,
    )
