"""Pydantic request/response models for the REST API."""
from __future__ import annotations

from pydantic import BaseModel, Field


class DomainNeedRequest(BaseModel):
    """One field the prompt reaches into, and how deep it goes."""

    field: str = Field(..., description="a key of taxonomy.FIELDS")
    depth: str = Field(
        "practitioner", description="surface | practitioner | specialist | frontier"
    )


class RouteRequest(BaseModel):
    """Either shape works: a full profile (preferred) or the legacy label pair.

    `domains` is what route() actually matches on — it names the fields the
    prompt needs and how deep into each, and a model must clear the bar on every
    one. `domain`/`complexity` remain for callers that only speak the old
    vocabulary; they are adapted to a single-field profile. When both are sent,
    the profile wins.
    """

    domain: str = Field("", description="coding | docs | reasoning | general (legacy)")
    complexity: str = Field(
        "", description="trivial | moderate | hard | expert (legacy)"
    )
    domains: list[DomainNeedRequest] = Field(default_factory=list)
    demands: list[str] = Field(
        default_factory=list,
        description="factual_precision | quantitative | long_synthesis | agentic",
    )
    stakes: str = Field("low", description="low | medium | high")
    needs_tools: bool = False
    needs_vision: bool = False
    est_tokens: int = 0
    exclude: list[str] = Field(default_factory=list)


class RouteResponse(BaseModel):
    """The pick, plus enough of the reasoning to audit it.

    `qualified` is False when nothing available cleared every bar and the pick is
    the closest miss — callers should treat such an answer's specifics as
    unverified rather than assume competence.
    """

    model: str
    profile: str = Field("", description="human-readable demand, e.g. 'Law @ specialist'")
    requirements: dict[str, float] = Field(default_factory=dict)
    scores: dict[str, float] = Field(
        default_factory=dict, description="the chosen model's score per required field"
    )
    qualified: bool = True
    why: str = ""
    domain: str = ""
    complexity: str = ""


class ModelSpecResponse(BaseModel):
    value: str
    provider: str
    cost: int
    ctx_k: int
    tools: bool
    vision: bool
    structured_outputs: bool = Field(
        default=False,
        description=(
            "honors a response_format json_schema, so a reply can be constrained "
            "to a shape; required for the router's own helper calls"
        ),
    )
    reasoning: bool = Field(
        default=False,
        description="emits thinking tokens before the answer",
    )
    reliability: float
    cost_input: float
    cost_output: float
    agentic: float = Field(
        default=0.0,
        description=(
            "measured tool-loop stamina, 0-1; 0 = never measured (most of the "
            "catalog), which the router treats as exempt rather than incapable"
        ),
    )
    competence: dict[str, float]
    profile: dict[str, float] = Field(
        default_factory=dict, description="per-taxonomy-field capability scores"
    )
    description: str = ""
    profile_ratings: dict[str, str] = Field(
        default_factory=dict,
        description="LLM-judged relative strengths per field; empty = rules only",
    )
    profile_note: str = Field(
        default="", description="one-line rationale from the profile rater"
    )


class ProfileRefineRequest(BaseModel):
    """Ask an LLM to refine model profiles (admin, Models page → Refine)."""

    limit: int = Field(
        default=0, description="max models to profile; 0 = the configured default"
    )
    only_missing: bool = Field(
        default=True, description="skip models that already carry LLM ratings"
    )
    dry_run: bool = Field(
        default=False,
        description="compute and report changes without writing anything",
    )
    model: str | None = Field(
        default=None,
        description="pin the rater for this run instead of routing to it",
    )
    audit_days: int = Field(
        default=30,
        description="window of routed traffic to replay when auditing the change",
    )


class ProfileRefineResponse(BaseModel):
    """What a refine run did, and what it would change about routing."""

    enrich: dict
    audit: dict


class SyncRequest(BaseModel):
    openrouter_key: str | None = None
    ollama_base_url: str | None = None
    timeout: int = 15
    profile: bool | None = Field(
        default=None,
        description="profile newly added models with an LLM; None follows the "
        "'Profile new models on sync' setting",
    )


