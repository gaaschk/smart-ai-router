"""Tests for the core routing logic."""
import pytest
from smart_ai_router.models import ModelSpec
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.router import route, DEFAULT_THRESHOLDS


def _store_with(*specs):
    store = SqliteStore(":memory:")
    for s in specs:
        store.upsert_model(s)
    return store


def test_picks_cheapest_over_bar():
    store = _store_with(
        ModelSpec("a", cost=5, reliability=1.0, competence={"coding": 0.90}),
        ModelSpec("b", cost=1, reliability=1.0, competence={"coding": 0.91}),
        ModelSpec("c", cost=0, reliability=1.0, competence={"coding": 0.89}),
    )
    assert route(store, "coding", "hard", needs_tools=False) == "c"


def test_excludes_low_reliability():
    store = _store_with(
        ModelSpec("cheap-flaky", cost=0, reliability=0.5, competence={"coding": 0.95}),
        ModelSpec("reliable",    cost=2, reliability=1.0, competence={"coding": 0.89}),
    )
    result = route(store, "coding", "hard", needs_tools=False)
    assert result == "reliable"


def test_exclude_set():
    store = _store_with(
        ModelSpec("a", cost=0, reliability=1.0, competence={"coding": 0.95}),
        ModelSpec("b", cost=1, reliability=1.0, competence={"coding": 0.90}),
    )
    assert route(store, "coding", "hard", needs_tools=False, exclude={"a"}) == "b"


def test_tools_filter():
    store = _store_with(
        ModelSpec("no-tools",   cost=0, reliability=1.0, tools=False, competence={"coding": 0.95}),
        ModelSpec("with-tools", cost=1, reliability=1.0, tools=True,  competence={"coding": 0.88}),
    )
    assert route(store, "coding", "hard", needs_tools=True) == "with-tools"


def test_fallback_when_nothing_clears_bar():
    store = _store_with(
        ModelSpec("only", cost=0, reliability=1.0, competence={"coding": 0.40}),
    )
    # 0.40 < 0.88 (hard bar) but fallback should still return it
    result = route(store, "coding", "hard", needs_tools=False)
    assert result == "only"


def test_denylist_excludes_matching_models(monkeypatch):
    # A broken-in-this-environment model is excluded by substring, even though
    # it's the cheapest and clears the bar. Survives sync() resetting reliability.
    monkeypatch.setenv("SMART_ROUTER_MODEL_DENYLIST", "mxfp8")
    store = _store_with(
        ModelSpec("ollama/qwen3.6:35b-a3b-coding-mxfp8", cost=0, reliability=1.0,
                  competence={"general": 0.95}),
        ModelSpec("ollama/llama3", cost=0, reliability=1.0,
                  competence={"general": 0.60}),
    )
    assert route(store, "general", "trivial", needs_tools=False) == "ollama/llama3"


def test_denylist_also_excludes_from_fallback(monkeypatch):
    # Even when nothing clears the bar, a denylisted model must not be the
    # fallback pick.
    monkeypatch.setenv("SMART_ROUTER_MODEL_DENYLIST", "mxfp8")
    store = _store_with(
        ModelSpec("ollama/qwen3.6:35b-a3b-coding-mxfp8", cost=0, reliability=1.0,
                  competence={"coding": 0.99}),
        ModelSpec("ollama/llama3", cost=0, reliability=1.0,
                  competence={"coding": 0.10}),
    )
    # hard bar is 0.88; only the denylisted model clears it, so fallback picks
    # the next-best eligible model instead.
    assert route(store, "coding", "hard", needs_tools=False) == "ollama/llama3"


def test_empty_denylist_is_noop(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_MODEL_DENYLIST", raising=False)
    store = _store_with(
        ModelSpec("ollama/qwen3.6:35b-a3b-coding-mxfp8", cost=0, reliability=1.0,
                  competence={"general": 0.95}),
    )
    assert route(store, "general", "trivial", needs_tools=False) == "ollama/qwen3.6:35b-a3b-coding-mxfp8"


def test_raises_when_matrix_empty():
    store = SqliteStore(":memory:")
    with pytest.raises(RuntimeError, match="no eligible model"):
        route(store, "coding", "hard", needs_tools=False)
