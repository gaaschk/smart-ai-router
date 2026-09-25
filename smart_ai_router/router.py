"""
Core routing logic — given what a prompt demands, pick the cheapest model that
is actually qualified to answer it.

No role knowledge. No pricing tables. The caller supplies explicit hints.

The match
─────────
A PromptProfile (see taxonomy.py) names the fields the prompt reaches into and
how deep it goes in each. A model's profile scores it on those same fields. To
qualify, a model must clear the requirement on **every** named field — so a
cheap coding specialist is disqualified from a law-plus-engineering prompt by its
law score, instead of winning on price because its coding score was high. Among
models that do qualify, cheapest still wins: that is the router's whole point.

Legacy callers that pass (domain, complexity) are adapted to a single-field
profile, which reproduces the old single-bar behavior exactly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from smart_ai_router import settings as _settings
from smart_ai_router.models import ModelSpec
from smart_ai_router.pricing import blended_rate
from smart_ai_router.scope import ModelScope
from smart_ai_router.store.base import MatrixStore
from smart_ai_router.taxonomy import (
    AGENTIC_FLOOR,
    FIELDS,
    PromptProfile,
    profile_from_labels,
)


# Default thresholds — callers can override by passing their own dict.
#
# The per-complexity bars are the legacy path only: they apply when a caller
# supplies (domain, complexity) with no profile, and are kept at their historical
# values so that path routes exactly as it did before. Profile-based routing
# derives its bars from taxonomy.DEPTHS instead.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "trivial":        0.50,
    "moderate":       0.70,
    "hard":           0.88,
    "expert":         0.94,
    "min_reliability": 0.70,
    # Only consulted for requests that involve tool use, and only for models whose
    # loop stamina has actually been measured. See taxonomy.AGENTIC_FLOOR.
    "min_agentic":    AGENTIC_FLOOR,
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

    UI-managed (Settings page) with SMART_ROUTER_MODEL_DENYLIST as env fallback.
    """
    raw = _settings.get_str("model_denylist")
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def _agent_denylisted() -> tuple[str, ...]:
    """Substrings of model `value`s to never route to *in agent mode*.

    Read from SMART_ROUTER_AGENT_DENYLIST (comma-separated, case-insensitive
    substring match — same shape as SMART_ROUTER_MODEL_DENYLIST).

    Some models advertise tools=True (so they pass the needs_tools filter) yet
    are unreliable at driving a streamed, multi-step tool-calling loop: they
    stall, emit tool calls they never close, or never produce a first token.
    Routing an agent task to one of those wedges the request. This denylist
    excludes such models from agent routing only — they stay eligible for plain
    chat, where single-shot generation is fine. Like the general denylist it is
    config-driven and survives sync() (which re-seeds reliability=1.0).

    UI-managed (Settings page) with SMART_ROUTER_AGENT_DENYLIST as env fallback.
    """
    raw = _settings.get_str("agent_denylist")
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def field_score(spec: ModelSpec, field: str) -> float:
    """This model's competence on one taxonomy field.

    Prefers the per-field profile written by sync. Falls back to the legacy
    4-value competence vector via the field's legacy domain, which keeps rows
    written before profiling existed (and any store implementation that doesn't
    persist profiles) routable instead of scoring them 0 and excluding them.
    """
    if spec.profile:
        val = spec.profile.get(field)
        if val is not None:
            return float(val)
    legacy_domain = FIELDS.get(field, ("", "general"))[1]
    return float(spec.competence.get(legacy_domain, 0.0))


def _needs_agentic(
    profile: PromptProfile, needs_tools: bool, agent_mode: bool
) -> bool:
    """Whether this request will actually put a model in a tool loop.

    Three independent signals, any of which is enough:

      - `needs_tools` — the request carries a `tools` array, so the model may be
        asked to call one and then use the result.
      - `agent_mode` — the router itself will drive a multi-step loop.
      - the `agentic` demand — the classifier read the prompt as multi-step work
        even where the client sent no tools (e.g. "research X, then write Y").

    Derived here rather than taken as a parameter so no caller can request tool use
    and forget the floor — the two arrive together or not at all.
    """
    return needs_tools or agent_mode or "agentic" in profile.demands


