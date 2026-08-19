"""Tests for the routing vocabulary — requirements, bumps, and normalization."""
import pytest

from smart_ai_router.taxonomy import (
    DEPTHS,
    FIELDS,
    DomainNeed,
    PromptProfile,
    normalize_profile,
    profile_from_labels,
)


def _p(*needs, demands=(), stakes="low"):
    return PromptProfile(
        domains=tuple(DomainNeed(f, d) for f, d in needs),
        demands=frozenset(demands),
        stakes=stakes,
    )


# ── The vocabulary itself ─────────────────────────────────────────────────────

def test_every_field_maps_to_a_legacy_domain():
    # The legacy summary is what the usage log and dashboard speak; a field with
    # an unmappable domain would silently break them.
    assert {legacy for _, legacy in FIELDS.values()} <= {
        "coding", "docs", "reasoning", "general"
    }


def test_depths_are_strictly_increasing():
    values = list(DEPTHS.values())
    assert values == sorted(values)
    assert len(set(values)) == len(values)


# ── Requirements ──────────────────────────────────────────────────────────────

def test_requirement_is_the_depth_bar_with_no_bumps():
    assert _p(("software_engineering", "specialist")).requirements() == {
        "software_engineering": DEPTHS["specialist"]
    }


def test_every_named_field_gets_its_own_bar():
    # The whole point: a model must clear the law bar AND the science bar, so a
    # coding specialist can't win on price by being strong on one axis.
    reqs = _p(("law_regulatory", "specialist"), ("natural_science", "practitioner")).requirements()
    assert set(reqs) == {"law_regulatory", "natural_science"}
    assert reqs["law_regulatory"] > reqs["natural_science"]


def test_demands_and_stakes_raise_every_bar():
    plain = _p(("finance_business", "practitioner")).requirements()
    loaded = _p(
        ("finance_business", "practitioner"),
        demands=["factual_precision"],
        stakes="high",
    ).requirements()
    assert loaded["finance_business"] > plain["finance_business"]


def test_bump_is_capped():
    # Without a cap, a high-stakes precise prompt would demand the priciest tier
    # for ordinary work, undoing the router's reason to exist.
    everything = _p(
        ("law_regulatory", "specialist"),
        ("medicine_health", "specialist"),
        demands=["factual_precision", "quantitative", "long_synthesis", "agentic"],
        stakes="high",
    )
    assert everything.bump() <= 0.08


def test_multi_domain_bump_needs_two_deep_fields():
    one_deep = _p(("law_regulatory", "specialist"), ("general_knowledge", "surface"))
    two_deep = _p(("law_regulatory", "specialist"), ("natural_science", "specialist"))
    assert one_deep.bump() == 0.0
    assert two_deep.bump() > 0.0


def test_repeated_field_takes_the_strictest_bar():
    reqs = _p(
        ("math_formal", "practitioner"), ("math_formal", "frontier")
    ).requirements()
    assert reqs == {"math_formal": DEPTHS["frontier"]}


def test_requirement_never_exceeds_the_ceiling():
    # Above the ceiling nothing could ever qualify, so ordinary prompts would
    # take the escalation path.
    peak = _p(
        ("law_regulatory", "frontier"),
        ("natural_science", "frontier"),
        demands=["factual_precision", "quantitative"],
        stakes="high",
    ).peak_requirement()
    assert peak <= 0.97


def test_empty_profile_requirements_are_empty():
    empty = PromptProfile(domains=())
    assert empty.requirements() == {}
    assert empty.primary_field() == "general_knowledge"


# ── Legacy summary ────────────────────────────────────────────────────────────

def test_legacy_labels_follow_the_primary_field():
    assert _p(("software_engineering", "practitioner")).legacy_labels()[0] == "coding"
    assert _p(("technical_writing", "practitioner")).legacy_labels()[0] == "docs"
    assert _p(("general_knowledge", "surface")).legacy_labels() == ("general", "trivial")


def test_legacy_complexity_tracks_the_peak_requirement():
    assert _p(("math_formal", "surface")).legacy_labels()[1] == "trivial"
    assert _p(("math_formal", "practitioner")).legacy_labels()[1] == "moderate"
    assert _p(("math_formal", "specialist")).legacy_labels()[1] == "hard"
    assert _p(("math_formal", "frontier")).legacy_labels()[1] == "expert"


def test_describe_names_fields_depths_and_extras():
    text = _p(
        ("law_regulatory", "specialist"),
        demands=["factual_precision"],
        stakes="high",
    ).describe()
    assert "Law & regulatory" in text
    assert "specialist" in text
    assert "high stakes" in text
    assert "factual_precision" in text


# ── normalize_profile ─────────────────────────────────────────────────────────

def test_normalize_accepts_the_schema_shape():
    p = normalize_profile(
        {
            "domains": [{"field": "medicine_health", "depth": "specialist"}],
            "demands": ["factual_precision"],
            "stakes": "high",
        }
    )
    assert p == _p(("medicine_health", "specialist"),
                   demands=["factual_precision"], stakes="high")


def test_normalize_accepts_bare_field_strings():
    # Small models bend the schema; a bare string is the most common way.
    assert normalize_profile({"domains": ["creative_writing"]}) == _p(
        ("creative_writing", "practitioner")
    )


def test_normalize_accepts_demands_as_boolean_object():
    p = normalize_profile(
        {"domains": ["data_analysis"], "demands": {"quantitative": True,
                                                  "agentic": False}}
    )
    assert p.demands == frozenset({"quantitative"})


def test_normalize_drops_unknown_labels():
    p = normalize_profile(
        {
            "domains": [{"field": "astrology", "depth": "cosmic"},
                        {"field": "humanities_social", "depth": "cosmic"}],
            "demands": ["telepathy"],
            "stakes": "apocalyptic",
        }
    )
    assert p == _p(("humanities_social", "practitioner"))


def test_normalize_deduplicates_and_caps_field_count():
    # More than three fields means the classifier is pattern-matching on topic
    # words; extra entries only inflate the bar.
    p = normalize_profile(
        {
            "domains": [
                {"field": "software_engineering", "depth": "surface"},
                {"field": "software_engineering", "depth": "frontier"},
                {"field": "math_formal", "depth": "surface"},
                {"field": "law_regulatory", "depth": "surface"},
                {"field": "medicine_health", "depth": "surface"},
            ]
        }
    )
    assert len(p.domains) == 3
    assert [d.field for d in p.domains] == [
        "software_engineering", "math_formal", "law_regulatory"
    ]


@pytest.mark.parametrize("raw", [None, "coding", 42, [], {}, {"domains": []}])
def test_normalize_returns_none_on_unusable_input(raw):
    assert normalize_profile(raw) is None


# ── Legacy adaptation ─────────────────────────────────────────────────────────

def test_profile_from_labels_is_single_field():
    p = profile_from_labels("coding", "hard")
    assert p.domains == (DomainNeed("software_engineering", "specialist"),)
    assert p.demands == frozenset()
    assert p.stakes == "low"


def test_profile_from_labels_round_trips_legacy_vocabulary():
    for domain in ("coding", "docs", "reasoning", "general"):
        for complexity in ("trivial", "moderate", "hard", "expert"):
            got = profile_from_labels(domain, complexity).legacy_labels()
            assert got == (domain, complexity)


def test_profile_from_labels_tolerates_junk():
    p = profile_from_labels("wizardry", "impossible")
    assert p.domains == (DomainNeed("general_knowledge", "practitioner"),)
