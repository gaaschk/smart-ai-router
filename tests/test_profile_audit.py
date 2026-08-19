"""Profile audit — replaying real routed prompts against proposed model profiles.

The audit is what makes "is this enrichment an improvement?" answerable, so these
tests pin the two things it must get right: it uses the real selection function
(a reported flip is a flip that would really happen), and it weights each flip by
how much traffic it affects.
"""
from __future__ import annotations

import dataclasses

from smart_ai_router.models import ModelSpec
from smart_ai_router.profile_audit import audit_profiles
from smart_ai_router.taxonomy import FIELD_KEYS


def _spec(value: str, cost: int, **scores) -> ModelSpec:
    profile = {f: 0.80 for f in FIELD_KEYS}
    profile.update(scores)
    return ModelSpec(value=value, provider="openrouter", cost=cost,
                     reliability=1.0, profile=profile)


def _recorded(field: str, depth: str, model: str, requests: int) -> dict:
    return {
        "profile": {"domains": [{"field": field, "depth": depth}],
                    "demands": [], "stakes": "low"},
        "routed_model": model,
        "requests": requests,
    }


def test_no_flips_when_profiles_are_unchanged():
    models = [_spec("cheap", 1), _spec("pricey", 9)]
    result = audit_profiles(
        recorded=[_recorded("law_regulatory", "practitioner", "cheap", 5)],
        before=models, after=models,
    )
    assert result.profiles == 1
    assert result.requests == 5
    assert result.flipped == 0
    assert result.flips == []


def test_a_downgraded_specialist_flips_traffic_to_the_frontier_model():
    """The change this whole feature exists to make: a cheap coder rated
    `unsuited` at law stops winning law prompts."""
    cheap_before = _spec("cheap-coder", 1, law_regulatory=0.88)
    cheap_after = _spec("cheap-coder", 1, law_regulatory=0.68)
    frontier = _spec("frontier", 9, law_regulatory=0.95)

    result = audit_profiles(
        recorded=[_recorded("law_regulatory", "specialist", "cheap-coder", 12)],
        before=[cheap_before, frontier], after=[cheap_after, frontier],
    )
    assert result.flipped == 1
    assert result.flipped_requests == 12
    flip = result.flips[0]
    assert flip.before_model == "cheap-coder"
    assert flip.after_model == "frontier"
    assert flip.direction() == "pricier"
    assert "Law & regulatory @ specialist" in flip.described_as


def test_a_flip_that_buys_qualification_is_labelled_as_such():
    """Going from "nothing clears this bar" to a real match is the most valuable
    outcome, and must not be reported merely as "pricier"."""
    weak = _spec("only-model", 1, law_regulatory=0.50)
    strong = _spec("upgraded", 5, law_regulatory=0.99)

    result = audit_profiles(
        recorded=[_recorded("law_regulatory", "specialist", "only-model", 3)],
        before=[weak], after=[weak, strong],
    )
    flip = result.flips[0]
    assert flip.before_qualified is False
    assert flip.after_qualified is True
    assert flip.direction() == "qualifies"


def test_losing_qualification_is_reported_as_a_regression():
    good = _spec("m", 1, law_regulatory=0.90)
    downgraded = _spec("m", 1, law_regulatory=0.50)
    result = audit_profiles(
        recorded=[_recorded("law_regulatory", "specialist", "m", 2)],
        before=[good], after=[downgraded],
    )
    assert result.flips[0].direction() == "unqualifies"


def test_a_flip_to_a_cheaper_model_is_labelled_cheaper():
    """`specialty` ratings can *save* money by qualifying a cheap specialist."""
    cheap_before = _spec("cheap-coder", 1, software_engineering=0.80)
    cheap_after = _spec("cheap-coder", 1, software_engineering=0.92)
    pricey = _spec("pricey", 9, software_engineering=0.95)
    result = audit_profiles(
        recorded=[_recorded("software_engineering", "specialist", "pricey", 7)],
        before=[cheap_before, pricey], after=[cheap_after, pricey],
    )
    flip = result.flips[0]
    assert flip.after_model == "cheap-coder"
    assert flip.direction() == "cheaper"


