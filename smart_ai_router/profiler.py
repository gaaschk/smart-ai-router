"""
Model profiling — score each model per taxonomy field, at sync time.

This is the model-side half of profile routing. taxonomy.py says what a prompt
needs; this says what each model was actually trained to do, on the same axes,
so route() can compare them.

Where the signal comes from, best first
───────────────────────────────────────
1. **Measured benchmarks.** The OpenRouter catalog carries
   `benchmarks.artificial_analysis` = {intelligence_index, coding_index,
   agentic_index} for a good share of models. These are real measurements that
   update as the catalog does, which is strictly better than guessing capability
   from a model's name — and they are the only reason a newly-released model gets
   ranked correctly without a code change here.
2. **The catalog description.** ~200 chars of "what this model is for"
   ("flagship-level Agentic Coding model", "designed for advanced reasoning,
   coding, and agent workflows"). This is the *specialization* signal: it says
   which fields the vendor tuned for, which is exactly what "trained to best
   respond to this prompt" means.
3. **Name priors.** competence.infer_competence(), the previous system. Still the
   fallback for local Ollama models and Bedrock, which ship no benchmarks or
   descriptions at all, and for catalog entries with no benchmark data.

The mechanism that matters
──────────────────────────
A narrow specialist gets *discounted* on the professional fields it does not
advertise. Without that, a cheap coding model with a high coding_index would keep
winning multi-domain prompts on price — the exact failure this system exists to
fix. A coding specialist is excellent at software_engineering and is not the
model you want reasoning about drug interactions or jurisdictional conflicts, and
its profile now says so.

Conversely, models whose description advertises breadth take no discount, so
general frontier models remain the only things that clear a specialist bar on an
unadvertised field like law_regulatory. That is the correct answer, not an
accident: the models that know regulatory law are the big general ones.

What is *not* a field
─────────────────────
`agentic_index` measures whether a model can hold a multi-step tool loop
together. That is not knowledge about a subject, so it is not a taxonomy field —
it is a separate axis on the spec (ModelSpec.agentic), produced by
agentic_level() here and consumed by the router when the request actually
involves tool use. See that function for what happened when it was folded into a
field instead.
"""
from __future__ import annotations

import re

from smart_ai_router.competence import infer_competence
from smart_ai_router.taxonomy import FIELDS

# ── Capability level from measured benchmarks ──────────────────────────────────
# artificial_analysis.intelligence_index → our 0-1 scale.
#
# A piecewise-linear anchor table rather than a formula, because the index is
# compressed at the top (the best model in the catalog scores ~63 of a nominal
# 100, and nothing ever approaches 100) while our depth bars are not. Anchors let
# each mapping point be justified and re-tuned against observed routing without
# refitting a curve.
#
# Calibrated so the resulting scores populate taxonomy.DEPTHS sensibly:
#   specialist (0.85) → roughly the top third of benchmarked models
#   frontier   (0.93) → roughly the top tenth
# Spot checks against the live catalog at time of writing: opus-5 (63.1) → 0.955,
# opus-4.8 (57.3) → 0.930, sonnet-5 (55.3) → 0.917, sonnet-4.6 (48.4) → 0.880,
# gpt-5.1 (37.5) → 0.813, haiku-4.5 (29.9) → 0.759, gemma-3-27b (7.4) → 0.485.
_INTELLIGENCE_ANCHORS: tuple[tuple[float, float], ...] = (
    (0.0, 0.30),
    (10.0, 0.55),
    (20.0, 0.66),
    (30.0, 0.76),
    (40.0, 0.83),
    (45.0, 0.86),
    (50.0, 0.89),
    (55.0, 0.915),
    (58.0, 0.935),
    (63.0, 0.955),
    (70.0, 0.97),
)


def _interpolate(x: float, anchors: tuple[tuple[float, float], ...]) -> float:
    """Piecewise-linear lookup, clamped at both ends."""
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            span = x1 - x0
            if span <= 0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / span
    return anchors[-1][1]


