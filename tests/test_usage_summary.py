"""Tests for the usage-log aggregation (SqliteStore.usage_summary).

The dashboard rolls up usage_log rows into totals + per-model / per-day /
per-domain / per-user breakdowns, computed in SQL. Admin (user=None) sees all
users and a by_user block; a scoped user sees only their own rows and no
by_user.
"""
import sqlite3

from smart_ai_router.models import UsageRecord
from smart_ai_router.store.sqlite_store import SqliteStore


def _rec(user, model, ts, *, pt=100, ct=50, cost=0.01, domain="coding",
         complexity="moderate", estimated=False):
    return UsageRecord(
        user=user, key_prefix=user[:4], routed_model=model, domain=domain,
        complexity=complexity, prompt_tokens=pt, completion_tokens=ct,
        cost_usd=cost, status=200, tokens_estimated=estimated, ts=ts,
    )


def _seed(store):
    # alice: 2 requests (one estimated), bob: 1 request, different models/days.
    store.record_usage(_rec("alice", "openrouter/gpt-4", "2026-07-01T10:00:00+00:00"))
    store.record_usage(_rec("alice", "ollama/llama3", "2026-07-02T10:00:00+00:00",
                            estimated=True, cost=0.0))
    store.record_usage(_rec("bob", "openrouter/gpt-4", "2026-07-02T12:00:00+00:00",
                            domain="docs", complexity="trivial", cost=0.02))


def test_totals_sum_across_all_users_for_admin():
    store = SqliteStore(":memory:")
    _seed(store)
    summary = store.usage_summary()
    t = summary["totals"]
    assert t["requests"] == 3
    assert t["prompt_tokens"] == 300
    assert t["completion_tokens"] == 150
    assert abs(t["cost_usd"] - 0.03) < 1e-9
    assert t["estimated_rows"] == 1


def test_by_model_groups_and_sums():
    store = SqliteStore(":memory:")
    _seed(store)
    by_model = {r["key"]: r for r in store.usage_summary()["by_model"]}
    assert by_model["openrouter/gpt-4"]["requests"] == 2
    assert by_model["ollama/llama3"]["requests"] == 1


def test_by_day_uses_iso_date_prefix():
    store = SqliteStore(":memory:")
    _seed(store)
    by_day = {r["key"]: r for r in store.usage_summary()["by_day"]}
    assert set(by_day) == {"2026-07-01", "2026-07-02"}
    assert by_day["2026-07-02"]["requests"] == 2  # alice(est) + bob


def test_by_domain_combines_domain_and_complexity():
    store = SqliteStore(":memory:")
    _seed(store)
    keys = {r["key"] for r in store.usage_summary()["by_domain"]}
    assert "coding/moderate" in keys
    assert "docs/trivial" in keys


def test_admin_view_includes_by_user():
    store = SqliteStore(":memory:")
    _seed(store)
    by_user = {r["key"]: r for r in store.usage_summary()["by_user"]}
    assert by_user["alice"]["requests"] == 2
    assert by_user["bob"]["requests"] == 1


def test_scoped_user_sees_only_own_rows_and_no_by_user():
    store = SqliteStore(":memory:")
    _seed(store)
    summary = store.usage_summary(user="alice")
    assert summary["totals"]["requests"] == 2
    assert "by_user" not in summary
    # No bob model leaks into alice's breakdown.
    models = {r["key"] for r in summary["by_model"]}
    assert models == {"openrouter/gpt-4", "ollama/llama3"}


def test_since_ts_bounds_the_window():
    store = SqliteStore(":memory:")
    _seed(store)
    summary = store.usage_summary(since_ts="2026-07-02T00:00:00+00:00")
    assert summary["totals"]["requests"] == 2  # July 1 row excluded


def test_empty_window_returns_zeroed_totals_not_none():
    store = SqliteStore(":memory:")
    summary = store.usage_summary()
    t = summary["totals"]
    assert t == {
        "requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "cost_usd": 0.0, "estimated_rows": 0,
    }
    assert summary["by_model"] == []


def test_tokens_estimated_round_trips_through_record_and_read():
    store = SqliteStore(":memory:")
    store.record_usage(_rec("alice", "ollama/x", "2026-07-01T00:00:00+00:00",
                            estimated=True))
    rec = store.recent_usage("alice", "2026-07-01T00:00:00+00:00")[0]
    assert rec.tokens_estimated is True


def test_migration_adds_tokens_estimated_to_preexisting_db(tmp_path):
    """A DB created before the tokens_estimated column must migrate cleanly and
    read old rows back as not-estimated."""
    db = tmp_path / "old.db"
    # Build a usage_log WITHOUT tokens_estimated, as an old release would have.
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user TEXT,
            key_prefix TEXT, routed_model TEXT, domain TEXT, complexity TEXT,
            prompt_tokens INTEGER, completion_tokens INTEGER, cost_usd REAL,
            status INTEGER
        )
    """)
    conn.execute(
        "INSERT INTO usage_log (ts, user, routed_model, prompt_tokens, "
        "completion_tokens, cost_usd, status) VALUES "
        "('2026-07-01T00:00:00+00:00','alice','m',10,5,0.001,200)"
    )
    conn.commit()
    conn.close()

    # Opening via SqliteStore runs _migrate(), which must add the column.
    store = SqliteStore(db)
    rec = store.recent_usage("alice", "2026-07-01T00:00:00+00:00")[0]
    assert rec.tokens_estimated is False
    assert rec.prompt_tokens == 10
    # And aggregation works over the migrated table.
    assert store.usage_summary()["totals"]["requests"] == 1
