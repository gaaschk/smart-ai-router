"""The agentic axis — persistence and sync wiring for ModelSpec.agentic.

`agentic_index` used to be written over the `operations_process` *field*, which
conflated two different things: knowledge about operations work, and the stamina
to hold a multi-step tool loop together. Because that field is one of two feeding
the legacy `general` column, an agentic benchmark also became half of every
model's reported general competence — claude-haiku-4.5's 0.68 was
mean(0.604 agentic, 0.759 knowledge).

It is now its own column. These tests cover the two places that can lose it: the
store (a new column on an existing DB) and sync (the only thing that populates
it). The router's use of it lives in test_router_select.py, and the mapping
itself in test_profiler.py.
"""
from __future__ import annotations

import json
from io import BytesIO

import pytest

from smart_ai_router import sync as sync_mod
from smart_ai_router.models import ModelSpec
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.sync import sync_from_providers


def _store() -> SqliteStore:
    return SqliteStore(":memory:")


# ── Persistence ───────────────────────────────────────────────────────────────

def test_agentic_round_trips():
    store = _store()
    store.upsert_model(ModelSpec(value="m", agentic=0.604))
    assert store.get("m").agentic == 0.604


def test_agentic_survives_all_models():
    # The router reads via all_models(), so a column that only round-trips
    # through get() would be invisible to every routing decision.
    store = _store()
    store.upsert_model(ModelSpec(value="m", agentic=0.62))
    assert store.all_models()[0].agentic == 0.62


def test_agentic_is_clamped_on_write():
    store = _store()
    store.upsert_model(ModelSpec(value="hi", agentic=4.0))
    store.upsert_model(ModelSpec(value="lo", agentic=-1.0))
    assert store.get("hi").agentic == 1.0
    assert store.get("lo").agentic == 0.0


def test_an_upsert_updates_agentic():
    # A re-sync with a fresh benchmark has to move the number, not keep the first
    # one it ever saw.
    store = _store()
    store.upsert_model(ModelSpec(value="m", agentic=0.20))
    store.upsert_model(ModelSpec(value="m", agentic=0.90))
    assert store.get("m").agentic == 0.90


def test_pre_migration_rows_read_as_unmeasured():
    """A DB written before this column existed must keep routing.

    The migration backfills DEFAULT 0.0, but a NULL can still reach the read path
    (an explicit NULL write, or a store implementation that adds the column
    without a default), and float(None) would raise on every all_models() call —
    taking the whole router down rather than one model's score.
    """
    store = _store()
    store.upsert_model(ModelSpec(value="m", agentic=0.62))
    store._conn.execute("UPDATE models SET agentic=NULL WHERE value='m'")
    assert store.get("m").agentic == 0.0


# ── Sync wiring ───────────────────────────────────────────────────────────────

def _fake_urlopen(payload):
    def _open(req, timeout=0):
        class _Resp(BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _Resp(json.dumps(payload).encode())
    return _open


def _openrouter_model(mid, **benchmarks):
    entry = {
        "id": mid,
        "architecture": {"modality": "text->text"},
        "context_length": 200_000,
        "pricing": {"prompt": "0.000001", "completion": "0.000005"},
        "supported_parameters": ["tools"],
        "description": "A model.",
    }
    if benchmarks:
        entry["benchmarks"] = {"artificial_analysis": benchmarks}
    return entry


def test_sync_puts_the_measured_index_on_the_axis(monkeypatch):
    store = _store()
    monkeypatch.setattr(
        sync_mod.urllib.request, "urlopen",
        _fake_urlopen({"data": [
            _openrouter_model("anthropic/claude-haiku-4.5",
                              intelligence_index=29.9, agentic_index=16.5),
        ]}),
    )
    sync_from_providers(store, openrouter_key="x")
    spec = store.get("openrouter/anthropic/claude-haiku-4.5")
    assert 0.0 < spec.agentic < 1.0


def test_sync_no_longer_lets_the_index_dent_a_field(monkeypatch):
    """The actual regression: haiku-4.5 reported 0.68 general competence because
    `operations_process` carried its agentic score instead of its knowledge."""
    store = _store()
    monkeypatch.setattr(
        sync_mod.urllib.request, "urlopen",
        _fake_urlopen({"data": [
            _openrouter_model("anthropic/claude-haiku-4.5",
                              intelligence_index=29.9, agentic_index=16.5),
        ]}),
    )
    sync_from_providers(store, openrouter_key="x")
    spec = store.get("openrouter/anthropic/claude-haiku-4.5")
    assert spec.profile["operations_process"] == spec.profile["general_knowledge"]
    # `general` averages operations_process and general_knowledge, so with the
    # override gone it reports knowledge rather than half a tool-loop benchmark.
    assert spec.competence["general"] == spec.profile["general_knowledge"]


def test_a_model_with_no_benchmarks_syncs_as_unmeasured(monkeypatch):
    store = _store()
    monkeypatch.setattr(
        sync_mod.urllib.request, "urlopen",
        _fake_urlopen({"data": [_openrouter_model("vendor/plain")]}),
    )
    sync_from_providers(store, openrouter_key="x")
    assert store.get("openrouter/vendor/plain").agentic == 0.0


def test_local_and_bedrock_models_are_unmeasured(monkeypatch):
    # Neither provider publishes benchmarks, so 0.0 is the honest answer — and the
    # router reads it as exempt, which is why these models keep tool traffic.
    store = _store()
    sync_from_providers(store, bedrock_key="x")
    assert all(s.agentic == 0.0 for s in store.all_models())

    monkeypatch.setattr(
        sync_mod.urllib.request, "urlopen",
        _fake_urlopen({"models": [{"name": "qwen3:30b", "size": 20_000_000_000}]}),
    )
    sync_from_providers(store, ollama_base_url="http://x")
    assert store.get("ollama/qwen3:30b").agentic == 0.0


def test_a_new_catalog_signal_cannot_be_dropped_silently(monkeypatch):
    """sync pops `agentic_index` out of the signals dict and passes the rest to
    profile_model(**signals). That is deliberate: adding a signal to
    extract_catalog_signals() without teaching profile_model about it must fail
    loudly rather than being ignored for the whole catalog.

    The signal key set is a fixed dict literal, so this can only ever break at
    code-change time, never on live catalog data — which is why a raise is the
    right kind of loud.
    """
    real = sync_mod.extract_catalog_signals
    monkeypatch.setattr(
        sync_mod, "extract_catalog_signals",
        lambda entry: {**real(entry), "brand_new_index": 1.0},
    )
    monkeypatch.setattr(
        sync_mod.urllib.request, "urlopen",
        _fake_urlopen({"data": [_openrouter_model("vendor/plain")]}),
    )
    with pytest.raises(TypeError, match="brand_new_index"):
        sync_from_providers(_store(), openrouter_key="x")