class SyncResponse(BaseModel):
    added: int
    updated: int
    unchanged: int
    removed: int
    total: int
    errors: list[str]
    # Present only when a sync-triggered profiling pass actually ran. Same shape
    # as the Refine endpoint's response, so the UI renders it with one function.
    profiled: ProfileRefineResponse | None = None
    # Models that warranted profiling but fell outside this run's ceiling. Never
    # silently dropped: a bounded run that says nothing reads as "all done".
    profile_pending: int = 0


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


# ── Usage summary (dashboard) ─────────────────────────────────────────────────

class UsageBucket(BaseModel):
    """Aggregated counters, shared by the totals block and every group-by row."""
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    estimated_rows: int = 0  # rows whose tokens were locally estimated


class UsageGroupRow(UsageBucket):
    """One group-by row: `key` is the model / date / user / dom/complexity."""
    key: str = ""


class UsageOverhead(BaseModel):
    """The router's own spend: prompt classification, the refine pass, model
    profiling. Separate from the user-traffic aggregates because it is not user
    traffic — nobody requested these calls — but it is on the same bill, and it
    used to be missing from this response entirely."""
    totals: UsageBucket = Field(default_factory=UsageBucket)
    by_kind: list[UsageGroupRow] = Field(default_factory=list)
    by_model: list[UsageGroupRow] = Field(default_factory=list)


class UsageSummaryResponse(BaseModel):
    """Dashboard rollup. `by_user` is present only for the admin (all-users)
    view; a per-user request omits it. Every field except `overhead` covers
    proxied user requests only."""
    totals: UsageBucket
    by_model: list[UsageGroupRow] = Field(default_factory=list)
    by_day: list[UsageGroupRow] = Field(default_factory=list)
    by_domain: list[UsageGroupRow] = Field(default_factory=list)
    by_classifier: list[UsageGroupRow] = Field(
        default_factory=list,
        description=(
            "which classifier profiled each request — llm | llm-free | "
            "llm-refined | keyword | default, or \"\" for rows logged before the "
            "column existed. A keyword-heavy mix means the configured model is "
            "not answering"
        ),
    )
    by_user: list[UsageGroupRow] | None = None
    overhead: UsageOverhead = Field(default_factory=UsageOverhead)


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


# ── Settings (UI-managed runtime config) ────────────────────────────────────────

class SettingResponse(BaseModel):
    key: str
    label: str
    group: str
    help: str = ""
    type: str = Field(..., description="str | int | bool")
    value: object = Field(..., description="Current effective value (typed)")
    default: object
    env: str = Field(..., description="Env var that serves as the fallback")
    source: str = Field(..., description="db | env | default — where value came from")
    sensitive: bool = False
    warning: str = Field(
        "",
        description=(
            "Advisory about the current value, checked against live deployment "
            "state rather than its type — e.g. a classifier model that isn't in "
            "the catalog. Empty when the value looks usable."
        ),
    )


class SettingsResponse(BaseModel):
    settings: list[SettingResponse]


class SettingsUpdateRequest(BaseModel):
    updates: dict[str, object] = Field(
        ..., description="Map of setting key → new value (typed per its spec)"
    )


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
    authenticated: bool          # False in open (no-auth) and anonymous mode
    kind: str                    # "admin" | "user" | "open" | "anon"
    user: str = ""               # "admin", the per-user label, or "" in open mode
    key_prefix: str = ""         # short non-secret prefix for a per-user key
    is_admin: bool = False       # may manage keys (root/env key or first-run)
    # Anonymous (public chat) mode. `degraded` says the daily spend cap has been
    # reached and answers now come from free/local models, which is worth telling
    # a visitor. The dollar figures behind it are deliberately not exposed — how
    # much budget an operator has left is nobody else's business.
    anon: bool = False
    degraded: bool = False
    agent_available: bool = True  # False for anon: filesystem tools are off


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
    instance: str = Field(
        default="",
        description=(
            "identity of the process answering, minted at startup; a changed "
            "value is how the UI proves the app restarted onto new code"
        ),
    )


class ApplyUpdateResponse(BaseModel):
    ok: bool
    detail: str
    # Set when finishing the update needs a human (e.g. the daemon is
    # root-owned): a copy-pasteable command shown alongside the failure.
    hint: str = ""
    pulled: str = Field(default="", description="short sha the pull landed on")
    instance: str = Field(
        default="",
        description="identity of the process that pulled, i.e. the one to outlive",
    )
