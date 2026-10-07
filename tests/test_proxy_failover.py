"""A model that fails before replying is recorded, and the request moves on.

The router picks on price among models that clear the bar, so a cheap model that
the provider refuses (403, 429, an outage) used to win every time and answer
nothing. Now the failure is stored with the provider's own words, the request is
re-routed to the next-best model, and a model that keeps failing sits out for a
while.
"""
import contextlib
import warnings

import httpx
import json
import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec
from smart_ai_router.store.sqlite_store import SqliteStore

_CODING = {"software_engineering": 0.95, "general_knowledge": 0.75}
_OK = {
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}
_SSE = (b'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n'
        b"data: [DONE]\n\n")


def _spec(value, cost) -> ModelSpec:
    return ModelSpec(
        value=value, provider="openrouter", ctx_k=200, reliability=1.0, tools=True,
        cost=cost, profile=dict(_CODING),
        competence={"coding": 0.95, "reasoning": 0.8, "docs": 0.8, "general": 0.8},
    )


class _Stream:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    async def aread(self):
        return self._body

    async def aiter_raw(self):
        yield self._body

    async def aiter_lines(self):
        for line in self._body.decode().splitlines():
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _client(monkeypatch) -> TestClient:
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    monkeypatch.delenv("SMART_ROUTER_MODEL_DENYLIST", raising=False)
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_FALLBACK", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_REFINE_MODEL", "")

    sent: list[str] = []

    def _refuse(body) -> bool:
        sent.append(body["model"])
        return "cheap" in body["model"]

    async def fake_post(self, url, **kw):
        req = httpx.Request("POST", url)
        if _refuse(kw["json"]):
            return httpx.Response(403, text="model not permitted", request=req)
        return httpx.Response(200, json=_OK, request=req)

    def fake_stream(self, method, url, **kw):
        if _refuse(kw["json"]):
            return _Stream(403, b"model not permitted")
        return _Stream(200, _SSE)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)

    store = SqliteStore(":memory:")
    store.upsert_model(_spec("openrouter/cheap-coder", cost=1))
    store.upsert_model(_spec("openrouter/dear-coder", cost=5))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = TestClient(create_app(CapabilityRouter(store=store)))
    c.sent, c.store = sent, store
    return c


def _chat(c, stream):
    return c.post("/v1/chat/completions", json={
        "model": "auto", "stream": stream,
        "messages": [{"role": "user", "content": "Refactor this Python parser."}],
    })


def _failures(c):
    return c.store.recent_model_failures("1970-01-01")


def test_refused_model_fails_over_and_is_recorded(monkeypatch):
    c = _client(monkeypatch)
    r = _chat(c, stream=False)
    assert r.status_code == 200
    assert c.sent == ["cheap-coder", "dear-coder"]
    (f,) = _failures(c)
    assert (f["model"], f["status"], f["detail"], f["failed_over_to"]) == (
        "openrouter/cheap-coder", 403, "model not permitted", "openrouter/dear-coder")


def test_streamed_request_fails_over_before_any_reply_byte(monkeypatch):
    c = _client(monkeypatch)
    r = _chat(c, stream=True)
    assert b'"content":"ok"' in r.content and b"error" not in r.content
    assert c.sent == ["cheap-coder", "dear-coder"]
    assert len(_failures(c)) == 1


def test_a_model_that_keeps_failing_sits_out(monkeypatch):
    c = _client(monkeypatch)
    _chat(c, stream=False)
    _chat(c, stream=False)          # second failure reaches the cooldown threshold
    c.sent.clear()
    _chat(c, stream=False)
    assert c.sent == ["dear-coder"]


