"""
Pricing — pure arithmetic over per-model rates stored in the matrix.

No rate tables. No live fetches. Prices come from the provider catalog
at sync time and live in ModelSpec.cost_input / cost_output.
Returns None when a model's price is unknown (cost_input == 0 AND cost_output == 0
and the provider is not known-free like Ollama).
"""
from __future__ import annotations

from smart_ai_router.models import ModelSpec

# Weighting assumes output volume ~3x input (a typical chat/generation mix).
_BLEND_WEIGHT_INPUT = 0.25
_BLEND_WEIGHT_OUTPUT = 0.75


def blended_rate(cost_input: float, cost_output: float) -> float:
    """One comparable $/1M figure for a model whose two rates differ.

    Output tokens are priced far higher than input (typically ~3-5x) and
    generation workloads emit more output than they ingest, so output dominates
    real cost: ranking by input alone mis-orders models (a
    cheap-input/expensive-output reasoning model looks cheaper than it is).

    Two callers, and they need the same number for different reasons — sync
    buckets it into ModelSpec.cost, and the router breaks price ties with it.
    Keeping one function means a tie can never be broken on a different notion of
    "cheaper" than the one that assigned the tier.
    """
    return _BLEND_WEIGHT_INPUT * cost_input + _BLEND_WEIGHT_OUTPUT * cost_output


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
