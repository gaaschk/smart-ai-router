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


# ── The tool-loop floor ───────────────────────────────────────────────────────
# ModelSpec.agentic is a measurement, not a field score: it says whether a model
# holds a multi-step tool loop together. It gates tool traffic only, and only for
# models that were actually measured.

# Same field scores, tools on, and the candidate under test is always the
# *cheapest* — so if a pricier model wins, the floor is the only thing that could
# have moved it, not a tiebreak.
def _looper(name, agentic, cost=1):
    return ModelSpec(
        name, cost=cost, reliability=1.0, tools=True, agentic=agentic,
        profile={"software_engineering": 0.95},
    )


def _weak_and_strong():
    return _store_with(_looper("weak-loop", 0.25), _looper("strong-loop", 0.62, cost=5))


def test_a_model_measured_weak_at_loops_loses_tool_traffic():
    store = _weak_and_strong()
    decision = select(
        store, profile=_p(("software_engineering", "specialist")), needs_tools=True
    )
    assert decision.model == "strong-loop"
    assert decision.agentic_excluded == 1
    assert "tool loops" in decision.explain()


def test_the_same_weak_model_still_wins_plain_chat():
    # The floor is about driving a loop. Nothing about a low agentic index says the
    # model can't answer a one-shot question, so a request with no tools must not
    # pay more for one.
    store = _weak_and_strong()
    decision = select(
        store, profile=_p(("software_engineering", "specialist")), needs_tools=False
    )
    assert decision.model == "weak-loop"
    assert decision.agentic_excluded == 0


def test_an_unmeasured_model_is_exempt():
    # Two thirds of the catalog and *every* local model carry no index. Reading 0.0
    # as "incapable" would hand all tool traffic to benchmarked paid models.
    store = _store_with(_looper("local", 0.0), _looper("strong-loop", 0.62, cost=5))
    decision = select(
        store, profile=_p(("software_engineering", "specialist")), needs_tools=True
    )
    assert decision.model == "local"
    assert decision.agentic_excluded == 0


def test_the_agentic_demand_gates_even_without_a_tools_array():
    # "research this, then write it up" is a loop whether or not the client
    # declared tools, and the classifier is what notices.
    store = _weak_and_strong()
    decision = select(
        store,
        profile=_p(("software_engineering", "specialist"), demands=["agentic"]),
        needs_tools=False,
    )
    assert decision.model == "strong-loop"


def test_agent_mode_gates_too():
    store = _weak_and_strong()
    decision = select(
        store,
        profile=_p(("software_engineering", "specialist")),
        needs_tools=False,
        agent_mode=True,
    )
    assert decision.model == "strong-loop"


def test_the_floor_is_overridable_like_every_other_threshold():
    # A deployment that finds `surface` too permissive should be able to raise it
    # from config rather than by editing taxonomy.
    store = _store_with(_looper("strong-loop", 0.62))
    assert select(
        store, profile=_p(("software_engineering", "specialist")), needs_tools=True
    ).model == "strong-loop"
    with pytest.raises(RuntimeError, match="tool-loop floor"):
        select(
            store,
            profile=_p(("software_engineering", "specialist")),
            needs_tools=True,
            thresholds={"min_agentic": 0.9},
        )


def test_the_floor_says_so_when_it_empties_the_pool():
    # Otherwise the 422 reads "run sync()" for a catalog that synced fine.
    store = _store_with(_looper("weak-loop", 0.25))
    with pytest.raises(RuntimeError, match="1 excluded as measured below"):
        select(
            store, profile=_p(("software_engineering", "specialist")), needs_tools=True
        )


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


# ── within-tier price ─────────────────────────────────────────────────────────

# The live regression: both Sonnets are tier 5 and both score 0.95 on the field
# the prompt actually needs, so the tie fell to `data_analysis` — a field this
# prompt touches only at surface depth (bar 0.53), which both clear by 0.3+. The
# 0.001 margin difference bought the 33%-dearer model 195 times.
_SONNET_4 = ModelSpec(
    "claude-sonnet-4", cost=5, cost_input=3.0, cost_output=15.0, reliability=1.0,
    profile={"software_engineering": 0.95, "data_analysis": 0.91},
)
_SONNET_5 = ModelSpec(
    "claude-sonnet-5", cost=5, cost_input=2.0, cost_output=10.0, reliability=1.0,
    profile={"software_engineering": 0.95, "data_analysis": 0.85},
)


