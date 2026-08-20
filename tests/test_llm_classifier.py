"""Tests for the LLM profile classifier — parsing, schema, chain, two-speed.

No network: every call either short-circuits before HTTP or goes through an
httpx MockTransport.
"""
import asyncio
import json

import httpx

from smart_ai_router import settings as _settings
from smart_ai_router.llm_classifier import (
    ClassifierTarget,
    _parse_profile,
    classifier_fallback_model,
    classifier_model,
    classify_chain,
    classify_llm,
    classify_profile_chain,
    classify_profile_llm,
    classify_profile_two_speed,
    needs_refinement,
)
from smart_ai_router.taxonomy import (
    DEMAND_KEYS,
    DEPTH_KEYS,
    FIELD_KEYS,
    STAKES_KEYS,
    DomainNeed,
    PromptProfile,
)


def _profile(*needs, demands=(), stakes="low"):
    """PromptProfile from (field, depth) pairs — test brevity helper."""
    return PromptProfile(
        domains=tuple(DomainNeed(f, d) for f, d in needs),
        demands=frozenset(demands),
        stakes=stakes,
    )


# ── Parsing ───────────────────────────────────────────────────────────────────

def test_parses_clean_json():
    p = _parse_profile(
        '{"domains":[{"field":"law_regulatory","depth":"specialist"}],'
        '"demands":["factual_precision"],"stakes":"high"}'
    )
    assert p == _profile(
        ("law_regulatory", "specialist"), demands=["factual_precision"], stakes="high"
    )


def test_parses_with_code_fence_and_prose():
    text = (
        'Sure!\n```json\n{"domains":[{"field":"software_engineering",'
        '"depth":"practitioner"}],"demands":[],"stakes":"low"}\n```'
    )
    assert _parse_profile(text) == _profile(("software_engineering", "practitioner"))


def test_drops_out_of_vocabulary_field():
    # "nuclear_engineering" isn't in FIELDS; the in-vocabulary entry survives.
    p = _parse_profile(
        '{"domains":[{"field":"nuclear_engineering","depth":"frontier"},'
        '{"field":"natural_science","depth":"specialist"}],'
        '"demands":[],"stakes":"low"}'
    )
    assert p == _profile(("natural_science", "specialist"))


def test_an_empty_domains_reply_profiles_as_a_trivial_prompt():
    """The live 3B triage model answers 'hi' and 'what's the capital of France?'
    with a schema-valid `{"domains": []}`. Dropping that on the floor cost the
    two-speed chain a third of its replies — all of them the easy prompts — and
    the fallback it landed on was the keyword classifier."""
    p = _parse_profile('{"domains": [], "demands": [], "stakes": "low"}')
    assert p == _profile(("general_knowledge", "surface"))


def test_returns_none_when_no_field_survives():
    # Nothing usable → None, so the caller falls through to the next target
    # rather than routing on an empty profile.
    assert _parse_profile('{"domains":[{"field":"astrology","depth":"frontier"}]}') is None


def test_rejects_garbage():
    assert _parse_profile("not json at all") is None
    assert _parse_profile("") is None
    assert _parse_profile("[]") is None


def test_unknown_depth_and_stakes_fall_back_to_defaults():
    p = _parse_profile(
        '{"domains":[{"field":"general_knowledge","depth":"godlike"}],'
        '"demands":["telepathy"],"stakes":"catastrophic"}'
    )
    assert p == _profile(("general_knowledge", "practitioner"))


# ── Config ────────────────────────────────────────────────────────────────────

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


def test_default_fallback_model(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_CLASSIFIER_FALLBACK", raising=False)
    assert classifier_fallback_model() == "nvidia/nemotron-nano-9b-v2:free"


def test_fallback_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_FALLBACK", "")
    assert classifier_fallback_model() == ""


# ── The HTTP call ─────────────────────────────────────────────────────────────

def test_returns_none_when_disabled(monkeypatch):
    # Non-blank prompt, but the model is disabled → None (fall back), no network.
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "")
    result = asyncio.run(
        classify_profile_llm(
            "Derive the hydrogen orbitals", base_url="http://localhost:11434/v1"
        )
    )
    assert result is None


