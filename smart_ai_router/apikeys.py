"""API key minting and hashing helpers.

Keys are shown to the operator exactly once, at creation time. Only the SHA-256
hash is persisted, so a leaked database never exposes usable keys, and revoking
means flipping a row's `enabled` flag rather than editing an env var + redeploy.

Wire format is unchanged from the flat-allowlist scheme: clients still send
`Authorization: Bearer <key>`. What changed is that a key now resolves to a
per-user record (identity + scope + limits) instead of an anonymous set member.
"""
from __future__ import annotations

import hashlib
import secrets

# Human-recognizable prefix so a key is identifiable in logs/UI without
# revealing the secret, e.g. "sk-smart-3f9a1c...". Matches the OpenAI-style
# `sk-` convention many clients expect.
_KEY_PREFIX = "sk-smart-"

# Chars of the full key kept for display/identification (includes the literal
# prefix). Long enough to disambiguate keys, short enough to stay non-secret.
_DISPLAY_PREFIX_LEN = len(_KEY_PREFIX) + 6


def generate_key() -> str:
    """Mint a new random API key (plaintext). Store only its hash."""
    return f"{_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_key(key: str) -> str:
    """Return the SHA-256 hex digest used as the stored key identifier."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def display_prefix(key: str) -> str:
    """Return the non-secret leading slice of a key, for display/identification."""
    return key[:_DISPLAY_PREFIX_LEN]
