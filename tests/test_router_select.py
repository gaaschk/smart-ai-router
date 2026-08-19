"""Tests for profile-based routing — select(), RouteDecision, field_score()."""
import pytest

from smart_ai_router.models import ModelSpec
from smart_ai_router.router import field_score, route, select
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.taxonomy import DEPTHS, DomainNeed, PromptProfile


def _store_with(*specs):
    store = SqliteStore(":memory:")
    for s in specs:
        store.upsert_model(s)
    return store


def _p(*needs, demands=(), stakes="low"):
    return PromptProfile(
        domains=tuple(DomainNeed(f, d) for f, d in needs),
        demands=frozenset(demands),
        stakes=stakes,
    )


# A cheap coding specialist and an expensive generalist — the exact pair the old
# router got wrong, because both cleared one 0.88 bar and the cheap one won.
_CODER = ModelSpec(
    "cheap-coder", cost=1, reliability=1.0,
    profile={"software_engineering": 0.95, "law_regulatory": 0.55,
             "natural_science": 0.60, "general_knowledge": 0.70},
)
_FRONTIER = ModelSpec(
    "pricey-frontier", cost=12, reliability=1.0,
    profile={"software_engineering": 0.94, "law_regulatory": 0.95,
             "natural_science": 0.94, "general_knowledge": 0.95},
)


# ── field_score ───────────────────────────────────────────────────────────────

def test_field_score_reads_the_profile():
    assert field_score(_CODER, "law_regulatory") == 0.55


def test_field_score_falls_back_to_legacy_competence():
    # Rows written before profiling existed must stay routable rather than
    # scoring 0 on every field and being excluded entirely.
    legacy = ModelSpec("old-row", competence={"coding": 0.9, "reasoning": 0.8})
    assert field_score(legacy, "software_engineering") == 0.9
    assert field_score(legacy, "law_regulatory") == 0.8  # legacy domain "reasoning"


def test_field_score_of_unknown_field_is_zero():
    assert field_score(ModelSpec("m"), "astrology") == 0.0


def test_partial_profile_falls_back_per_field():
    # A profile missing one key shouldn't make that field score 0 when the legacy
    # vector still has an answer for it.
    spec = ModelSpec(
        "partial", profile={"software_engineering": 0.9}, competence={"reasoning": 0.8}
    )
    assert field_score(spec, "math_formal") == 0.8


# ── The core match ────────────────────────────────────────────────────────────

def test_cheapest_qualified_model_wins():
    store = _store_with(_CODER, _FRONTIER)
    decision = select(
        store, profile=_p(("software_engineering", "specialist")), needs_tools=False
    )
    assert decision.model == "cheap-coder"
    assert decision.qualified is True


def test_every_named_field_must_clear_its_bar():
    # The headline fix: the coder is disqualified by its law score even though its
    # coding score is the best available and it is 12x cheaper.
    store = _store_with(_CODER, _FRONTIER)
    decision = select(
        store,
        profile=_p(("law_regulatory", "specialist"), ("software_engineering", "specialist")),
        needs_tools=False,
    )
    assert decision.model == "pricey-frontier"
    assert decision.qualified is True
    assert decision.qualified_count == 1


def test_binding_constraint_is_the_weakest_field():
    store = _store_with(_FRONTIER)
    decision = select(
        store,
        profile=_p(("natural_science", "specialist"), ("general_knowledge", "surface")),
        needs_tools=False,
    )
    # natural_science (0.94 vs 0.85) is tighter than general_knowledge (0.95 vs 0.45).
    assert "natural_science" in decision.explain()


def test_scores_report_only_the_required_fields():
    store = _store_with(_FRONTIER)
    decision = select(store, profile=_p(("law_regulatory", "specialist")), needs_tools=False)
    assert set(decision.scores) == {"law_regulatory"}
    assert set(decision.requirements) == {"law_regulatory"}


# ── Nothing qualifies: the escalation path ────────────────────────────────────

def test_unqualified_pick_is_reported_honestly():
    # This is the case the old router could not even detect: it returned a model
    # with no indication that nothing available was competent.
    store = _store_with(_CODER)
    decision = select(
        store, profile=_p(("law_regulatory", "frontier")), needs_tools=False
    )
    assert decision.model == "cheap-coder"
    assert decision.qualified is False
    assert decision.qualified_count == 0
    assert decision.shortfalls() == {"law_regulatory": (DEPTHS["frontier"], 0.55)}
    assert "clears every bar" in decision.explain()


