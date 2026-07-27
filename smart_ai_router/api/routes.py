"""Route handlers — mounted onto the FastAPI app by create_app()."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request

from smart_ai_router.apikeys import display_prefix, generate_key, hash_key
from smart_ai_router.models import ApiKey, ProviderConfig
from smart_ai_router.scope import parse_scope
from smart_ai_router.api.schemas import (
    ApiKeyCreatedResponse,
    ApiKeyCreateRequest,
    ApiKeyEnabledRequest,
    ApiKeyResponse,
    ApplyUpdateResponse,
    CapabilitiesResponse,
    CostRequest,
    CostResponse,
    ModelSpecResponse,
    ProviderRequest,
    ProviderResponse,
    RouteRequest,
    RouteResponse,
    SettingResponse,
    SettingsResponse,
    SettingsUpdateRequest,
    SyncRequest,
    SyncResponse,
    UpdateStatusResponse,
    UsageSummaryResponse,
    WhoAmIResponse,
)

api_router = APIRouter()


def _router_instance(request: Request):
    return request.app.state.capability_router


def _require_admin(request: Request) -> None:
    """Guard key-management endpoints: only the admin identity may manage keys.

    A per-user DB key must not be able to enumerate or revoke other users' keys.
    The "admin" identity is set by the middleware for an env (SMART_ROUTER_API_KEYS)
    key. When no keys are configured at all the router is open (first-run), so we
    allow management too — otherwise you could never mint the first key.
    """
    user = getattr(request.state, "user", "") or ""
    if user == "admin":
        return
    no_keys_configured = (
        not os.environ.get("SMART_ROUTER_API_KEYS", "").strip()
        and not request.app.state.capability_router.all_api_keys()
    )
    if no_keys_configured:
        return
    raise HTTPException(
        status_code=403,
        detail="Key management requires an admin key (SMART_ROUTER_API_KEYS).",
    )


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


@api_router.get("/capabilities", response_model=CapabilitiesResponse)
def capabilities(request: Request):
    """What this deployment can do right now, derived live from the reachable
    model matrix. Narrowed to the caller's scope when a per-user key is used, so
    a scoped client sees only the features its allowed models provide.
    """
    cr = _router_instance(request)
    record = getattr(request.state, "api_key", None)
    scope = None
    if record is not None:
        parsed = parse_scope(record.scope_models, record.max_tier)
        scope = parsed if parsed.is_restricted else None
    caps = cr.capabilities(scope=scope)
    return CapabilitiesResponse(
        vision=caps.vision,
        tools=caps.tools,
        max_context_k=caps.max_context_k,
        model_count=caps.model_count,
        providers=list(caps.providers),
    )


@api_router.get("/whoami", response_model=WhoAmIResponse)
def whoami(request: Request):
    """Who the current key authenticates as, for the UI to display.

    Reads the identity the auth middleware attached to the request:
      - "admin"  → the env/root key (SMART_ROUTER_API_KEYS); may manage keys.
      - a per-user label → a generated DB key; shows the label + key prefix.
      - ""       → open (no-auth) mode, when no keys are configured at all.
    Never exposes the secret — only a label and the non-secret prefix.
    """
    user = getattr(request.state, "user", "") or ""
    key_prefix = getattr(request.state, "key_prefix", "") or ""

    if user == "admin":
        return WhoAmIResponse(
            authenticated=True, kind="admin", user="admin", is_admin=True
        )
    if user:
        return WhoAmIResponse(
            authenticated=True, kind="user", user=user, key_prefix=key_prefix
        )
    # No identity: either open mode (no keys configured) or an unauthenticated
    # request that slipped through an open path. Report first-run admin ability
    # so the UI knows whether the Keys page is usable.
    no_keys_configured = (
        not os.environ.get("SMART_ROUTER_API_KEYS", "").strip()
        and not _router_instance(request).all_api_keys()
    )
    return WhoAmIResponse(
        authenticated=False, kind="open", is_admin=no_keys_configured
    )


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
        unchanged=result.unchanged,
        removed=result.removed,
        total=result.total,
        errors=result.errors,
    )


@api_router.post("/cost", response_model=CostResponse)
def cost(body: CostRequest, request: Request):
    cr = _router_instance(request)
    cost_usd = cr.cost_for(body.model, body.prompt_tokens, body.completion_tokens)
    return CostResponse(model=body.model, cost_usd=cost_usd)


@api_router.get("/usage", response_model=UsageSummaryResponse)
def usage(request: Request, days: int = 30):
    """Aggregated usage for the dashboard.

    Scoped like conversations: the admin identity sees all users (with a
    by_user breakdown); a per-user key sees only its own rows. Open (no-auth)
    mode has caller "", which matches the ""-owner rows written in that mode.
    `days` bounds the window (clamped 1–365).
    """
    cr = _router_instance(request)
    caller = getattr(request.state, "user", "") or ""
    is_admin = caller == "admin"
    days = max(1, min(days, 365))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    scope_user = None if is_admin else caller
    return cr.usage_summary(user=scope_user, since_ts=since)


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


# ── Settings (UI-managed runtime config) ────────────────────────────────────────

@api_router.get("/settings", response_model=SettingsResponse)
def get_settings(request: Request):
    """Effective runtime settings + metadata for the Settings page.

    Admin-gated (same rule as key management): these change application behavior
    for everyone, and a per-user key must not read/alter deployment policy.
    """
    _require_admin(request)
    from smart_ai_router import settings as _settings
    return SettingsResponse(
        settings=[SettingResponse(**s) for s in _settings.effective()]
    )


@api_router.put("/settings", response_model=SettingsResponse)
def update_settings(body: SettingsUpdateRequest, request: Request):
    """Persist a batch of setting changes; applied live (no restart).

    Values are validated/coerced per each setting's type; an unknown key or a
    value that doesn't fit its type is a 422.
    """
    _require_admin(request)
    from smart_ai_router import settings as _settings
    try:
        _settings.apply(body.updates)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return SettingsResponse(
        settings=[SettingResponse(**s) for s in _settings.effective()]
    )


# ── API keys (per-user auth) ────────────────────────────────────────────────────

@api_router.get("/keys", response_model=list[ApiKeyResponse])
def list_api_keys(request: Request):
    _require_admin(request)
    cr = _router_instance(request)
    return [_to_api_key_response(k) for k in cr.all_api_keys()]


@api_router.post("/keys", response_model=ApiKeyCreatedResponse, status_code=201)
def create_api_key(body: ApiKeyCreateRequest, request: Request):
    _require_admin(request)
    if not body.user.strip():
        raise HTTPException(status_code=422, detail="user must not be empty")
    cr = _router_instance(request)
    plaintext = generate_key()
    record = ApiKey(
        key_hash=hash_key(plaintext),
        user=body.user.strip(),
        key_prefix=display_prefix(plaintext),
        enabled=True,
        scope_models=body.scope_models,
        max_tier=body.max_tier,
        rl_window_s=body.rl_window_s,
        rl_max_req=body.rl_max_req,
        rl_max_tokens=body.rl_max_tokens,
    )
    created = cr.create_api_key(record)
    # Only place the plaintext key is ever exposed — the caller must save it now.
    return ApiKeyCreatedResponse(key=plaintext, **_to_api_key_response(created).model_dump())


@api_router.put("/keys/{key_prefix}/enabled", response_model=ApiKeyResponse)
def set_api_key_enabled(key_prefix: str, body: ApiKeyEnabledRequest, request: Request):
    _require_admin(request)
    cr = _router_instance(request)
    if not cr.set_api_key_enabled(key_prefix, body.enabled):
        raise HTTPException(status_code=404, detail=f"Key {key_prefix!r} not found")
    match = next((k for k in cr.all_api_keys() if k.key_prefix == key_prefix), None)
    return _to_api_key_response(match)


@api_router.delete("/keys/{key_prefix}", status_code=204)
def delete_api_key(key_prefix: str, request: Request):
    _require_admin(request)
    cr = _router_instance(request)
    if not cr.delete_api_key(key_prefix):
        raise HTTPException(status_code=404, detail=f"Key {key_prefix!r} not found")


@api_router.post(
    "/keys/{key_prefix}/recreate", response_model=ApiKeyCreatedResponse
)
def recreate_api_key(key_prefix: str, request: Request):
    """Rotate a key's secret in place, keeping its user/scope/limits/enabled.

    Mints a fresh plaintext key, replaces the stored hash + prefix, and returns
    the new plaintext ONCE (same as creation). The old secret immediately stops
    working — the recovery path for a lost or leaked key, without ever storing
    plaintext.
    """
    _require_admin(request)
    cr = _router_instance(request)
    plaintext = generate_key()
    updated = cr.recreate_api_key(
        key_prefix, new_hash=hash_key(plaintext), new_prefix=display_prefix(plaintext)
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Key {key_prefix!r} not found")
    return ApiKeyCreatedResponse(key=plaintext, **_to_api_key_response(updated).model_dump())


# ── Updates ───────────────────────────────────────────────────────────────────

@api_router.get("/updates", response_model=UpdateStatusResponse)
def get_update_status(fetch: bool = True):
    from smart_ai_router import updates
    return updates.source_update_status(fetch=fetch)


@api_router.post("/updates/apply", response_model=ApplyUpdateResponse)
def apply_update():
    from smart_ai_router import updates
    return updates.apply_source_update()


def _to_api_key_response(k) -> ApiKeyResponse:
    return ApiKeyResponse(
        user=k.user,
        key_prefix=k.key_prefix,
        enabled=k.enabled,
        scope_models=k.scope_models,
        max_tier=k.max_tier,
        rl_window_s=k.rl_window_s,
        rl_max_req=k.rl_max_req,
        rl_max_tokens=k.rl_max_tokens,
        created_at=k.created_at,
        last_used_at=k.last_used_at,
    )


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
