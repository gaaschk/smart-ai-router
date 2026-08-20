"""FastAPI application factory."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from smart_ai_router import public_access as _public
from smart_ai_router import settings as _settings
from smart_ai_router.apikeys import hash_key
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.api.routes import api_router
from smart_ai_router.api.proxy import proxy_router
from smart_ai_router.api.files_routes import files_router
from smart_ai_router.api.conversations_routes import conversations_router

_UI_DIR = Path(__file__).parent / "ui"

_OPEN_PATHS = frozenset({"/", "/favicon.ico"})

# Paths an anonymous visitor may reach when public chat is enabled: enough to
# hold a conversation, and nothing more. Everything absent from this set still
# 401s without a key — notably /api/keys, /api/settings, /api/usage and the file
# endpoints, so opening the chat page never opens the dashboard or the upload
# path. Matched as exact paths or, with a trailing "/", as a prefix.
_ANON_PATHS = frozenset({
    "/v1/chat/completions",
    "/v1/models",
    "/api/whoami",
    "/api/conversations",
    "/api/conversations/",
})


def _anon_path_allowed(path: str) -> bool:
    if path in _ANON_PATHS:
        return True
    return any(p.endswith("/") and path.startswith(p) for p in _ANON_PATHS)


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": "Invalid or missing API key. Set Authorization: Bearer <key>"},
    )


def _get_env_api_keys() -> set[str]:
    """Load unrestricted "admin" keys from SMART_ROUTER_API_KEYS (comma-separated).

    These keep the pre-per-user behavior: any listed key authenticates with full
    access and no per-user scope or limits. They exist so existing deployments
    (and the operator's own key in .env) keep working; per-user keys live in the
    database and layer on top.
    """
    raw = os.environ.get("SMART_ROUTER_API_KEYS", "").strip()
    if not raw:
        return set()
    return {k.strip() for k in raw.split(",") if k.strip()}


def create_app(capability_router: CapabilityRouter | None = None) -> FastAPI:
    """Return a configured FastAPI application.

    Args:
        capability_router: pre-configured CapabilityRouter instance.
                           Defaults to one backed by a local SQLite store.
    """
    app = FastAPI(
        title="smart-ai-router",
        description="Vendor-agnostic LLM capability router",
        version="0.1.0",
    )
    app.state.capability_router = capability_router or CapabilityRouter()
    # Bind the settings layer to the live router so UI-managed settings resolve
    # DB → env → default. The router exposes get/set/all_setting passthroughs.
    _settings.bind_store(app.state.capability_router)

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        env_keys = _get_env_api_keys()
        cr = request.app.state.capability_router
        db_keys = cr.all_api_keys()

        # Default identity attached to every request; overwritten on a DB-key
        # match so downstream (proxy usage logging, later scope/limit phases)
        # can read who is calling.
        request.state.user = ""
        request.state.key_prefix = ""
        request.state.api_key = None
        request.state.is_anon = False

        # Auth is only enforced once at least one key exists (env or DB); with
        # none configured the router stays open, preserving first-run behavior.
        if not env_keys and not db_keys:
            return await call_next(request)

        path = request.url.path
        if path in _OPEN_PATHS or path.startswith("/static"):
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""

        if token:
            # Env keys authenticate as an unrestricted "admin" identity.
            if any(secrets.compare_digest(token, k) for k in env_keys):
                request.state.user = "admin"
                return await call_next(request)

            # Per-user DB keys: match by hash, honor the enabled flag.
            record = cr.get_api_key_by_hash(hash_key(token))
            if record is not None and record.enabled:
                request.state.user = record.user
                request.state.key_prefix = record.key_prefix
                request.state.api_key = record
                cr.touch_api_key(record.key_hash)
                return await call_next(request)

        # Anonymous (no-key) access to the chat page, when the operator has
        # turned it on. Deliberately the last thing tried: a real key always
        # wins, so enabling this can never downgrade an authenticated caller to
        # anonymous limits. See public_access.py for the policy and its limits.
        if _public.enabled() and _anon_path_allowed(path):
            if not _public.is_same_origin_browser_request(request):
                # A bare client (no Origin/Sec-Fetch-Site) is asking for the API,
                # not the chat page. The API is not open; say so as a 401 rather
                # than silently serving it.
                return _unauthorized()

            ip = _public.client_ip(request)
            ok, retry_after = _public.check_ip_rate(ip)
            if not ok:
                return JSONResponse(
                    status_code=429,
                    content={"error": "Rate limit reached for anonymous use. "
                                      "Try again shortly, or use an API key."},
                    headers={"Retry-After": str(retry_after)},
                )

            if not _public.inflight.try_acquire():
                return JSONResponse(
                    status_code=429,
                    content={"error": "Too many anonymous requests in flight. "
                                      "Try again in a moment."},
                    headers={"Retry-After": "5"},
                )
            try:
                cookie = request.cookies.get(_public.COOKIE_NAME, "")
                session_id = _public.read_session(cookie, cr)
                fresh = session_id is None
                if fresh:
                    cookie = _public.issue_session(cr)
                    session_id = _public.read_session(cookie, cr) or ""

                record = _public.policy_key(cr, session_id=session_id)
                request.state.user = record.user
                request.state.api_key = record
                request.state.is_anon = True

                response = await call_next(request)
                if fresh:
                    # Same-site strict: this cookie is only ever presented by our
                    # own page, and it is the visitor's private-conversation key.
                    response.set_cookie(
                        _public.COOKIE_NAME, cookie,
                        max_age=_public.COOKIE_MAX_AGE_S,
                        httponly=True, samesite="strict",
                        secure=request.url.scheme == "https",
                    )
                return response
            finally:
                _public.inflight.release()

        # Browser navigations (a GET that accepts HTML, outside the JSON API
        # surface) get bounced to the UI at "/", where the key prompt lives —
        # so hitting e.g. /login shows the app instead of a raw JSON error.
        # Programmatic clients on /api or /v1 still get a proper JSON 401.
        accepts_html = "text/html" in request.headers.get("accept", "")
        is_api_path = path.startswith("/api") or path.startswith("/v1")
        if request.method == "GET" and accepts_html and not is_api_path:
            return RedirectResponse(url="/", status_code=302)

        return _unauthorized()

    app.include_router(api_router, prefix="/api")
    app.include_router(conversations_router, prefix="/api")
    app.include_router(proxy_router)
    app.include_router(files_router)

    _static_dir = _UI_DIR / "static"
    if _UI_DIR.is_dir():
        if _static_dir.is_dir() and any(_static_dir.iterdir()):
            app.mount("/static", StaticFiles(directory=_static_dir), name="static")

        @app.get("/", include_in_schema=False)
        def ui_index():
            return FileResponse(_UI_DIR / "index.html")

    return app
