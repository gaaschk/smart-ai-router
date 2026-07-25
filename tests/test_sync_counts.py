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