# coding_index tracks intelligence_index at roughly this ratio across the
# catalog, so the *residual* — not the raw value — is what says "unusually strong
# at code for its overall level". Deliberately a mild nudge: the two indices
# correlate tightly enough that treating coding_index as independent evidence
# would overstate what it adds.
_CODING_RATIO = 1.30
_CODING_RESIDUAL_SCALE = 0.006
_CODING_RESIDUAL_CAP = 0.05

# agentic_index collapses much faster than intelligence for weaker models (a
# model at index 30 scores ~17 agentic; at index 10, ~1). That makes it a real
# discriminator for tool-driving work rather than a restatement of intelligence,
# so it maps on its own anchors.
_AGENTIC_ANCHORS: tuple[tuple[float, float], ...] = (
    (0.0, 0.20),
    (10.0, 0.50),
    (20.0, 0.66),
    (30.0, 0.76),
    (40.0, 0.84),
    (50.0, 0.90),
    (60.0, 0.95),
)

# ── Description cues ──────────────────────────────────────────────────────────
# Substrings that mean "the vendor tuned this for <field>". Matched against the
# description plus the model name, lowercased.
#
# Kept tight on purpose: a false cue grants competence the model may not have.
# Fields absent here (law, medicine, finance) are almost never advertised — those
# scores stay driven by the measured capability level, which is the honest
# answer, since it is the broad frontier models that carry that knowledge.
_CUES: dict[str, tuple[str, ...]] = {
    "software_engineering": (
        "coding", "code", "coder", "software engineering", "programming",
        "swe-bench", "developer", "pull request", "bug finding", "code review",
    ),
    # No bare "workflow" — "agent workflows" appears in a large share of
    # descriptions and would credit architecture depth to nearly everything.
    "systems_architecture": (
        "long-horizon", "enterprise-grade", "orchestrat", "system design",
        "architecture design", "distributed system",
    ),
    "math_formal": (
        "math", "mathematic", "aime", "olympiad", "theorem", "formal reasoning",
        "logical reasoning",
    ),
    "data_analysis": (
        "data analysis", "analytics", "tabular", "structured data",
        "quantitative",
    ),
    "natural_science": ("science", "scientific", "gpqa", "physics", "chemistry", "biolog"),
    "medicine_health": ("medical", "clinical", "biomedical", "healthcare", "medicine"),
    "law_regulatory": ("legal", "law", "compliance", "regulatory", "contract"),
    "finance_business": ("financ", "trading", "accounting", "business analysis"),
    "humanities_social": ("humanities", "history", "philosoph", "social science"),
    "creative_writing": (
        "creative writing", "creative", "roleplay", "role-play", "storytelling",
        "fiction", "narrative", "prose", "character",
    ),
    "technical_writing": ("documentation", "technical writing", "summariz"),
    "education_explanation": ("tutor", "teaching", "educational", "explain"),
    "translation_multilingual": (
        "multilingual", "translation", "translate", "languages",
    ),
    # Never a bare "design": "designed for advanced reasoning" appears in a large
    # fraction of descriptions and would make every model a design specialist.
    "product_design": (
        "product design", "ui design", "user interface", "ux", "front-end",
        "frontend", "web design",
    ),
    "operations_process": ("agentic", "agent workflows", "tool use", "tool-use", "automation"),
    "general_knowledge": ("general-purpose", "general purpose", "versatile"),
}

# A model that advertises a cue for a field is credited above its measured level:
# the vendor tuned for it and the benchmarks may not capture that.
_CUE_BOOST = 0.04

# ── Specialization ────────────────────────────────────────────────────────────
# Cues that mark a *narrow* model — one built for one job. Only these trigger the
# discount, because "agentic" or "reasoning" appearing in a description says
# nothing about narrowness (nearly every model claims both).
_NARROW_MARKERS: dict[str, tuple[str, ...]] = {
    "software_engineering": ("coder", "-code", "code model", "coding model", "codestral", "starcoder"),
    "creative_writing": ("roleplay", "role-play", "storytelling", "companion", "uncensored"),
    "math_formal": ("math-", "-math", "prover", "theorem"),
    "translation_multilingual": ("translation model", "translate-"),
    "medicine_health": ("medgemma", "medical model", "-med-", "biomed"),
}

