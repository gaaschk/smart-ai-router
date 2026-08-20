"""helper_models.resolve() — which model the router uses for its own calls.

The router's claim is that it picks the cheapest genuinely qualified model. These
tests pin the part of that claim that applies to the router itself: its two
internal LLM calls (the classifier's refine pass and the model profiler) are
routed like any prompt, subject to the same denylist and floors, with a pin left
as the escape hatch and an off switch that stays an off switch.
"""
from __future__ import annotations

from smart_ai_router import helper_models
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.taxonomy import FIELD_KEYS

_TASKS = (helper_models.REFINE, helper_models.PROFILER)


def _spec(value: str, cost: int, *, score: float = 0.95,
          structured: bool = True, **kw) -> ModelSpec:
    return ModelSpec(
        value=value, provider="openrouter", cost=cost, reliability=1.0,
        structured_outputs=structured, profile={f: score for f in FIELD_KEYS},
        **kw,
    )


def _cr(*specs: ModelSpec) -> CapabilityRouter:
    store = SqliteStore(":memory:")
    for spec in specs:
        store.upsert_model(spec)
    return CapabilityRouter(store=store)


def _env(task: helper_models.HelperTask) -> str:
    from smart_ai_router.settings import _BY_KEY

    return _BY_KEY[task.key].env


# ── The three states of the setting ───────────────────────────────────────────

def test_auto_routes_to_the_cheapest_qualified_model(monkeypatch):
    """The whole point: a cheaper model that clears the bar wins, without anyone
    editing a setting — including a model that only appeared on the last sync."""
    for task in _TASKS:
        monkeypatch.setenv(_env(task), "auto")
    cr = _cr(_spec("openrouter/cheap", 1), _spec("openrouter/pricey", 9))
    for task in _TASKS:
        choice = helper_models.resolve(task, cr)
        assert choice is not None
        assert choice.model == "openrouter/cheap"
        assert choice.pinned is False
        # The reason names both the task and the binding constraint, because the
        # profiler reports it and a pick nobody can explain is one nobody trusts.
        assert task.purpose in choice.why
        assert "general_knowledge" in choice.why


def test_an_empty_setting_still_means_off(monkeypatch):
    """Unchanged meaning, and load-bearing: a deployment that turned the refine
    pass off to stop spending money must not have routing turn it back on."""
    for task in _TASKS:
        monkeypatch.setenv(_env(task), "")
    cr = _cr(_spec("openrouter/cheap", 1))
    assert all(helper_models.resolve(task, cr) is None for task in _TASKS)


def test_a_pin_is_returned_verbatim_and_never_routed(monkeypatch):
    """The escape hatch for "the router's pick is wrong, fix it now". Returned
    exactly as typed — a bare provider-side id resolves through proxy's
    unknown-prefix fall-through, so a previously pinned deployment is unchanged."""
    for task in _TASKS:
        monkeypatch.setenv(_env(task), "openai/gpt-5.6-luna")
    cr = _cr(_spec("openrouter/cheap", 1))
    for task in _TASKS:
        choice = helper_models.resolve(task, cr)
        assert choice is not None
        assert (choice.model, choice.pinned) == ("openai/gpt-5.6-luna", True)


def test_a_pinned_model_absent_from_the_catalog_is_still_honored(monkeypatch):
    """A pin bypasses selection entirely, so it works on an empty catalog — which
    is what makes it usable as a recovery lever when routing itself is the
    problem."""
    monkeypatch.setenv(_env(helper_models.PROFILER), "some/unsynced-model")
    choice = helper_models.resolve(helper_models.PROFILER, _cr())
    assert choice is not None and choice.model == "some/unsynced-model"


def test_auto_is_case_insensitive(monkeypatch):
    monkeypatch.setenv(_env(helper_models.PROFILER), "  Auto ")
    choice = helper_models.resolve(helper_models.PROFILER, _cr(_spec("openrouter/c", 1)))
    assert choice is not None and choice.pinned is False


# ── When routing can't produce a usable answer ────────────────────────────────

def test_an_empty_catalog_resolves_to_none(monkeypatch):
    """select() raises on an empty matrix. A helper call is optional by design, so
    that becomes "skip", not a failed request."""
    monkeypatch.setenv(_env(helper_models.PROFILER), "auto")
    assert helper_models.resolve(helper_models.PROFILER, _cr()) is None


def test_a_model_that_ignores_a_schema_is_not_offered_one(monkeypatch):
    """Both helper calls constrain the reply to a json_schema, and a model that
    accepts response_format while ignoring the schema answers the prompt instead
    of filling in the shape — which parses as nothing at all."""
    monkeypatch.setenv(_env(helper_models.PROFILER), "auto")
    cr = _cr(_spec("openrouter/prose", 1, structured=False))
    assert helper_models.resolve(helper_models.PROFILER, cr) is None


def test_an_unqualified_closest_miss_is_refused(monkeypatch):
    """select() falls back to the nearest available model for a user request,
    because some answer beats none. Here it is the opposite: a rater too weak to
    judge a model, or a refine pass no better than the triage model it second-
    guesses, spends money to make the decision worse."""
    monkeypatch.setenv(_env(helper_models.PROFILER), "auto")
    cr = _cr(_spec("openrouter/mid", 1, score=0.70))
    decision = cr.select(helper_models.PROFILER.profile)
    assert decision.model == "openrouter/mid" and not decision.qualified
    assert helper_models.resolve(helper_models.PROFILER, cr) is None


def test_the_refine_pass_demands_more_than_the_profiler(monkeypatch):
    """Different depths on purpose. The refine pass only runs for prompts already
    headed to the top tier, so a model that doesn't clear the top bar has nothing
    to add; the profiler runs one call per model, so a frontier bar would price a
    catalog-wide run out for judgment specialist depth already covers."""
    for task in _TASKS:
        monkeypatch.setenv(_env(task), "auto")
    cr = _cr(_spec("openrouter/strong-but-not-frontier", 1, score=0.90))
    assert helper_models.resolve(helper_models.PROFILER, cr) is not None
    assert helper_models.resolve(helper_models.REFINE, cr) is None


def test_the_denylist_applies_to_the_routers_own_calls(monkeypatch):
    """The concrete thing pinning cost us: an operator-excluded model stayed in
    use for the router's internal calls, because those never went through
    selection."""
    monkeypatch.setenv(_env(helper_models.PROFILER), "auto")
    monkeypatch.setenv("SMART_ROUTER_MODEL_DENYLIST", "banned")
    cr = _cr(_spec("openrouter/banned-cheapie", 1), _spec("openrouter/fine", 9))
    choice = helper_models.resolve(helper_models.PROFILER, cr)
    assert choice is not None and choice.model == "openrouter/fine"
