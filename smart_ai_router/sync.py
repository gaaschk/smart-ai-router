"""
Provider sync — fetch the live model catalog from each provider and upsert
into a MatrixStore.  Role-agnostic. No pricing tables — rates come from
the provider catalog directly.

Returns a SyncResult with counts of added/updated models and any errors.
"""
from __future__ import annotations

import dataclasses
import json
import urllib.request
from dataclasses import dataclass, field

from smart_ai_router.models import ModelSpec
from smart_ai_router.profiler import (
    agentic_level,
    apply_ratings,
    extract_catalog_signals,
    legacy_competence,
    profile_model,
)
from smart_ai_router.store.base import MatrixStore


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)
    # Models whose *shape* evidence changed this sync, and so are worth an LLM
    # profiling pass: new arrivals, plus models whose vendor description was
    # rewritten. See _needs_profiling() for why the list is this narrow.
    needs_profiling: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Models that actually changed this sync (new + genuinely updated)."""
        return self.added + self.updated


def _carry_profile_shape(spec: ModelSpec, prior: ModelSpec) -> ModelSpec:
    """Re-attach a stored LLM shape judgment to a freshly-built spec.

    Sync rebuilds specs from the catalog alone, so a fresh spec never carries
    ratings. Without this, every sync would (a) blank the ratings of every
    enriched model and (b) report each one as "updated" because the fresh spec
    differs from the stored one — so it must run *before* the equality check
    below, not after.

    The ratings are re-composed here rather than copied, which is the whole point
    of storing shape instead of numbers: fresh benchmarks move the level, the
    stored judgment re-applies on top, and nothing calls an LLM again.
    """
    if not prior.profile_ratings:
        return spec
    effective = apply_ratings(spec.profile, prior.profile_ratings)
    return dataclasses.replace(
        spec,
        profile=effective,
        # Re-derived, never carried: the legacy 4-value vector must summarize the
        # profile the router matches on, or the dashboard describes a decision
        # that was never made.
        competence=legacy_competence(effective),
        profile_rules=spec.profile if effective != spec.profile else {},
        profile_ratings=dict(prior.profile_ratings),
        profile_note=prior.profile_note,
    )


def _needs_profiling(spec: ModelSpec, prior: ModelSpec | None) -> bool:
    """Whether an LLM profiling pass should look at this model after the sync.

    Two cases, and deliberately not "anything that changed":

      - **New model.** It arrives with a profile shaped only by the cue table, so
        it is exactly the case the LLM pass exists for.
      - **Rewritten description.** The description is the *only* shape evidence
        sync has, so a rewrite means the stored judgment was formed from
        evidence that no longer stands.

    Everything else that makes a sync report "updated" — a price move, a context
    bump, a fresh benchmark index — changes the model's *level*, and levels are
    re-composed onto the stored ratings for free by _carry_profile_shape. Asking
    the rater again would pay for an answer we already have, and the answer would
    be the same: "relative to its own capability" is invariant to the level it is
    relative to.
    """
    if prior is None:
        return True
    return bool(spec.description) and spec.description != prior.description


def _apply_spec(
    store: MatrixStore,
    spec: ModelSpec,
    existing: dict[str, ModelSpec],
    result: SyncResult,
) -> None:
    """Upsert one model, counting it as added / updated / unchanged.

    Skips the write entirely when the stored spec already matches, so an
    unchanged catalog reports zero updates instead of re-touching every row.
    """
    prior = existing.get(spec.value)
    if _needs_profiling(spec, prior):
        result.needs_profiling.append(spec.value)
    if prior is None:
        store.upsert_model(spec)
        result.added += 1
        return
    spec = _carry_profile_shape(spec, prior)
    if prior != spec:
        store.upsert_model(spec)
        result.updated += 1
    else:
        result.unchanged += 1


def _prune_missing(
    store: MatrixStore,
    provider: str,
    seen: set[str],
    existing: dict[str, ModelSpec],
    result: SyncResult,
) -> None:
    """Delete stored models of `provider` that were absent from the fresh
    catalog. Only call after a *successful* fetch — a failed fetch must never
    reach here, or a transient outage would wipe the catalog.
    """
    for value, spec in existing.items():
        if spec.provider == provider and value not in seen:
            if store.delete_model(value):
                result.removed += 1


# ── Cost tier ─────────────────────────────────────────────────────────────────
# The router sorts primarily by ModelSpec.cost (an integer tier). We derive the
# tier from a *blended* effective price, not input price alone: output tokens
# are priced far higher than input (typically ~3-5x) and generation workloads
# emit more output than they ingest, so output dominates real cost. Ranking by
# input alone mis-orders models (e.g. cheap-input/expensive-output reasoning
# models look cheaper than they are).
#
# Weighting assumes output volume ~3x input (a typical chat/generation mix).
_TIER_WEIGHT_INPUT = 0.25
_TIER_WEIGHT_OUTPUT = 0.75


def _cost_tier(cost_input: float, cost_output: float, *, is_free: bool = False) -> int:
    """Map per-1M input/output $ rates to an integer cost tier for routing.

    Blends input and output rates (see weights above) then buckets. Both rates
    zero → tier 0 (local/unknown) or 1 (:free). Buckets are calibrated on the
    blended scale so distinct price points stay in distinct tiers, e.g.
    Haiku ($1/$5)≈$4→3, Sonnet ($3/$15)≈$12→5, Opus 4.8 ($5/$25)≈$20→8,
    Opus 4.1 ($15/$75)≈$60→15.
    """
    if cost_input == 0.0 and cost_output == 0.0:
        return 1 if is_free else 0
    eff = _TIER_WEIGHT_INPUT * cost_input + _TIER_WEIGHT_OUTPUT * cost_output
    if eff < 0.5:
        return 1
    if eff < 2:
        return 2
    if eff < 5:
        return 3
    if eff < 15:
        return 5
    if eff < 30:
        return 8
    if eff < 60:
        return 12
    return 15


def sync_from_providers(
    store: MatrixStore,
    *,
    openrouter_key: str | None = None,
    ollama_base_url: str | None = None,
    bedrock_key: str | None = None,
    timeout: int = 15,
) -> SyncResult:
    """Fetch model catalogs from configured providers and upsert into the store.

    Providers are skipped silently when their credentials/URLs are not supplied.
    """
    result = SyncResult()

    if ollama_base_url:
        _sync_ollama(store, ollama_base_url.rstrip("/"), result, timeout)

    if openrouter_key:
        _sync_openrouter(store, openrouter_key, result, timeout)

    if bedrock_key:
        _sync_bedrock(store, result)

    return result


# ── Bedrock (Claude) ────────────────────────────────────────────────────────
# Bedrock's OpenAI-compatible endpoint uses stable us.anthropic.* model IDs.
# We seed a curated set of Claude models with benchmark-informed competence and
# real per-1M input/output rates. The cost tier is derived from those rates via
# the same _cost_tier() blend used for OpenRouter models, so both providers land
# on one consistent scale (a model appearing in both won't show two tiers).
# Claude is still the most expensive tier, so the router only picks it when no
# cheaper model clears the quality bar (the fallback), or when forced.

# Capability is intentionally NOT listed here. It used to be a hand-written
# competence dict per row, which duplicated the same numbers in competence.py and
# gave them two places to drift apart. The profiler derives both the per-field
# profile and the legacy competence summary from the model id, so these rows now
# carry only what is genuinely Bedrock-specific: the id, context, and rates.
# max_output is the conservative floor across the Claude family rather than each
# model's advertised best. Anthropic rejects a max_tokens above the model's own
# limit, so overstating it turns a long answer into an error, while understating
# it only means a very long document gets a continuation — and 32k is already far
# past anything a chat reply needs.
_BEDROCK_CLAUDE_MODELS = [
    # (model_id, ctx_k, cost_input, cost_output, max_output)
    ("us.anthropic.claude-haiku-4-5",   200, 1.0,  5.0, 32000),
    ("us.anthropic.claude-sonnet-4-6", 1000, 3.0, 15.0, 32000),
    ("us.anthropic.claude-opus-4-8",   1000, 5.0, 25.0, 32000),
]


def _sync_bedrock(store: MatrixStore, result: SyncResult) -> None:
    existing = {s.value: s for s in store.all_models()}
    seen: set[str] = set()
    for mid, ctx_k, cost_input, cost_output, max_output in _BEDROCK_CLAUDE_MODELS:
        value = f"bedrock/{mid}"
        # Every Claude model on Bedrock supports extended thinking.
        profile = profile_model(value, supports_reasoning=True)
        spec = ModelSpec(
            value=value,
            provider="bedrock",
            cost=_cost_tier(cost_input, cost_output),
            ctx_k=ctx_k,
            max_output=max_output,
            tools=True,
            vision=True,
            # Left False because it is unverified, not because Claude can't do
            # it: Bedrock's OpenAI-compatible endpoint implements a subset of the
            # request body, and no Bedrock provider was configured on any
            # deployment available to test `response_format: json_schema`
            # against. False is the safe direction — it keeps these rows out of
            # the router's schema-constrained helper calls rather than sending
            # them a schema they might ignore. Flip it once measured.
            structured_outputs=False,
            reasoning=True,
            reliability=0.95,
            cost_input=cost_input,
            cost_output=cost_output,
            competence=legacy_competence(profile),
            profile=profile,
        )
        seen.add(value)
        _apply_spec(store, spec, existing, result)
    _prune_missing(store, "bedrock", seen, existing, result)


# ── Ollama ────────────────────────────────────────────────────────────────────
# /api/tags lists installed models but reports nothing about what they can do.
# The per-model capabilities live behind /api/show, which returns e.g.
#   {"capabilities": ["completion", "vision", "tools", "thinking"],
#    "model_info": {"gemma4.context_length": 262144, ...}}
# Before we read it, ollama models were hardcoded tools=False with vision unset,
# so tool-capable local models were invisible to agent mode and vision requests
# could never route locally — both silently fell through to paid providers.


def _ollama_details(base_url: str, name: str, timeout: int) -> tuple[list[str], int]:
    """Fetch (capabilities, context_length) for one installed model.

    Returns ([], 0) on any failure: /api/show is a per-model extra request, and a
    single flaky response must degrade that model to conservative defaults rather
    than abort the whole provider sync.
    """
    try:
        req = urllib.request.Request(
            f"{base_url}/api/show",
            data=json.dumps({"model": name}).encode(),
            headers={
                "User-Agent": "smart-ai-router",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            info = json.load(r)
    except Exception:
        return [], 0
    caps = [str(c) for c in (info.get("capabilities") or [])]
    # The context key is namespaced by architecture ("gemma4.context_length",
    # "qwen3.context_length", ...), so match on the suffix rather than guessing
    # the family name.
    ctx = 0
    for key, val in (info.get("model_info") or {}).items():
        if key.endswith(".context_length"):
            try:
                ctx = int(val)
            except (TypeError, ValueError):
                ctx = 0
            break
    return caps, ctx


def _sync_ollama(
    store: MatrixStore,
    base_url: str,
    result: SyncResult,
    timeout: int,
) -> None:
    try:
        req = urllib.request.Request(
            f"{base_url}/api/tags",
            headers={"User-Agent": "smart-ai-router"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            tags = json.load(r)
    except Exception as e:
        result.errors.append(f"Ollama: {e}")
        return

    existing = {s.value: s for s in store.all_models()}
    seen: set[str] = set()
    for m in tags.get("models", []):
        name = m.get("name", "")
        if not name:
            continue
        value = f"ollama/{name}"
        caps, ctx_len = _ollama_details(base_url, name, timeout)
        if ctx_len:
            ctx_k = ctx_len // 1000
        else:
            # /api/show unavailable — fall back to the old size-based guess so a
            # partial outage still yields a usable (if coarse) row.
            size_gb = (m.get("size", 0) or 0) / 1e9
            ctx_k = 128 if size_gb > 30 else (32 if size_gb > 10 else 8)
        # Ollama publishes no description or benchmarks, so the profile rests on
        # name priors — plus the one real capability signal /api/show does give
        # us: whether the model can think. Local models still land on the same
        # per-field scale as catalog models, so route() needs no per-provider
        # special case.
        thinking = "thinking" in caps
        profile = profile_model(value, supports_reasoning=thinking)

        spec = ModelSpec(
            value=value,
            provider="ollama",
            cost=0,
            ctx_k=ctx_k,
            tools="tools" in caps,
            vision="vision" in caps,
            # True for every Ollama model, and not a claim about the model:
            # Ollama implements `response_format` as constrained decoding in the
            # server, so the schema is enforced by the runtime whatever the
            # weights would have emitted. Verified against qwen2.5:3b-instruct,
            # which is small enough that it would certainly ignore a schema it
            # was merely asked to follow.
            structured_outputs=True,
            reasoning=thinking,
            reliability=1.0,
            cost_input=0.0,
            cost_output=0.0,
            competence=legacy_competence(profile),
            profile=profile,
        )
        seen.add(value)
        _apply_spec(store, spec, existing, result)
    _prune_missing(store, "ollama", seen, existing, result)


# ── OpenRouter ────────────────────────────────────────────────────────────────

def _sync_openrouter(
    store: MatrixStore,
    api_key: str,
    result: SyncResult,
    timeout: int,
) -> None:
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={
                "User-Agent": "smart-ai-router",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            catalog = json.load(r)
    except Exception as e:
        result.errors.append(f"OpenRouter: {e}")
        return

    existing = {s.value: s for s in store.all_models()}
    seen: set[str] = set()
    for m in catalog.get("data", []):
        mid = m.get("id", "")
        if not mid or mid.startswith("openrouter/") or "/" not in mid:
            continue
        # ':batch' variants are async-only — OpenRouter answers a synchronous
        # /v1/chat/completions call for them with 404 "This model is only
        # available through the Batch API". They're also priced ~50% under the
        # sibling they mirror, so leaving them in the catalog hands every
        # cheapest-first sort a model that cannot serve a request. Nothing about
        # them is a preference, so this is a hard filter rather than a denylist.
        if mid.endswith(":batch"):
            continue
        arch = (m.get("architecture") or {})
        modality = arch.get("modality", "text->text")
        # Accept any text-in / text-out model (e.g. text->text,
        # text+image->text, text+image+file->text). Reject models that
        # don't take text input or don't produce text output.
        inp, _, outp = modality.partition("->")
        if "text" not in inp or "text" not in outp:
            continue

        value = f"openrouter/{mid}"
        ctx_k = (m.get("context_length") or 0) // 1000
        # The provider's own output ceiling. Absent for a handful of models, and
        # left as 0 (unknown) rather than defaulted to the context window — a
        # guess here becomes a request the provider rejects, which is worse than
        # having no number. Range on the live catalog is 2048 to 1.8M, so this
        # cannot be inferred from context_length.
        max_output = int(
            (m.get("top_provider") or {}).get("max_completion_tokens") or 0
        )

        pr = m.get("pricing") or {}
        try:
            cost_input = round(float(pr.get("prompt", 0)) * 1_000_000, 4)
        except Exception:
            cost_input = 0.0
        try:
            cost_output = round(float(pr.get("completion", 0)) * 1_000_000, 4)
        except Exception:
            cost_output = 0.0

        # Cost tier for router sorting — blends input + output rates.
        cost = _cost_tier(cost_input, cost_output, is_free=mid.endswith(":free"))

        supports = m.get("supported_parameters") or []
        tools = "tools" in supports
        vision = "image" in inp  # inp = input side of modality (e.g. "text+image")
        # `structured_outputs`, not `response_format`. OpenRouter lists them
        # separately and 23 models on the live catalog carry only the latter,
        # meaning they accept `json_object` but ignore a schema — which is the
        # silent failure this flag exists to prevent, not a weaker version of the
        # capability. Measured at the time of the change: 336 of 415 models
        # advertise `structured_outputs`, 359 advertise `response_format`.
        structured_outputs = "structured_outputs" in supports
        reliability = 0.5 if mid.endswith(":free") else 0.9

        # Per-field capability profile from the catalog's own evidence: measured
        # artificial_analysis indices where present, the description's
        # specialization cues, and name priors to fill the gaps. This is the
        # richest signal available and it refreshes with every sync, so a newly
        # released model is ranked correctly without a code change.
        signals = extract_catalog_signals(m)
        # agentic_index measures loop stamina, not knowledge of a field, so it
        # goes on its own axis instead of into the profile. Popped rather than
        # ignored so a future signal added to extract_catalog_signals() still
        # fails loudly here rather than being silently dropped.
        agentic = agentic_level(signals.pop("agentic_index"))
        # Read, not popped: the profiler uses this to lift the reasoning-heavy
        # fields, and the spec stores it as a shape flag. One derivation, two
        # consumers — deriving it twice from `supports` would let them disagree.
        reasoning = bool(signals.get("supports_reasoning"))
        profile = profile_model(value, **signals)

        spec = ModelSpec(
            value=value,
            provider="openrouter",
            cost=cost,
            ctx_k=ctx_k,
            max_output=max_output,
            tools=tools,
            vision=vision,
            structured_outputs=structured_outputs,
            reasoning=reasoning,
            reliability=reliability,
            cost_input=cost_input,
            cost_output=cost_output,
            competence=legacy_competence(profile),
            profile=profile,
            description=signals["description"],
            agentic=agentic,
        )
        seen.add(value)
        _apply_spec(store, spec, existing, result)
    _prune_missing(store, "openrouter", seen, existing, result)
