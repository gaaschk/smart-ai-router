"""
LLM-based prompt classifier — primary classification path.

Asks a small, fast local model (default: an Ollama model) to build a
taxonomy.PromptProfile: which fields the prompt reaches into, how deep into each,
what it demands, and what is at stake. This is more robust than keyword matching
for prompts whose vocabulary isn't in the deterministic classifier's hint sets
(e.g. a physics derivation that never uses the word "reasoning").

Two-speed classification
────────────────────────
A small local model on the hot path of every request is the right tool for "this
is a coding question at practitioner depth" and the wrong tool for judging whether a
multi-jurisdiction regulatory analysis needs specialist or frontier depth in law
— and that second judgment is the expensive one to get wrong in either
direction. So triage runs on the small local model, and only when it reports
something consequential (high stakes, two or more specialist-depth fields, or any
frontier depth) does a stronger model re-profile the prompt. The refine call
fires only on prompts already headed for an expensive model, so its marginal cost
is negligible; everything else is classified locally and routed immediately.

Design contract: nothing here raises, and nothing blocks the request for long. On
any failure — disabled, network error, timeout, malformed output, an
out-of-vocabulary label — the function returns None and the caller falls back to
the next target and ultimately to the deterministic classifier in classifier.py.
Classification must never be the reason a request fails.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from smart_ai_router import overhead as _overhead
from smart_ai_router import settings as _settings
from smart_ai_router.taxonomy import (
    DEMAND_KEYS,
    DEPTH_KEYS,
    DEPTH_RANK,
    FIELD_KEYS,
    STAKES_KEYS,
    PromptProfile,
    normalize_profile,
)

# Strict structured-output schema, derived from taxonomy.py so the two can't
# drift. This is the real guard: a plain json_object response_format forces
# *valid JSON* but not the right *shape* — a chatty model handed "write me a
# paper" will happily emit a valid {"title":..., "abstract":...} object and blow
# past max_tokens, yielding no parseable profile. Constraining to an enum schema
# makes the model physically unable to answer the prompt; it can only fill in the
# profile. Honored by Ollama's OpenAI-compatible endpoint and by OpenRouter
# (OpenAI Structured Outputs).
#
# Deliberately no minItems/maxItems/uniqueItems: OpenAI strict mode rejects those
# keywords outright, which would 400 every refine call. The list length cap and
# de-duplication happen in taxonomy.normalize_profile() instead, which has to be
# defensive anyway for models that only approximate the schema.
_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "prompt_profile",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "domains": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string", "enum": list(FIELD_KEYS)},
                            "depth": {"type": "string", "enum": list(DEPTH_KEYS)},
                        },
                        "required": ["field", "depth"],
                        "additionalProperties": False,
                    },
                },
                "demands": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(DEMAND_KEYS)},
                },
                "stakes": {"type": "string", "enum": list(STAKES_KEYS)},
            },
            "required": ["domains", "demands", "stakes"],
            "additionalProperties": False,
        },
    },
}

# Classifier models are UI-managed (see settings.py "classifier_model" /
# "classifier_fallback", which hold the canonical defaults). Design notes:
#   - primary: small + fast is the priority — classification is a trivial task
#     on the hot path of every request, so avoid "thinking" models. Empty
#     disables the LLM path entirely → always fall back.
#   - fallback: a small, free OpenRouter model tried when the local model
#     fails/times out, before giving up to the keyword classifier. Free-tier is
#     rate-limited and sends prompts off-box, so it's a resilience backstop, not
#     the primary. Empty disables the fallback.

# Read budget covers a cold model load: Ollama unloads an idle model after a
# few minutes, and the first request then pays a one-time load cost (~8s for an
# 8B model on Apple silicon). Warm calls return in well under a second. A slow
# or truly hung model still degrades to the deterministic fallback rather than
# stalling the request indefinitely.
_TIMEOUT = httpx.Timeout(connect=3.0, read=20.0, write=3.0, pool=20.0)

_SYSTEM_PROMPT = (
    "You profile prompts for an LLM router. Do NOT answer the user's prompt. "
    "Decide what answering it *correctly* would require, and reply with ONLY a "
    "compact JSON object.\n"
    '{"domains": [{"field": <field>, "depth": <depth>}, ...], '
    '"demands": [<demand>, ...], "stakes": <stakes>}\n'
    "\n"
    "domains: 1-3 fields the answer genuinely requires expertise in, most "
    "important first. Name a field only if a weakness there would make the "
    "answer wrong — not merely because the topic is mentioned. Use "
    "general_knowledge for casual questions, chit-chat, and simple lookups.\n"
    f"Allowed fields: {', '.join(FIELD_KEYS)}\n"
    "\n"
    "depth: how far into that field the answer must go.\n"
    "  - surface: any well-informed generalist gets this right (definitions, "
    "summaries, simple how-tos)\n"
    "  - practitioner: needs someone who does this work daily (idiomatic code, "
    "standard multi-step analysis)\n"
    "  - specialist: needs years of focused expertise — non-obvious edge cases, "
    "specific named statutes/standards/APIs, domain formalism\n"
    "  - frontier: at the limit of published expertise — novel synthesis, "
    "questions experts genuinely disagree on, or reasoning that must hold across "
    "many interacting specialist constraints at once\n"
    "\n"
    "demands (include only those that apply):\n"
    "  - factual_precision: the answer must state real specifics exactly — "
    "statutes, standards, citations, API signatures, dosages. Include this "
    "whenever a confident wrong specific would be worse than 'I don't know'.\n"
    "  - quantitative: requires numeric derivation or estimation, not prose\n"
    "  - long_synthesis: must integrate many sources into one coherent artifact\n"
    "  - agentic: requires multi-step tool use to complete\n"
    "\n"
    "stakes: high if someone could act on this to their real harm (medical, "
    "legal, financial, safety-critical); medium for professional work that will "
    "actually be used; low for casual or exploratory questions.\n"
    "\n"
    "Judge what a correct answer demands, not the prompt's topic or its length. "
    "One short sentence can require frontier depth; a long request to reformat "
    "text requires almost none. Do not inflate: most prompts are practitioner "
    "depth or below in a single field."
)

# Appended for the second pass. The refine model is told what triage guessed
# because the expensive errors are systematic — a small model reads topic words
# ("nuclear", "jurisdictions") as depth — and naming the suspected bias is more
# useful than asking for an unanchored re-profile.
_REFINE_SUFFIX = (
    "\n\nA small local classifier profiled this prompt as:\n{triage}\n"
    "It escalated to you because that profile is consequential. Small "
    "classifiers systematically over-read topic vocabulary as depth and "
    "under-read prompts whose difficulty is implicit. Re-profile the prompt "
    "yourself and emit your own answer; agree with the triage only if it is "
    "right. Lowering depth is as valid a correction as raising it."
)


@dataclass(frozen=True)
class ClassifierTarget:
    """One step in the classifier fallback chain."""
    model: str
    base_url: str
    api_key: str = ""
    label: str = "llm"  # reported via X-Classifier, e.g. "llm" or "llm-free"


def classifier_model() -> str:
    """The configured primary classifier model, or "" if disabled. UI-managed
    (Settings page) with SMART_ROUTER_CLASSIFIER_MODEL as env fallback."""
    return _settings.get_str("classifier_model").strip()


def classifier_fallback_model() -> str:
    """The configured free/remote fallback classifier model, or "" if disabled.
    UI-managed (Settings page) with SMART_ROUTER_CLASSIFIER_FALLBACK as env
    fallback."""
    return _settings.get_str("classifier_fallback").strip()


def _parse_profile(text: str) -> PromptProfile | None:
    """Parse a model reply into a validated PromptProfile, or None.

    Tolerant of code fences and surrounding prose: extracts the outermost {...}
    block, then hands the object to taxonomy.normalize_profile(), which drops
    out-of-vocabulary labels and returns None if nothing usable survives.
    """
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    return normalize_profile(obj)


async def classify_profile_llm(
    prompt: str,
    *,
    base_url: str,
    model: str | None = None,
    api_key: str = "",
    system_prompt: str | None = None,
    kind: str = _overhead.CLASSIFY,
) -> PromptProfile | None:
    """Profile a prompt via an OpenAI-compatible LLM. None on any failure.

    Args:
        prompt:        The user prompt text to profile.
        base_url:      OpenAI-compatible base URL (e.g. "http://host:11434/v1"
                       for Ollama, or "https://openrouter.ai/api/v1").
        model:         Override the configured classifier model. If None, uses
                       classifier_model(); if that is "", the LLM path is off.
        api_key:       Bearer token for the endpoint (required for OpenRouter,
                       unused for local Ollama).
        system_prompt: Override the rubric — used by the refine pass to append
                       the triage profile it is checking.
        kind:          How this call is attributed in the usage log — triage or
                       refine. Two passes at very different prices run through
                       this one function, and a single "classifier" line would
                       hide which of them the money went to.
    """
    if not prompt or not prompt.strip():
        return None
    mdl = model if model is not None else classifier_model()
    if not mdl or not base_url:
        return None

    payload = {
        "model": mdl,
        "messages": [
            {"role": "system", "content": system_prompt or _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "temperature": 0,
        # Constrain the reply to exactly the profile shape — see
        # _RESPONSE_FORMAT. Without a *schema* (a bare json_object, or nothing),
        # chatty instruct models answer the prompt instead of profiling it and
        # the chain silently falls through to the keyword classifier.
        "response_format": _RESPONSE_FORMAT,
        # Room for three {field, depth} objects plus demands; the profile is a
        # few dozen tokens, and truncated JSON parses as nothing at all.
        "max_tokens": 256,
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
    # Billed whether or not the reply parses, so it is noted before the parse.
    _overhead.note(kind, model=mdl, usage=data.get("usage"))
    return _parse_profile(content)


async def classify_profile_chain(
    prompt: str,
    targets: list[ClassifierTarget],
) -> tuple[PromptProfile, str] | None:
    """Try each target in order; return the first (profile, label) that works.

    None if every target fails, so the caller can fall back to the deterministic
    keyword classifier. Targets are tried sequentially: the fast local model is
    preferred and remote calls only happen when it fails.
    """
    for t in targets:
        profile = await classify_profile_llm(
            prompt, base_url=t.base_url, model=t.model, api_key=t.api_key
        )
        if profile is not None:
            return profile, t.label
    return None


def needs_refinement(profile: PromptProfile) -> bool:
    """Whether this profile is too consequential to trust to the triage model.

    The three triggers are the judgments where a small classifier's error is
    expensive in one direction or the other:
      - high stakes: someone may act on the answer to their real harm
      - two or more specialist-depth fields: cross-domain synthesis, where
        generalists produce their most plausible nonsense
      - any frontier depth: a claim that only the priciest tier can answer

    Everything else routes on the local profile with no second call. Note this is
    evaluated on the *triage* profile, so a prompt the local model reads as
    ordinary never pays for refinement — the cost of that miss is a cheaper model
    than ideal, which is the failure mode the router is allowed to have.
    """
    return (
        profile.stakes == "high"
        or profile.deep_field_count() >= 2
        or any(DEPTH_RANK.get(d.depth, 0) >= DEPTH_RANK["frontier"] for d in profile.domains)
    )


async def classify_profile_two_speed(
    prompt: str,
    targets: list[ClassifierTarget],
    refine: Callable[[], ClassifierTarget | None] | None = None,
) -> tuple[PromptProfile, str] | None:
    """Profile a prompt, escalating to a stronger model only when it matters.

    Returns (profile, label) where label reports which path produced the answer
    ("llm", "llm-free", or the refine target's label), or None if every attempt
    failed and the caller should use the deterministic classifier.

    `refine` is a *factory*, not a target: which model refines is now a routing
    decision (see helper_models.py), so resolving it reads the model catalog. The
    refine pass fires on a small minority of prompts, so nothing is resolved
    until one actually escalates — and callers, which build their arguments up
    front on every request, don't pay for a decision that usually isn't made.

    The refine pass is strictly an improvement attempt: if it fails for any
    reason — no model available, no key, network error, malformed reply — the
    triage profile is returned as-is. A classifier upgrade must never be able to
    turn a routable request into a failed one.
    """
    triaged = await classify_profile_chain(prompt, targets)
    if triaged is None:
        return None
    profile, label = triaged
    if refine is None or not needs_refinement(profile):
        return profile, label
    target = refine()
    if target is None or not target.model:
        return profile, label

    refined = await classify_profile_llm(
        prompt,
        base_url=target.base_url,
        model=target.model,
        api_key=target.api_key,
        system_prompt=_SYSTEM_PROMPT + _REFINE_SUFFIX.format(triage=profile.describe()),
        kind=_overhead.CLASSIFY_REFINE,
    )
    if refined is None:
        return profile, label
    return refined, target.label


async def classify_llm(
    prompt: str,
    *,
    base_url: str,
    model: str | None = None,
    api_key: str = "",
) -> tuple[str, str] | None:
    """Legacy (domain, complexity) classification. None on any failure.

    Derived from the profile rather than asked for separately, so the two can
    never disagree and there is only one rubric to maintain. Kept for callers
    that speak only the old vocabulary (the bakeoff script, the /route API).
    """
    profile = await classify_profile_llm(
        prompt, base_url=base_url, model=model, api_key=api_key
    )
    return profile.legacy_labels() if profile is not None else None


async def classify_chain(
    prompt: str,
    targets: list[ClassifierTarget],
) -> tuple[str, str, str] | None:
    """Legacy (domain, complexity, label) chain — see classify_profile_chain."""
    result = await classify_profile_chain(prompt, targets)
    if result is None:
        return None
    profile, label = result
    domain, complexity = profile.legacy_labels()
    return domain, complexity, label
