"""Tests for the pricing module."""
from smart_ai_router.models import ModelSpec
from smart_ai_router.pricing import cost_for


def test_ollama_is_free():
    spec = ModelSpec("ollama/llama3.1:8b", provider="ollama", cost_input=0, cost_output=0)
    assert cost_for(spec, 10000, 5000) == 0.0


def test_known_price():
    spec = ModelSpec("openrouter/x", provider="openrouter", cost_input=1.0, cost_output=3.0)
    result = cost_for(spec, 1_000_000, 500_000)
    assert abs(result - 2.50) < 0.001


def test_unknown_price_returns_none():
    spec = ModelSpec("bedrock/some-arn", provider="bedrock", cost_input=0.0, cost_output=0.0)
    assert cost_for(spec, 1000, 500) is None


def test_free_tier_openrouter():
    spec = ModelSpec("openrouter/x:free", provider="openrouter", cost_input=0.0, cost_output=0.0)
    assert cost_for(spec, 1000, 500) is None
