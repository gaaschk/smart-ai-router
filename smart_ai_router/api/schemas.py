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
    unchanged: int
    removed: int
    total: int
    errors: list[str]


class CapabilitiesResponse(BaseModel):
    vision: bool
    tools: bool
    max_context_k: int
    model_count: int
    providers: list[str]


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


class WhoAmIResponse(BaseModel):
    """The identity behind the current request's key, for the UI to display.

    Never includes the secret — only a display label and the safe key prefix.
    """
    authenticated: bool          # False in open (no-auth) mode
    kind: str                    # "admin" | "user" | "open"
    user: str = ""               # "admin", the per-user label, or "" in open mode
    key_prefix: str = ""         # short non-secret prefix for a per-user key
    is_admin: bool = False       # may manage keys (root/env key or first-run)


# ── Files (uploads) ────────────────────────────────────────────────────────────

class FileResponse(BaseModel):
    """OpenAI-compatible file object.

    Mirrors the shape returned by OpenAI's Files API so OpenAI-compatible
    clients (and claudish downstream) can consume it unchanged. `created_at`
    is a Unix timestamp (seconds), per the OpenAI convention.
    """
    id: str
    object: str = "file"
    bytes: int = 0
    created_at: int = 0
    filename: str = ""
    purpose: str = "assistants"


class FileListResponse(BaseModel):
    object: str = "list"
    data: list[FileResponse] = Field(default_factory=list)


class FileDeletedResponse(BaseModel):
    id: str
    object: str = "file"
    deleted: bool = True


# ── Chat history (conversations) ─────────────────────────────────────────────────

class ConversationResponse(BaseModel):
    """A saved chat thread's metadata (no messages)."""
    id: str
    title: str = "New chat"
    created_at: str = ""
    updated_at: str = ""


class ConversationListResponse(BaseModel):
    object: str = "list"
    data: list[ConversationResponse] = Field(default_factory=list)


class ConversationCreateRequest(BaseModel):
    title: str = "New chat"


class ConversationUpdateRequest(BaseModel):
    title: str = Field(..., description="New title for the conversation")


class ChatMessageResponse(BaseModel):
    """One turn. `content` is a plain string, or a content-parts array/object
    when the original turn carried file/image refs."""
    role: str
    content: object = ""
    ts: str = ""


class ChatMessageCreateRequest(BaseModel):
    role: str = Field(..., description="user | assistant | system")
    # A plain string, or an OpenAI content-parts array (text + file/image refs).
    content: object = ""


class ConversationDetailResponse(ConversationResponse):
    """A conversation plus its full message list (for loading into the UI)."""
    messages: list[ChatMessageResponse] = Field(default_factory=list)


class ConversationDeletedResponse(BaseModel):
    id: str
    object: str = "conversation"
    deleted: bool = True


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
