"""ModelSpec — the canonical per-model data container."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class ModelSpec:
    value: str                          # e.g. "openrouter/meta-llama/llama-3.3-70b-instruct"
    provider: str = ""                  # "openrouter" | "ollama" | "bedrock" | ...
    cost: int = 0                       # relative tier for router sorting (0=local, 1=free-tier, 2+=paid)
    ctx_k: int = 0                      # context window in K tokens
    tools: bool = False                 # supports tool/function calling
    reliability: float = 1.0           # 0.0–1.0; models below threshold skipped by router
    cost_input: float = 0.0            # $/M input tokens (0 = unknown or free)
    cost_output: float = 0.0           # $/M output tokens (0 = unknown or free)
    competence: dict[str, float] = field(default_factory=dict)
    # competence keys: "coding" | "docs" | "reasoning" | "general"  → 0.0–1.0