def test_same_tier_picks_the_genuinely_cheaper_model():
    store = _store_with(_SONNET_4, _SONNET_5)
    decision = select(
        store, needs_tools=False,
        profile=_p(("software_engineering", "practitioner"),
                   ("data_analysis", "surface"), stakes="medium"),
    )
    assert decision.qualified
    assert decision.model == "claude-sonnet-5"


# ── cost:quality bias ─────────────────────────────────────────────────────────

# The live complaint: a free local model that clears the bar by 0.00 beat a
# $0.31/1M hosted model that cleared it by 0.30, because the coarse cost tier is
# the primary sort key. Free won, and it was slow.
_SLOW_LOCAL = ModelSpec(
    "ollama/slow-local", cost=0, cost_input=0.0, cost_output=0.0, reliability=1.0,
    profile={"software_engineering": 0.68},
)
_CHEAP_HOSTED = ModelSpec(
    "openrouter/openai/gpt-5-nano", cost=1, cost_input=0.125, cost_output=0.375,
    reliability=1.0, profile={"software_engineering": 0.98},
)
# Present so the pool's price ceiling is realistic. It matters: both axes are
# normalized against the pool, so in a two-model pool where each wins one axis
# the crossover sits at exactly 50% and the knob reads as a coin flip. Against a
# real ceiling the log scale puts $0.31 and $0 almost on top of each other, which
# is why a *low* bias is enough to buy a lot of headroom.
_DEAR_FRONTIER = ModelSpec(
    "openrouter/anthropic/claude-sonnet-5", cost=5, cost_input=2.0, cost_output=10.0,
    reliability=1.0, profile={"software_engineering": 0.95},
)


def _practitioner(store, bias, monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_COST_QUALITY_BIAS", str(bias))
    return select(
        store, needs_tools=False,
        profile=_p(("software_engineering", "practitioner")),
    ).model


def test_bias_zero_keeps_strict_cheapest_first(monkeypatch):
    # Default behavior must not move: an existing deployment that never opens the
    # Settings page keeps the pick it had.
    store = _store_with(_SLOW_LOCAL, _CHEAP_HOSTED, _DEAR_FRONTIER)
    assert _practitioner(store, 0, monkeypatch) == "ollama/slow-local"


def test_bias_buys_headroom_from_a_barely_qualifying_free_model(monkeypatch):
    # The pick that matters: not the dearest model, the one whose headroom is
    # nearly free. Free-and-barely-adequate loses to $0.31-and-best.
    store = _store_with(_SLOW_LOCAL, _CHEAP_HOSTED, _DEAR_FRONTIER)
    assert _practitioner(store, 40, monkeypatch) == "openrouter/openai/gpt-5-nano"
    # And it does not take much: with an $8/1M model setting the ceiling the log
    # scale puts $0.31 at 0.124 of the price range, so the crossover is ~11%.
    assert _practitioner(store, 15, monkeypatch) == "openrouter/openai/gpt-5-nano"
    assert _practitioner(store, 5, monkeypatch) == "ollama/slow-local"


def test_full_bias_ignores_price(monkeypatch):
    # At 100 the most capable wins even when it is the most expensive, which is
    # the only way to tell the blend from a cheap-model preference.
    dear_and_strong = ModelSpec(
        "dear", cost=15, cost_input=100.0, cost_output=300.0, reliability=1.0,
        profile={"software_engineering": 0.98},
    )
    store = _store_with(_SLOW_LOCAL, dear_and_strong)
    assert _practitioner(store, 100, monkeypatch) == "dear"
    assert _practitioner(store, 0, monkeypatch) == "ollama/slow-local"


def test_bias_never_promotes_an_unqualified_model(monkeypatch):
    # The blend ranks the qualified pool only. A strong-but-unqualified model must
    # not be reachable by turning the knob up.
    fails_the_bar = ModelSpec(
        "fails", cost=15, cost_input=100.0, cost_output=300.0, reliability=1.0,
        profile={"software_engineering": 0.50, "law_regulatory": 0.99},
    )
    store = _store_with(_SLOW_LOCAL, fails_the_bar)
    assert _practitioner(store, 100, monkeypatch) == "ollama/slow-local"


def test_equal_price_still_breaks_on_margin():
    # Price only outranks margin when the prices actually differ.
    dearer_but_stronger = ModelSpec(
        "strong", cost=5, cost_input=3.0, cost_output=15.0, reliability=1.0,
        profile={"software_engineering": 0.98, "data_analysis": 0.91},
    )
    store = _store_with(_SONNET_4, dearer_but_stronger)
    assert select(
        store, needs_tools=False,
        profile=_p(("software_engineering", "practitioner"),
                   ("data_analysis", "surface"), stakes="medium"),
    ).model == "strong"
