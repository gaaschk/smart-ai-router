"""Tests for sync change-counting and pruning of vanished models.

The dashboard's "updated" count should reflect only models that actually
changed, and models that disappear from a provider's catalog should be removed
from the store (but never on a failed fetch).
"""
import json
from io import BytesIO

import pytest

from smart_ai_router import sync as sync_mod
from smart_ai_router.models import ModelSpec
from smart_ai_router.sync import sync_from_providers
from smart_ai_router.store.sqlite_store import SqliteStore


# ── Change counting (bedrock is deterministic, needs no network) ───────────────

def test_first_bedrock_sync_all_added():
    store = SqliteStore(":memory:")
    r = sync_from_providers(store, bedrock_key="x")
    assert r.added == 3
    assert r.updated == 0 and r.unchanged == 0 and r.removed == 0
    assert r.total == 3


def test_second_identical_sync_reports_no_changes():
    store = SqliteStore(":memory:")
    sync_from_providers(store, bedrock_key="x")
    r = sync_from_providers(store, bedrock_key="x")
    # Nothing changed → all unchanged, total (changed) is zero.
    assert r.added == 0 and r.updated == 0 and r.removed == 0
    assert r.unchanged == 3
    assert r.total == 0


def test_only_genuinely_changed_models_count_as_updated():
    store = SqliteStore(":memory:")
    sync_from_providers(store, bedrock_key="x")
    # Mutate one stored model so it differs from the catalog spec.
    haiku = next(s for s in store.all_models() if "haiku" in s.value)
    store.upsert_model(ModelSpec(**{**haiku.__dict__, "ctx_k": haiku.ctx_k + 1}))

    r = sync_from_providers(store, bedrock_key="x")
    assert r.updated == 1
    assert r.unchanged == 2
    assert r.added == 0 and r.removed == 0


# ── Pruning models that no longer exist ────────────────────────────────────────

