"""Tests for model profiling — the model-side half of profile routing."""
import pytest

from smart_ai_router.profiler import (
    _MAX_SCORE,
    _MIN_SCORE,
    extract_catalog_signals,
    legacy_competence,
    profile_model,
)
from smart_ai_router.taxonomy import DEPTHS, FIELDS


# ── Shape and bounds ──────────────────────────────────────────────────────────

def test_profile_covers_every_field():
    # route() looks up requirements by field name; a missing key would silently
    # fall through to the coarse legacy competence vector.
    profile = profile_model("openrouter/vendor/some-model")
    assert set(profile) == set(FIELDS)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"intelligence_index": 0.0},
        {"intelligence_index": 100.0},
        {"intelligence_index": -5.0},
        {"intelligence_index": 60.0, "coding_index": 99.0, "agentic_index": 90.0},
        {"supports_reasoning": True, "intelligence_index": 63.0},
    ],
)
def test_scores_stay_in_bounds(kwargs):
    profile = profile_model("openrouter/vendor/model", **kwargs)
    assert all(_MIN_SCORE <= v <= _MAX_SCORE for v in profile.values())


def test_no_metadata_still_yields_a_usable_profile():
    # Ollama and Bedrock ship no benchmarks or descriptions at all; they must
    # land on the same scale rather than needing a per-provider special case.
    profile = profile_model("ollama/llama3.1:8b")
    assert set(profile) == set(FIELDS)
    assert all(v > 0 for v in profile.values())


# ── Measured benchmarks drive the level ───────────────────────────────────────

def test_higher_intelligence_index_scores_higher():
    weak = profile_model("openrouter/v/m", intelligence_index=10.0)
    strong = profile_model("openrouter/v/m", intelligence_index=60.0)
    assert strong["law_regulatory"] > weak["law_regulatory"]


def test_frontier_index_clears_the_frontier_bar():
    # The anchor table is only meaningful if the top of the catalog actually
    # reaches taxonomy's top depth; otherwise every frontier prompt escalates.
    profile = profile_model(
        "openrouter/anthropic/claude-opus-5",
        description="Our most capable model.",
        intelligence_index=63.1,
    )
    assert profile["general_knowledge"] >= DEPTHS["frontier"]


def test_midrange_index_does_not_clear_specialist():
    # And the bar has to bite: a mid-tier model clearing `specialist` would put
    # us back where we started, with cheap models winning demanding prompts.
    profile = profile_model("openrouter/v/mid", intelligence_index=30.0)
    assert profile["law_regulatory"] < DEPTHS["specialist"]


def test_agentic_index_overrides_the_tool_driving_field():
    # agentic collapses far faster than intelligence for weak models, so it is
    # real evidence rather than a restatement — it replaces, not nudges.
    high = profile_model("openrouter/v/m", intelligence_index=40.0, agentic_index=50.0)
    low = profile_model("openrouter/v/m", intelligence_index=40.0, agentic_index=2.0)
    assert high["operations_process"] > low["operations_process"]
    assert low["operations_process"] < low["general_knowledge"]


def test_coding_residual_rewards_unusual_code_strength():
    ordinary = profile_model("openrouter/v/m", intelligence_index=40.0, coding_index=52.0)
    strong = profile_model("openrouter/v/m", intelligence_index=40.0, coding_index=75.0)
    assert strong["software_engineering"] > ordinary["software_engineering"]


# ── Description cues ──────────────────────────────────────────────────────────

def test_advertised_field_is_credited():
    plain = profile_model("openrouter/v/m", intelligence_index=40.0)
    advertised = profile_model(
        "openrouter/v/m",
        description="A model for medical and clinical reasoning.",
        intelligence_index=40.0,
    )
    assert advertised["medicine_health"] > plain["medicine_health"]


def test_reasoning_support_credits_step_by_step_fields():
    plain = profile_model("openrouter/v/m", intelligence_index=40.0)
    thinking = profile_model(
        "openrouter/v/m", intelligence_index=40.0, supports_reasoning=True
    )
    assert thinking["math_formal"] > plain["math_formal"]
    # …and only those fields.
    assert thinking["creative_writing"] == plain["creative_writing"]


def test_reasoning_credit_tapers_at_the_ceiling():
    # Untapered, the bump pushed natural_science above software_engineering for a
    # model whose own description is entirely about code.
    profile = profile_model(
        "openrouter/anthropic/claude-opus-5",
        description="Best coding model in the world, for advanced coding and agents.",
        intelligence_index=63.1,
        coding_index=80.0,
        supports_reasoning=True,
    )
    assert profile["software_engineering"] >= profile["natural_science"]


