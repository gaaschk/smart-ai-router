"""Tests for per-user model scope parsing + enforcement."""
from smart_ai_router.models import ModelSpec
from smart_ai_router.router import route
from smart_ai_router.scope import ModelScope, parse_scope
from smart_ai_router.store.sqlite_store import SqliteStore


# ── parse_scope ─────────────────────────────────────────────────────────────

def test_empty_scope_is_unrestricted():
    s = parse_scope("", 0)
    assert not s.is_restricted


def test_parses_allow_deny_lowercased():
    s = parse_scope('{"allow": ["OpenRouter/", "Ollama/"], "deny": ["Claude"]}', 0)
    assert s.allow == ("openrouter/", "ollama/")
    assert s.deny == ("claude",)
    assert s.is_restricted


def test_malformed_json_falls_back_to_unrestricted_but_keeps_max_tier():
    s = parse_scope("not json", 5)
    assert s.allow == () and s.deny == ()
    assert s.max_tier == 5
    assert s.is_restricted  # max_tier alone is a restriction


def test_max_tier_negative_clamped_to_zero():
    assert parse_scope("", -3).max_tier == 0


# ── ModelScope.permits ────────────────────────────────────────────────────────

def _spec(value, provider="", cost=0):
    return ModelSpec(value=value, provider=provider, cost=cost,
                     reliability=1.0, competence={"coding": 0.95})


def test_allow_is_whitelist():
    s = ModelScope(allow=("ollama/",))
    assert s.permits(_spec("ollama/llama3.1:8b"))
    assert not s.permits(_spec("openrouter/anthropic/claude"))


def test_deny_overrides_allow():
    s = ModelScope(allow=("openrouter/",), deny=("claude",))
    assert s.permits(_spec("openrouter/meta-llama/llama-3.3-70b"))
    assert not s.permits(_spec("openrouter/anthropic/claude-sonnet-4-6"))


def test_max_tier_ceiling():
    s = ModelScope(max_tier=3)
    assert s.permits(_spec("cheap", cost=2))
    assert s.permits(_spec("edge", cost=3))
    assert not s.permits(_spec("pricey", cost=8))


def test_matches_provider_field_too():
    s = ModelScope(deny=("bedrock",))
    assert not s.permits(_spec("some-model", provider="bedrock"))


# ── enforcement in route() ─────────────────────────────────────────────────────

def _store_with(*specs):
    store = SqliteStore(":memory:")
    for s in specs:
        store.upsert_model(s)
    return store


def test_route_respects_scope_over_cheapest():
    # Cheapest is the ollama model, but scope denies it → picks the allowed one.
    store = _store_with(
        _spec("ollama/llama3.1:8b", provider="ollama", cost=0),
        ModelSpec("openrouter/x", provider="openrouter", cost=2,
                  reliability=1.0, competence={"coding": 0.95}),
    )
    scope = ModelScope(allow=("openrouter/",))
    assert route(store, "coding", "hard", needs_tools=False, scope=scope) == "openrouter/x"


def test_scope_applies_to_fallback_pick():
    # Nothing clears the "hard" bar; fallback must still stay within scope.
    store = _store_with(
        ModelSpec("ollama/weak", provider="ollama", cost=0,
                  reliability=1.0, competence={"coding": 0.60}),
        ModelSpec("openrouter/weak", provider="openrouter", cost=2,
                  reliability=1.0, competence={"coding": 0.55}),
    )
    scope = ModelScope(allow=("openrouter/",))
    assert route(store, "coding", "hard", needs_tools=False, scope=scope) == "openrouter/weak"


def test_max_tier_excludes_expensive_in_route():
    store = _store_with(
        ModelSpec("cheap", cost=2, reliability=1.0, competence={"coding": 0.90}),
        ModelSpec("premium", cost=12, reliability=1.0, competence={"coding": 0.99}),
    )
    scope = ModelScope(max_tier=5)
    assert route(store, "coding", "hard", needs_tools=False, scope=scope) == "cheap"