def _fake_urlopen(payload):
    def _open(req, timeout=0):
        class _Resp(BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _Resp(json.dumps(payload).encode())
    return _open


def test_missing_ollama_models_are_removed(monkeypatch):
    store = SqliteStore(":memory:")
    monkeypatch.setattr(
        sync_mod.urllib.request, "urlopen",
        _fake_urlopen({"models": [{"name": "a"}, {"name": "b"}]}),
    )
    r1 = sync_from_providers(store, ollama_base_url="http://x")
    assert r1.added == 2

    # Next catalog dropped "b" → it should be deleted from the store.
    monkeypatch.setattr(
        sync_mod.urllib.request, "urlopen",
        _fake_urlopen({"models": [{"name": "a"}]}),
    )
    r2 = sync_from_providers(store, ollama_base_url="http://x")
    assert r2.removed == 1
    assert {s.value for s in store.all_models()} == {"ollama/a"}


def test_failed_fetch_never_prunes(monkeypatch):
    store = SqliteStore(":memory:")
    monkeypatch.setattr(
        sync_mod.urllib.request, "urlopen",
        _fake_urlopen({"models": [{"name": "a"}, {"name": "b"}]}),
    )
    sync_from_providers(store, ollama_base_url="http://x")

    def _boom(req, timeout=0):
        raise OSError("network down")
    monkeypatch.setattr(sync_mod.urllib.request, "urlopen", _boom)

    r = sync_from_providers(store, ollama_base_url="http://x")
    assert r.errors and r.removed == 0
    # Catalog is preserved through a transient outage.
    assert len(store.all_models()) == 2


def test_pruning_is_scoped_to_the_synced_provider(monkeypatch):
    store = SqliteStore(":memory:")
    sync_from_providers(store, bedrock_key="x")  # 3 bedrock models
    monkeypatch.setattr(
        sync_mod.urllib.request, "urlopen",
        _fake_urlopen({"models": [{"name": "a"}]}),
    )
    r = sync_from_providers(store, ollama_base_url="http://x")
    # Ollama sync must not delete bedrock models.
    assert r.removed == 0
    assert len([s for s in store.all_models() if s.provider == "bedrock"]) == 3


# ── Ollama capability detection (/api/show) ───────────────────────────────────
# Ollama's /api/tags says nothing about capabilities, so sync used to hardcode
# tools=False and leave vision unset. Every local model therefore looked
# incapable and agent-mode/vision requests could never route locally.

def _fake_ollama(tags, show_by_model, *, show_fails=False):
    """Route /api/tags vs /api/show, keyed on the requested model name."""
    def _open(req, timeout=0):
        class _Resp(BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        url = req.full_url if hasattr(req, "full_url") else str(req)
        if url.endswith("/api/show"):
            if show_fails:
                raise OSError("show unavailable")
            name = json.loads(req.data.decode())["model"]
            return _Resp(json.dumps(show_by_model.get(name, {})).encode())
        return _Resp(json.dumps(tags).encode())
    return _open


def test_ollama_capabilities_come_from_api_show(monkeypatch):
    store = SqliteStore(":memory:")
    monkeypatch.setattr(
        sync_mod.urllib.request, "urlopen",
        _fake_ollama(
            {"models": [
                {"name": "seer:12b", "size": 7_600_000_000},
                {"name": "plain:8b", "size": 4_900_000_000},
                {"name": "dumb:1b", "size": 1_000_000_000},
            ]},
            {
                "seer:12b": {
                    "capabilities": ["completion", "vision", "tools", "thinking"],
                    "model_info": {"seer.context_length": 262144},
                },
                "plain:8b": {
                    "capabilities": ["completion", "tools"],
                    "model_info": {"plain.context_length": 131072},
                },
                "dumb:1b": {"capabilities": ["completion"], "model_info": {}},
            },
        ),
    )
    sync_from_providers(store, ollama_base_url="http://x")
    by_id = {s.value: s for s in store.all_models()}

    seer = by_id["ollama/seer:12b"]
    assert (seer.tools, seer.vision) == (True, True)
    assert seer.ctx_k == 262  # real context, not the size-based guess (32)

    plain = by_id["ollama/plain:8b"]
    assert (plain.tools, plain.vision) == (True, False)
    assert plain.ctx_k == 131

    # No tools capability → stays off. The fix must not blanket-enable.
    dumb = by_id["ollama/dumb:1b"]
    assert (dumb.tools, dumb.vision) == (False, False)


# ── OpenRouter ':batch' variants ──────────────────────────────────────────────
# OpenRouter lists batch-only mirrors of many models at ~50% of the sibling's
# price. A synchronous /v1/chat/completions call to one 404s with "This model is
# only available through the Batch API", but the discount put them at the front
# of every cheapest-first sort — so the router kept picking a model that cannot
# answer. They must never enter the catalog.

def _openrouter_model(mid, prompt="0.000003", completion="0.000015"):
    return {
        "id": mid,
        "architecture": {"modality": "text->text"},
        "context_length": 200_000,
        "pricing": {"prompt": prompt, "completion": completion},
        "supported_parameters": ["tools"],
    }


def test_batch_only_variants_are_never_synced(monkeypatch):
    store = SqliteStore(":memory:")
    monkeypatch.setattr(
        sync_mod.urllib.request, "urlopen",
        _fake_urlopen({"data": [
            _openrouter_model("anthropic/claude-sonnet-5"),
            # Half price, so it would win any cheapest-first tie-break.
            _openrouter_model("anthropic/claude-sonnet-5:batch",
                              prompt="0.0000015", completion="0.0000075"),
            _openrouter_model("openai/gpt-5:batch"),
        ]}),
    )
    r = sync_from_providers(store, openrouter_key="x")
    assert r.added == 1
    assert {s.value for s in store.all_models()} == {
        "openrouter/anthropic/claude-sonnet-5"
    }


def test_batch_variants_already_in_the_store_are_pruned(monkeypatch):
    """The filter has to clear catalogs synced before it existed."""
    store = SqliteStore(":memory:")
    store.upsert_model(ModelSpec(
        value="openrouter/anthropic/claude-sonnet-5:batch",
        provider="openrouter", ctx_k=200, cost=3, tools=True,
    ))
    monkeypatch.setattr(
        sync_mod.urllib.request, "urlopen",
        _fake_urlopen({"data": [_openrouter_model("anthropic/claude-sonnet-5")]}),
    )
    r = sync_from_providers(store, openrouter_key="x")
    assert r.removed == 1
    assert not [s for s in store.all_models() if s.value.endswith(":batch")]


def test_non_batch_variants_still_sync(monkeypatch):
    """':free' and ':thinking' answer synchronously — only ':batch' is async."""
    store = SqliteStore(":memory:")
    monkeypatch.setattr(
        sync_mod.urllib.request, "urlopen",
        _fake_urlopen({"data": [
            _openrouter_model("qwen/qwen3-235b:free", prompt="0", completion="0"),
            _openrouter_model("qwen/qwen-plus:thinking"),
        ]}),
    )
    r = sync_from_providers(store, openrouter_key="x")
    assert r.added == 2


def test_ollama_falls_back_to_size_guess_when_show_fails(monkeypatch):
    """A flaky /api/show degrades one model's metadata, not the whole sync."""
    store = SqliteStore(":memory:")
    monkeypatch.setattr(
        sync_mod.urllib.request, "urlopen",
        _fake_ollama(
            {"models": [{"name": "big:70b", "size": 42_000_000_000}]},
            {},
            show_fails=True,
        ),
    )
    r = sync_from_providers(store, ollama_base_url="http://x")
    assert not r.errors and r.added == 1
    spec = store.all_models()[0]
    assert spec.ctx_k == 128          # size-based fallback (>30GB)
    assert (spec.tools, spec.vision) == (False, False)  # conservative
