"""Anonymous identity recovery (`/api/anon/...`).

An anonymous visitor's whole history hangs off one cookie, and cookies are the
first thing a browser throws away — Safari's ITP evicts them, storage pressure
evicts them, and "clear recent history" takes them deliberately. When that cookie
goes, the conversations behind it are unreachable by anyone, including the
operator. These two endpoints are the second and third chances at it:

* ``GET /api/anon/session`` hands the page the signed token behind its own
  cookie, so it can mirror it into localStorage (cleared by different browser
  actions than cookies are, so losing both takes intent) and offer it as a
  recovery link.
* ``POST /api/anon/claim`` accepts such a token back and re-establishes the
  cookie from it. One endpoint serves both the mirror restore and the link.

Why not just drop ``httponly`` and let the page read the cookie? Because then
every future XSS gets the token for free and passively. Going through an explicit
same-origin fetch is not a cure — script running on the page can call this too —
but it keeps the default path ``httponly``, makes the read a single auditable
surface, and costs nothing.

**The token is a bearer credential for that history.** Anyone holding it is that
visitor. That is inherent to "no account, no personal information": there is no
second factor to ask for, because we deliberately know nothing about them. So the
link is opt-in, the UI says plainly that sharing it shares the chats, and it rides
in a URL *fragment* — never sent to the server, so it stays out of access logs and
``Referer`` headers.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from smart_ai_router import public_access as _public
from smart_ai_router.api.schemas import AnonClaimRequest, AnonClaimResponse, AnonSessionResponse

anon_router = APIRouter()


def _require_public_chat() -> None:
    """404 when anonymous access is off — the endpoint genuinely doesn't exist then."""
    if not _public.enabled():
        raise HTTPException(status_code=404, detail="Anonymous access is not enabled")


@anon_router.get("/anon/session", response_model=AnonSessionResponse)
def get_anon_session(request: Request):
    """The signed token behind this request's own anonymous cookie.

    Only ever returns the caller's *own* session — it reads what the auth
    middleware already established for this request, so there is no id to
    supply and nothing to enumerate.
    """
    _require_public_chat()
    if not getattr(request.state, "is_anon", False):
        # An authenticated caller has a key, which is a better identity than this;
        # nothing to hand back.
        raise HTTPException(status_code=404, detail="Not an anonymous session")
    token = getattr(request.state, "anon_cookie", "") or ""
    session_id = (getattr(request.state, "user", "") or "")[len(_public.ANON_PREFIX):]
    if not token or not session_id:
        raise HTTPException(status_code=404, detail="Not an anonymous session")
    return AnonSessionResponse(token=token, session_id=session_id)


@anon_router.post("/anon/claim", response_model=AnonClaimResponse)
def claim_anon_session(body: AnonClaimRequest, request: Request, response: Response):
    """Adopt the anonymous identity named by a signed token.

    The HMAC is the entire check, and it is enough: a forged or truncated token
    verifies as nothing, and a valid one could only have come from this server.
    Rejected tokens are a 400 rather than a silent no-op so a stale recovery link
    can say so instead of appearing to work and showing an empty chat list.
    """
    _require_public_chat()
    cr = request.app.state.capability_router
    token = (body.token or "").strip()
    session = _public.read_session_meta(token, cr)
    if session is None:
        raise HTTPException(status_code=400, detail="That recovery token is not valid")

    # Re-stamp on the way in: a token that has been sitting in a bookmark for a
    # year is still valid, and the cookie it establishes should get a full lifetime
    # from now rather than inheriting an expiry that has already passed.
    fresh = _public.resign_session(session.session_id, cr)
    response.set_cookie(value=fresh, **_public.cookie_kwargs(request))
    # Tell the middleware to keep its hands off: it computed a cookie for whatever
    # identity this request arrived as, and setting that too would leave two
    # Set-Cookie headers racing to decide who the visitor is.
    request.state.anon_cookie_set = True
    return AnonClaimResponse(
        user=f"{_public.ANON_PREFIX}{session.session_id}",
        session_id=session.session_id,
        token=fresh,
    )
