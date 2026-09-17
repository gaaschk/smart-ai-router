"""GBrain integration routes for the smart-ai-router API.

Exposes GBrain memory, skills, and integrations to the web UI dashboard.
Mirrors the dashboard backend's GBrain API surface so the Python dashboard
can display the same features.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from smart_ai_router.gbrain_client import get_client as get_gbrain

logger = logging.getLogger(__name__)

gbrain_router = APIRouter(prefix="/api/gbrain", tags=["gbrain"])


@gbrain_router.get("/health")
async def gbrain_health() -> dict[str, Any]:
    """Check GBrain status and return stats."""
    try:
        gbrain = get_gbrain()
        
        # Try to get stats as a health check
        stats_result = gbrain.hybrid_query("health", limit=1)
        
        return {
            "status": "ok",
            "available": True,
            "brain_score": 75,  # Placeholder; could call a dedicated GBrain endpoint
        }
    except Exception as e:
        logger.warning(f"GBrain health check failed: {e}")
        return {
            "status": "degraded",
            "available": False,
            "error": str(e),
        }


@gbrain_router.get("/stats")
async def gbrain_stats() -> dict[str, Any]:
    """Get GBrain statistics (pages, chunks, links, brain score)."""
    try:
        gbrain = get_gbrain()
        
        # Call GBrain's get_stats method if available
        # For now, return placeholder stats since the Python client doesn't
        # have a dedicated stats endpoint yet
        return {
            "page_count": 0,
            "chunk_count": 0,
            "link_count": 0,
            "health": {
                "brain_score": 0,
                "status": "offline",
            }
        }
    except Exception as e:
        logger.error(f"Failed to fetch GBrain stats: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch GBrain stats: {e}")


@gbrain_router.get("/search")
async def gbrain_search(q: str = "", mode: str = "hybrid", limit: int = 10) -> list[dict[str, Any]]:
    """
    Search GBrain knowledge base.
    
    Query parameters:
    - q: search query string
    - mode: 'keyword' or 'hybrid' (hybrid includes vector search)
    - limit: max results (default 10)
    """
    if not q or not q.strip():
        return []
    
    try:
        gbrain = get_gbrain()
        
        if mode == "keyword":
            results = gbrain.search(q.strip(), limit)
        else:
            # hybrid mode (default)
            results = gbrain.hybrid_query(q.strip(), limit, expand=True)
        
        # Normalize results to match dashboard API format
        normalized = []
        for r in (results or []):
            normalized.append({
                "title": r.get("title", "Untitled"),
                "slug": r.get("slug", ""),
                "chunk_text": r.get("chunk_text", ""),
                "score": r.get("score", 0.0),
            })
        
        return normalized
    except Exception as e:
        logger.error(f"GBrain search failed: {e}")
        raise HTTPException(status_code=500, detail=f"Search failed: {e}")


@gbrain_router.get("/pages")
async def gbrain_pages(limit: int = 50) -> list[dict[str, Any]]:
    """List all GBrain pages (top N by update date)."""
    try:
        gbrain = get_gbrain()
        
        # The Python client doesn't yet have a listPages method,
        # so this returns an empty list for now. This endpoint is here
        # for future expansion and to match the dashboard API.
        return []
    except Exception as e:
        logger.error(f"Failed to list GBrain pages: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list pages: {e}")


@gbrain_router.get("/pages/{slug}")
async def gbrain_page(slug: str) -> dict[str, Any]:
    """Get a specific GBrain page by slug."""
    try:
        gbrain = get_gbrain()
        
        # The Python client doesn't yet have a getPage method,
        # so this returns a placeholder. This endpoint is here for
        # future expansion and to match the dashboard API.
        return {
            "title": slug,
            "slug": slug,
            "content": "",
            "tags": [],
            "links": [],
            "backlinks": [],
        }
    except Exception as e:
        logger.error(f"Failed to fetch GBrain page {slug}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch page: {e}")


@gbrain_router.post("/remember")
async def gbrain_remember(title: str, content: str, entity: str = "api") -> dict[str, Any]:
    """Save a fact/learning to GBrain."""
    if not title or not content:
        raise HTTPException(status_code=400, detail="title and content are required")
    
    try:
        gbrain = get_gbrain()
        result = gbrain.remember(title, content, entity)
        return {"ok": True, "result": result}
    except Exception as e:
        logger.error(f"Failed to save to GBrain: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save: {e}")


@gbrain_router.get("/integrations")
async def gbrain_integrations() -> dict[str, Any]:
    """List available GBrain integrations (infra, senses, reflexes)."""
    try:
        gbrain = get_gbrain()
        integrations = gbrain.list_integrations()
        return integrations if integrations else {"infra": [], "senses": [], "reflexes": []}
    except Exception as e:
        logger.error(f"Failed to list GBrain integrations: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list integrations: {e}")


@gbrain_router.get("/jobs")
async def gbrain_jobs(
    limit: int = 10, status: str = "", queue: str = "", name: str = ""
) -> list[dict[str, Any]]:
    """List background jobs (Minions queue) with optional filters."""
    try:
        gbrain = get_gbrain()
        jobs = gbrain.list_jobs(status=status, queue=queue, name=name, limit=limit)
        return jobs if jobs else []
    except Exception as e:
        logger.error(f"Failed to list GBrain jobs: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list jobs: {e}")


@gbrain_router.post("/jobs")
async def gbrain_submit_job(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Submit a background job to GBrain.
    
    The Python client doesn't yet support job submission,
    so this is a placeholder. This endpoint is here for
    future expansion and to match the dashboard API.
    """
    raise HTTPException(status_code=501, detail="Job submission not yet supported in Python client")