def _output_floor(profile: PromptProfile) -> int:
    """Output capacity a request prefers its model to have, 0 = don't care.

    Only documents care. A model's own max_output is the one ceiling the router
    cannot raise by asking, and the cheap end of the catalog is where the low ones
    live — several cap at 2–4k tokens — so cheapest-qualified-wins will reliably
    hand a story to a model that must stop partway through it. Expressed as a
    floor on capacity rather than a hard requirement: see _select().
    """
    if not profile.is_long_form():
        return 0
    return max(0, _settings.get_int("long_form_min_model_output"))


def _price(spec: ModelSpec) -> float:
    """What this model really costs, for ranking inside a cost tier.

    ModelSpec.cost is a coarse bucket — that is what makes it usable as an API
    key's max_tier ceiling — so models at visibly different prices share one:
    claude-sonnet-4 ($3/$15) and claude-sonnet-5 ($2/$10) are both tier 5. With
    only the tier to sort on, the tie fell to routing margin, and margin is
    decided by field scores that need not have anything to do with price. On the
    live deployment that sent 195 requests to Sonnet 4 over Sonnet 5 — same 0.95
    on the binding field, 33% dearer — on a margin difference of 0.001 in a field
    the prompt touched only at surface depth.

    Zero for local and unknown-price models, which is what the tier already says
    about them; ordering between those stays the tier's business.
    """
    return blended_rate(spec.cost_input, spec.cost_output)


def _cost_quality_bias() -> float:
    """Operator preference between price and headroom, 0.0-1.0. 0 = cheapest."""
    return max(0, min(100, _settings.get_int("cost_quality_bias"))) / 100.0


def _rank_by_bias(
    pool: list[ModelSpec], requirements: dict[str, float], bias: float
) -> list[ModelSpec]:
    """Order `pool` by a blend of price and headroom, cheapest-leaning at low bias.

    Both axes are normalized against this pool rather than compared raw, because
    they are not remotely commensurable: on a live 323-model qualified pool the
    price spread was $0 to $487.50 per 1M tokens while the margin spread was 0.000
    to 0.300 (margin is bounded by the profiler's 0.98 ceiling minus the bar). A
    raw `price - k * margin` score is therefore a step function — no k changes the
    pick until one suddenly buys the most expensive model in the catalog.

    Price is normalized on a log scale, so the distance from $0.05 to $0.50 counts
    like the distance from $5 to $50. On a linear scale every model under a dollar
    collapses into the same point, which is exactly the range where the
    interesting choices are.
    """
    top_margin = max(_margin(s, requirements) for s in pool)
    top_price = max(_price(s) for s in pool)
    log_ceiling = math.log1p(top_price) or 1.0

    def score(spec: ModelSpec) -> tuple[float, str]:
        cheapness = math.log1p(_price(spec)) / log_ceiling
        shortfall = 1.0 - (_margin(spec, requirements) / top_margin if top_margin else 1.0)
        # `spec.value` keeps the order stable when two models score identically,
        # which is common among the free local models.
        return ((1.0 - bias) * cheapness + bias * shortfall, spec.value)

    return sorted(pool, key=score)


def _margin(spec: ModelSpec, requirements: dict[str, float]) -> float:
    """How much room this model has on its *weakest* required field.

    Negative means unqualified, and its magnitude is how far short it falls on
    the binding constraint. Ranking the unqualified pool by this picks the model
    that is least wrong where it matters most, rather than the one that happens
    to be strong on a field the prompt barely touches.
    """
    if not requirements:
        return 0.0
    return min(field_score(spec, f) - req for f, req in requirements.items())