def test_flips_are_sorted_by_traffic():
    before = [_spec("cheap", 1, law_regulatory=0.90, medicine_health=0.90),
              _spec("big", 9, law_regulatory=0.95, medicine_health=0.95)]
    after = [_spec("cheap", 1, law_regulatory=0.50, medicine_health=0.50),
             _spec("big", 9, law_regulatory=0.95, medicine_health=0.95)]
    result = audit_profiles(
        recorded=[
            _recorded("law_regulatory", "specialist", "cheap", 2),
            _recorded("medicine_health", "specialist", "cheap", 40),
        ],
        before=before, after=after,
    )
    assert [f.requests for f in result.flips] == [40, 2]
    assert result.flipped_requests == 42


def test_the_same_profile_logged_under_two_models_counts_once():
    """Distinct *profiles* are replayed, not log rows: the same demand can appear
    under several past picks, and replaying it twice would double its weight."""
    before = [_spec("cheap", 1, law_regulatory=0.90), _spec("big", 9, law_regulatory=0.95)]
    after = [_spec("cheap", 1, law_regulatory=0.50), _spec("big", 9, law_regulatory=0.95)]
    result = audit_profiles(
        recorded=[
            _recorded("law_regulatory", "specialist", "cheap", 3),
            _recorded("law_regulatory", "specialist", "gone-model", 4),
        ],
        before=before, after=after,
    )
    assert result.profiles == 1
    assert result.flipped == 1
    assert result.flips[0].requests == 7


def test_unroutable_recorded_profiles_are_reported_not_dropped():
    """A taxonomy change can orphan logged profiles; that must be visible, since
    silently skipping them would understate the audit's coverage."""
    models = [_spec("m", 1)]
    result = audit_profiles(
        recorded=[{"profile": {"domains": [{"field": "phrenology",
                                           "depth": "frontier"}]},
                   "routed_model": "m", "requests": 1}],
        before=models, after=models,
    )
    assert result.profiles == 0
    assert result.flipped == 0
    assert "unroutable recorded profile" in result.errors[0]


def test_malformed_recorded_rows_are_skipped():
    models = [_spec("m", 1)]
    result = audit_profiles(
        recorded=[{"profile": "not a dict", "requests": 1}, {"requests": 2}],
        before=models, after=models,
    )
    assert result.profiles == 0


def test_an_empty_matrix_is_an_error_not_a_flip():
    """No eligible model is a matrix problem; reporting it as a routing flip
    would blame the profile change for it."""
    result = audit_profiles(
        recorded=[_recorded("law_regulatory", "specialist", "m", 1)],
        before=[], after=[],
    )
    assert result.flipped == 0
    assert result.errors and "no eligible model" in result.errors[0]


def test_as_dict_is_json_shaped_for_the_api():
    before = [_spec("cheap", 1, law_regulatory=0.90), _spec("big", 9, law_regulatory=0.95)]
    after = [_spec("cheap", 1, law_regulatory=0.50), _spec("big", 9, law_regulatory=0.95)]
    payload = audit_profiles(
        recorded=[_recorded("law_regulatory", "specialist", "cheap", 5)],
        before=before, after=after,
    ).as_dict()
    assert payload["flipped_requests"] == 5
    entry = payload["flips"][0]
    assert entry["before"]["model"] == "cheap"
    assert entry["after"]["model"] == "big"
    assert entry["after"]["cost"] == 9
    assert entry["direction"] == "pricier"


def test_audit_honors_the_hard_filters_the_router_applies():
    """The audit routes through the real select path, so an unreliable model is
    excluded here exactly as it would be live."""
    unreliable = dataclasses.replace(
        _spec("cheap", 1, law_regulatory=0.99), reliability=0.10
    )
    big = _spec("big", 9, law_regulatory=0.95)
    result = audit_profiles(
        recorded=[_recorded("law_regulatory", "specialist", "big", 1)],
        before=[big], after=[unreliable, big],
    )
    assert result.flipped == 0  # the cheap model never becomes eligible
