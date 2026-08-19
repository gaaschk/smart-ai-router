"""
The routing vocabulary — the shared language between "what does this prompt
need" and "what was this model trained to do".

Why this module exists
──────────────────────
Routing used to compare two 4-element vectors: a prompt collapsed to
(domain, complexity) and a model collapsed to {coding, docs, reasoning,
general}. Both sides were too coarse to express the thing that actually decides
model choice. "Analyze the legal, ethical, and technical implications of an
autonomous reactor control system against 48 jurisdictions' regulations" and
"refactor this god class" both classified as reasoning/hard and competed for the
same competence bar — so the cheapest model at 0.88 won both, and the first one
got answered by a model with no regulatory depth at all.

The fix is to name *which* fields a prompt reaches into and *how deep* into each
one, then require a model to clear the bar on **every** named field. A
coding-specialist with a high coding score is disqualified by the law axis
rather than winning on price.

Two vocabularies, deliberately closed
─────────────────────────────────────
FIELDS is a closed set because both sides must be scored on it: a small local
classifier has to pick from it reliably, and sync has to infer it from provider
catalog metadata. An open-ended "subject" string would be unroutable — there
would be nothing to compare it against.

DEPTHS is 4 levels rather than a 0-1 float for the same reason: a 3B model can
choose between four described tiers far more reliably than it can calibrate a
continuous score.
"""
from __future__ import annotations

from dataclasses import dataclass

# ── Fields ────────────────────────────────────────────────────────────────────
# Each entry maps a field to (short label, legacy domain). The legacy domain is
# how this field summarizes into the old coding/docs/reasoning/general vocabulary,
# which is still what the usage log, the /route API, and the dashboard speak.
#
# Coverage rule: every prompt must land somewhere, so `general_knowledge` is the
# residual. Fields are chosen to be distinguishable by a small model from the
# prompt alone AND inferable for a model from its catalog description.
FIELDS: dict[str, tuple[str, str]] = {
    "software_engineering":    ("Software engineering",     "coding"),
    "systems_architecture":    ("Systems architecture",     "reasoning"),
    "data_analysis":           ("Data & statistics",        "reasoning"),
    "math_formal":             ("Math & formal reasoning",  "reasoning"),
    "natural_science":         ("Natural science & eng.",   "reasoning"),
    "medicine_health":         ("Medicine & health",        "reasoning"),
    "law_regulatory":          ("Law & regulatory",         "reasoning"),
    "finance_business":        ("Finance & business",       "reasoning"),
    "humanities_social":       ("Humanities & social sci.", "reasoning"),
    "creative_writing":        ("Creative writing",         "docs"),
    "technical_writing":       ("Technical writing",        "docs"),
    "education_explanation":   ("Teaching & explanation",   "docs"),
    "translation_multilingual": ("Translation & multilingual", "docs"),
    "product_design":          ("Product & design",         "reasoning"),
    "operations_process":      ("Operations & process",     "general"),
    "general_knowledge":       ("General knowledge",        "general"),
}

FIELD_KEYS = tuple(FIELDS)

# ── Depth ─────────────────────────────────────────────────────────────────────
# How far into a field the answer has to go, and the model score that demands.
#
# The numbers are calibrated against the scores the profiler actually emits (see
# profiler.py) so the tiers stay meaningful:
#   surface       any model that can hold a conversation
#   practitioner  excludes the weakest models only
#   specialist    sonnet / gpt-4o class and up
#   frontier      opus / fable class only
DEPTHS: dict[str, float] = {
    "surface":      0.45,
    "practitioner": 0.68,
    "specialist":   0.85,
    "frontier":     0.93,
}

DEPTH_KEYS = tuple(DEPTHS)

# Ordinal for comparisons ("is this at least specialist?").
DEPTH_RANK: dict[str, int] = {name: i for i, name in enumerate(DEPTH_KEYS)}

# ── Demands ───────────────────────────────────────────────────────────────────
# Task properties that raise the bar without naming a new field. Each describes
# the whole request, so its bump applies to every named field.
#
# factual_precision is the heaviest because it is the hallucination axis: a
# prompt that must name real statutes, standards, APIs, or citations punishes a
# weaker model far more than one that only needs coherent prose.
DEMANDS: dict[str, tuple[float, str]] = {
    "factual_precision": (0.05, "must name real statutes/standards/APIs/citations exactly"),
    "quantitative":      (0.03, "requires numeric derivation or estimation, not prose"),
    "long_synthesis":    (0.03, "must integrate many sources into one coherent artifact"),
    "agentic":           (0.02, "requires multi-step tool use"),
}

