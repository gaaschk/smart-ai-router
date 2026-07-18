"""FastAPI application factory."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.api.routes import api_router

_UI_DIR = Path(__file__).parent / "ui"


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
    app.include_router(api_router, prefix="/api")

    _static_dir = _UI_DIR / "static"
    if _UI_DIR.is_dir():
        if _static_dir.is_dir() and any(_static_dir.iterdir()):
            app.mount("/static", StaticFiles(directory=_static_dir), name="static")

        @app.get("/", include_in_schema=False)
        def ui_index():
            return FileResponse(_UI_DIR / "index.html")

    return app