# Note on what is deliberately *not* here: an earlier cut waived the discount for
# models whose description named many fields, on the theory that breadth signals
# a generalist. Measured against the live catalog, no model names more than four
# fields and the ones that come closest are code specialists — so the waiver
# could only ever have fired on exactly the models it was meant to spare. The
# narrow markers below are name-shaped instead, which is what actually
# distinguishes "qwen3-coder" from "a general model that mentions coding".

# Professional fields a narrow specialist is discounted on. Excludes the fields
# any competent model can attempt (general knowledge, explanation, writing) — the
# discount is about not trusting a code model with a regulatory question, not
# about pretending it cannot form sentences.
_DISCOUNTED_FIELDS = frozenset({
    "law_regulatory", "medicine_health", "finance_business",
    "natural_science", "humanities_social", "data_analysis",
    "systems_architecture", "product_design",
})

# Size of that discount. Tuned so a narrow specialist at frontier capability
# still clears `practitioner` on unrelated professional fields but not
# `specialist` — it can help, it cannot be the sole answer.
_SPECIALIST_DISCOUNT = 0.12

# Thinking models get a small credit on the fields where step-by-step work is
# what actually decides correctness. Tapered by remaining headroom: at the top of
# the scale every capable model reasons (and intelligence_index already measured
# it with thinking enabled), so an untapered bump would double-count and could
# push science above coding for a model whose own description is all about code.
_REASONING_BUMP = 0.03
_REASONING_FIELDS = frozenset({"math_formal", "data_analysis", "natural_science"})
_REASONING_TAPER_FROM = 0.10  # headroom below _MAX_SCORE over which it fades out

_MIN_SCORE, _MAX_SCORE = 0.10, 0.98


def _reasoning_credit(score: float) -> float:
    """Thinking-support credit for `score`, faded out near the ceiling."""
    headroom = max(0.0, _MAX_SCORE - score)
    return _REASONING_BUMP * min(1.0, headroom / _REASONING_TAPER_FROM)


def _cue_fields(text: str) -> set[str]:
    """Fields the text advertises."""
    return {
        field for field, cues in _CUES.items()
        if any(cue in text for cue in cues)
    }


def _narrow_specialty(text: str) -> str | None:
    """The field this model is narrowly built for, or None if it is general."""
    for field, markers in _NARROW_MARKERS.items():
        if any(marker in text for marker in markers):
            return field
    return None


def _prior_fields(model_value: str) -> dict[str, float]:
    """Per-field scores from name patterns alone — the no-benchmark fallback.

    Expands the legacy 4-score competence vector across the taxonomy via each
    field's legacy domain, so Ollama and Bedrock models (which ship no catalog
    metadata) land on the same scale as benchmarked ones.
    """
    legacy = infer_competence(model_value)
    return {
        field: legacy.get(legacy_domain, legacy.get("general", 0.70))
        for field, (_label, legacy_domain) in FIELDS.items()
    }