def test_unqualified_fallback_prefers_the_closest_miss_over_the_cheapest():
    # Once nothing qualifies the question is capability, not price, so cost is
    # only a tiebreak.
    close_but_pricey = ModelSpec(
        "close", cost=12, reliability=1.0, profile={"law_regulatory": 0.90}
    )
    cheap_but_hopeless = ModelSpec(
        "hopeless", cost=0, reliability=1.0, profile={"law_regulatory": 0.20}
    )
    store = _store_with(close_but_pricey, cheap_but_hopeless)
    decision = select(
        store, profile=_p(("law_regulatory", "frontier")), needs_tools=False
    )
    assert decision.model == "close"
    assert decision.qualified is False


def test_qualified_explain_names_the_pick_and_the_bar():
    store = _store_with(_FRONTIER)
    decision = select(store, profile=_p(("law_regulatory", "specialist")), needs_tools=False)
    text = decision.explain()
    assert "qualified" in text
    assert "law_regulatory" in text


def test_no_requirements_is_not_reported_as_a_constraint():
    store = _store_with(_CODER)
    decision = select(store, profile=PromptProfile(domains=()), needs_tools=False)
    assert decision.qualified is True
    assert decision.explain() == "no capability constraints"


# ── Demands and stakes change the outcome, not just the number ────────────────

def test_high_stakes_can_disqualify_a_borderline_model():
    borderline = ModelSpec(
        "borderline", cost=0, reliability=1.0,
        profile={"law_regulatory": DEPTHS["specialist"] + 0.01},
    )
    store = _store_with(borderline, _FRONTIER)
    plain = select(store, profile=_p(("law_regulatory", "specialist")), needs_tools=False)
    loaded = select(
        store,
        profile=_p(("law_regulatory", "specialist"),
                   demands=["factual_precision"], stakes="high"),
        needs_tools=False,
    )
    assert plain.model == "borderline"
    assert loaded.model == "pricey-frontier"


# ── Hard filters still apply ──────────────────────────────────────────────────

def test_hard_filters_apply_to_profile_routing():
    store = _store_with(
        ModelSpec("no-tools", cost=0, reliability=1.0, tools=False,
                  profile={"software_engineering": 0.99}),
        ModelSpec("tools", cost=5, reliability=1.0, tools=True,
                  profile={"software_engineering": 0.90}),
    )
    decision = select(
        store, profile=_p(("software_engineering", "specialist")), needs_tools=True
    )
    assert decision.model == "tools"
    assert decision.eligible_count == 1


def test_exclude_applies_to_profile_routing():
    store = _store_with(_CODER, _FRONTIER)
    decision = select(
        store,
        profile=_p(("software_engineering", "specialist")),
        needs_tools=False,
        exclude={"cheap-coder"},
    )
    assert decision.model == "pricey-frontier"


def test_raises_when_nothing_is_eligible():
    store = SqliteStore(":memory:")
    with pytest.raises(RuntimeError, match="no eligible model"):
        select(store, profile=_p(("software_engineering", "specialist")), needs_tools=False)


def test_eligibility_error_describes_the_demand():
    # The message is what a 422 shows the caller, so it has to say what was asked
    # for, not just that something failed.
    store = SqliteStore(":memory:")
    with pytest.raises(RuntimeError, match="Law & regulatory"):
        select(store, profile=_p(("law_regulatory", "frontier")), needs_tools=False)


# ── route() interop ───────────────────────────────────────────────────────────

def test_route_accepts_a_profile():
    store = _store_with(_CODER, _FRONTIER)
    assert route(
        store, "", "", needs_tools=False,
        profile=_p(("law_regulatory", "specialist")),
    ) == "pricey-frontier"


def test_route_without_profile_uses_the_legacy_single_bar():
    # A deployment that tuned `thresholds` keeps its tuning: the legacy path takes
    # its bar from the thresholds dict, not from a depth label.
    store = _store_with(
        ModelSpec("a", cost=0, reliability=1.0, competence={"coding": 0.80}),
        ModelSpec("b", cost=5, reliability=1.0, competence={"coding": 0.95}),
    )
    assert route(store, "coding", "hard", needs_tools=False) == "b"
    assert route(store, "coding", "hard", needs_tools=False,
                 thresholds={"hard": 0.75}) == "a"


def test_expert_tier_has_a_threshold():
    store = _store_with(
        ModelSpec("good", cost=0, reliability=1.0, competence={"reasoning": 0.90}),
        ModelSpec("best", cost=5, reliability=1.0, competence={"reasoning": 0.96}),
    )
    assert route(store, "reasoning", "hard", needs_tools=False) == "good"
    assert route(store, "reasoning", "expert", needs_tools=False) == "best"
