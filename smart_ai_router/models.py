"""Data containers for the smart-ai-router."""
from __future__ import annotations
import secrets
from dataclasses import dataclass, field


def generate_conversation_id() -> str:
    """Opaque conversation id, e.g. 'conv-9f3a...' (mirrors files.generate_file_id)."""
    return f"conv-{secrets.token_hex(16)}"


@dataclass
class ProviderConfig:
    name: str                        # "openrouter" | "ollama" | user-defined slug
    kind: str                        # "openrouter" | "ollama"  (driver selector)
    enabled: bool = True
    api_key: str = ""                # bearer token; empty for local providers
    base_url: str = ""               # e.g. "http://localhost:11434" for Ollama
    timeout: int = 15


@dataclass
class ModelSpec:
    value: str                          # e.g. "openrouter/meta-llama/llama-3.3-70b-instruct"
    provider: str = ""                  # "openrouter" | "ollama" | "bedrock" | ...
    cost: int = 0                       # relative tier for router sorting (0=local, 1=free-tier, 2+=paid)
    ctx_k: int = 0                      # context window in K tokens
    tools: bool = False                 # supports tool/function calling
    vision: bool = False                # supports image inputs
    structured_outputs: bool = False
    # Honors `response_format: {"type": "json_schema"}` — a *schema*, not merely
    # valid JSON. The distinction is the whole reason this is stored: a model that
    # accepts `json_object` but ignores the schema answers the prompt instead of
    # filling in the requested shape, which parses as nothing and fails silently.
    # The router's own helper calls (prompt refinement, model profiling) depend on
    # schema-constrained replies, so this is a hard filter for them.
    reasoning: bool = False
    # Emits thinking tokens before the answer. Not a quality signal in either
    # direction — it is a *shape* signal: a thinking model handed a small output
    # budget spends it reasoning and returns an empty message, which is why the
    # prompt classifier wants a model without it. Stored rather than re-derived so
    # a profiler change can be re-applied without re-fetching every catalog, the
    # same reason `description` is stored.
    reliability: float = 1.0           # 0.0–1.0; models below threshold skipped by router
    cost_input: float = 0.0            # $/M input tokens (0 = unknown or free)
    cost_output: float = 0.0           # $/M output tokens (0 = unknown or free)
    agentic: float = 0.0
    # Measured ability to hold a multi-step tool loop together (profiler.
    # agentic_level). Deliberately NOT a taxonomy field: it is not knowledge about
    # a subject, and folding it into one made an agentic benchmark half of the
    # legacy `general` column. **0.0 means never measured, not incapable** — only
    # ~a third of the OpenRouter catalog carries the index and no local model
    # does, so the router treats 0.0 as exempt rather than disqualifying.
    competence: dict[str, float] = field(default_factory=dict)
    # competence keys: "coding" | "docs" | "reasoning" | "general"  → 0.0–1.0
    # Legacy summary of `profile`, derived by profiler.legacy_competence() so the
    # two can never disagree. Still read by the /route API and the matrix UI.
    profile: dict[str, float] = field(default_factory=dict)
    # Per-field depth scores keyed by smart_ai_router.taxonomy.FIELDS → 0.0–1.0.
    # This is what route() matches a PromptProfile against. Empty for rows written
    # before profiling existed; router falls back to `competence` in that case.
    #
    # This is the *effective* profile: when LLM ratings exist the store composes
    # them onto the rules baseline on read, so the router never has to know
    # enrichment happened and pays nothing per request for it.
    description: str = ""
    # Provider-supplied blurb ("flagship-level Agentic Coding model"). Kept
    # because it is the input the profiler reads specialization from — storing it
    # means a profiler change can be re-applied without re-fetching every catalog.
    profile_rules: dict[str, float] = field(default_factory=dict)
    # The deterministic profile before LLM ratings were applied.
    #
    # Invariant: populated *only* when an overlay actually changed `profile`.
    # Empty therefore means "`profile` IS the baseline", which keeps un-enriched
    # rows round-tripping byte-identical to what sync wrote. Read it through
    # profiler.baseline_profile() rather than directly.
    profile_ratings: dict[str, str] = field(default_factory=dict)
    # field → one of profiler.RATING_KEYS, as judged by the enrichment pass.
    # Stored rather than the composed numbers so a re-sync with fresh benchmarks
    # re-levels the profile without a second LLM call: the LLM owns the shape, the
    # benchmarks own the level.
    profile_note: str = ""
    # One line from the rater explaining the shape it chose. Shown in the models
    # UI — an enrichment nobody can inspect is an enrichment nobody should trust.


