"""GBrain integration routes for the smart-ai-router API.

Exposes GBrain memory, skills, and integrations to the web UI dashboard.
Mirrors the dashboard backend's GBrain API surface so the Python dashboard
can display the same features.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from smart_ai_router.gbrain_client import RUNNABLE_JOBS, get_client as get_gbrain

logger = logging.getLogger(__name__)

gbrain_router = APIRouter(prefix="/api/gbrain", tags=["gbrain"])


@gbrain_router.get("/health")
async def gbrain_health() -> dict[str, Any]:
    """Check GBrain status and return its health/brain-score."""
    try:
        gbrain = get_gbrain()
        health = gbrain.get_health()
        if not health:
            return {"status": "degraded", "available": False, "error": "GBrain returned no health data"}
        return {
            "status": "ok",
            "available": True,
            "brain_score": health.get("brain_score", 0),
            "health": health,
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
        stats = gbrain.get_stats()
        health = gbrain.get_health()
        return {
            "page_count": stats.get("page_count", 0),
            "chunk_count": stats.get("chunk_count", 0),
            "link_count": stats.get("link_count", 0),
            "tag_count": stats.get("tag_count", 0),
            "embedded_count": stats.get("embedded_count", 0),
            "pages_by_type": stats.get("pages_by_type", {}),
            "health": {
                "brain_score": health.get("brain_score", 0),
                "status": "ok" if health else "offline",
                "embed_coverage": health.get("embed_coverage", 0),
                "stale_pages": health.get("stale_pages", 0),
                "orphan_pages": health.get("orphan_pages", 0),
                "dead_links": health.get("dead_links", 0),
            },
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
async def gbrain_pages(type: str = "", tag: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """List pages, optionally filtered by type/tag -- the browsable page index."""
    try:
        gbrain = get_gbrain()
        pages = gbrain.list_pages(type=type, tag=tag, limit=limit)
        return pages if pages else []
    except Exception as e:
        logger.error(f"Failed to list GBrain pages: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to list pages: {e}")


@gbrain_router.get("/pages/{slug:path}")
async def gbrain_page(slug: str) -> dict[str, Any]:
    """
    Get a specific GBrain page by slug, plus its tags, outgoing links, and
    backlinks. `slug:path` so slugs containing '/' (e.g. "src/tests/readme")
    still route here instead of 404ing on the first segment.
    """
    try:
        gbrain = get_gbrain()
        page = gbrain.get_page(slug)
        if not page:
            raise HTTPException(status_code=404, detail=f"Page not found: {slug}")

        tags = gbrain.get_tags(slug)
        links = gbrain.get_links(slug)
        backlinks = gbrain.get_backlinks(slug)

        return {
            **page,
            "tags": tags,
            "links": links,
            "backlinks": backlinks,
        }
    except HTTPException:
        raise
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


@gbrain_router.get("/integrations/{integration_id}/status")
async def gbrain_integration_status(integration_id: str) -> dict[str, Any]:
    """Status, configured secrets, and heartbeat for one integration."""
    try:
        gbrain = get_gbrain()
        status = gbrain.get_integration_status(integration_id)
        return status if status else {}
    except Exception as e:
        logger.error(f"Failed to fetch GBrain integration status for {integration_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch integration status: {e}")


@gbrain_router.get("/jobs/catalog")
async def gbrain_jobs_catalog() -> dict[str, Any]:
    """
    Built-in Minions job types safe to submit on demand from this UI.
    Mirrors the Node dashboard's RUNNABLE_JOBS allow-list.
    """
    return {
        "jobs": [
            {"id": name, "name": name, "description": description}
            for name, description in RUNNABLE_JOBS.items()
        ]
    }


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


@gbrain_router.get("/jobs/{job_id}")
async def gbrain_job(job_id: int) -> dict[str, Any]:
    """Fetch a single job's status/result by id."""
    try:
        gbrain = get_gbrain()
        job = gbrain.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
        return job
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch GBrain job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch job: {e}")


@gbrain_router.post("/jobs")
async def gbrain_submit_job(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Submit a background job to GBrain's Minions queue.

    Only the fixed RUNNABLE_JOBS allow-list may be submitted here -- `shell`
    is deliberately excluded, matching the Node dashboard's policy: we don't
    want an arbitrary-command trigger reachable from a web UI.
    """
    if name not in RUNNABLE_JOBS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported job \"{name}\". Allowed: {', '.join(RUNNABLE_JOBS)}",
        )
    try:
        gbrain = get_gbrain()
        job = gbrain.submit_job(name, params or {})
        if not job:
            raise HTTPException(status_code=502, detail="GBrain did not return a job record")
        return job
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to submit GBrain job {name}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to submit job: {e}")