@dataclass(frozen=True)
class RouteDecision:
    """The chosen model and an honest account of why.

    `qualified` is False when nothing cleared every bar and we fell back to the
    closest available model — the caller surfaces that rather than implying the
    pick was fully competent.
    """
    model: str
    requirements: dict[str, float]
    scores: dict[str, float]        # the chosen model's score on each required field
    qualified: bool
    eligible_count: int             # models that passed the hard filters
    qualified_count: int            # of those, how many cleared every field bar
    slow_excluded: int = 0
    # Models removed by the throughput floor. Same reason as `agentic_excluded`:
    # measured slowness appears in no score, so without this the pick looks
    # arbitrary.
    agentic_excluded: int = 0
    # Models dropped by the tool-loop floor (see taxonomy.AGENTIC_FLOOR). Reported
    # separately from eligible_count because this filter is the one exclusion that
    # leaves no trace in `scores`: the field scores of a model rejected for loop
    # stamina look fine, so without a count the decision reads as if that model
    # was never in the catalog.
    output_deprioritized: int = 0
    # Qualified models that were cheaper but couldn't emit a whole document, so a
    # dearer one was picked. Counted for the same reason as above and one more: this
    # is the only case where the router knowingly declines the cheapest qualified
    # model, and a bill that went up for no visible reason is the kind of thing an
    # operator should be able to read off the decision.

    def shortfalls(self) -> dict[str, tuple[float, float]]:
        """field → (required, actual) for every field the pick falls short on."""
        return {
            f: (req, self.scores.get(f, 0.0))
            for f, req in self.requirements.items()
            if self.scores.get(f, 0.0) < req
        }

    def explain(self) -> str:
        """One line naming the binding constraint, for logs and the UI badge."""
        if self.qualified:
            if not self.requirements:
                return "no capability constraints" + self._agentic_clause()
            binding = min(
                self.requirements,
                key=lambda f: self.scores.get(f, 0.0) - self.requirements[f],
            )
            return (
                f"cheapest of {self.qualified_count} qualified; binding constraint "
                f"{binding} needed {self.requirements[binding]:.2f}, "
                f"has {self.scores.get(binding, 0.0):.2f}"
            ) + self._agentic_clause()
        gaps = ", ".join(
            f"{f} needed {req:.2f}, best available has {actual:.2f}"
            for f, (req, actual) in self.shortfalls().items()
        )
        return (
            f"no model of {self.eligible_count} clears every bar ({gaps})"
            + self._agentic_clause()
        )

    def _agentic_clause(self) -> str:
        """The exclusions that leave no trace in `scores`, appended when there were any."""
        clauses = ""
        if self.agentic_excluded:
            clauses += (
                f"; {self.agentic_excluded} skipped as measured weak at tool loops"
            )
        if self.slow_excluded:
            clauses += f"; {self.slow_excluded} skipped as measured too slow"
        if self.output_deprioritized:
            clauses += (
                f"; {self.output_deprioritized} cheaper but capped too low to "
                "finish a document"
            )
        return clauses


def select(
    store: MatrixStore,
    *,
    profile: PromptProfile,
    needs_tools: bool,
    needs_vision: bool = False,
    needs_structured: bool = False,
    est_tokens: int = 0,
    exclude: set[str] | None = None,
    scope: ModelScope | None = None,
    thresholds: dict[str, float] | None = None,
    agent_mode: bool = False,
) -> RouteDecision:
    """Pick the cheapest model qualified on every field the prompt demands.

    Args:
        store:        MatrixStore implementation to read models from.
        profile:      What the prompt demands (see taxonomy.PromptProfile).
        needs_tools:  If True, exclude models where tools=False.
        needs_vision: If True, exclude models where vision=False.
        needs_structured: If True, exclude models that don't honor a
                      `response_format` json_schema. Set by callers whose reply
                      must fill in a fixed shape rather than read as an answer —
                      the router's own helper calls (see helper_models.py).
        est_tokens:   Estimated prompt size in tokens (0 = skip ctx filter).
        exclude:      Model value strings to skip (e.g. previously rate-limited).
        scope:        Per-user ModelScope; models outside it are ineligible
                      (applies to the fallback pick too).
        thresholds:   Override the reliability threshold (and the legacy
                      per-complexity bars used by route()).
        agent_mode:   If True, also exclude models in SMART_ROUTER_AGENT_DENYLIST
                      (models that pass needs_tools but can't reliably drive the
                      streamed multi-step tool loop).

    Raises RuntimeError only when the matrix has no eligible model at all.
    """
    return _select(
        store.all_models(),
        requirements=profile.requirements(),
        described_as=profile.describe(),
        needs_tools=needs_tools,
        needs_agentic=_needs_agentic(profile, needs_tools, agent_mode),
        needs_vision=needs_vision,
        needs_structured=needs_structured,
        est_tokens=est_tokens,
        min_output=_output_floor(profile),
        exclude=exclude,
        scope=scope,
        thresholds=thresholds,
        agent_mode=agent_mode,
    )


