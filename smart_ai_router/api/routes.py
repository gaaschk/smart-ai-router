"""Route handlers — mounted onto the FastAPI app by create_app()."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from smart_ai_router.models import ProviderConfig
from smart_ai_router.api.schemas import (
    ApplyUpdateResponse,
    CostRequest,
    CostResponse,
    ModelSpecResponse,
    ProviderRequest,
    ProviderResponse,
    RouteRequest,
    RouteResponse,
    SyncRequest,
    SyncResponse,
    UpdateStatusResponse,
)

api_router = APIRouter()


def _router_instance(request: Request):
    return request.app.state.capability_router


@api_router.post("/route", response_model=RouteResponse)
def route(body: RouteRequest, request: Request):
    cr = _router_instance(request)
    try:
        model = cr.route(
            body.domain,
            body.complexity,
            needs_tools=body.needs_tools,
            needs_vision=body.needs_vision,
            est_tokens=body.est_tokens,
            exclude=set(body.exclude) if body.exclude else None,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return RouteResponse(model=model)


@api_router.get("/models", response_model=list[ModelSpecResponse])
def list_models(request: Request):
    cr = _router_instance(request)
    return [_to_response(s) for s in cr.all_models()]


@api_router.get("/models/{model_id:path}", response_model=ModelSpecResponse)
def get_model(model_id: str, request: Request):
    cr = _router_instance(request)
    spec = cr.get_model(model_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Model {model_id!r} not found")
    return _to_response(spec)


@api_router.post("/sync", response_model=SyncResponse)
def sync(body: SyncRequest, request: Request):
    cr = _router_instance(request)
    result = cr.sync(
        openrouter_key=body.openrouter_key,
        ollama_base_url=body.ollama_base_url,
        timeout=body.timeout,
    )
    return SyncResponse(
        added=result.added,
        updated=result.updated,
        total=result.total,
        errors=result.errors,
    )


@api_router.post("/cost", response_model=CostResponse)
def cost(body: CostRequest, request: Request):
    cr = _router_instance(request)
    cost_usd = cr.cost_for(body.model, body.prompt_tokens, body.completion_tokens)
    return CostResponse(model=body.model, cost_usd=cost_usd)


# ── Providers ─────────────────────────────────────────────────────────────────

@api_router.get("/providers", response_model=list[ProviderResponse])
def list_providers(request: Request):
    cr = _router_instance(request)
    return [_to_provider_response(p) for p in cr.all_providers()]


@api_router.get("/providers/{name}", response_model=ProviderResponse)
def get_provider(name: str, request: Request):
    cr = _router_instance(request)
    cfg = cr.get_provider(name)
    if cfg is None:
        raise HTTPException(status_code=404, detail=f"Provider {name!r} not found")
    return _to_provider_response(cfg)


@api_router.put("/providers/{name}", response_model=ProviderResponse)
def upsert_provider(name: str, body: ProviderRequest, request: Request):
    if name != body.name:
        raise HTTPException(
            status_code=422,
            detail="URL name and body name must match",
        )
    cr = _router_instance(request)
    cfg = ProviderConfig(
        name=body.name,
        kind=body.kind,
        enabled=body.enabled,
        api_key=body.api_key,
        base_url=body.base_url,
        timeout=body.timeout,
    )
    cr.upsert_provider(cfg)
    return _to_provider_response(cfg)


@api_router.delete("/providers/{name}", status_code=204)
def delete_provider(name: str, request: Request):
    cr = _router_instance(request)
    found = cr.delete_provider(name)
    if not found:
        raise HTTPException(status_code=404, detail=f"Provider {name!r} not found")


# ── Updates ───────────────────────────────────────────────────────────────────

@api_router.get("/updates", response_model=UpdateStatusResponse)
def get_update_status(fetch: bool = True):
    from smart_ai_router import updates
    return updates.source_update_status(fetch=fetch)


@api_router.post("/updates/apply", response_model=ApplyUpdateResponse)
def apply_update():
    from smart_ai_router import updates
    return updates.apply_source_update()


def _to_provider_response(cfg) -> ProviderResponse:
    return ProviderResponse(
        name=cfg.name,
        kind=cfg.kind,
        enabled=cfg.enabled,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        timeout=cfg.timeout,
    )


def _to_response(spec) -> ModelSpecResponse:
    return ModelSpecResponse(
        value=spec.value,
        provider=spec.provider,
        cost=spec.cost,
        ctx_k=spec.ctx_k,
        tools=spec.tools,
        vision=spec.vision,
        reliability=spec.reliability,
        cost_input=spec.cost_input,
        cost_output=spec.cost_output,
        competence=spec.competence,
    )