def test_returns_none_on_empty_prompt():
    assert asyncio.run(classify_profile_llm("", base_url="http://localhost:11434/v1")) is None


def _mock_reply(monkeypatch, content: str, captured: dict):
    """Point llm_classifier's httpx at a transport returning `content`."""
    import smart_ai_router.llm_classifier as lc

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(lc.httpx, "AsyncClient", fake_client)


def test_requests_strict_profile_schema(monkeypatch):
    # The request MUST ask for a strict json_schema. Without a *schema* (a bare
    # json_object, or nothing), chatty instruct models answer the prompt instead
    # of profiling it and the whole chain silently falls through to the keyword
    # classifier — the live bug this guards.
    captured: dict = {}
    _mock_reply(
        monkeypatch,
        '{"domains":[{"field":"software_engineering","depth":"specialist"}],'
        '"demands":[],"stakes":"medium"}',
        captured,
    )
    result = asyncio.run(
        classify_profile_llm(
            "write a parser", base_url="http://localhost:11434/v1", model="llama3.1:8b"
        )
    )
    assert result == _profile(("software_engineering", "specialist"), stakes="medium")

    rf = captured["body"]["response_format"]
    assert rf["type"] == "json_schema"
    js = rf["json_schema"]
    assert js["strict"] is True
    schema = js["schema"]
    assert schema["required"] == ["domains", "demands", "stakes"]
    item_props = schema["properties"]["domains"]["items"]["properties"]
    assert tuple(item_props["field"]["enum"]) == FIELD_KEYS
    assert tuple(item_props["depth"]["enum"]) == DEPTH_KEYS
    assert tuple(schema["properties"]["demands"]["items"]["enum"]) == DEMAND_KEYS
    assert tuple(schema["properties"]["stakes"]["enum"]) == STAKES_KEYS
    # OpenAI strict mode rejects these outright, which would 400 every call.
    assert "minItems" not in schema["properties"]["domains"]
    assert "maxItems" not in schema["properties"]["domains"]
    # Three {field, depth} objects plus demands need real headroom; truncated
    # JSON parses as nothing at all.
    assert captured["body"]["max_tokens"] >= 200


def test_system_prompt_override_is_sent(monkeypatch):
    # The refine pass works by appending the triage profile to the rubric.
    captured: dict = {}
    _mock_reply(
        monkeypatch,
        '{"domains":[{"field":"general_knowledge","depth":"surface"}],'
        '"demands":[],"stakes":"low"}',
        captured,
    )
    asyncio.run(
        classify_profile_llm(
            "hi",
            base_url="http://x/v1",
            model="m",
            system_prompt="CUSTOM RUBRIC",
        )
    )
    assert captured["body"]["messages"][0] == {
        "role": "system",
        "content": "CUSTOM RUBRIC",
    }


def test_http_error_returns_none(monkeypatch):
    import smart_ai_router.llm_classifier as lc

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    real_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(lc.httpx, "AsyncClient", fake_client)
    assert asyncio.run(
        classify_profile_llm("x", base_url="http://x/v1", model="m")
    ) is None


# ── The fallback chain ────────────────────────────────────────────────────────

def test_chain_falls_through_to_second_target(monkeypatch):
    # First target fails; the chain must try the second.
    import smart_ai_router.llm_classifier as lc

    calls = []

    async def fake(prompt, *, base_url, model=None, api_key="", system_prompt=None):
        calls.append(model)
        if model == "local-bad":
            return None
        return _profile(("math_formal", "specialist"))

    monkeypatch.setattr(lc, "classify_profile_llm", fake)
    targets = [
        ClassifierTarget(model="local-bad", base_url="http://x/v1", label="llm"),
        ClassifierTarget(model="free-good", base_url="http://y/v1", label="llm-free"),
    ]
    profile, label = asyncio.run(classify_profile_chain("prompt", targets))
    assert profile == _profile(("math_formal", "specialist"))
    assert label == "llm-free"
    assert calls == ["local-bad", "free-good"]


