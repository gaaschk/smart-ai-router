"""Tests for Bedrock provider resolution in the proxy.

`_bedrock_base` builds the base URL the proxy forwards `bedrock/<model>`
requests to. AWS's current docs name `https://bedrock-runtime.{region}
.amazonaws.com/openai/v1/chat/completions` as the canonical OpenAI-compatible
route — the `/openai/` segment matters, a bare `/v1/chat/completions` is only
ever documented as a legacy override, not the default. These tests pin that
we build the `/openai/v1` form.
"""
from smart_ai_router.api.proxy import _bedrock_base, _resolve_provider
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ProviderConfig
from smart_ai_router.store.sqlite_store import SqliteStore


def _router_with_bedrock(region: str = "") -> CapabilityRouter:
    store = SqliteStore(":memory:")
    cr = CapabilityRouter(store)
    cr.upsert_provider(
        ProviderConfig(name="bedrock", kind="bedrock", api_key="fake-key",
                        base_url=region)
    )
    return cr


def test_bedrock_base_uses_openai_v1_path():
    cr = _router_with_bedrock("us-west-2")
    base_url, api_key = _bedrock_base(cr)
    assert base_url == "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1"
    assert api_key == "fake-key"


def test_bedrock_base_defaults_region_to_us_east_1():
    cr = _router_with_bedrock("")
    base_url, _ = _bedrock_base(cr)
    assert base_url == "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1"


def test_bedrock_base_is_none_without_a_configured_provider():
    store = SqliteStore(":memory:")
    cr = CapabilityRouter(store)
    assert _bedrock_base(cr) is None


def test_resolve_provider_strips_the_bedrock_prefix_and_builds_the_openai_url():
    cr = _router_with_bedrock("eu-central-1")
    base_url, api_key, real_model = _resolve_provider(
        "bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0", cr
    )
    assert base_url == "https://bedrock-runtime.eu-central-1.amazonaws.com/openai/v1"
    assert api_key == "fake-key"
    assert real_model == "anthropic.claude-sonnet-4-5-20250929-v1:0"
