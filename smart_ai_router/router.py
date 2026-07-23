"""
Core routing logic — given (domain, complexity, constraints), pick the cheapest
model that clears the competence bar and meets the reliability threshold.

No role knowledge. No pricing tables. The caller supplies explicit hints.
"""
from __future__ import annotations

import os

from smart_ai_router.models import ModelSpec
from smart_ai_router.store.base import MatrixStore


# Default thresholds — callers can override by passing their own dict.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "trivial":        0.50,
    "moderate":       0.70,
    "hard":           0.88,
    "min_reliability": 0.70,
}


def _denylisted() -> tuple[str, ...]:
    """Substrings of model `value`s to never route to.

    Read from SMART_ROUTER_MODEL_DENYLIST (comma-separated). Matched as
    case-insensitive substrings so a whole family can be excluded with one
    entry (e.g. "mxfp8" or "qwen3.6:35b"). This is a durable, config-driven
    override: unlike a hand-edited reliability value, it survives sync(),
    which re-seeds models with reliability=1.0. Use it to route away from a
    model that's installed but broken in this environment (e.g. an MLX-quant
    model whose runtime can't load) without deleting it from the catalog.
    """
    raw = os.environ.get("SMART_ROUTER_MODEL_DENYLIST", "")
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def route(
    store: MatrixStore,
    domain: str,
    complexity: str,
    *,
    needs_tools: bool,
    needs_vision: bool = False,
    est_tokens: int = 0,
    exclude: set[str] | None = None,
    thresholds: dict[str, float] | None = None,
) -> str:
    """Return the cheapest model string that clears the competence + reliability bars.

    Falls back to the best-competence model when no candidate clears the bar
    (never fails to route as long as the matrix is non-empty).

    Args:
        store:        MatrixStore implementation to read models from.
        domain:       "coding" | "docs" | "reasoning" | "general"
        complexity:   "trivial" | "moderate" | "hard"
        needs_tools:  If True, exclude models where tools=False.
        est_tokens:   Estimated prompt size in tokens (0 = skip ctx filter).
        exclude:      Model value strings to skip (e.g. previously rate-limited).
        thresholds:   Override default competence/reliability thresholds.
    """
    thr = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    bar: float = thr.get(complexity, 0.70)
    min_rel: float = thr.get("min_reliability", 0.70)
    _exclude = exclude or set()
    _deny = _denylisted()

    models = store.all_models()

    def _eligible(spec: ModelSpec) -> bool:
        if spec.value in _exclude:
            return False
        if _deny and any(d in spec.value.lower() for d in _deny):
            return False
        if spec.reliability < min_rel:
            return False
        if needs_tools and not spec.tools:
            return False
        if needs_vision and not spec.vision:
            return False
        if est_tokens > 0 and spec.ctx_k > 0 and est_tokens > spec.ctx_k * 1000:
            return False
        return True

    candidates = [
        (spec.cost, -spec.competence.get(domain, 0.0), spec.value)
        for spec in models
        if _eligible(spec) and spec.competence.get(domain, 0.0) >= bar
    ]

    if candidates:
        candidates.sort()
        return candidates[0][2]

    # No candidate clears the bar — return highest-competence eligible model
    fallback = [
        (-spec.competence.get(domain, 0.0), spec.cost, spec.value)
        for spec in models
        if _eligible(spec)
    ]

    if fallback:
        fallback.sort()
        return fallback[0][2]

    raise RuntimeError(
        f"route: no eligible model for domain={domain!r}, complexity={complexity!r}, "
        f"needs_tools={needs_tools}. Run sync() to populate the matrix."
    )
