"""Tests for the blended cost-tier calculation."""
from smart_ai_router.sync import _cost_tier


def test_blends_output_not_just_input():
    # Two models with identical input but very different output prices must
    # NOT land in the same tier — output dominates real cost.
    cheap_output = _cost_tier(3.0, 6.0)
    dear_output = _cost_tier(3.0, 60.0)
    assert dear_output > cheap_output


def test_current_claude_rates_are_ordered():
    haiku = _cost_tier(1.0, 5.0)      # eff ≈ 4.0
    sonnet = _cost_tier(3.0, 15.0)    # eff ≈ 12.0
    opus48 = _cost_tier(5.0, 25.0)    # eff ≈ 20.0
    opus41 = _cost_tier(15.0, 75.0)   # eff ≈ 60.0
    assert haiku < sonnet < opus48 < opus41


def test_newer_opus_is_cheaper_tier_than_older():
    # The reported symptom: Opus 4.8 ($5/$25) should tier below Opus 4.1
    # ($15/$75) because it is genuinely cheaper.
    assert _cost_tier(5.0, 25.0) < _cost_tier(15.0, 75.0)


def test_free_and_local():
    assert _cost_tier(0.0, 0.0) == 0            # local / unknown
    assert _cost_tier(0.0, 0.0, is_free=True) == 1


def test_expensive_output_reasoning_model_ranks_high():
    # Cheap input, very expensive output (a classic reasoning-model shape)
    # must not masquerade as a cheap model.
    reasoning = _cost_tier(1.0, 40.0)   # eff ≈ 30.75
    flat_mid = _cost_tier(6.0, 6.0)     # eff ≈ 6.0
    assert reasoning > flat_mid
