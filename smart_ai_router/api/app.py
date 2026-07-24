"""FastAPI application factory."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from smart_ai_router.apikeys import hash_key
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.api.routes import api_router
from smart_ai_router.api.proxy import proxy_router

_UI_DIR = Path(__file__).parent / "ui"

_OPEN_PATHS = frozenset({"/", "/favicon.ico"})


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

        return JSONResponse(
            status_code=401,
            content={"error": "Invalid or missing API key. Set Authorization: Bearer <key>"},
        )

    app.include_router(api_router, prefix="/api")
    app.include_router(proxy_router)

    _static_dir = _UI_DIR / "static"
    if _UI_DIR.is_dir():
        if _static_dir.is_dir() and any(_static_dir.iterdir()):
            app.mount("/static", StaticFiles(directory=_static_dir), name="static")

        @app.get("/", include_in_schema=False)
        def ui_index():
            return FileResponse(_UI_DIR / "index.html")

    return app
