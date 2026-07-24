"""Pydantic request/response models for the REST API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class RouteRequest(BaseModel):
    domain: str = Field(..., description="coding | docs | reasoning | general")
    complexity: str = Field(..., description="trivial | moderate | hard")
    needs_tools: bool = False
    needs_vision: bool = False
    est_tokens: int = 0
    exclude: list[str] = Field(default_factory=list)


class RouteResponse(BaseModel):
    model: str


class ModelSpecResponse(BaseModel):
    value: str
    provider: str
    cost: int
    ctx_k: int
    tools: bool
    vision: bool
    reliability: float
    cost_input: float
    cost_output: float
    competence: dict[str, float]


class SyncRequest(BaseModel):
    openrouter_key: str | None = None
    ollama_base_url: str | None = None
    timeout: int = 15


class SyncResponse(BaseModel):
    added: int
    updated: int
    total: int
    errors: list[str]


class CostRequest(BaseModel):
    model: str
    prompt_tokens: int
    completion_tokens: int


class CostResponse(BaseModel):
    model: str
    cost_usd: float | None


# ── Provider config ───────────────────────────────────────────────────────────

class ProviderRequest(BaseModel):
    name: str
    kind: str = Field(..., description="openrouter | ollama")
    enabled: bool = True
    api_key: str = ""
    base_url: str = ""
    timeout: int = 15


class ProviderResponse(BaseModel):
    name: str
    kind: str
    enabled: bool
    api_key: str
    base_url: str
    timeout: int


# ── API keys (per-user auth) ────────────────────────────────────────────────────

class ApiKeyCreateRequest(BaseModel):
    user: str = Field(..., description="Identity label this key belongs to")
    # Scope/limit fields persist now but are enforced in later phases.
    scope_models: str = Field("", description="Phase 2: allow/deny for models")
    max_tier: int = Field(0, description="Phase 2: max cost tier (0 = no ceiling)")
    rl_window_s: int = Field(0, description="Phase 3: rate-limit window seconds")
    rl_max_req: int = Field(0, description="Phase 3: max requests / window")
    rl_max_tokens: int = Field(0, description="Phase 3: max tokens / window")


class ApiKeyResponse(BaseModel):
    """A key's metadata. Never includes the secret (only the display prefix)."""
    user: str
    key_prefix: str
    enabled: bool
    scope_models: str = ""
    max_tier: int = 0
    rl_window_s: int = 0
    rl_max_req: int = 0
    rl_max_tokens: int = 0
    created_at: str = ""
    last_used_at: str = ""


class ApiKeyCreatedResponse(ApiKeyResponse):
    """Returned once, at creation — the ONLY time the plaintext key is exposed."""
    key: str = Field(..., description="Plaintext key — shown once; store it now")


class ApiKeyEnabledRequest(BaseModel):
    enabled: bool


# ── Updates ───────────────────────────────────────────────────────────────────

class UpdateStatusResponse(BaseModel):
    ok: bool
    local: str = ""
    remote: str = ""
    behind: int = 0
    ahead: int = 0
    update_available: bool = False
    detail: str = ""


class ApplyUpdateResponse(BaseModel):
    ok: bool
    detail: str
