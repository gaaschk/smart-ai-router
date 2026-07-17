"""
Pricing — pure arithmetic over per-model rates stored in the matrix.

No rate tables. No live fetches. Prices come from the provider catalog
at sync time and live in ModelSpec.cost_input / cost_output.
Returns None when a model's price is unknown (cost_input == 0 AND cost_output == 0
and the provider is not known-free like Ollama).
"""
from __future__ import annotations

from smart_ai_router.models import ModelSpec


def cost_for(
    spec: ModelSpec,
    prompt_tokens: int,
    completion_tokens: int,
) -> float | None:
    """Return USD cost estimate, or None if the model's price is unknown.

    Ollama (provider="ollama") is always $0. Any other model with both
    cost_input=0 and cost_output=0 is treated as unknown → returns None.
    """
    if spec.provider == "ollama":
        return 0.0

    if spec.cost_input == 0.0 and spec.cost_output == 0.0:
        return None  # unknown price — caller renders "cost unavailable"

    input_usd  = spec.cost_input  * prompt_tokens     / 1_000_000
    output_usd = spec.cost_output * completion_tokens / 1_000_000
    return input_usd + output_usd
