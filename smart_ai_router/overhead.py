"""Overhead accounting — the router's own LLM calls, logged like any other spend.

The router does not only forward requests. It profiles every prompt with a
classifier, escalates consequential prompts to a stronger profiler, and rates
catalog models after a sync or a Refine run. Those are real calls against real
provider accounts, and until they were recorded the usage page confidently
reported a number smaller than the bill: a deployment could be spending most of
its money on judging prompts and see none of it.

Why a context-scoped sink instead of passing a store around
───────────────────────────────────────────────────────────
llm_classifier and llm_profiler are deliberately store-free — they take a
base_url, a model, and an api_key, which is what makes them usable from the
bakeoff script and from tests with no database. Handing them a store (or a
CapabilityRouter) to log through would invert that.

So they only *announce* what they spent, via note(), and the caller that already
has a store — the proxy for classification, the facade for profiling — opens a
collect() block around the work and writes the rows. When no sink is active
note() is a no-op, so every existing caller and test keeps working untouched and
nothing is written by code paths that have no user to attribute it to.

Best-effort, like the rest of usage logging: a failure here must never surface
in a request that otherwise worked.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from smart_ai_router.models import UsageRecord

# Kinds, mirroring UsageRecord.kind. Kept here as constants because the writers
# (llm_classifier, llm_profiler) and the readers (dashboard) must agree on the
# exact strings — they are stored values, not labels.
CLASSIFY = "classify"
CLASSIFY_REFINE = "classify-refine"
PROFILE = "profile"


@dataclass(frozen=True)
class OverheadCall:
    """One completed non-request call: what it was, and what it consumed."""

    kind: str
    model: str          # provider-side model id, e.g. "openai/gpt-5.6-luna"
    prompt_tokens: int = 0
    completion_tokens: int = 0


_sink: contextvars.ContextVar[list[OverheadCall] | None] = contextvars.ContextVar(
    "smart_ai_router_overhead_sink", default=None
)


@contextmanager
def collect() -> Iterator[list[OverheadCall]]:
    """Collect the overhead calls made inside this block.

    The list is live: tasks spawned inside the block (e.g. the profiler's
    asyncio.gather) inherit a copy of the context, which holds a reference to
    this same list, so their notes land here too.
    """
    calls: list[OverheadCall] = []
    token = _sink.set(calls)
    try:
        yield calls
    finally:
        _sink.reset(token)


def note(kind: str, *, model: str, usage: object = None) -> None:
    """Record that an overhead call completed. No-op outside a collect() block.

    `usage` is the provider's OpenAI-style token block, or None when the reply
    carried none. A call with no reported tokens is still recorded — it happened,
    it was billed, and its count is the honest part even when its size isn't.
    """
    calls = _sink.get()
    if calls is None:
        return
    block = usage if isinstance(usage, dict) else {}
    try:
        prompt = int(block.get("prompt_tokens") or 0)
        completion = int(block.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        prompt = completion = 0
    calls.append(
        OverheadCall(
            kind=kind, model=model, prompt_tokens=prompt, completion_tokens=completion
        )
    )


def _catalog_value(cr, model: str) -> str:
    """Map a provider-side model id onto its catalog value, for pricing.

    Overhead callers hold the id the provider wants ("openai/gpt-5.6-luna",
    "qwen2.5:3b-instruct"); the store keys models by a provider-prefixed value
    ("openrouter/openai/gpt-5.6-luna", "ollama/qwen2.5:3b-instruct"). Rather than
    thread the catalog value through the classifier chain, we look for the row
    that exists. An unmatched id is returned as-is: the row still records the
    call at zero cost, which is right for a local model and honest for a model
    that isn't in the catalog.
    """
    for candidate in (
        model,
        f"openrouter/{model}",
        f"ollama/{model}",
        f"bedrock/{model}",
    ):
        try:
            if cr.get_model(candidate) is not None:
                return candidate
        except Exception:  # noqa: BLE001 — a store hiccup must not break logging
            return model
    return model


def record(cr, calls: list[OverheadCall], *, user: str = "", key_prefix: str = "") -> None:
    """Write collected overhead calls to the usage log (best-effort, never raises).

    Attributed to the identity whose work caused them: the requesting user for
    classification, the admin who triggered a sync/Refine for profiling. That is
    what makes "this user's prompts are expensive to route" visible at all — and
    the rate limiter ignores these rows, so attribution costs the user nothing.
    """
    for call in calls:
        try:
            value = _catalog_value(cr, call.model)
            cost = cr.cost_for(value, call.prompt_tokens, call.completion_tokens) or 0.0
            cr.record_usage(
                UsageRecord(
                    kind=call.kind,
                    user=user,
                    key_prefix=key_prefix,
                    routed_model=value,
                    prompt_tokens=call.prompt_tokens,
                    completion_tokens=call.completion_tokens,
                    cost_usd=cost,
                )
            )
        except Exception:  # noqa: BLE001 — accounting is never worth a failed call
            continue