def select_from(
    models: list[ModelSpec],
    *,
    profile: PromptProfile,
    needs_tools: bool = False,
    needs_vision: bool = False,
    needs_structured: bool = False,
    est_tokens: int = 0,
    exclude: set[str] | None = None,
    scope: ModelScope | None = None,
    thresholds: dict[str, float] | None = None,
    agent_mode: bool = False,
) -> RouteDecision:
    """select() against an explicit candidate list instead of a store.

    For callers holding models that aren't (or aren't yet) in the store: the
    profile audit compares a decision made with today's profiles against one made
    with proposed profiles, and both must go through the real selection rules for
    the comparison to mean anything.
    """
    return _select(
        models,
        requirements=profile.requirements(),
        described_as=profile.describe(),
        needs_tools=needs_tools,
        needs_agentic=_needs_agentic(profile, needs_tools, agent_mode),
        needs_vision=needs_vision,
        needs_structured=needs_structured,
        est_tokens=est_tokens,
        min_output=_output_floor(profile),
        exclude=exclude,
        scope=scope,
        thresholds=thresholds,
        agent_mode=agent_mode,
    )


def _select(
    models: list[ModelSpec],
    *,
    requirements: dict[str, float],
    described_as: str,
    needs_tools: bool,
    needs_agentic: bool = False,
    needs_vision: bool = False,
    needs_structured: bool = False,
    est_tokens: int = 0,
    min_output: int = 0,
    exclude: set[str] | None = None,
    scope: ModelScope | None = None,
    thresholds: dict[str, float] | None = None,
    agent_mode: bool = False,
) -> RouteDecision:
    """The matcher, against an explicit {field: minimum score} map.

    Split out from select() so the legacy (domain, complexity) path can supply a
    caller-tuned bar directly instead of one derived from a depth label.

    Takes the candidate models rather than a store: this is a pure function of
    (models, requirements), which is what lets the profile audit replay past
    decisions against a hypothetical set of profiles and see which ones would
    flip — no store, no mutation, no second copy of the selection rules to drift.
    """
    thr = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    min_rel: float = thr.get("min_reliability", 0.70)
    min_agentic: float = thr.get("min_agentic", AGENTIC_FLOOR)
    _exclude = exclude or set()
    _deny = _denylisted()
    _agent_deny = _agent_denylisted() if agent_mode else ()
    min_tps: float = max(0.0, float(_settings.get_int("min_tokens_per_second")))
    assumed_local_tps: float = max(0.0, float(_settings.get_int("assumed_local_tps")))
    stale_days: int = max(0, _settings.get_int("tps_staleness_days"))
    # Stored as a percentage in settings (a floor of "70" reads better than "0.7"
    # in the UI) and compared against the 0..1 average.
    min_tool_health: float = max(0.0, _settings.get_int("min_tool_health") / 100.0)
    agentic_excluded = 0
    slow_excluded = 0
    output_deprioritized = 0

    def _drives_loops(spec: ModelSpec) -> bool:
        """Whether this model may be handed a multi-step tool task.

        Two independent readings, because they come from different places and one
        of them cannot see most of the catalog:

        `spec.agentic` is the profiler's benchmark index. 0.0 means never measured,
        not incapable — two thirds of the catalog and **every local model** are in
        that position — so an unmeasured model is admitted and judged on its other
        fields. That exemption is deliberate, but on its own it was a hole exactly
        where it hurt: the models nobody benchmarks are the ones most likely to fail
        a tool loop, so the filter removed measured-weak cloud models and then
        handed the turn to an unmeasured local one because it was free.

        `spec.observed_tool_health` closes it with evidence this deployment
        gathered itself — the share of tool-bearing turns the model answered with
        anything at all. Same 0.0-means-unmeasured convention, same expiry, and off
        entirely until the operator sets a floor.
        """
        if spec.agentic > 0.0 and spec.agentic < min_agentic:
            return False
        if min_tool_health > 0.0:
            health = spec.observed_tool_health
            if health > 0.0 and not _stale(spec.observed_tool_health_at):
                return health >= min_tool_health
        return True

    def _stale(measured_at: str) -> bool:
        """Whether a measurement is too old to route on.

        An unparseable or missing stamp counts as stale: those are rows written
        before the column existed, and the safe reading of a figure whose age we
        cannot establish is to go and take a fresh one.
        """
        if stale_days <= 0:
            return False
        try:
            when = datetime.fromisoformat(measured_at)
        except ValueError:
            return True
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - when).days >= stale_days

    def _known_tps(spec: ModelSpec) -> float:
        """Best tokens/sec estimate available for this model, 0.0 = no idea.

        A fresh measurement always beats the assumption, so `assumed_local_tps`
        decides the first call to a local model and then steps aside. It steps back
        in if that measurement ages out, which is the behavior you want across a
        hardware change: the stale figures expire and the prior covers the gap
        until new ones form.

        The assumption is offered for local models alone because that is the one
        place a prior is defensible without measuring: local decode is bounded by
        the host's memory bandwidth, which is an order of magnitude under a
        datacenter GPU's, and that gap is a property of the machine rather than of
        any particular weights. There is deliberately no size-derived estimate on
        top of it — parameter count looks like it should predict speed and doesn't,
        since an MoE reads only its active experts per token (a 30B-A3B decodes
        roughly like a 3B), so a formula would be most confidently wrong exactly
        where it mattered.
        """
        if spec.observed_tps > 0.0 and not _stale(spec.observed_tps_at):
            return spec.observed_tps
        if assumed_local_tps > 0.0 and spec.provider == "ollama":
            return assumed_local_tps
        return 0.0

    def _fast_enough(spec: ModelSpec) -> bool:
        """Whether this model is too slow to be worth routing to.

        Same convention as `_drives_loops`: an unknown rate passes. That is what
        makes the floor self-populating rather than a chicken-and-egg problem — an
        unmeasured model stays reachable, gets picked on price, and measures itself
        on the way through. Only models with evidence against them are removed, and
        a demoted model is one good run away from coming back (see _TPS_ALPHA).
        """
        tps = _known_tps(spec)
        return tps <= 0.0 or tps >= min_tps

    def _eligible(spec: ModelSpec) -> bool:
        nonlocal agentic_excluded, slow_excluded
        if spec.value in _exclude:
            return False
        if _deny and any(d in spec.value.lower() for d in _deny):
            return False
        if _agent_deny and any(d in spec.value.lower() for d in _agent_deny):
            return False
        if scope is not None and not scope.permits(spec):
            return False
        if spec.reliability < min_rel:
            return False
        if needs_tools and not spec.tools:
            return False
        if needs_agentic and not _drives_loops(spec):
            # Counted, not just dropped: this filter is new and invisible in the
            # scores, so a caller left wondering why a model it expected did not
            # win — or why nothing was eligible — needs it named.
            agentic_excluded += 1
            return False
        if min_tps > 0 and not _fast_enough(spec):
            # Counted for the same reason as the agentic floor: a model missing
            # from the pool for a reason that appears in no score is otherwise
            # indistinguishable from one that simply lost on price.
            slow_excluded += 1
            return False
        if needs_vision and not spec.vision:
            return False
        if needs_structured and not spec.structured_outputs:
            return False
        if est_tokens > 0 and spec.ctx_k > 0 and est_tokens > spec.ctx_k * 1000:
            return False
        return True

    eligible = [spec for spec in models if _eligible(spec)]
    if not eligible:
        raise RuntimeError(
            f"route: no eligible model for {described_as}, "
            f"needs_tools={needs_tools}"
            + (
                f" ({agentic_excluded} excluded as measured below the "
                f"{min_agentic:.2f} tool-loop floor)"
                if agentic_excluded
                else ""
            )
            # Same reason the tool-loop floor names itself here: a configured floor
            # that empties the pool is not a catalog problem, and "run sync()" sends
            # the operator to fix something that isn't broken.
            + (
                f" ({slow_excluded} excluded as slower than the "
                f"{min_tps:.0f} tokens/sec floor)"
                if slow_excluded
                else ""
            )
            + ". Run sync() to populate the matrix."
        )

    def _decision(spec: ModelSpec, qualified: bool, qualified_count: int) -> RouteDecision:
        return RouteDecision(
            model=spec.value,
            requirements=dict(requirements),
            scores={f: field_score(spec, f) for f in requirements},
            qualified=qualified,
            eligible_count=len(eligible),
            qualified_count=qualified_count,
            agentic_excluded=agentic_excluded,
            slow_excluded=slow_excluded,
            output_deprioritized=output_deprioritized,
        )

    # Qualified = clears the bar on EVERY named field. Cheapest wins — by tier,
    # then by real blended price within the tier (see _price) — and only models
    # that cost the same are separated by how comfortably they clear, rather than
    # a field score deciding which of two different prices gets paid.
    qualified = [spec for spec in eligible if _margin(spec, requirements) >= 0]
    if qualified:
        # For a document, prefer a model that can actually emit one. A ranking
        # preference rather than a filter, and deliberately so: it costs money —
        # the cramped models are the cheap ones — and a request must never fail
        # because nothing roomy qualified. An unknown ceiling (max_output == 0,
        # every local model) counts as roomy, since the alternative is excluding
        # models on no evidence.
        roomy = qualified
        if min_output > 0:
            spacious = [
                s for s in qualified
                if s.max_output <= 0 or s.max_output >= min_output
            ]
            if spacious:
                output_deprioritized = len(qualified) - len(spacious)
                roomy = spacious
        # Bias 0 keeps the tier-first order exactly, so a deployment that never
        # touches the setting sees no change: the blend below ranks on real price
        # and would reorder within a tier even at bias 0.
        bias = _cost_quality_bias()
        if bias <= 0:
            roomy.sort(key=lambda s: (s.cost, _price(s), -_margin(s, requirements), s.value))
        else:
            roomy = _rank_by_bias(roomy, requirements, bias)
        return _decision(roomy[0], True, len(qualified))

    # Nothing is genuinely qualified. Take the model that falls shortest on the
    # binding field — cost is only a tiebreak here, because at this point the
    # question is capability, not price.
    eligible.sort(key=lambda s: (-_margin(s, requirements), s.cost, _price(s), s.value))
    return _decision(eligible[0], False, 0)


