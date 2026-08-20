"""
LLM model profiling — optional refinement of the deterministic model profiles.

What this fixes
───────────────
profiler.py scores every model on the taxonomy from measured benchmarks plus a
~200-char catalog blurb read through a fixed cue table. The *level* it produces
is solid — it comes from real measurements. The *shape* is only as good as the
blurb: a description reading "flagship model for enterprise workloads" matches no
cue at all, so the model gets a flat profile and looks equally competent at
software engineering and clinical medicine. Flat profiles are exactly what
profile routing exists to eliminate, because the router picks the cheapest model
that clears every bar and a flat profile clears bars it hasn't earned.

A model that knows the model landscape can say what the blurb doesn't: that
qwen3-coder is superb at code and should not be answering questions about drug
interactions.

Shape, not numbers
──────────────────
This asks for a *relative rating* per field (profiler.RATINGS), never an absolute
score. Asked for 16 numbers between 0 and 1, models return 0.8-0.9 for
everything; asked whether a model is unusually strong, ordinary, or bad at a
field relative to itself, they answer usefully. The ratings become signed offsets
on the benchmark-derived baseline, so measurement owns the level and judgment
owns the shape.

The ratings are what gets stored (see ModelSpec.profile_ratings), so a later sync
with fresh benchmarks re-levels the profile automatically without calling any LLM
again.

Bounded and inspectable
───────────────────────
The catalog runs to hundreds of models, so a run is capped (`limit`), skips
already-rated models by default (`only_missing`), and works **cheapest first** —
cost-ascending is the order the router itself considers models, so an
overstated cheap model is the one that does damage. Every run can be a `dry_run`,
which computes the new profiles and reports what would change without writing.
Each rating carries a one-line note from the rater, shown in the models UI: an
adjustment nobody can inspect is one nobody should trust.

Failure contract, same as llm_classifier.py: nothing here raises on a provider
problem. A model that can't be rated is left exactly as sync profiled it.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import dataclass, field

import httpx

from smart_ai_router import overhead as _overhead
from smart_ai_router import settings as _settings
from smart_ai_router.models import ModelSpec
from smart_ai_router.profiler import (
    RATINGS,
    RATING_KEYS,
    apply_ratings,
    baseline_profile,
    legacy_competence,
)
from smart_ai_router.taxonomy import FIELDS, FIELD_KEYS

# Every field is `required` and additionalProperties is False — strict mode
# demands it, and it also removes the failure mode where a model rates three
# fields and silently leaves thirteen unjudged. As in llm_classifier, no
# minItems/maxItems/minLength: OpenAI strict mode rejects those keywords, and
# length limits are enforced in normalization instead.
_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "model_profile_ratings",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                **{
                    field_name: {"type": "string", "enum": list(RATING_KEYS)}
                    for field_name in FIELD_KEYS
                },
                "note": {"type": "string"},
            },
            "required": [*FIELD_KEYS, "note"],
            "additionalProperties": False,
        },
    },
}

# Longer read budget than the classifier's: this is a 17-key reply from a large
# model, and unlike classification it is off the request path entirely — nothing
# is waiting on it but the admin who pressed the button.
_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=5.0, pool=60.0)

# How many models to rate at once. Small on purpose: a Refine run is a burst of
# otherwise-unprompted traffic against a shared provider account, and tripping a
# rate limit would fail the models at the end of a long run for no reason.
_CONCURRENCY = 4

_RATING_LINES = "\n".join(
    f"  - {name}: {desc}" for name, (_offset, desc) in RATINGS.items()
)
_FIELD_LINES = "\n".join(
    f"  - {key}: {label}" for key, (label, _legacy) in FIELDS.items()
)

_SYSTEM_PROMPT = (
    "You assess large language models for a router that picks the cheapest model "
    "genuinely qualified to answer a given prompt. For the model described "
    "below, rate its ability in each field RELATIVE TO ITS OWN OVERALL "
    "CAPABILITY.\n"
    "\n"
    "Relative is the whole point. Do not tell us how strong the model is — its "
    "benchmark scores already told us that. Tell us where it deviates from its "
    "own level: which fields it was built for, and which it should be kept away "
    "from.\n"
    "\n"
    f"Ratings:\n{_RATING_LINES}\n"
    "\n"
    f"Fields:\n{_FIELD_LINES}\n"
    "\n"
    "Guidance:\n"
    "  - `capable` is the correct answer for most fields of most models. A "
    "general-purpose model should be `capable` nearly everywhere; that is what "
    "general-purpose means.\n"
    "  - Reserve `specialty` for a genuine headline capability the vendor built "
    "and markets, not for 'probably quite good at'.\n"
    "  - Use `weak` and `unsuited` where a narrow or small model would produce "
    "confident, wrong, unverifiable answers — a code-tuned model on clinical "
    "medicine or regulatory law, an uncensored roleplay merge on anything "
    "factual, a tiny model on specialist professional work.\n"
    "  - Judge the specific model named, not its family. Size and tuning matter: "
    "a 3B instruct model and a 400B frontier model from one vendor deserve very "
    "different shapes.\n"
    "  - If you do not recognize the model, do not guess a shape from its name "
    "alone: rate everything `capable` and say so in the note.\n"
    "\n"
    "note: one sentence, under 200 characters, naming what drove the shape. Say "
    "plainly if you are unfamiliar with the model."
)


@dataclass
class EnrichResult:
    """Outcome of one enrichment run, for the API response and the UI."""

    considered: int = 0          # models that matched the candidate filters
    rated: int = 0               # models the rater returned a usable reply for
    changed: int = 0             # of those, how many moved at least one score
    failed: int = 0              # models whose rating call failed (left as-is)
    written: int = 0             # rows actually upserted (0 when dry_run)
    dry_run: bool = False
    rater: str = ""              # the model that did the rating
    rater_why: str = ""
    # Why that model. Reported because the rater is now chosen by routing rather
    # than typed into settings: a run that reshapes profiles across the catalog
    # should say which model reshaped them and on what grounds, or the next admin
    # to read the report has no way to judge it.
    errors: list[str] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    # Per-model detail: {"model", "note", "ratings" (only the non-`capable`
    # entries), "shifts" {field: [before, after]}}. This is what makes a dry run
    # useful — the admin sees the proposed shape before anything is stored.
    rated_specs: list[ModelSpec] = field(default_factory=list)
    # The rated specs themselves, whether or not they were written. Not part of
    # as_dict() — this is for the caller, so a dry run can hand the *proposed*
    # model set to profile_audit and answer "what would this change about
    # routing?" without persisting anything first.

    def as_dict(self) -> dict:
        return {
            "considered": self.considered,
            "rated": self.rated,
            "changed": self.changed,
            "failed": self.failed,
            "written": self.written,
            "dry_run": self.dry_run,
            "rater": self.rater,
            "rater_why": self.rater_why,
            "errors": list(self.errors),
            "changes": list(self.changes),
        }


def profiler_limit() -> int:
    """Default ceiling on models per run. UI-managed (Settings page)."""
    return max(1, _settings.get_int("model_profiler_limit"))


def normalize_ratings(raw: object) -> tuple[dict[str, str], str]:
    """Extract (ratings, note) from a rater reply. ({}, "") if nothing usable.

    Drops out-of-vocabulary fields and ratings rather than trusting them, and
    omits `capable` entries entirely — `capable` means "no adjustment", so
    storing it would bloat every row with the default and make a genuinely
    unremarkable model indistinguishable from an unrated one.
    """
    if not isinstance(raw, dict):
        return {}, ""
    ratings: dict[str, str] = {}
    for key, val in raw.items():
        if key not in FIELDS:
            continue
        rating = str(val).strip().lower()
        if rating in RATINGS and RATINGS[rating][0] != 0.0:
            ratings[key] = rating
    note = str(raw.get("note") or "").strip()[:240]
    return ratings, note


def _parse(text: str) -> tuple[dict[str, str], str]:
    """Fence-and-prose-tolerant parse of a rater reply into (ratings, note)."""
    if not text:
        return {}, ""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}, ""
    try:
        obj = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return {}, ""
    return normalize_ratings(obj)


def _describe_model(spec: ModelSpec) -> str:
    """The user-message half of the rating call: what we know about this model.

    Includes the benchmark-derived baseline so the rater is judging deviation
    from a level it can see, which is what "relative to its own capability"
    asks of it — and includes the vendor description because that is the one
    piece of ground truth about intent.
    """
    base = baseline_profile(spec)
    lines = [f"Model id: {spec.value}"]
    if spec.provider:
        lines.append(f"Provider: {spec.provider}")
    if spec.description:
        lines.append(f"Vendor description: {spec.description}")
    if base:
        general = base.get("general_knowledge")
        if general is not None:
            lines.append(
                f"Measured overall capability (0-1, from benchmarks): {general:.2f}"
            )
    lines.append(
        "Rate this model's ability in each field relative to that overall level."
    )
    return "\n".join(lines)


async def rate_model(
    spec: ModelSpec,
    *,
    base_url: str,
    model: str = "",
    api_key: str = "",
) -> tuple[dict[str, str], str] | None:
    """Ask the rater to judge one model's shape. None on any failure.

    A `None` here means "leave this model's profile exactly as sync computed it",
    which is a safe outcome: the deterministic profile is what the router has been
    using all along.

    `model` is the rater to use, and it is the caller's to choose: which model
    rates is a routing decision now (helper_models.PROFILER), and routing needs
    both the store and the provider config, neither of which belongs here.
    """
    rater = model
    if not rater or not base_url:
        return None

    payload = {
        "model": rater,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _describe_model(spec)},
        ],
        "stream": False,
        "temperature": 0,
        "response_format": _RESPONSE_FORMAT,
        # 16 short enum values plus a one-sentence note. Generous because a
        # truncated reply parses as nothing at all — and on a reasoning-capable
        # rater this budget covers thinking tokens too.
        "max_tokens": 1200,
    }
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    url = f"{base_url.rstrip('/')}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            return None
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return None
    # A Refine run is the router's largest single burst of self-directed spend —
    # one call per model — so it is logged as usage like anything else. Noted
    # before the parse: an unusable reply is billed the same as a usable one.
    _overhead.note(_overhead.PROFILE, model=rater, usage=data.get("usage"))

    ratings, note = _parse(content)
    # An all-`capable` reply is a real answer ("nothing unusual about this
    # model"), and worth recording so the run doesn't re-ask next time. It is
    # only a failure if we couldn't parse a note either.
    if not ratings and not note:
        return None
    return ratings, note


def enrichment_candidates(
    models: list[ModelSpec],
    *,
    only_missing: bool = True,
    limit: int = 0,
) -> list[ModelSpec]:
    """Which models this run should rate, in the order it should rate them.

    Cheapest first, deliberately. The router selects the cheapest model that
    clears every bar, so a cheap model with an overstated profile wins prompts it
    has no business answering, while an overstated expensive one mostly just sits
    there. Bounded runs should therefore fix the cheap end first.

    Models with no deterministic profile at all (legacy rows awaiting a sync) are
    skipped: there is no baseline for a rating to adjust, and inventing one from
    an LLM's opinion of the name is exactly the guessing this system replaced.

    `only_missing` keys on the note as well as the ratings, because "nothing
    unusual about this model" is a legitimate verdict that stores zero ratings.
    Keying on ratings alone would re-ask about every general-purpose model on
    every run — paying repeatedly for the same answer.
    """
    pool = [
        spec for spec in models
        if baseline_profile(spec)
        and not (only_missing and (spec.profile_ratings or spec.profile_note))
    ]
    pool.sort(key=lambda s: (s.cost, s.value))
    return pool[:limit] if limit > 0 else pool


def _rated_spec(spec: ModelSpec, ratings: dict[str, str], note: str) -> ModelSpec:
    """A copy of `spec` with the rating overlay applied and its legacy vector
    re-derived, ready to upsert."""
    base = baseline_profile(spec)
    effective = apply_ratings(base, ratings)
    return dataclasses.replace(
        spec,
        profile=effective,
        competence=legacy_competence(effective),
        profile_rules=base if effective != base else {},
        profile_ratings=ratings,
        profile_note=note,
    )


def _change_entry(spec: ModelSpec, rated: ModelSpec) -> dict:
    """Human-readable diff of what the ratings did to this model.

    Shifts are measured against the *deterministic baseline*, not against the
    model's current effective profile: on a re-rate those differ, and what the
    admin needs to see is the whole effect of the LLM's judgment, not the delta
    from a previous judgment they are replacing anyway.
    """
    base = baseline_profile(spec)
    shifts = {
        field_name: [round(base[field_name], 3), round(score, 3)]
        for field_name, score in rated.profile.items()
        if abs(score - base.get(field_name, score)) > 1e-9
    }
    return {
        "model": spec.value,
        "note": rated.profile_note,
        "ratings": dict(rated.profile_ratings),
        "shifts": shifts,
    }


async def enrich_models(
    store,
    models: list[ModelSpec],
    *,
    base_url: str,
    api_key: str = "",
    model: str = "",
    rater_why: str = "",
    only_missing: bool = True,
    limit: int = 0,
    dry_run: bool = False,
) -> EnrichResult:
    """Rate a bounded batch of models and persist the results.

    Args:
        store:        MatrixStore to upsert into. Untouched when dry_run.
        models:       The candidate pool (typically store.all_models()).
        base_url:     OpenAI-compatible base URL for the rater.
        api_key:      Bearer token for that endpoint.
        model:        The rater. Chosen by the caller (see helper_models.PROFILER);
                      "" means none is available and no run happens.
        rater_why:    Why that rater, for the run's own report.
        only_missing: Skip models that already carry ratings (resumable runs).
        limit:        Ceiling on models rated; 0 means the configured default.
        dry_run:      Compute and report changes without writing anything.

    Never raises for a provider failure — failures are counted and the affected
    models keep the profiles sync gave them.
    """
    rater = model
    result = EnrichResult(dry_run=dry_run, rater=rater, rater_why=rater_why)
    if not rater:
        result.errors.append(
            "no model profiler available (Settings → Model profiling)"
        )
        return result
    if not base_url:
        result.errors.append("no OpenRouter provider configured for the profiler")
        return result

    candidates = enrichment_candidates(
        models, only_missing=only_missing, limit=limit or profiler_limit()
    )
    result.considered = len(candidates)
    if not candidates:
        return result

    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(spec: ModelSpec):
        async with sem:
            return spec, await rate_model(
                spec, base_url=base_url, model=rater, api_key=api_key
            )

    for spec, outcome in await asyncio.gather(*(_one(s) for s in candidates)):
        if outcome is None:
            result.failed += 1
            result.errors.append(f"{spec.value}: no usable rating")
            continue
        ratings, note = outcome
        result.rated += 1
        rated = _rated_spec(spec, ratings, note)
        result.rated_specs.append(rated)
        if rated.profile != spec.profile:
            result.changed += 1
            result.changes.append(_change_entry(spec, rated))
        if not dry_run:
            store.upsert_model(rated)
            result.written += 1

    return result
