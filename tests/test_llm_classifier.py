"""Tests for the LLM classifier parsing + config (no network)."""
import asyncio
import json

import httpx

from smart_ai_router import settings as _settings
from smart_ai_router.llm_classifier import (
    ClassifierTarget,
    _parse_classification,
    classifier_fallback_model,
    classifier_model,
    classify_chain,
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
    # With no override, the shipped spec default applies. Compared against the
    # registry rather than a literal: which small model classifies best is a
    # benchmarked tuning decision, and re-tuning it shouldn't fail this test.
    monkeypatch.delenv("SMART_ROUTER_CLASSIFIER_MODEL", raising=False)
    shipped = next(s for s in _settings.SPECS if s.key == "classifier_model").default
    assert classifier_model() == shipped


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


def test_classify_llm_requests_json_object(monkeypatch):
    # The request MUST ask for JSON output — without response_format, chatty
    # instruct models answer the prompt instead of classifying, and the whole
    # chain silently falls through to the keyword classifier (the live bug this
    # fixes). Capture the outgoing payload via a mock transport.
    import smart_ai_router.llm_classifier as lc

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"domain":"coding","complexity":"hard"}'}}]},
        )

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(lc.httpx, "AsyncClient", fake_client)
    result = asyncio.run(
        classify_llm("write a parser", base_url="http://localhost:11434/v1", model="llama3.1:8b")
    )
    assert result == ("coding", "hard")
    # A strict json_schema (not a bare json_object) is what actually constrains
    # the reply to {domain, complexity} — a chatty model handed a "write a
    # paper" prompt emits valid-but-wrong-shaped JSON otherwise.
    rf = captured["body"]["response_format"]
    assert rf["type"] == "json_schema"
    js = rf["json_schema"]
    assert js["strict"] is True
    props = js["schema"]["properties"]
    assert set(props["domain"]["enum"]) == lc._DOMAINS
    assert set(props["complexity"]["enum"]) == lc._COMPLEXITIES
    assert js["schema"]["required"] == ["domain", "complexity"]
    assert captured["body"]["max_tokens"] >= 40


def test_default_fallback_model(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_CLASSIFIER_FALLBACK", raising=False)
    assert classifier_fallback_model() == "nvidia/nemotron-nano-9b-v2:free"


def test_fallback_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_FALLBACK", "")
    assert classifier_fallback_model() == ""


def test_chain_falls_through_to_second_target(monkeypatch):
    # First target has an unreachable base_url (fails fast to None); the chain
    # must try the second. We disable real network by pointing both at a bad
    # port, but stub classify_llm to simulate first-fail / second-succeed.
    import smart_ai_router.llm_classifier as lc

    calls = []

    async def fake_classify_llm(prompt, *, base_url, model=None, api_key=""):
        calls.append(model)
        if model == "local-bad":
            return None
        return ("reasoning", "hard")

    monkeypatch.setattr(lc, "classify_llm", fake_classify_llm)
    targets = [
        ClassifierTarget(model="local-bad", base_url="http://x/v1", label="llm"),
        ClassifierTarget(model="free-good", base_url="http://y/v1", label="llm-free"),
    ]
    result = asyncio.run(lc.classify_chain("prompt", targets))
    assert result == ("reasoning", "hard", "llm-free")
    assert calls == ["local-bad", "free-good"]


def test_chain_returns_none_when_all_fail(monkeypatch):
    import smart_ai_router.llm_classifier as lc

    async def always_none(prompt, *, base_url, model=None, api_key=""):
        return None

    monkeypatch.setattr(lc, "classify_llm", always_none)
    targets = [ClassifierTarget(model="a", base_url="http://x/v1")]
    assert asyncio.run(lc.classify_chain("prompt", targets)) is None


def test_chain_empty_targets_returns_none():
    assert asyncio.run(classify_chain("prompt", [])) is None


def test_chain_stops_at_first_success(monkeypatch):
    import smart_ai_router.llm_classifier as lc

    calls = []

    async def fake(prompt, *, base_url, model=None, api_key=""):
        calls.append(model)
        return ("coding", "moderate")

    monkeypatch.setattr(lc, "classify_llm", fake)
    targets = [
        ClassifierTarget(model="first", base_url="http://x/v1", label="llm"),
        ClassifierTarget(model="second", base_url="http://y/v1", label="llm-free"),
    ]
    result = asyncio.run(lc.classify_chain("prompt", targets))
    assert result == ("coding", "moderate", "llm")
    assert calls == ["first"]  # second target never tried
