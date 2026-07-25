"""Integration tests for the proxy's vision capability guard + file resolution.

The locked no-vision-model behavior: if a request carries an image but no
reachable model accepts images, the proxy fails clearly (422) instead of
silently dropping the image. These paths reject *before* any upstream forward,
so no provider mock is needed.
"""
import warnings

import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec
from smart_ai_router.store.sqlite_store import SqliteStore


def _client(cr) -> TestClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return TestClient(create_app(cr))


@pytest.fixture
def text_only(tmp_path, monkeypatch):
    """Open router whose only model has no vision support."""
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    monkeypatch.setenv("SMART_ROUTER_FILES_DIR", str(tmp_path / "blobs"))
    store = SqliteStore(":memory:")
    store.upsert_model(ModelSpec(
        "ollama/llama3.1:8b", provider="ollama", cost=0,
        reliability=1.0, tools=True, vision=False,
        competence={"general": 0.80, "coding": 0.80},
    ))
    return _client(CapabilityRouter(store=store))


def _inline_image_body():
    return {
        "model": "smart-worker",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]}],
    }


def test_image_without_vision_model_is_422(text_only):
    resp = text_only.post("/v1/chat/completions", json=_inline_image_body())
    assert resp.status_code == 422
    assert "vision" in resp.json()["detail"].lower()


def test_unknown_file_reference_is_404(text_only):
    body = {
        "model": "smart-worker",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "file-deadbeef"}},
        ]}],
    }
    resp = text_only.post("/v1/chat/completions", json=body)
    assert resp.status_code == 404


def test_text_only_request_passes_guard(text_only, monkeypatch):
    # A plain text request must not trip the vision guard; it should proceed to
    # routing/forwarding (which fails at the unreachable provider, not the guard).
    resp = text_only.post("/v1/chat/completions", json={
        "model": "smart-worker",
        "messages": [{"role": "user", "content": "hello"}],
    })
    # Not a 422 vision error — the guard let it through.
    assert resp.status_code != 422 or "vision" not in resp.json().get("detail", "").lower()
