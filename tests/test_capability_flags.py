"""The two capability flags the router persists: structured_outputs, reasoning.

Both are *shape* facts, not quality ones, and both are load-bearing:
`structured_outputs` is a hard filter for the router's own helper calls (prompt
refinement, model profiling), which fail silently — parsing nothing at all —
against a model that accepts `response_format` and ignores the schema. `reasoning`
records that a model spends output budget thinking before it answers.

Covered here: derivation per provider on sync, persistence through the store
(including a row written before the columns existed), and the router filter.
"""
from __future__ import annotations

import dataclasses
import json
from io import BytesIO

import pytest

from smart_ai_router import sync as sync_mod
from smart_ai_router.models import ModelSpec
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.sync import sync_from_providers
from smart_ai_router.taxonomy import DomainNeed, PromptProfile, FIELD_KEYS


def _store() -> SqliteStore:
    return SqliteStore(":memory:")


def _fake_urlopen(payload):
    def _open(req, timeout=0):
        class _Resp(BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _Resp(json.dumps(payload).encode())
    return _open


# ── Persistence ───────────────────────────────────────────────────────────────

def test_flags_round_trip():
    store = _store()
    store.upsert_model(ModelSpec(value="m", structured_outputs=True, reasoning=True))
    got = store.get("m")
    assert (got.structured_outputs, got.reasoning) == (True, True)


def test_flags_default_to_false():
    """False in both directions is the safe default: a model is only offered a
    schema, or excused from a small output budget, on positive evidence."""
    store = _store()
    store.upsert_model(ModelSpec(value="m"))
    got = store.get("m")
    assert (got.structured_outputs, got.reasoning) == (False, False)


def test_an_upsert_can_turn_a_flag_off_again():
    """The ON CONFLICT clause has to carry these columns: a catalog that stops
    advertising structured outputs must stop the router offering it a schema."""
    store = _store()
    store.upsert_model(ModelSpec(value="m", structured_outputs=True))
    store.upsert_model(dataclasses.replace(store.get("m"), structured_outputs=False))
    assert store.get("m").structured_outputs is False


def test_pre_migration_rows_read_as_false():
    """A DB written before these columns existed has NULL there, which must read
    as "not known to support it" rather than crashing or reading as True."""
    store = _store()
    store.upsert_model(ModelSpec(value="m", structured_outputs=True, reasoning=True))
    store._conn.execute(
        "UPDATE models SET structured_outputs=NULL, reasoning=NULL WHERE value='m'"
    )
    got = store.get("m")
    assert (got.structured_outputs, got.reasoning) == (False, False)


def test_all_models_reads_the_flags_too():
    """The router reads via all_models(), so a flag only read by get() would be
    invisible to the filter that depends on it."""
    store = _store()
    store.upsert_model(ModelSpec(value="m", structured_outputs=True))
    assert store.all_models()[0].structured_outputs is True


# ── Derivation on sync ────────────────────────────────────────────────────────

def _or_catalog(*entries) -> dict:
    return {"data": list(entries)}


def _or_entry(mid: str, supported: list[str]) -> dict:
    return {
        "id": mid,
        "context_length": 128_000,
        "architecture": {"input_modalities": ["text"]},
        "pricing": {"prompt": "0.000001", "completion": "0.000003"},
        "supported_parameters": supported,
    }


def test_openrouter_reads_structured_outputs_not_response_format(monkeypatch):
    """The distinction is the whole point of the flag. A model advertising only
    `response_format` accepts `json_object` and ignores the schema, which is the
    silent failure — an answer to the prompt where a filled-in shape was needed."""
    store = _store()
    monkeypatch.setattr(sync_mod.urllib.request, "urlopen", _fake_urlopen(_or_catalog(
        _or_entry("v/schema", ["structured_outputs", "response_format"]),
        _or_entry("v/json-only", ["response_format"]),
        _or_entry("v/neither", ["tools"]),
    )))
    sync_from_providers(store, openrouter_key="k")
    flags = {s.value: s.structured_outputs for s in store.all_models()}
    assert flags == {
        "openrouter/v/schema": True,
        "openrouter/v/json-only": False,
        "openrouter/v/neither": False,
    }


def test_openrouter_reasoning_comes_from_the_same_signal_as_the_profile(monkeypatch):
    """One derivation, two consumers: the profiler lifts the reasoning-heavy
    fields off `supports_reasoning` and the spec stores the same bit. Deriving it
    twice would let the stored flag disagree with the profile it shaped."""
    store = _store()
    monkeypatch.setattr(sync_mod.urllib.request, "urlopen", _fake_urlopen(_or_catalog(
        _or_entry("v/thinker", ["reasoning"]),
        _or_entry("v/plain", ["tools"]),
    )))
    sync_from_providers(store, openrouter_key="k")
    flags = {s.value: s.reasoning for s in store.all_models()}
    assert flags == {"openrouter/v/thinker": True, "openrouter/v/plain": False}


def test_ollama_models_all_honor_a_schema(monkeypatch):
    """Ollama implements response_format server-side as constrained decoding, so
    it holds for every model it serves regardless of what the model was tuned
    for — the flag is a property of the server here, not of the weights."""
    store = _store()
    monkeypatch.setattr(sync_mod.urllib.request, "urlopen", _fake_urlopen(
        {"models": [{"name": "qwen2.5:3b"}]}
    ))
    sync_from_providers(store, ollama_base_url="http://x")
    assert store.get("ollama/qwen2.5:3b").structured_outputs is True


def test_bedrock_models_are_flagged_reasoning_but_not_schema():
    """Extended thinking is documented for every Claude model on Bedrock; schema
    support through its OpenAI-compatible endpoint is unverified, and False is
    the direction that fails by not using a model rather than by trusting one."""
    store = _store()
    sync_from_providers(store, bedrock_key="x")
    specs = store.all_models()
    assert specs and all(s.reasoning for s in specs)
    assert not any(s.structured_outputs for s in specs)


# ── The router filter ─────────────────────────────────────────────────────────

def _spec(value: str, cost: int, *, structured: bool) -> ModelSpec:
    return ModelSpec(
        value=value, provider="openrouter", cost=cost, reliability=1.0,
        structured_outputs=structured, profile={f: 0.95 for f in FIELD_KEYS},
    )


def _profile() -> PromptProfile:
    return PromptProfile(domains=(DomainNeed("general_knowledge", "specialist"),))


def test_needs_structured_skips_a_cheaper_model_that_cannot_hold_a_shape():
    store = _store()
    store.upsert_model(_spec("openrouter/cheap", 1, structured=False))
    store.upsert_model(_spec("openrouter/schema", 5, structured=True))

    assert CapabilityRouter(store=store).select(_profile()).model == "openrouter/cheap"
    picked = CapabilityRouter(store=store).select(_profile(), needs_structured=True)
    assert picked.model == "openrouter/schema"
    assert picked.qualified


def test_needs_structured_with_no_capable_model_leaves_nothing_eligible():
    """A hard filter, not a preference: it empties the pool rather than falling
    back to the closest miss, which is how every other hard filter behaves and
    why helper_models.resolve() treats the RuntimeError as "skip this call".
    Routing to a model that returns prose where a schema was required would cost
    money to produce something nothing downstream can parse."""
    store = _store()
    store.upsert_model(_spec("openrouter/cheap", 1, structured=False))
    with pytest.raises(RuntimeError):
        CapabilityRouter(store=store).select(_profile(), needs_structured=True)