def test_chain_returns_none_when_all_fail(monkeypatch):
    import smart_ai_router.llm_classifier as lc

    async def always_none(prompt, *, base_url, model=None, api_key="", system_prompt=None):
        return None

    monkeypatch.setattr(lc, "classify_profile_llm", always_none)
    targets = [ClassifierTarget(model="a", base_url="http://x/v1")]
    assert asyncio.run(classify_profile_chain("prompt", targets)) is None


def test_chain_empty_targets_returns_none():
    assert asyncio.run(classify_profile_chain("prompt", [])) is None


def test_chain_stops_at_first_success(monkeypatch):
    import smart_ai_router.llm_classifier as lc

    calls = []

    async def fake(prompt, *, base_url, model=None, api_key="", system_prompt=None):
        calls.append(model)
        return _profile(("technical_writing", "practitioner"))

    monkeypatch.setattr(lc, "classify_profile_llm", fake)
    targets = [
        ClassifierTarget(model="first", base_url="http://x/v1", label="llm"),
        ClassifierTarget(model="second", base_url="http://y/v1", label="llm-free"),
    ]
    profile, label = asyncio.run(classify_profile_chain("prompt", targets))
    assert label == "llm"
    assert calls == ["first"]  # second target never tried


# ── Escalation trigger ────────────────────────────────────────────────────────

def test_needs_refinement_on_high_stakes():
    assert needs_refinement(_profile(("medicine_health", "practitioner"), stakes="high"))


def test_needs_refinement_on_two_specialist_fields():
    # Cross-domain synthesis: where generalists produce their best nonsense.
    assert needs_refinement(
        _profile(("law_regulatory", "specialist"), ("natural_science", "specialist"))
    )


def test_needs_refinement_on_frontier_depth():
    assert needs_refinement(_profile(("math_formal", "frontier")))


def test_no_refinement_for_ordinary_prompts():
    assert not needs_refinement(_profile(("software_engineering", "practitioner")))
    assert not needs_refinement(_profile(("general_knowledge", "surface")))
    # One specialist field at medium stakes is exactly the case the local model
    # is trusted with — refining it would spend money on every serious prompt.
    assert not needs_refinement(
        _profile(("software_engineering", "specialist"), stakes="medium")
    )


# ── Two-speed chain ───────────────────────────────────────────────────────────

_TRIAGE = ClassifierTarget(model="local", base_url="http://x/v1", label="llm")
_REFINE = ClassifierTarget(
    model="big", base_url="http://y/v1", api_key="k", label="llm-refined"
)


def _stub_two_speed(monkeypatch, triage, refined, calls):
    import smart_ai_router.llm_classifier as lc

    async def fake(prompt, *, base_url, model=None, api_key="", system_prompt=None,
                   kind="classify"):
        calls.append(model)
        return refined if model == "big" else triage

    monkeypatch.setattr(lc, "classify_profile_llm", fake)


def test_two_speed_skips_refine_for_ordinary_prompt(monkeypatch):
    calls: list = []
    triage = _profile(("software_engineering", "practitioner"))
    _stub_two_speed(monkeypatch, triage, _profile(("math_formal", "frontier")), calls)
    profile, label = asyncio.run(
        classify_profile_two_speed("fix my test", [_TRIAGE], lambda: _REFINE)
    )
    assert (profile, label) == (triage, "llm")
    assert calls == ["local"]  # no paid call for an ordinary prompt


def test_two_speed_refines_consequential_prompt(monkeypatch):
    calls: list = []
    triage = _profile(("law_regulatory", "specialist"), ("natural_science", "specialist"))
    refined = _profile(
        ("law_regulatory", "frontier"),
        ("natural_science", "specialist"),
        demands=["factual_precision"],
        stakes="high",
    )
    _stub_two_speed(monkeypatch, triage, refined, calls)
    profile, label = asyncio.run(
        classify_profile_two_speed("48 jurisdictions of reactor law", [_TRIAGE], lambda: _REFINE)
    )
    assert (profile, label) == (refined, "llm-refined")
    assert calls == ["local", "big"]


