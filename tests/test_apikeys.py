"""Tests for API key minting/hashing helpers (no network, no DB)."""
from smart_ai_router.apikeys import display_prefix, generate_key, hash_key


def test_generated_keys_are_prefixed_and_unique():
    a, b = generate_key(), generate_key()
    assert a.startswith("sk-smart-")
    assert b.startswith("sk-smart-")
    assert a != b  # 32 bytes of urlsafe randomness — collisions are impossible


def test_hash_is_deterministic_and_hex():
    key = generate_key()
    h = hash_key(key)
    assert h == hash_key(key)              # deterministic
    assert len(h) == 64                     # sha-256 hex
    assert h != key                         # never the plaintext


def test_different_keys_hash_differently():
    assert hash_key(generate_key()) != hash_key(generate_key())


def test_display_prefix_is_non_secret_slice():
    key = generate_key()
    prefix = display_prefix(key)
    assert key.startswith(prefix)
    assert len(prefix) < len(key)           # never the whole secret
    assert prefix.startswith("sk-smart-")
