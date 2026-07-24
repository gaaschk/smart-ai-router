"""Tests for the SQLite API-key + usage-log store layer."""
from smart_ai_router.apikeys import display_prefix, generate_key, hash_key
from smart_ai_router.models import ApiKey, UsageRecord
from smart_ai_router.store.sqlite_store import SqliteStore


def _key(user="alice"):
    plaintext = generate_key()
    return plaintext, ApiKey(
        key_hash=hash_key(plaintext),
        user=user,
        key_prefix=display_prefix(plaintext),
    )


def test_create_and_lookup_by_hash():
    store = SqliteStore(":memory:")
    plaintext, key = _key()
    created = store.create_api_key(key)
    assert created.id > 0
    assert created.created_at  # stamped on insert

    found = store.get_api_key_by_hash(hash_key(plaintext))
    assert found is not None
    assert found.user == "alice"
    assert found.enabled is True


def test_lookup_unknown_hash_returns_none():
    store = SqliteStore(":memory:")
    assert store.get_api_key_by_hash(hash_key("nope")) is None


def test_disable_and_delete_by_prefix():
    store = SqliteStore(":memory:")
    plaintext, key = _key()
    store.create_api_key(key)
    prefix = key.key_prefix

    assert store.set_api_key_enabled(prefix, False) is True
    assert store.get_api_key_by_hash(hash_key(plaintext)).enabled is False

    assert store.delete_api_key(prefix) is True
    assert store.get_api_key_by_hash(hash_key(plaintext)) is None
    # Idempotent: deleting again reports "nothing matched".
    assert store.delete_api_key(prefix) is False
    assert store.set_api_key_enabled(prefix, True) is False


def test_all_api_keys_ordered():
    store = SqliteStore(":memory:")
    for u in ("a", "b", "c"):
        _, k = _key(u)
        store.create_api_key(k)
    users = [k.user for k in store.all_api_keys()]
    assert users == ["a", "b", "c"]


def test_touch_updates_last_used():
    store = SqliteStore(":memory:")
    plaintext, key = _key()
    store.create_api_key(key)
    assert store.get_api_key_by_hash(hash_key(plaintext)).last_used_at == ""
    store.touch_api_key(hash_key(plaintext))
    assert store.get_api_key_by_hash(hash_key(plaintext)).last_used_at != ""


def test_record_and_query_usage():
    store = SqliteStore(":memory:")
    store.record_usage(UsageRecord(
        user="alice", key_prefix="sk-smart-aaa", routed_model="ollama/llama3.1:8b",
        domain="coding", complexity="moderate",
        prompt_tokens=100, completion_tokens=50, cost_usd=0.0, status=200,
        ts="2026-07-24T00:00:00+00:00",
    ))
    store.record_usage(UsageRecord(
        user="bob", prompt_tokens=10, ts="2026-07-24T00:00:00+00:00",
    ))
    alice = store.recent_usage("alice", "2026-01-01T00:00:00+00:00")
    assert len(alice) == 1
    assert alice[0].prompt_tokens == 100
    assert alice[0].routed_model == "ollama/llama3.1:8b"
    # Window filter excludes older rows.
    assert store.recent_usage("alice", "2027-01-01T00:00:00+00:00") == []
