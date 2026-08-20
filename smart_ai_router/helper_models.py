"""
Which model the router uses for its **own** calls.

Why this module exists
──────────────────────
The router's thesis is "pick the cheapest model genuinely qualified for this
work". Its own internal calls used to opt out of it: the second-pass prompt
profiler and the model profiler were pinned to hand-typed model names in
settings, dispatched straight at a hardcoded provider base URL. That meant the
one part of the system nobody was routing was the part the router runs itself —
so the model denylist didn't apply to it, the reliability floor didn't apply to
it, a retired model name failed silently instead of re-routing, and nothing
picked a cheaper qualified model when one appeared.

A helper call is an unusually *easy* thing to route, because it is a known
workload: we know exactly what the profiler is being asked to do every time. So
there is nothing to classify — each task carries a hand-written PromptProfile,
and the ordinary selection rules do the rest.

Three states, not two
─────────────────────
Each task's setting is still a single string, and it now means one of:

  "auto"     route it — the default, and the reason this module exists
  ""         disabled — do not make this call at all (unchanged meaning)
  <a name>   pinned to that exact model (unchanged meaning, kept as the escape
             hatch for "the router's pick is wrong and I need it fixed now")

A pin is returned verbatim, including a bare provider-side id like
`openai/gpt-5.6-luna`, which api.proxy._resolve_provider() still resolves to
OpenRouter via its unknown-prefix fall-through — so an existing pinned
deployment behaves exactly as it did.

Why these profiles
──────────────────
Both tasks name one field, `general_knowledge`, because neither is domain work:
the refine pass judges *what a prompt would demand* across any subject, and the
model profiler judges *what a model is for*. Depth is where they differ, and it
is the only real knob:

  refine   frontier    — this pass exists only for prompts already headed to the
                         top tier, and its entire justification is being better
                         than a 3B model at the judgment that decides that. A
                         model that doesn't clear the top bar has nothing to add.
  profiler specialist  — needs broad knowledge of the model landscape, but it
                         runs as a bounded batch of one call per model, so a
                         frontier bar would make a 40-model run cost real money
                         for a judgment specialist depth already covers.

Measured on the live 356-model catalog at the time of the change, both resolve to
`openrouter/qwen/qwen3-235b-a22b-2507` (cost tier 1, general_knowledge 0.950) —
cheaper than the `openai/gpt-5.6-luna` both were pinned to (tier 2, 0.901) and
higher-scoring on the profiles the router already trusts for user traffic. Worth
stating plainly: that pick is only as good as those profiles, and the profiler's
own pick being chosen by profiles the profiler exists to correct is a real
circularity. It is bounded by the same things that bound any Refine run — the
dry run, the audit, and the pin.

Why triage is not a HelperTask
─────────────────────────────
The obvious third entry here is the *triage* classifier — the local call that
profiles every prompt — and it is deliberately absent. It stays a pinned model
name (`classifier_model`, which refuses `auto` for this reason). Routing it was
tried against the live catalog and is measurably worse than pinning:

  • Routing means "cheapest model that clears the bar". Every local candidate is
    free — nine ollama rows, all cost tier 0, all reliability 1.0, all flagged
    structured_outputs — so cost discriminates nothing and the sort falls through
    to its next key, competence margin. Triage therefore routes to the *biggest*
    local model, which is the opposite of what a hot-path JSON call wants.
  • On the live host that pick was `qwen3:30b-a3b` (general_knowledge 0.82, the
    highest of the nine). Measured with scripts/bakeoff_classifier.py, that model
    emits a usable profile on **0 of 32** attempts: it thinks, and finish_reason
    comes back `length` before any JSON appears. Routing triage would hand every
    request's profiling to a model that cannot profile, and because the chain
    degrades silently, nothing would look broken.
  • Excluding reasoning models doesn't rescue it. The next pick is
    `qwen2.5:3b-instruct` (0.78), ranked above `llama3.1:8b` (0.76) — the exact
    inverse of the bakeoff, where the 3B misses 2 escalations to the 8B's 0 and
    names the right field 19 times to its 26.

The reason is structural, not a scoring bug: competence measures what a model
knows, and triage fitness is "does it emit strict JSON inside a 256-token budget
without thinking first". Those are different properties, and only the second one
decides whether the call works at all. Until a *measured* triage-fitness signal
exists in the catalog, the honest configuration is a name someone benchmarked,
and the bakeoff script is how that name gets chosen.

Failure contract
────────────────
resolve() returns None rather than raising, for any reason a call can't be made:
disabled, an empty catalog, or nothing qualified. Both callers treat None as
"skip this helper", which degrades a routing decision or a profiling run but
never fails a request — the same contract llm_classifier.py and llm_profiler.py
already hold.
"""
from __future__ import annotations

from dataclasses import dataclass

from smart_ai_router import settings as _settings
from smart_ai_router.taxonomy import DomainNeed, PromptProfile

# Setting value that means "let the router decide". A word rather than an empty
# string because empty already means "disabled", and the two need to stay
# distinguishable: one is a policy, the other is an off switch.
AUTO = "auto"


@dataclass(frozen=True)
class HelperTask:
    """One internal LLM call the router makes, and what it demands of a model."""

    key: str            # settings key holding "auto" | "" | a pinned model name
    label: str          # how this call is identified in headers and logs
    profile: PromptProfile
    purpose: str        # one line, used in the "why this model" explanation


@dataclass(frozen=True)
class HelperChoice:
    """The model a helper call will use, and why it is that one."""

    model: str          # provider-prefixed value when routed; verbatim when pinned
    why: str
    pinned: bool = False


# The second-pass prompt profiler (llm_classifier's refine call).
REFINE = HelperTask(
    key="classifier_refine_model",
    label="llm-refined",
    profile=PromptProfile(domains=(DomainNeed("general_knowledge", "frontier"),)),
    purpose="re-profile a prompt a small classifier read as consequential",
)

# The model profiler (llm_profiler's per-model rating call).
PROFILER = HelperTask(
    key="model_profiler_model",
    label="model-profiler",
    profile=PromptProfile(domains=(DomainNeed("general_knowledge", "specialist"),)),
    purpose="judge what a catalog model is good and bad at",
)


def resolve(task: HelperTask, cr) -> HelperChoice | None:
    """The model to use for `task` right now, or None to skip the call.

    Args:
        task: which internal call this is.
        cr:   the SmartRouter facade — used for its select(), so a helper call is
              subject to the same denylist, reliability floor, and thresholds as
              user traffic.

    Only *qualified* picks are returned. select() falls back to the closest
    available model when nothing clears every bar, which is right for a user
    request — an answer from an imperfect model beats no answer — and wrong here:
    a refine pass that doesn't out-rank the triage model it is second-guessing,
    or a rater that isn't strong enough to judge a model's shape, is worse than
    not making the call, because it costs money to make the decision worse.
    """
    setting = _settings.get_str(task.key).strip()
    if not setting:
        return None
    if setting.lower() != AUTO:
        return HelperChoice(model=setting, why="pinned in settings", pinned=True)

    try:
        decision = cr.select(task.profile, needs_structured=True)
    except RuntimeError:
        # Empty or fully-excluded catalog. Not an error worth propagating: the
        # deployment has bigger problems than an unprofiled model, and every
        # caller here is optional by design.
        return None
    if not decision.qualified:
        return None
    # Both halves of the reason, because a routed pick is reported to whoever has
    # to judge the run: what the call was for, and why this model got it.
    return HelperChoice(model=decision.model, why=f"{task.purpose}: {decision.explain()}")