def test_failover_off_returns_the_provider_error(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_FAILOVER_ATTEMPTS", "0")
    c = _client(monkeypatch)
    r = _chat(c, stream=False)
    assert r.status_code == 403
    assert len(_failures(c)) == 1 and _failures(c)[0]["failed_over_to"] == ""


def test_failures_are_readable_over_the_api(monkeypatch):
    c = _client(monkeypatch)
    _chat(c, stream=False)
    (row,) = c.get("/api/model-failures").json()
    assert row["model"] == "openrouter/cheap-coder" and row["status"] == 403


def test_agent_rate_limit_fails_over_and_records_failure(monkeypatch):
    c = _client(monkeypatch)

    def rate_limited_stream(self, method, url, **kw):
        model = kw["json"]["model"]
        c.sent.append(model)
        assert kw["json"]["tools"]
        if model == "cheap-coder":
            return _Stream(429, b"upstream temporarily rate-limited")
        return _Stream(200, _SSE)

    monkeypatch.setattr(httpx.AsyncClient, "stream", rate_limited_stream)
    r = c.post("/v1/chat/completions", json={
        "model": "auto", "stream": True, "agent": True,
        "messages": [{"role": "user", "content": "Refactor this Python parser."}],
    })
    assert b'"content": "ok"' in r.content and b'"error"' not in r.content
    assert c.sent == ["cheap-coder", "dear-coder"]
    (failure,) = _failures(c)
    assert failure["status"] == 429
    assert failure["failed_over_to"] == "openrouter/dear-coder"


@pytest.mark.parametrize("partial", [False, True])
def test_agent_connection_error_only_fails_over_before_delta(monkeypatch, partial):
    c = _client(monkeypatch)

    class BrokenStream(_Stream):
        async def aiter_lines(self):
            if partial:
                yield 'data: {"choices":[{"delta":{"content":"partial"}}]}'
            raise httpx.ReadError("connection lost")

    def stream(self, method, url, **kw):
        model = kw["json"]["model"]
        c.sent.append(model)
        return BrokenStream(200, b"") if model == "cheap-coder" else _Stream(200, _SSE)

    monkeypatch.setattr(httpx.AsyncClient, "stream", stream)
    r = c.post("/v1/chat/completions", json={
        "model": "auto", "agent": True, "messages": [],
    })
    assert c.sent == (["cheap-coder"] if partial else ["cheap-coder", "dear-coder"])
    assert (b'"error"' in r.content) is partial


def test_agent_later_round_failover_preserves_completed_tools(monkeypatch):
    c = _client(monkeypatch)
    executions = []
    tool_call = {"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": "call_1", "type": "function",
        "function": {"name": "list_dir", "arguments": "{}"},
    }]}}]}

    def execute(user, name, args, **kw):
        executions.append(name)
        return "file.txt"

    def stream(self, method, url, **kw):
        c.sent.append(kw["json"]["model"])
        if len(c.sent) == 1:
            return _Stream(200, ("data: " + json.dumps(tool_call) + "\n\ndata: [DONE]\n\n").encode())
        assert kw["json"]["messages"][-1] == {
            "role": "tool", "tool_call_id": "call_1", "content": "file.txt",
        }
        return _Stream(429, b"rate limited") if len(c.sent) == 2 else _Stream(200, _SSE)

    monkeypatch.setattr("smart_ai_router.tools.execute_tool", execute)
    monkeypatch.setattr(httpx.AsyncClient, "stream", stream)
    r = c.post("/v1/chat/completions", json={
        "model": "auto", "agent": True, "messages": [],
    })
    assert b'"error"' not in r.content
    assert c.sent == ["cheap-coder", "cheap-coder", "dear-coder"]
    assert executions == ["list_dir"]


def test_agent_failover_off_records_original_error(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_FAILOVER_ATTEMPTS", "0")
    c = _client(monkeypatch)
    r = c.post("/v1/chat/completions", json={
        "model": "auto", "agent": True, "messages": [],
    })
    assert b"provider 403" in r.content
    assert c.sent == ["cheap-coder"]
    assert _failures(c)[0]["failed_over_to"] == ""