def profile_model(
    model_value: str,
    *,
    description: str = "",
    intelligence_index: float | None = None,
    coding_index: float | None = None,
    supports_reasoning: bool = False,
) -> dict[str, float]:
    """Score `model_value` on every taxonomy field (0-1).

    Every argument beyond the model value is optional: a model with no catalog
    metadata at all still gets a complete profile from name priors, so no caller
    has to special-case a provider.

    Takes no `agentic_index`: that signal is not knowledge about a field, and
    belongs to agentic_level() instead.
    """
    priors = _prior_fields(model_value)

    if intelligence_index is not None:
        base = _interpolate(float(intelligence_index), _INTELLIGENCE_ANCHORS)
    else:
        # No measurement — anchor on the name prior's general score.
        base = priors.get("general_knowledge", 0.70)

    text = f"{model_value} {description}".lower()
    # Collapse version punctuation so "qwen3-coder" and "qwen3.coder" both match.
    text = re.sub(r"[_/]+", "-", text)

    cues = _cue_fields(text)
    specialty = _narrow_specialty(text)

    coding_residual = 0.0
    if intelligence_index is not None and coding_index is not None:
        residual = float(coding_index) - _CODING_RATIO * float(intelligence_index)
        coding_residual = max(
            -_CODING_RESIDUAL_CAP,
            min(_CODING_RESIDUAL_CAP, residual * _CODING_RESIDUAL_SCALE),
        )

    profile: dict[str, float] = {}
    for field in FIELDS:
        # A measurement beats a guess: when benchmarks exist the level comes from
        # them alone. Without them, each field starts at its own name prior
        # rather than a single blended number, so the shape the prior encodes
        # (a coder model's coding/docs gap) survives.
        score = base if intelligence_index is not None else priors[field]

        if field in cues:
            score += _CUE_BOOST
        if specialty is not None and field != specialty and field in _DISCOUNTED_FIELDS:
            score -= _SPECIALIST_DISCOUNT
        if field == "software_engineering":
            score += coding_residual
        if supports_reasoning and field in _REASONING_FIELDS:
            score += _reasoning_credit(score)

        profile[field] = round(max(_MIN_SCORE, min(_MAX_SCORE, score)), 4)

    return profile


def agentic_level(agentic_index: float | None) -> float:
    """Measured ability to hold a multi-step tool loop together, 0-1.

    Returns 0.0 for "never measured", which every consumer must read as *unknown*
    rather than *zero*: only ~a third of the catalog carries this index, and no
    local Ollama or Bedrock model carries it at all.

    This used to be written into the `operations_process` field instead, as an
    override that replaced whatever the level and cues had produced. Two things
    were wrong with that. It conflated skills: `operations_process` is knowledge
    about operations and process work — runbooks, workflow design — which a model
    can do in one shot, while this index measures loop stamina. And because that
    field is one of two feeding the legacy `general` column, an agentic benchmark
    silently became half of every model's reported "general competence" —
    claude-haiku-4.5's 0.68 was mean(0.604 agentic, 0.759 knowledge).

    Measured on the live catalog at the time of the change: 118 of 347 models
    carried the index, and the override dented that one field on every one of
    them — by a median of 0.133 and up to 0.630. Worst case was gpt-4o-mini,
    which carries an agentic_index but *no* intelligence_index, so 15 fields were
    name-prior guesses near 0.86 while this one was a measurement at 0.23.
    """
    if agentic_index is None:
        return 0.0
    return round(_interpolate(float(agentic_index), _AGENTIC_ANCHORS), 4)


# ── LLM-judged shape ──────────────────────────────────────────────────────────
# The deterministic profile above gets the *level* right — it comes from measured
# benchmarks — and gets the *shape* only roughly, because all it can read is a
# ~200-char blurb through a fixed cue table. A description that says "flagship
# model for enterprise workloads" tells the cue table nothing, while a model that
# knows the catalog can say plainly that qwen3-coder should not be answering
# questions about drug interactions.
#
# So the LLM pass supplies shape, not numbers. It rates each field *relative to
# the model's own general level*, and those ratings become signed offsets on the
# benchmark-derived baseline. Two reasons this beats asking for 16 absolute
# scores:
#
#   1. Calibration. Asked for absolute 0-1 scores, models emit 0.8-0.9 for
#      everything, which would flatten the profile and put us back where PR 1
#      started — every model clearing every bar.
#   2. Durability. The judgment "this coder is weak at medicine" stays true when
#      the catalog publishes a new intelligence_index next week. Storing ratings
#      rather than composed numbers means a re-sync re-levels the profile for
#      free, with no second LLM call.
#
# Magnitudes are set against the DEPTHS ladder (practitioner 0.68 → specialist
# 0.85 → frontier 0.93): `weak` costs a model more than the specialist→frontier
# gap, `unsuited` costs more than a full tier, and `specialty` is deliberately the
# same size as _CUE_BOOST because it is the same claim ("the vendor tuned for
# this") from a better reader.
RATINGS: dict[str, tuple[float, str]] = {
    "specialty": (0.04, "purpose-built or explicitly tuned for this; a headline capability"),
    "capable":   (0.00, "handles this about as well as its overall level suggests"),
    "weak":      (-0.10, "noticeably worse at this than its overall level suggests"),
    "unsuited":  (-0.20, "should not be relied on for this at all"),
}

