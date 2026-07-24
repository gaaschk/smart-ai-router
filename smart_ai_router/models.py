"""Data containers for the smart-ai-router."""
from __future__ import annotations
from dataclasses import dataclass, field


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
    reliability: float = 1.0           # 0.0–1.0; models below threshold skipped by router
    cost_input: float = 0.0            # $/M input tokens (0 = unknown or free)
    cost_output: float = 0.0           # $/M output tokens (0 = unknown or free)
    competence: dict[str, float] = field(default_factory=dict)
    # competence keys: "coding" | "docs" | "reasoning" | "general"  → 0.0–1.0


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
class UsageRecord:
    """One proxied request, attributed to a user for logging/quota accounting."""
    user: str = ""
    key_prefix: str = ""
    routed_model: str = ""
    domain: str = ""
    complexity: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    status: int = 200
    id: int = 0
    ts: str = ""
