"""Tests for the core routing logic."""
import pytest
from capability_router.models import ModelSpec
from capability_router.store.sqlite_store import SqliteStore
from capability_router.router import route, DEFAULT_THRESHOLDS


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


def test_raises_when_matrix_empty():
    store = SqliteStore(":memory:")
    with pytest.raises(RuntimeError, match="no eligible model"):
        route(store, "coding", "hard", needs_tools=False)