def route(
    store: MatrixStore,
    domain: str,
    complexity: str,
    *,
    needs_tools: bool,
    needs_vision: bool = False,
    est_tokens: int = 0,
    exclude: set[str] | None = None,
    scope: ModelScope | None = None,
    thresholds: dict[str, float] | None = None,
    agent_mode: bool = False,
    profile: PromptProfile | None = None,
) -> str:
    """Return the model string for these hints — the legacy string-only entry point.

    Kept so existing callers (the /route API and anything that only has the old
    labels) work unchanged. Pass `profile` to route on a full prompt profile;
    otherwise the (domain, complexity) pair is adapted to a single-field profile
    whose bar is the historical per-complexity threshold, reproducing the old
    behavior exactly.

    Callers that want to explain the decision should use select() instead.
    """
    if profile is not None:
        return select(
            store,
            profile=profile,
            needs_tools=needs_tools,
            needs_vision=needs_vision,
            est_tokens=est_tokens,
            exclude=exclude,
            scope=scope,
            thresholds=thresholds,
            agent_mode=agent_mode,
        ).model

    # Legacy path: one field, and the bar comes from the caller's thresholds dict
    # rather than from a depth label, so a deployment that tuned `thresholds`
    # keeps its tuning and routes exactly as it did before profiles existed.
    thr = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    field = profile_from_labels(domain, complexity).primary_field()
    bar = thr.get(complexity, thr.get("moderate", 0.70))
    return _select(
        store.all_models(),
        requirements={field: bar},
        described_as=f"{domain}/{complexity} ({field} >= {bar:.2f})",
        needs_tools=needs_tools,
        # The legacy pair carries no demands to read, so tool use is the only
        # signal available. The floor still applies: this path's promise is that
        # the *bar* is unchanged, not that a model measured unable to finish a
        # tool loop should keep receiving tool traffic.
        needs_agentic=needs_tools or agent_mode,
        needs_vision=needs_vision,
        est_tokens=est_tokens,
        exclude=exclude,
        scope=scope,
        thresholds=thresholds,
        agent_mode=agent_mode,
    ).model
