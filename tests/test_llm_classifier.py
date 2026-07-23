"""Tests for the LLM classifier parsing + config (no network)."""
import asyncio

from smart_ai_router.llm_classifier import (
    _parse_classification,
    classifier_model,
    classify_llm,
)


def test_parses_clean_json():
    assert _parse_classification('{"domain": "reasoning", "complexity": "hard"}') == (
        "reasoning",
        "hard",
    )


def test_parses_with_code_fence_and_prose():
    text = 'Sure!\n```json\n{"domain":"coding","complexity":"moderate"}\n```'
    assert _parse_classification(text) == ("coding", "moderate")


def test_case_insensitive_labels():
    assert _parse_classification('{"domain":"DOCS","complexity":"Trivial"}') == (
        "docs",
        "trivial",
    )


def test_rejects_unknown_domain():
    assert _parse_classification('{"domain":"math","complexity":"hard"}') is None


def test_rejects_unknown_complexity():
    assert _parse_classification('{"domain":"coding","complexity":"epic"}') is None


def test_rejects_missing_field():
    assert _parse_classification('{"domain":"coding"}') is None


def test_rejects_garbage():
    assert _parse_classification("not json at all") is None
    assert _parse_classification("") is None


def test_disabled_model_env(monkeypatch):
    # Empty env value disables the LLM path.
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "")
    assert classifier_model() == ""


def test_default_model(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_CLASSIFIER_MODEL", raising=False)
    assert classifier_model() == "llama3.1:8b"


def test_classify_llm_returns_none_when_disabled(monkeypatch):
    # No blank prompt, but the model is disabled → None (fall back), no network.
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "")
    result = asyncio.run(
        classify_llm("Derive the hydrogen orbitals", base_url="http://localhost:11434/v1")
    )
    assert result is None


def test_classify_llm_returns_none_on_empty_prompt():
    result = asyncio.run(classify_llm("", base_url="http://localhost:11434/v1"))
    assert result is None