DEMAND_KEYS = tuple(DEMANDS)

# Consequence of being wrong. High stakes buys a stricter bar, not a different one.
STAKES: dict[str, float] = {"low": 0.0, "medium": 0.02, "high": 0.05}

STAKES_KEYS = tuple(STAKES)

# Cross-domain synthesis is harder than either domain alone — holding two
# specialist frames at once is where generalists start producing plausible
# nonsense. Applied when 2+ named fields are at specialist depth or deeper.
_MULTI_DOMAIN_BUMP = 0.04

# Ceiling on the *sum* of all bumps. Without it, a high-stakes precise prompt at
# specialist depth would demand ~0.97 and escalate to the priciest tier — which
# would quietly undo the router's whole reason to exist. With the cap, the ladder
# is: specialist + every bump == frontier (opus class), frontier + every bump ==
# above any model's score, which deliberately triggers route()'s
# best-available-model escalation path.
_MAX_BUMP = 0.08

# No requirement may exceed this; above it, nothing would ever qualify and the
# escalation path would fire on prompts that a top model handles fine.
_MAX_REQUIREMENT = 0.97

# Legacy complexity summary. The profile is the real signal; these labels exist
# because the usage log, X-Complexity, and the dashboard still speak them. The
# thresholds are read off the requirement scale, so the summary can never
# disagree with the routing decision it describes.
_LEGACY_COMPLEXITY: tuple[tuple[float, str], ...] = (
    (0.55, "trivial"),
    (0.72, "moderate"),
    (0.90, "hard"),
)
_LEGACY_COMPLEXITY_TOP = "expert"


@dataclass(frozen=True)
class DomainNeed:
    """One field the prompt reaches into, and how deep it goes."""
    field: str
    depth: str

    @property
    def label(self) -> str:
        return FIELDS.get(self.field, (self.field, "general"))[0]


@dataclass(frozen=True)
class PromptProfile:
    """What answering this prompt actually demands of a model.

    `domains` is ordered by importance — the first entry is the primary field.
    """
    domains: tuple[DomainNeed, ...]
    demands: frozenset[str] = frozenset()
    stakes: str = "low"

    # ── Derived requirements ──────────────────────────────────────────────────

    def bump(self) -> float:
        """Total addition to every field's bar, from demands + stakes + breadth."""
        total = sum(DEMANDS[d][0] for d in self.demands if d in DEMANDS)
        total += STAKES.get(self.stakes, 0.0)
        if self.deep_field_count() >= 2:
            total += _MULTI_DOMAIN_BUMP
        return min(total, _MAX_BUMP)

    def deep_field_count(self) -> int:
        """How many named fields sit at specialist depth or deeper."""
        floor = DEPTH_RANK["specialist"]
        return sum(1 for d in self.domains if DEPTH_RANK.get(d.depth, 0) >= floor)

    def requirements(self) -> dict[str, float]:
        """field → minimum model score. A model must clear *every* entry.

        This is the whole point of the profile: the nuclear-regulation prompt
        yields {law_regulatory: 0.97, natural_science: 0.93, ...} and a model
        that is brilliant at code but shallow on law fails on the law key.
        """
        bump = self.bump()
        out: dict[str, float] = {}
        for need in self.domains:
            base = DEPTHS.get(need.depth, DEPTHS["practitioner"])
            required = min(base + bump, _MAX_REQUIREMENT)
            # A field named twice takes its strictest requirement.
            out[need.field] = max(out.get(need.field, 0.0), required)
        return out

    def peak_requirement(self) -> float:
        """The binding constraint — the strictest single field requirement."""
        reqs = self.requirements()
        return max(reqs.values()) if reqs else DEPTHS["surface"]

    # ── Summaries ─────────────────────────────────────────────────────────────

    def primary_field(self) -> str:
        return self.domains[0].field if self.domains else "general_knowledge"

    def legacy_labels(self) -> tuple[str, str]:
        """(domain, complexity) in the old vocabulary, for the usage log, the
        X- headers, and the dashboard. Derived, never independently decided."""
        domain = FIELDS.get(self.primary_field(), ("", "general"))[1]
        peak = self.peak_requirement()
        for ceiling, name in _LEGACY_COMPLEXITY:
            if peak < ceiling:
                return domain, name
        return domain, _LEGACY_COMPLEXITY_TOP

    def to_dict(self) -> dict:
        """Serializable form that round-trips through normalize_profile().

        Exists so a routing decision can be *recorded* and replayed later: the
        usage log stores this, and the profile audit rebuilds the exact profile a
        past request routed on to check whether a profiler change would have sent
        it somewhere else. Keys and value vocabularies match the classifier's
        schema, so there is one shape to reason about, not two.
        """
        return {
            "domains": [
                {"field": need.field, "depth": need.depth} for need in self.domains
            ],
            "demands": sorted(self.demands),
            "stakes": self.stakes,
        }

    def describe(self) -> str:
        """One-line human explanation of the demand, for the routing badge and
        the escalation note. Honest about *why* a model was required."""
        parts = [f"{need.label} @ {need.depth}" for need in self.domains]
        text = " + ".join(parts) if parts else "general knowledge"
        extras: list[str] = []
        if self.stakes != "low":
            extras.append(f"{self.stakes} stakes")
        extras.extend(sorted(self.demands & set(DEMAND_KEYS)))
        if extras:
            text += f" ({', '.join(extras)})"
        return text