def test_two_speed_can_lower_the_bar(monkeypatch):
    # Correcting downward matters as much as upward: a small model reading topic
    # words as depth is the expensive-in-the-other-direction failure.
    calls: list = []
    triage = _profile(("law_regulatory", "frontier"), stakes="high")
    refined = _profile(("general_knowledge", "surface"))
    _stub_two_speed(monkeypatch, triage, refined, calls)
    profile, label = asyncio.run(
        classify_profile_two_speed("what does GDPR stand for?", [_TRIAGE], lambda: _REFINE)
    )
    assert (profile, label) == (refined, "llm-refined")


def test_two_speed_keeps_triage_when_refine_fails(monkeypatch):
    # A classifier upgrade must never turn a routable request into a failed one.
    calls: list = []
    triage = _profile(("math_formal", "frontier"))
    _stub_two_speed(monkeypatch, triage, None, calls)
    profile, label = asyncio.run(
        classify_profile_two_speed("prove it", [_TRIAGE], lambda: _REFINE)
    )
    assert (profile, label) == (triage, "llm")
    assert calls == ["local", "big"]


def test_two_speed_does_not_resolve_a_refine_model_it_will_not_use(monkeypatch):
    """The refine target is a factory, not a value, because resolving it now means
    routing — a read of the whole model catalog. The escalation fires on a small
    minority of prompts, and the caller builds its arguments on every one, so the
    decision must not be made until a prompt actually escalates."""
    calls: list = []
    resolved: list[str] = []
    triage = _profile(("software_engineering", "practitioner"))
    _stub_two_speed(monkeypatch, triage, _profile(("math_formal", "frontier")), calls)

    def factory():
        resolved.append("resolved")
        return _REFINE

    asyncio.run(classify_profile_two_speed("fix my test", [_TRIAGE], factory))
    assert resolved == []


def test_two_speed_keeps_triage_when_no_refine_model_is_available(monkeypatch):
    """A factory returning None is the normal outcome of "nothing qualified to
    refine with" (helper_models.resolve). It degrades the routing decision to the
    local model's read of the prompt; it must never fail the request."""
    calls: list = []
    triage = _profile(("math_formal", "frontier"))
    _stub_two_speed(monkeypatch, triage, _profile(("general_knowledge", "surface")), calls)
    assert asyncio.run(
        classify_profile_two_speed("prove it", [_TRIAGE], lambda: None)
    ) == (triage, "llm")
    assert calls == ["local"]


def test_two_speed_without_refine_target(monkeypatch):
    calls: list = []
    triage = _profile(("math_formal", "frontier"))
    _stub_two_speed(monkeypatch, triage, _profile(("general_knowledge", "surface")), calls)
    assert asyncio.run(classify_profile_two_speed("prove it", [_TRIAGE], None)) == (
        triage,
        "llm",
    )
    assert calls == ["local"]


def test_two_speed_returns_none_when_triage_fails(monkeypatch):
    import smart_ai_router.llm_classifier as lc

    async def always_none(prompt, *, base_url, model=None, api_key="", system_prompt=None):
        return None

    monkeypatch.setattr(lc, "classify_profile_llm", always_none)
    assert asyncio.run(classify_profile_two_speed("x", [_TRIAGE], lambda: _REFINE)) is None


# ── Legacy label wrappers ─────────────────────────────────────────────────────

def test_legacy_classify_llm_derives_labels(monkeypatch):
    captured: dict = {}
    _mock_reply(
        monkeypatch,
        '{"domains":[{"field":"software_engineering","depth":"specialist"}],'
        '"demands":[],"stakes":"low"}',
        captured,
    )
    assert asyncio.run(
        classify_llm("refactor this", base_url="http://x/v1", model="m")
    ) == ("coding", "hard")


def test_legacy_classify_chain_derives_labels(monkeypatch):
    import smart_ai_router.llm_classifier as lc

    async def fake(prompt, *, base_url, model=None, api_key="", system_prompt=None):
        return _profile(("general_knowledge", "surface"))

    monkeypatch.setattr(lc, "classify_profile_llm", fake)
    assert asyncio.run(
        classify_chain("hi", [ClassifierTarget(model="m", base_url="http://x/v1")])
    ) == ("general", "trivial", "llm")
