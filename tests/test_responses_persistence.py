"""Owner isolation, continuation, retention, pagination and failure semantics."""
import asyncio
import json

import httpx
import pytest
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from smart_ai_router.api import responses_routes as api
from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec, ProviderConfig
from smart_ai_router.store.sqlite_store import SqliteStore


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", "admin-key")
    for name in ("CLASSIFIER_MODEL", "CLASSIFIER_FALLBACK", "CLASSIFIER_REFINE_MODEL", "MODEL_DENYLIST"):
        monkeypatch.setenv("SMART_ROUTER_" + name, "")
    from smart_ai_router.api import proxy
    monkeypatch.setattr(proxy, "_enrich_with_gbrain_context", lambda messages, *args, **kwargs: messages)
    monkeypatch.setattr(proxy, "_schedule_gbrain_save_back", lambda *args, **kwargs: None)
    store = SqliteStore(":memory:")
    store.upsert_model(ModelSpec(value="openrouter/test", provider="openrouter", tools=True,
                                structured_outputs=True, ctx_k=200, cost=2, reliability=1,
                                competence={"coding": .99, "general": .99, "reasoning": .99, "docs": .99}))
    cr = CapabilityRouter(store=store)
    client = TestClient(create_app(cr))
    admin = {"Authorization": "Bearer admin-key"}
    headers = {}
    for user in ("alice", "bob"):
        key = client.post("/api/keys", headers=admin, json={"user": user}).json()["key"]
        headers[user] = {"Authorization": "Bearer " + key}
    return client, cr, store, headers, admin


