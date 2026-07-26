"""Integration tests for the proxy's agent-mode capability guard.

Agent (filesystem) mode requires a tool-capable model. If none is reachable
for the caller, the proxy must fail clearly (422) rather than silently ignore
the request's tools and answer without ever touching the workspace. Mirrors the
vision guard — rejects before any upstream forward, so no provider mock needed.
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
def no_tools(tmp_path, monkeypatch):
    """Open router whose only model does NOT support tool calling."""
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    monkeypatch.setenv("SMART_ROUTER_WORKSPACE_DIR", str(tmp_path / "ws"))
    store = SqliteStore(":memory:")
    store.upsert_model(ModelSpec(
        "ollama/llama3.1:8b", provider="ollama", cost=0,
        reliability=1.0, tools=False, vision=False,
        competence={"general": 0.80, "coding": 0.80},
    ))
    return _client(CapabilityRouter(store=store))


@pytest.fixture
def with_tools(tmp_path, monkeypatch):
    """Open router whose model DOES support tool calling."""
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    monkeypatch.setenv("SMART_ROUTER_WORKSPACE_DIR", str(tmp_path / "ws"))
    # Disable the LLM classifier so tests use the deterministic path only.
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_FALLBACK", "")
    store = SqliteStore(":memory:")
    store.upsert_model(ModelSpec(
        "ollama/llama3.1:8b", provider="ollama", cost=0,
        reliability=1.0, tools=True, vision=False,
        competence={"general": 0.80, "coding": 0.80},
    ))
    return _client(CapabilityRouter(store=store))


def test_agent_mode_without_tool_model_is_422(no_tools):
    resp = no_tools.post("/v1/chat/completions", json={
        "model": "smart-worker",
        "agent": True,
        "messages": [{"role": "user", "content": "list my files"}],
    })
    assert resp.status_code == 422
    assert "tool" in resp.json()["detail"].lower()


def test_non_agent_request_is_not_blocked_by_agent_guard(no_tools):
    # Without agent mode, a tools-less deployment must still serve plain chat
    # (it fails later at the unreachable provider, not at the agent guard).
    resp = no_tools.post("/v1/chat/completions", json={
        "model": "smart-worker",
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert not (resp.status_code == 422 and "tool" in resp.json().get("detail", "").lower())


# ── auto-detection (tri-state agent flag) ─────────────────────────────────────

def _is_agent_guard_422(resp) -> bool:
    return resp.status_code == 422 and "tool" in resp.json().get("detail", "").lower()


def test_auto_actionable_without_tool_model_degrades_not_422(no_tools):
    # Auto must NEVER lock a user out: an actionable prompt on a tools-less
    # deployment falls back to plain chat instead of the explicit-mode 422.
    resp = no_tools.post("/v1/chat/completions", json={
        "model": "smart-worker",
        "agent": "auto",
        "messages": [{"role": "user", "content": "make me a resume PDF"}],
    })
    assert not _is_agent_guard_422(resp)


def test_absent_flag_defaults_to_auto_not_422(no_tools):
    # No `agent` key at all → auto → actionable prompt still must not 422 here.
    resp = no_tools.post("/v1/chat/completions", json={
        "model": "smart-worker",
        "messages": [{"role": "user", "content": "create a report.docx"}],
    })
    assert not _is_agent_guard_422(resp)


def test_explicit_false_never_enters_agent(no_tools):
    # Even an actionable prompt stays plain chat when the user turned agent off.
    resp = no_tools.post("/v1/chat/completions", json={
        "model": "smart-worker",
        "agent": False,
        "messages": [{"role": "user", "content": "make me a resume PDF"}],
    })
    assert not _is_agent_guard_422(resp)


def test_auto_actionable_with_tool_model_enters_agent(with_tools):
    # Response headers are set before the (lazy) stream runs, so we can assert
    # the agent path was taken even though the upstream provider isn't reachable.
    resp = with_tools.post("/v1/chat/completions", json={
        "model": "smart-worker",
        "agent": "auto",
        "messages": [{"role": "user", "content": "make me a resume PDF"}],
    })
    assert resp.headers.get("X-Agent") == "true"
    assert resp.headers.get("X-Agent-Auto") == "true"


def test_auto_plain_question_stays_non_agent(with_tools):
    resp = with_tools.post("/v1/chat/completions", json={
        "model": "smart-worker",
        "agent": "auto",
        "messages": [{"role": "user", "content": "what is the capital of France?"}],
    })
    assert resp.headers.get("X-Agent") != "true"


def test_explicit_true_with_tool_model_is_not_auto(with_tools):
    resp = with_tools.post("/v1/chat/completions", json={
        "model": "smart-worker",
        "agent": True,
        "messages": [{"role": "user", "content": "what is the capital of France?"}],
    })
    assert resp.headers.get("X-Agent") == "true"
    assert resp.headers.get("X-Agent-Auto") == "false"
