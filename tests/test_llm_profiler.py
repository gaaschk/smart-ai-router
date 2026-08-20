"""LLM model profiling — rating vocabulary, composition, and the enrichment run.

Every test here is offline: rate_model() is monkeypatched or the HTTP layer is
faked, so the suite never depends on a provider being reachable.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from smart_ai_router import llm_profiler
from smart_ai_router.models import ModelSpec
from smart_ai_router.profiler import (
    RATINGS,
    apply_ratings,
    baseline_profile,
    profile_model,
)
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.taxonomy import FIELD_KEYS


def _spec(value: str, *, cost: int = 1, **kw) -> ModelSpec:
    profile = kw.pop("profile", None) or {f: 0.80 for f in FIELD_KEYS}
    return ModelSpec(value=value, provider="openrouter", cost=cost,
                     profile=profile, **kw)


# ── The ratings vocabulary ────────────────────────────────────────────────────

def test_ratings_are_signed_offsets_around_capable():
    """`capable` must be exactly neutral, or an unremarkable model would drift."""
    assert RATINGS["capable"][0] == 0.0
    assert RATINGS["specialty"][0] > 0
    assert RATINGS["weak"][0] < 0
    assert RATINGS["unsuited"][0] < RATINGS["weak"][0]


def test_apply_ratings_shifts_only_named_fields():
    base = {"software_engineering": 0.80, "law_regulatory": 0.80}
    out = apply_ratings(base, {"law_regulatory": "unsuited"})
    assert out["software_engineering"] == 0.80
    assert out["law_regulatory"] == pytest.approx(0.60)


def test_apply_ratings_clamps_to_the_profiler_scale():
    """A specialty rating on an already-topped-out model can't exceed the
    ceiling, and stacking unsuited can't drive a score negative."""
    out = apply_ratings({"a": 0.98, "b": 0.10}, {"a": "specialty", "b": "unsuited"})
    assert out["a"] <= 0.98
    assert out["b"] >= 0.10


def test_apply_ratings_ignores_unknown_ratings_and_empty_baseline():
    assert apply_ratings({"a": 0.5}, {"a": "brilliant"})["a"] == 0.5
    assert apply_ratings({}, {"a": "unsuited"}) == {}


def test_weak_rating_costs_more_than_the_top_depth_gap():
    """The magnitudes have to matter against taxonomy.DEPTHS or a rating is
    decorative: `weak` must be able to drop a model below a bar it cleared."""
    from smart_ai_router.taxonomy import DEPTHS

    assert abs(RATINGS["weak"][0]) > DEPTHS["frontier"] - DEPTHS["specialist"]


# ── Parsing rater replies ─────────────────────────────────────────────────────

def test_normalize_ratings_drops_capable_and_unknown_keys():
    ratings, note = llm_profiler.normalize_ratings({
        "software_engineering": "specialty",
        "law_regulatory": "capable",       # neutral — not worth storing
        "quantum_basketweaving": "weak",   # not a field
        "medicine_health": "nonsense",     # not a rating
        "note": "code-tuned model",
    })
    assert ratings == {"software_engineering": "specialty"}
    assert note == "code-tuned model"


def test_normalize_ratings_rejects_non_objects():
    assert llm_profiler.normalize_ratings("specialty") == ({}, "")
    assert llm_profiler.normalize_ratings(None) == ({}, "")


def test_parse_tolerates_fences_and_prose():
    ratings, note = llm_profiler._parse(
        'Sure!\n```json\n{"law_regulatory": "unsuited", "note": "coder"}\n```'
    )
    assert ratings == {"law_regulatory": "unsuited"}
    assert note == "coder"


def test_parse_truncates_a_runaway_note():
    ratings, note = llm_profiler._parse(
        '{"note": "%s"}' % ("x" * 500)
    )
    assert ratings == {}
    assert len(note) == 240


# ── The schema sent to the provider ───────────────────────────────────────────

def test_response_format_is_strict_and_covers_every_field():
    schema = llm_profiler._RESPONSE_FORMAT["json_schema"]
    assert schema["strict"] is True
    assert set(schema["schema"]["required"]) == {*FIELD_KEYS, "note"}
    assert schema["schema"]["additionalProperties"] is False


def test_response_format_avoids_keywords_openai_strict_mode_rejects():
    """minItems/maxItems/minLength 400 the whole call under strict mode."""
    import json

    blob = json.dumps(llm_profiler._RESPONSE_FORMAT)
    for banned in ("minItems", "maxItems", "uniqueItems", "minLength", "maxLength"):
        assert banned not in blob


