"""Tests for the capability layer — column-reduction over the model matrix."""
from smart_ai_router.capabilities import compute_capabilities, reachable_models
from smart_ai_router.models import ModelSpec
from smart_ai_router.scope import ModelScope


def _m(value, *, provider="openrouter", vision=False, tools=False, ctx_k=8,
       reliability=1.0, cost=1):
    return ModelSpec(
        value=value, provider=provider, vision=vision, tools=tools, ctx_k=ctx_k,
        reliability=reliability, cost=cost, competence={"coding": 0.9},
    )


def test_empty_matrix_supports_nothing():
    caps = compute_capabilities([])
    assert not caps.vision and not caps.tools
    assert caps.max_context_k == 0 and caps.model_count == 0
    assert caps.providers == ()


def test_vision_on_when_any_model_has_it():
    caps = compute_capabilities([
        _m("ollama/qwen3", provider="ollama", vision=False),
        _m("ollama/llava", provider="ollama", vision=True),
    ])
    assert caps.vision is True


def test_vision_off_when_no_model_has_it():
    caps = compute_capabilities([
        _m("ollama/qwen3", provider="ollama", vision=False),
        _m("ollama/mistral", provider="ollama", vision=False),
    ])
    assert caps.vision is False


def test_provider_agnostic_any_provider_lights_the_column():
    # The vision model is on openrouter; the deployment still reports vision on.
    caps = compute_capabilities([
        _m("ollama/qwen3", provider="ollama", vision=False, tools=True),
        _m("openrouter/gpt-vision", provider="openrouter", vision=True),
    ])
    assert caps.vision is True and caps.tools is True
    assert set(caps.providers) == {"ollama", "openrouter"}


def test_max_context_is_largest_reachable():
    caps = compute_capabilities([_m("a", ctx_k=8), _m("b", ctx_k=200)])
    assert caps.max_context_k == 200


def test_unreliable_models_are_not_reachable():
    # A vision model below the reliability bar shouldn't advertise vision.
    caps = compute_capabilities([_m("flaky", vision=True, reliability=0.1)])
    assert caps.vision is False
    assert caps.model_count == 0


def test_denylist_excludes_from_capabilities(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_MODEL_DENYLIST", "llava")
    caps = compute_capabilities([
        _m("ollama/llava", provider="ollama", vision=True),
        _m("ollama/qwen3", provider="ollama", vision=False),
    ])
    # The only vision model is denylisted → vision unavailable.
    assert caps.vision is False


def test_scope_narrows_capabilities():
    models = [
        _m("ollama/llava", provider="ollama", vision=True),
        _m("openrouter/text-only", provider="openrouter", vision=False),
    ]
    # A key scoped to openrouter/ can't reach the vision model.
    scope = ModelScope(allow=("openrouter/",))
    caps = compute_capabilities(models, scope=scope)
    assert caps.vision is False
    assert caps.providers == ("openrouter",)


def test_reachable_models_helper_filters():
    models = [
        _m("good"),
        _m("flaky", reliability=0.0),
    ]
    reach = reachable_models(models)
    assert [m.value for m in reach] == ["good"]
