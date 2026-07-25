"""Deployment capability negotiation — a column-reduction over the model matrix.

A feature is "available" for a deployment iff at least one *reachable* model
supports it. Reachable = registered ∩ enabled (reliability bar + not denylisted)
∩ in-scope for the calling key. This is provider-agnostic by construction: it
doesn't matter whether the vision model comes from ollama, openrouter, or a
future bedrock — if any reachable row has the column set, the feature is on.

Computed live from the current model list on each call (cheap: a few scans);
never cached, so provider/scope/denylist changes take effect immediately.
"""
from __future__ import annotations

from dataclasses import dataclass

from smart_ai_router.models import ModelSpec
from smart_ai_router.router import DEFAULT_THRESHOLDS, _denylisted
from smart_ai_router.scope import ModelScope


@dataclass(frozen=True)
class Capabilities:
    """What a deployment can do, derived from its reachable models."""
    vision: bool = False          # any reachable model accepts image input
    tools: bool = False           # any reachable model does function/tool calling
    max_context_k: int = 0        # largest context window (K tokens) reachable
    model_count: int = 0          # number of reachable models
    providers: tuple[str, ...] = ()  # distinct providers among reachable models


def reachable_models(
    models: list[ModelSpec],
    *,
    scope: ModelScope | None = None,
    min_reliability: float | None = None,
) -> list[ModelSpec]:
    """Models the router could actually pick: passes reliability + denylist + scope.

    Mirrors router._eligible's *static* filters (the ones independent of a
    specific request's domain/tools/vision needs), so capability answers line up
    with what routing can reach.
    """
    min_rel = (
        DEFAULT_THRESHOLDS["min_reliability"]
        if min_reliability is None else min_reliability
    )
    deny = _denylisted()
    out = []
    for spec in models:
        if spec.reliability < min_rel:
            continue
        if deny and any(d in spec.value.lower() for d in deny):
            continue
        if scope is not None and not scope.permits(spec):
            continue
        out.append(spec)
    return out


def compute_capabilities(
    models: list[ModelSpec],
    *,
    scope: ModelScope | None = None,
) -> Capabilities:
    """Reduce the model matrix to per-feature availability flags."""
    reach = reachable_models(models, scope=scope)
    if not reach:
        return Capabilities()
    return Capabilities(
        vision=any(m.vision for m in reach),
        tools=any(m.tools for m in reach),
        max_context_k=max((m.ctx_k for m in reach), default=0),
        model_count=len(reach),
        providers=tuple(sorted({m.provider for m in reach if m.provider})),
    )