def test_system_prompt_states_the_relative_framing():
    """The single most important instruction: rate relative to the model's own
    level. Without it the rater emits absolute scores and everything flattens."""
    assert "RELATIVE TO ITS OWN OVERALL" in llm_profiler._SYSTEM_PROMPT
    for rating in RATINGS:
        assert rating in llm_profiler._SYSTEM_PROMPT


# ── Candidate selection ───────────────────────────────────────────────────────

def test_candidates_are_cheapest_first():
    """Cost-ascending, because the router picks the cheapest qualifying model —
    an overstated cheap profile is the one that does damage."""
    models = [_spec("pricey", cost=9), _spec("cheap", cost=1), _spec("mid", cost=4)]
    got = llm_profiler.enrichment_candidates(models, limit=0)
    assert [s.value for s in got] == ["cheap", "mid", "pricey"]


def test_candidates_skip_already_rated_when_only_missing():
    rated = _spec("rated", profile_ratings={"law_regulatory": "weak"})
    fresh = _spec("fresh")
    got = llm_profiler.enrichment_candidates([rated, fresh], only_missing=True)
    assert [s.value for s in got] == ["fresh"]
    got_all = llm_profiler.enrichment_candidates([rated, fresh], only_missing=False)
    assert len(got_all) == 2


def test_candidates_skip_a_note_only_verdict():
    """An all-`capable` verdict stores no ratings but is still an answer; without
    this, every general-purpose model would be re-rated on every run."""
    judged = _spec("general", profile_note="broad general-purpose model")
    got = llm_profiler.enrichment_candidates([judged, _spec("fresh")])
    assert [s.value for s in got] == ["fresh"]


def test_candidates_skip_models_with_no_deterministic_profile():
    """Nothing to adjust, so nothing to rate — a rating would be inventing a
    profile out of the model's name, which is what benchmarks replaced."""
    unprofiled = ModelSpec(value="legacy", competence={"general": 0.7})
    got = llm_profiler.enrichment_candidates([unprofiled, _spec("ok")])
    assert [s.value for s in got] == ["ok"]


def test_candidate_limit_bounds_the_run():
    models = [_spec(f"m{i}", cost=i) for i in range(10)]
    assert len(llm_profiler.enrichment_candidates(models, limit=3)) == 3


# ── The enrichment run ────────────────────────────────────────────────────────

def _store_with(*specs: ModelSpec) -> SqliteStore:
    store = SqliteStore(":memory:")
    for spec in specs:
        store.upsert_model(spec)
    return store


def _fake_rater(replies: dict[str, tuple[dict[str, str], str] | None]):
    async def rate(spec, **kwargs):
        return replies.get(spec.value)
    return rate


def test_enrich_writes_ratings_and_recomposes_the_profile(monkeypatch):
    coder = _spec("openrouter/qwen3-coder", cost=1)
    store = _store_with(coder)
    monkeypatch.setattr(llm_profiler, "rate_model", _fake_rater({
        "openrouter/qwen3-coder": (
            {"law_regulatory": "unsuited", "software_engineering": "specialty"},
            "code specialist",
        ),
    }))

    result = asyncio.run(llm_profiler.enrich_models(
        store, store.all_models(), base_url="http://x/v1", model="rater",
    ))
    assert (result.rated, result.changed, result.written, result.failed) == (1, 1, 1, 0)

    stored = store.get("openrouter/qwen3-coder")
    assert stored.profile_ratings == {
        "law_regulatory": "unsuited", "software_engineering": "specialty"
    }
    assert stored.profile_note == "code specialist"
    assert stored.profile["law_regulatory"] == pytest.approx(0.60)
    assert stored.profile["software_engineering"] == pytest.approx(0.84)
    # The baseline survives the write, so a re-rate starts from measurement
    # rather than stacking on the previous adjustment.
    assert baseline_profile(stored)["law_regulatory"] == pytest.approx(0.80)


def test_enrich_dry_run_writes_nothing_but_reports_the_shifts(monkeypatch):
    store = _store_with(_spec("m", cost=1))
    monkeypatch.setattr(llm_profiler, "rate_model", _fake_rater({
        "m": ({"medicine_health": "weak"}, "small model"),
    }))
    result = asyncio.run(llm_profiler.enrich_models(
        store, store.all_models(), base_url="http://x/v1", model="rater",
        dry_run=True,
    ))
    assert result.written == 0
    assert result.changed == 1
    assert store.get("m").profile_ratings == {}
    shifts = result.changes[0]["shifts"]
    assert shifts["medicine_health"] == [0.8, 0.7]
    assert result.as_dict()["dry_run"] is True