# ── Parsing / construction ────────────────────────────────────────────────────

# A classifier that names more fields than this is pattern-matching on topic
# words rather than judging the task; the extra entries only inflate the bar.
_MAX_DOMAINS = 3


def normalize_profile(raw: object) -> PromptProfile | None:
    """Build a PromptProfile from loosely-shaped classifier output, or None.

    Tolerant of the ways a small model bends a schema (a bare string instead of
    an object, unknown field names, a missing depth) but never invents a demand
    or a field that wasn't in-vocabulary — an out-of-vocabulary label is dropped,
    and if nothing survives we return None so the caller can fall back.
    """
    if not isinstance(raw, dict):
        return None

    needs: list[DomainNeed] = []
    seen: set[str] = set()
    for entry in raw.get("domains") or []:
        if isinstance(entry, str):
            field, depth = entry, "practitioner"
        elif isinstance(entry, dict):
            field = str(entry.get("field", "")).strip().lower()
            depth = str(entry.get("depth", "practitioner")).strip().lower()
        else:
            continue
        if field not in FIELDS or field in seen:
            continue
        if depth not in DEPTHS:
            depth = "practitioner"
        seen.add(field)
        needs.append(DomainNeed(field=field, depth=depth))
        if len(needs) >= _MAX_DOMAINS:
            break

    if not needs:
        return None

    # Demands arrive either as a list of names or as an object of booleans;
    # accept both, since which shape a small model emits is not reliable.
    raw_demands = raw.get("demands")
    demands: set[str] = set()
    if isinstance(raw_demands, dict):
        demands = {
            k for k, v in raw_demands.items() if k in DEMANDS and bool(v)
        }
    elif isinstance(raw_demands, list):
        demands = {
            str(d).strip().lower() for d in raw_demands
            if str(d).strip().lower() in DEMANDS
        }

    stakes = str(raw.get("stakes", "low")).strip().lower()
    if stakes not in STAKES:
        stakes = "low"

    return PromptProfile(
        domains=tuple(needs), demands=frozenset(demands), stakes=stakes
    )


# Legacy (domain, complexity) → the field we treat as that domain's stand-in.
# Used by callers that still supply only the old pair (the /route endpoint, and
# any client that predates the profile).
_DOMAIN_TO_FIELD: dict[str, str] = {
    "coding": "software_engineering",
    "docs": "technical_writing",
    "reasoning": "math_formal",
    "general": "general_knowledge",
}

# Old complexity labels → depth. "expert" is accepted so a caller can ask for
# the top tier explicitly through the legacy API.
_COMPLEXITY_TO_DEPTH: dict[str, str] = {
    "trivial": "surface",
    "moderate": "practitioner",
    "hard": "specialist",
    "expert": "frontier",
}


def profile_from_labels(domain: str, complexity: str) -> PromptProfile:
    """Adapt a legacy (domain, complexity) pair into a single-field profile.

    Keeps every existing caller working: a one-field profile at the mapped depth
    reproduces the old single-bar behavior exactly.
    """
    field = _DOMAIN_TO_FIELD.get((domain or "").strip().lower(), "general_knowledge")
    depth = _COMPLEXITY_TO_DEPTH.get((complexity or "").strip().lower(), "practitioner")
    return PromptProfile(domains=(DomainNeed(field=field, depth=depth),))