@dataclass
class ApiKey:
    """A per-user API key. The plaintext key is never stored — only its SHA-256
    hash. `key_prefix` is a short, non-secret slice kept for display/identification.

    Scope and rate-limit fields are persisted now (Phase 1) but only enforced in
    later phases; 0 / "" mean "unset / no restriction".
    """
    key_hash: str                    # SHA-256 hex of the plaintext key
    user: str                        # identity label this key belongs to
    key_prefix: str = ""             # first chars of the key, safe to display
    enabled: bool = True             # revoke = flip to False (no redeploy)
    scope_models: str = ""           # Phase 2: JSON allow/deny for models/providers
    max_tier: int = 0                # Phase 2: max cost tier (0 = no ceiling)
    rl_window_s: int = 0             # Phase 3: rate-limit window seconds (0 = off)
    rl_max_req: int = 0              # Phase 3: max requests / window
    rl_max_tokens: int = 0           # Phase 3: max tokens / window
    id: int = 0
    created_at: str = ""
    last_used_at: str = ""


@dataclass
class FileRecord:
    """An uploaded file. Bytes live on disk (path); this row is only metadata.

    Follows the OpenAI Files API shape (id like "file-...", purpose, bytes,
    filename) so OpenAI-compatible clients work unchanged. `extracted_text` holds
    server-side-extracted text for documents (PDF/text/code); empty for images,
    which are inlined as base64 at request time instead.
    """
    id: str                          # "file-<token>" — OpenAI-style identifier
    user: str = ""                   # owner identity (per-user scoping)
    filename: str = ""               # original client filename
    purpose: str = "assistants"      # OpenAI purpose label
    mime: str = "application/octet-stream"
    bytes: int = 0                   # size on disk
    path: str = ""                   # absolute path to the stored blob
    extracted_text: str = ""         # server-extracted text (documents only)
    created_at: str = ""


@dataclass
class Conversation:
    """A saved chat thread, owned by a user, so history survives reloads and
    restarts. Messages live in ChatMessage rows keyed on `id`.

    `title` is a short human label (auto-derived from the first user message,
    editable). `created_at`/`updated_at` are ISO-8601 UTC; `updated_at` bumps
    whenever a message is appended, so the conversation list can sort by recency.
    """
    id: str                          # "conv-<token>" — opaque identifier
    user: str = ""                   # owner identity (per-user scoping)
    title: str = "New chat"          # short display label
    created_at: str = ""
    updated_at: str = ""


@dataclass
class ChatMessage:
    """One turn in a Conversation, in OpenAI shape. `content` is stored as text;
    when a turn carries structured content (content-parts array with file/image
    refs) the API layer JSON-encodes it and sets `content_json=True` so it can
    round-trip back to the same shape the client sent.
    """
    conversation_id: str             # FK → Conversation.id
    role: str = "user"               # "user" | "assistant" | "system"
    content: str = ""                # message text (or JSON when content_json)
    content_json: bool = False       # True → `content` is a JSON-encoded parts array
    id: int = 0                      # autoincrement
    ordinal: int = 0                 # per-conversation sequence (stable ordering)
    ts: str = ""


@dataclass
class UsageRecord:
    """One billable LLM call, attributed to a user for logging/quota accounting.

    Usually a proxied request, but not always — see `kind`.
    """
    # What this call *was*, since not every call the router bills for is a user
    # request. The router spends money on its own behalf too: it profiles every
    # prompt, sometimes escalates that to a stronger model, and rates catalog
    # models after a sync. That spend was previously invisible, which made the
    # usage page understate the real bill.
    #
    #   proxy           — a user request forwarded to the routed model
    #   classify        — prompt profiling (the two-speed classifier's triage)
    #   classify-refine — the second-pass profiler on a consequential prompt
    #   profile         — one model-shape rating during a Refine/sync pass
    #
    # Only `proxy` rows are user traffic, so the dashboard aggregates and the
    # rate limiter count those alone and report the rest as overhead.
    kind: str = "proxy"
    user: str = ""
    key_prefix: str = ""
    routed_model: str = ""
    domain: str = ""
    complexity: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    status: int = 200
    # True when tokens were estimated locally (char/4) rather than reported by
    # the provider — e.g. a streamed response whose backend ignored
    # stream_options.include_usage. Lets the dashboard flag approximate figures.
    tokens_estimated: bool = False
    # The full prompt profile behind this routing decision, in
    # taxonomy.normalize_profile() shape. `domain`/`complexity` above are the
    # lossy legacy summary; this is what actually chose the model, so it is the
    # only thing that lets a later profiler change be *judged* — replay these and
    # see which real decisions would flip. Empty for rows written before
    # profiles, and for the legacy (domain, complexity) route() path.
    profile: dict | None = None
    id: int = 0
    ts: str = ""