def test_enrich_leaves_a_model_alone_when_rating_fails(monkeypatch):
    store = _store_with(_spec("good", cost=1), _spec("bad", cost=2))
    monkeypatch.setattr(llm_profiler, "rate_model", _fake_rater({
        "good": ({"law_regulatory": "weak"}, "ok"),
        "bad": None,
    }))
    result = asyncio.run(llm_profiler.enrich_models(
        store, store.all_models(), base_url="http://x/v1", model="rater",
    ))
    assert (result.rated, result.failed) == (1, 1)
    assert any("bad" in e for e in result.errors)
    assert store.get("bad").profile == _spec("bad").profile


def test_enrich_records_an_all_capable_verdict(monkeypatch):
    """"Nothing unusual here" is a real answer. It must be stored (as an empty
    rating set plus a note) so the next only_missing run doesn't re-ask — and
    only_missing keys on the note as well as the ratings."""
    store = _store_with(_spec("general", cost=1))
    monkeypatch.setattr(llm_profiler, "rate_model", _fake_rater({
        "general": ({}, "broad general-purpose model"),
    }))
    result = asyncio.run(llm_profiler.enrich_models(
        store, store.all_models(), base_url="http://x/v1", model="rater",
    ))
    assert (result.rated, result.changed, result.written) == (1, 0, 1)
    assert store.get("general").profile_note == "broad general-purpose model"


def test_enrich_refuses_to_run_without_a_rater():
    """The rater is the caller's to choose (helper_models.PROFILER); "" means it
    found none, and rating with no rater is not a thing this can improvise."""
    store = _store_with(_spec("m"))
    result = asyncio.run(llm_profiler.enrich_models(
        store, store.all_models(), base_url="http://x/v1", model="",
    ))
    assert result.rated == 0
    assert "no model profiler available" in result.errors[0]


def test_enrich_refuses_to_run_without_a_base_url():
    store = _store_with(_spec("m"))
    result = asyncio.run(llm_profiler.enrich_models(
        store, store.all_models(), base_url="", model="rater",
    ))
    assert result.rated == 0
    assert "OpenRouter" in result.errors[0]


def test_enrich_respects_the_limit(monkeypatch):
    store = _store_with(*[_spec(f"m{i}", cost=i) for i in range(6)])
    monkeypatch.setattr(llm_profiler, "rate_model", _fake_rater(
        {f"m{i}": ({"law_regulatory": "weak"}, "n") for i in range(6)}
    ))
    result = asyncio.run(llm_profiler.enrich_models(
        store, store.all_models(), base_url="http://x/v1", model="rater", limit=2,
    ))
    assert result.considered == 2
    assert result.written == 2


# ── The HTTP call itself ──────────────────────────────────────────────────────

def test_rate_model_sends_the_strict_schema_and_parses_the_reply(monkeypatch):
    sent: list[dict] = []

    async def fake_post(self, url, **kwargs):
        sent.append(kwargs.get("json") or {})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content":
                '{"law_regulatory": "unsuited", "note": "a coder"}'}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    got = asyncio.run(llm_profiler.rate_model(
        _spec("openrouter/qwen3-coder"), base_url="http://x/v1", model="rater",
    ))
    assert got == ({"law_regulatory": "unsuited"}, "a coder")
    assert sent[0]["response_format"] == llm_profiler._RESPONSE_FORMAT
    assert sent[0]["temperature"] == 0
    # The rater must see the level it is judging deviation from.
    assert "Measured overall capability" in sent[0]["messages"][1]["content"]


def test_rate_model_returns_none_on_provider_error(monkeypatch):
    async def fake_post(self, url, **kwargs):
        return httpx.Response(500, text="boom", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    assert asyncio.run(llm_profiler.rate_model(
        _spec("m"), base_url="http://x/v1", model="rater",
    )) is None


def test_rate_model_returns_none_on_network_failure(monkeypatch):
    async def fake_post(self, url, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    assert asyncio.run(llm_profiler.rate_model(
        _spec("m"), base_url="http://x/v1", model="rater",
    )) is None


def test_rate_model_returns_none_on_unparseable_reply(monkeypatch):
    async def fake_post(self, url, **kwargs):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "I'd rather not."}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    assert asyncio.run(llm_profiler.rate_model(
        _spec("m"), base_url="http://x/v1", model="rater",
    )) is None


def test_describe_model_includes_the_vendor_description():
    spec = profile_model("openrouter/qwen3-coder", description="Agentic coding model")
    text = llm_profiler._describe_model(
        ModelSpec(value="openrouter/qwen3-coder", profile=spec,
                  description="Agentic coding model")
    )
    assert "Agentic coding model" in text
    assert "openrouter/qwen3-coder" in text