def canned(message=None, finish="stop"):
    return {"choices": [{"message": message or {"content": "ok"}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def fake_chat(monkeypatch, data=None):
    async def chat(request):
        return JSONResponse(data or canned())
    monkeypatch.setattr(api, "chat_completions", chat)


def post(client, headers, **kwargs):
    return client.post("/v1/responses", headers=headers, json={"model": "smart-worker", "input": "hello", **kwargs})


def test_storage_ownership_and_stateless(setup, monkeypatch):
    client, cr, store, headers, admin = setup
    fake_chat(monkeypatch)
    result = post(client, headers["alice"]).json()
    rid = result["id"]
    assert result["store"] is True
    assert client.get(f"/v1/responses/{rid}", headers=headers["alice"]).json() == result
    for other in (headers["bob"], admin):
        for suffix in ("", "/input_items"):
            denied = client.get(f"/v1/responses/{rid}{suffix}", headers=other)
            assert denied.status_code == 404
            assert denied.json()["error"]["code"] == "response_not_found"
        assert client.delete(f"/v1/responses/{rid}", headers=other).status_code == 404
        assert post(client, other, previous_response_id=rid).status_code == 404
    assert client.get(f"/v1/responses/{rid}").status_code == 401
    assert isinstance(client.get(f"/v1/responses/{rid}").json()["error"], dict)
    transient = post(client, headers["alice"], store=False).json()
    assert cr.get_response(transient["id"], "alice") is None
    assert post(client, headers["alice"], previous_response_id=transient["id"]).status_code == 404
    assert client.delete(f"/v1/responses/{rid}", headers=headers["alice"]).json()["deleted"]
    assert client.get(f"/v1/responses/{rid}", headers=headers["alice"]).status_code == 404
    # A late stream finalizer may not recreate a deleted object.
    cr.save_response("alice", result, [])
    assert cr.get_response(rid, "alice") is None


def test_chained_tool_continuation_and_paging(setup, monkeypatch):
    client, cr, store, headers, _ = setup
    seen = []

    async def chat(request):
        body = await request.json()
        seen.append(body)
        if len(seen) == 1:
            return JSONResponse(canned({"content": None, "tool_calls": [
                {"id": "call_a", "function": {"name": "read", "arguments": "{}"}},
                {"id": "call_b", "function": {"name": "read", "arguments": "{}"}}]}, "tool_calls"))
        return JSONResponse(canned())

    monkeypatch.setattr(api, "chat_completions", chat)
    tools = [{"type": "function", "name": "read", "strict": False}]
    first = post(client, headers["alice"], tools=tools, instructions="Old").json()
    second = post(client, headers["alice"], tools=tools, previous_response_id=first["id"], instructions="New", input=[
        {"type": "function_call_output", "call_id": "call_a", "output": "A"},
        {"type": "function_call_output", "call_id": "call_b", "output": "B"}]).json()
    assert second["status"] == "completed"
    assert seen[1]["messages"][0]["content"] == "New"
    assert "Old" not in json.dumps(seen[1])
    assert [c["id"] for c in seen[1]["messages"][2]["tool_calls"]] == ["call_a", "call_b"]
    path = f"/v1/responses/{second['id']}/input_items"
    all_items = client.get(path, headers=headers["alice"], params={"order": "asc", "limit": 100}).json()["data"]
    assert len(all_items) == 5
    page = client.get(path, headers=headers["alice"], params={"order": "asc", "limit": 2}).json()
    assert page["has_more"] and page["data"] == all_items[:2]
    next_page = client.get(path, headers=headers["alice"], params={"order": "asc", "limit": 100, "after": page["last_id"]}).json()
    assert next_page["data"] == all_items[2:] and not next_page["has_more"]
    assert client.get(path, headers=headers["alice"], params={"order": "desc"}).json()["data"] == all_items[::-1]
    assert client.get(path, headers=headers["alice"], params={"limit": "bad"}).status_code == 400
    assert client.get(path, headers=headers["alice"], params={"after": "bad"}).status_code == 400
    assert post(client, headers["alice"], previous_response_id=first["id"], input=[
        {"type": "function_call_output", "call_id": "other", "output": "bad"}]).status_code == 400
    assert post(client, headers["alice"], previous_response_id=first["id"], input=[
        {"type": "function_call_output", "call_id": "call_a", "output": "only one"}]).status_code == 400
    # Children have a self-contained snapshot and survive deletion of the parent.
    client.delete(f"/v1/responses/{first['id']}", headers=headers["alice"])
    assert post(client, headers["alice"], previous_response_id=second["id"]).status_code == 200


def test_sqlite_restart_and_retention(tmp_path, monkeypatch):
    path = tmp_path / "responses.db"
    store = SqliteStore(path)
    response = {"id": "resp_1", "status": "completed"}
    store.save_response("alice", response, [{"id": "item_1"}], create=True)
    reopened = SqliteStore(path)
    assert reopened.get_response("resp_1", "alice")["input"] == [{"id": "item_1"}]
    assert reopened.get_response("resp_1", "bob") is None
    import time
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 30 * 86400 + 1)
    assert reopened.get_response("resp_1", "alice") is None
    assert reopened._conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 0


def test_stream_storage_failure_and_cancellation(setup, monkeypatch):
    client, cr, store, headers, _ = setup

    async def chat(request):
        async def source():
            yield b'data: {"error":{"message":"failed"}}\n\n'
        return StreamingResponse(source())

    monkeypatch.setattr(api, "chat_completions", chat)
    result = post(client, headers["alice"], stream=True)
    events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
    rid = events[0]["response"]["id"]
    assert cr.get_response(rid, "alice")["response"]["status"] == "failed"
    assert post(client, headers["alice"], previous_response_id=rid).status_code == 400

    async def check():
        closed = []
        response = api._response({"model": "smart-worker"})
        cr.save_response("alice", response, [], create=True)

        async def source():
            try:
                yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                await asyncio.sleep(60)
            finally:
                closed.append(True)

        stream = api._stream(source(), response, save=lambda r: cr.save_response("alice", r, []))
        for _ in range(3):
            await anext(stream)
        await stream.aclose()
        assert closed
        assert cr.get_response(response["id"], "alice")["response"]["error"]["message"] == "Client disconnected"
    asyncio.run(check())


@pytest.mark.parametrize("tools,structured", [(False, True), (True, False)])
def test_essential_capabilities_not_silently_dropped(setup, monkeypatch, tools, structured):
    client, cr, store, headers, _ = setup
    model = cr.get_model("openrouter/test")
    model.tools, model.structured_outputs = tools, structured
    store.upsert_model(model)
    result = post(client, headers["alice"], tools=[{"type": "function", "name": "read", "strict": True}])
    assert result.status_code == 422
    if not structured:
        schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        assert post(client, headers["alice"], text={"format": {"type": "json_schema", "name": "result", "strict": True, "schema": schema}}).status_code == 422


def test_required_tool_and_schema_validation(setup, monkeypatch):
    client, cr, store, headers, _ = setup
    fake_chat(monkeypatch)
    assert post(client, headers["alice"], tools=[{"type": "function", "name": "read", "strict": False}], tool_choice="required").status_code == 502
    schema = {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"], "additionalProperties": False}
    fmt = {"format": {"type": "json_schema", "name": "plan", "strict": True, "schema": schema}}
    assert post(client, headers["alice"], text=fmt).status_code == 502
    fake_chat(monkeypatch, canned({"content": '{"action":"run"}'}))
    assert post(client, headers["alice"], text=fmt).status_code == 200
    fmt["format"]["schema"]["additionalProperties"] = True
    assert post(client, headers["alice"], text=fmt).status_code == 400


@pytest.mark.parametrize("provider,base", [("ollama", "http://localhost:11434/v1"),
    ("openrouter", "https://openrouter.ai/api/v1"), ("bedrock", "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1")])
def test_provider_translation_accounting(setup, monkeypatch, provider, base):
    client, cr, store, headers, _ = setup
    store.delete_model("openrouter/test")
    store.upsert_model(ModelSpec(value=f"{provider}/test", provider=provider, tools=True, ctx_k=200,
                                cost=1, reliability=1, competence={"general": .99}))
    cr.upsert_provider(ProviderConfig(name=provider, kind=provider, api_key="provider-secret", base_url=""))
    sent = []

    async def forward(self, url, **kwargs):
        sent.append((url, kwargs["json"]))
        return httpx.Response(200, json=canned(), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", forward)
    result = post(client, headers["alice"], tools=[{"type": "function", "name": "read", "strict": False}])
    assert result.status_code == 200
    assert sent[0][0] == base + "/chat/completions"
    assert sent[0][1]["tools"][0]["function"]["name"] == "read"
    summary = cr.usage_summary(user="alice")["totals"]
    assert summary["requests"] == 1 and summary["prompt_tokens"] == 10 and summary["completion_tokens"] == 5


def test_scopes_quotas_and_rejected_output_accounting(setup, monkeypatch):
    client, cr, store, headers, admin = setup

    async def forward(self, url, **kwargs):
        return httpx.Response(200, json=canned(), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", forward)
    for restriction in ({"scope_models": json.dumps({"allow": ["ollama/"]})}, {"max_tier": 1}):
        created = client.post("/api/keys", headers=admin, json={"user": "restricted", **restriction})
        assert created.status_code == 201
        key = {"Authorization": "Bearer " + created.json()["key"]}
        assert post(client, key).status_code in {403, 422}
    created = client.post("/api/keys", headers=admin, json={"user": "limited", "rl_window_s": 3600, "rl_max_req": 1}).json()
    key = {"Authorization": "Bearer " + created["key"]}
    assert post(client, key).status_code == 200
    denied = post(client, key)
    assert denied.status_code == 429 and int(denied.headers["retry-after"]) > 0
    assert denied.json()["error"]["type"] == "rate_limit_error"
    # Rejection after provider generation preserves its billable usage, but is not success.
    result = post(client, headers["alice"], tool_choice="required",
                  tools=[{"type": "function", "name": "read", "strict": False}])
    assert result.status_code == 502
    rows = cr.recent_usage("alice", "")
    assert len(rows) == 1 and rows[0].status == 502
    assert rows[0].prompt_tokens == 10 and rows[0].requested_model == "smart-worker"


def test_secret_redaction(setup, monkeypatch):
    client, cr, store, headers, _ = setup
    cr.upsert_provider(ProviderConfig(name="openrouter", kind="openrouter", api_key="private-provider-token"))
    from fastapi import HTTPException

    async def chat(request):
        raise HTTPException(502, "Provider failed with private-provider-token and Bearer another-token")

    monkeypatch.setattr(api, "chat_completions", chat)
    result = post(client, headers["alice"])
    assert result.status_code == 502
    assert "private-provider-token" not in result.text and "another-token" not in result.text


def test_shared_upstream_cancellation_is_accounted_once(setup, monkeypatch):
    client, cr, store, headers, _ = setup
    from starlette.requests import Request
    closed = []

    class Stream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(True)

        async def aiter_raw(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            await asyncio.Event().wait()

    monkeypatch.setattr(httpx.AsyncClient, "stream", lambda *args, **kwargs: Stream())

    async def check():
        raw = json.dumps({"model": "smart-worker", "input": "hi", "stream": True}).encode()

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        request = Request({"type": "http", "method": "POST", "path": "/v1/responses", "headers": [],
                           "app": client.app, "state": {"user": "alice"}}, receive)
        result = await api.create_response(request)
        stream = result.body_iterator
        created = await anext(stream)
        rid = json.loads(created.decode().split("data: ")[1])["response"]["id"]
        await anext(stream)  # response.in_progress
        await anext(stream)  # heartbeat
        await anext(stream)  # opens provider and starts the output item
        await stream.aclose()
        assert closed == [True]
        assert cr.get_response(rid, "alice")["response"]["status"] == "failed"
        rows = cr.recent_usage("alice", "")
        assert len(rows) == 1 and rows[0].status == 499 and rows[0].tokens_estimated

    asyncio.run(check())