RATING_KEYS = tuple(RATINGS)

# What a field gets when the rater didn't mention it. `capable` means "no
# adjustment", so a partial reply degrades to the rules profile for the fields it
# skipped instead of scoring them 0.
DEFAULT_RATING = "capable"


def apply_ratings(
    baseline: dict[str, float], ratings: dict[str, str]
) -> dict[str, float]:
    """Compose LLM shape ratings onto a deterministic baseline profile.

    Returns the baseline unchanged when there are no ratings, or when the
    baseline is empty — a model with no profile to adjust (a legacy row routing
    on its competence columns) must not end up with a fabricated one.
    """
    if not ratings or not baseline:
        return dict(baseline)
    out: dict[str, float] = {}
    for field_name, score in baseline.items():
        offset = RATINGS.get(ratings.get(field_name, DEFAULT_RATING), (0.0, ""))[0]
        out[field_name] = round(
            max(_MIN_SCORE, min(_MAX_SCORE, score + offset)), 4
        )
    return out


def baseline_profile(spec) -> dict[str, float]:
    """The deterministic profile behind a spec, before any LLM adjustment.

    `profile_rules` is populated only when an LLM overlay actually changed
    `profile`, so an un-rated model round-trips through the store byte-identical
    to how sync wrote it. Callers that need the baseline — the enricher, so it
    never stacks new ratings on top of old ones, and the audit, so it can show
    what changed — go through here rather than reading either field directly.
    """
    return spec.profile_rules or spec.profile


def legacy_competence(profile: dict[str, float]) -> dict[str, float]:
    """Collapse a field profile back to {coding, docs, reasoning, general}.

    The old 4 columns are still read by the /route API, the model-matrix UI, and
    any external caller, so they must keep working — and they must be *derived*
    from the profile rather than tracked separately, or the two would drift and
    the dashboard would describe a decision the router never made.
    """
    buckets: dict[str, list[float]] = {}
    for field, (_label, legacy_domain) in FIELDS.items():
        if field in profile:
            buckets.setdefault(legacy_domain, []).append(profile[field])
    out: dict[str, float] = {}
    for domain in ("coding", "docs", "reasoning", "general"):
        vals = buckets.get(domain) or []
        out[domain] = round(sum(vals) / len(vals), 4) if vals else 0.70
    # `coding` has exactly one contributing field, so it is already exact.
    return out


def extract_catalog_signals(entry: dict) -> dict:
    """Pull the profiling inputs out of one OpenRouter catalog entry.

    Isolated from the sync loop so the field names OpenRouter uses are asserted
    in one place, and a catalog shape change fails here rather than silently
    producing flat profiles for the whole catalog.
    """
    benchmarks = entry.get("benchmarks") or {}
    aa = benchmarks.get("artificial_analysis") or {}
    if not isinstance(aa, dict):
        aa = {}

    def _num(key: str) -> float | None:
        val = aa.get(key)
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    reasoning = entry.get("reasoning") or {}
    supports_reasoning = bool(
        (isinstance(reasoning, dict) and reasoning.get("supported_efforts"))
        or "reasoning" in (entry.get("supported_parameters") or [])
    )

    return {
        "description": str(entry.get("description") or ""),
        "intelligence_index": _num("intelligence_index"),
        "coding_index": _num("coding_index"),
        "agentic_index": _num("agentic_index"),
        "supports_reasoning": supports_reasoning,
    }