def test_generic_reasoning_boilerplate_is_not_a_design_cue():
    # "designed for advanced reasoning" appears in a large share of catalog
    # descriptions; treating it as a cue made nearly every model a design
    # specialist.
    profile = profile_model(
        "openrouter/v/m",
        description="Designed for advanced reasoning, coding, and agent workflows.",
        intelligence_index=40.0,
    )
    plain = profile_model("openrouter/v/m", intelligence_index=40.0)
    assert profile["product_design"] == plain["product_design"]
    assert profile["systems_architecture"] == plain["systems_architecture"]


# ── The specialist discount: the mechanism that matters ───────────────────────

def test_narrow_coder_is_discounted_on_professional_fields():
    # The whole point. A cheap coding specialist used to win a law-plus-code
    # prompt on price because its coding score was high; now its law score
    # disqualifies it.
    coder = profile_model(
        "openrouter/qwen/qwen3-coder-next",
        description="A powerful coding model for agentic coding tasks.",
        intelligence_index=40.0,
        coding_index=60.0,
    )
    assert coder["software_engineering"] > coder["law_regulatory"]
    assert coder["law_regulatory"] < DEPTHS["specialist"]


def test_narrow_specialist_still_clears_practitioner_elsewhere():
    # The discount says "don't make this the sole answer on law", not "this model
    # cannot form sentences".
    coder = profile_model(
        "openrouter/vendor/some-coder",
        description="Coding model.",
        intelligence_index=60.0,
    )
    assert coder["law_regulatory"] >= DEPTHS["practitioner"]


def test_general_model_takes_no_discount():
    general = profile_model(
        "openrouter/vendor/big-general",
        description="A versatile general-purpose model, strong at coding and science.",
        intelligence_index=55.0,
    )
    coder = profile_model(
        "openrouter/vendor/big-coder",
        description="A coder model.",
        intelligence_index=55.0,
    )
    assert general["law_regulatory"] > coder["law_regulatory"]


def test_specialty_field_itself_is_not_discounted():
    med = profile_model(
        "openrouter/google/medgemma-27b",
        description="A medical model.",
        intelligence_index=40.0,
    )
    assert med["medicine_health"] > med["law_regulatory"]


# ── Legacy summary ────────────────────────────────────────────────────────────

def test_legacy_competence_has_the_four_columns():
    legacy = legacy_competence(profile_model("openrouter/v/m", intelligence_index=40.0))
    assert set(legacy) == {"coding", "docs", "reasoning", "general"}
    assert all(0.0 <= v <= 1.0 for v in legacy.values())


def test_legacy_competence_tracks_the_profile():
    weak = legacy_competence(profile_model("openrouter/v/m", intelligence_index=10.0))
    strong = legacy_competence(profile_model("openrouter/v/m", intelligence_index=60.0))
    assert strong["reasoning"] > weak["reasoning"]


def test_legacy_competence_of_empty_profile_is_neutral():
    # A store row written before profiling existed must stay routable rather than
    # scoring 0 and being excluded.
    assert legacy_competence({}) == {
        "coding": 0.70, "docs": 0.70, "reasoning": 0.70, "general": 0.70
    }


# ── Catalog extraction ────────────────────────────────────────────────────────

def test_extract_reads_artificial_analysis_indices():
    signals = extract_catalog_signals(
        {
            "description": "A model.",
            "benchmarks": {
                "artificial_analysis": {
                    "intelligence_index": 55.3,
                    "coding_index": 61.0,
                    "agentic_index": 42.0,
                }
            },
            "supported_parameters": ["tools", "reasoning"],
        }
    )
    assert signals == {
        "description": "A model.",
        "intelligence_index": 55.3,
        "coding_index": 61.0,
        "agentic_index": 42.0,
        "supports_reasoning": True,
    }


def test_extract_handles_missing_and_malformed_blocks():
    # Most of the catalog carries no benchmarks; a shape change must degrade to
    # name priors, not raise inside the sync loop.
    assert extract_catalog_signals({}) == {
        "description": "",
        "intelligence_index": None,
        "coding_index": None,
        "agentic_index": None,
        "supports_reasoning": False,
    }
    junk = extract_catalog_signals(
        {"benchmarks": {"artificial_analysis": "nope"}, "reasoning": None}
    )
    assert junk["intelligence_index"] is None


def test_extract_reads_reasoning_from_supported_efforts():
    signals = extract_catalog_signals({"reasoning": {"supported_efforts": ["low", "high"]}})
    assert signals["supports_reasoning"] is True


def test_extract_survives_non_numeric_index():
    signals = extract_catalog_signals(
        {"benchmarks": {"artificial_analysis": {"intelligence_index": "n/a"}}}
    )
    assert signals["intelligence_index"] is None
