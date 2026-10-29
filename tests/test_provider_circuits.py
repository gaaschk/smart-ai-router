import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest

from smart_ai_router.circuit_breaker import CircuitClient, DEFAULTS, ProviderCircuits
from tests.test_proxy_failover import _client, _spec, _Stream, _OK


def test_closed_open_half_open_recovery_and_stale_completion():
    now = [0]
    c = ProviderCircuits(clock=lambda: now[0])
    first = c.acquire('openrouter', 60)
    old = c.acquire('openrouter', 60)
    c.finish('openrouter', first, 1, False)
    c.finish('openrouter', old, 1, True)
    assert c.blocked('openrouter', 60)  # stale in-flight success cannot undo opening
    now[0] = 60
    assert not c.blocked('openrouter', 60)
    probe = c.acquire('openrouter', 60)
    assert c.snapshot(DEFAULTS)['openrouter']['state'] == 'half_open'
    with pytest.raises(httpx.ConnectError):
        c.acquire('openrouter', 60)
    c.finish('openrouter', probe, 1, True)
    assert c.snapshot(DEFAULTS)['openrouter']['state'] == 'closed'


def test_failed_probe_reopens_and_cancelled_probe_releases_slot():
    now = [0]
    c = ProviderCircuits(clock=lambda: now[0])
    c.finish('ollama', c.acquire('ollama', 30), 1, False)
    now[0] = 30
    c.finish('ollama', c.acquire('ollama', 30), 1, None)
    probe = c.acquire('ollama', 30)
    c.finish('ollama', probe, 1, False)
    assert c.blocked('ollama', 30)
    assert not c.blocked('bedrock', 120)


def test_half_open_allows_only_one_concurrent_probe():
    now = [0]
    c = ProviderCircuits(clock=lambda: now[0])
    c.finish('openrouter', c.acquire('openrouter', 60), 1, False)
    now[0] = 60
    def attempt(_):
        try:
            c.acquire('openrouter', 60)
            return True
        except httpx.ConnectError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(16))) == 1


@pytest.mark.parametrize('status,opens', [(200, False), (400, False), (403, False), (429, True), (503, True)])
def test_http_status_classification(status, opens):
    c = ProviderCircuits()
    async def post(*args, **kwargs):
        return httpx.Response(status)
    client = CircuitClient(SimpleNamespace(post=post), c, lambda: 'openrouter', {'openrouter': (1, 60)})
    asyncio.run(client.post('https://mock.invalid'))
    assert c.blocked('openrouter', 60) is opens


@pytest.mark.parametrize('protocol', ['chat', 'responses'])
@pytest.mark.parametrize('stream', [False, True])
def test_provider_failure_falls_back_to_other_provider_and_then_skips_outage(monkeypatch, protocol, stream):
    c = _client(monkeypatch)
    c.store.set_setting('provider_circuit_enabled', 'true')
    c.store.set_setting('circuit_openrouter_failures', '1')
    c.store.upsert_model(_spec('ollama/backup', 6))
    sent = []
    async def post(self, url, **kwargs):
        model = kwargs['json']['model']; sent.append(model)
        return httpx.Response(200 if model == 'backup' else 503, json=_OK if model == 'backup' else {'error': 'outage'}, request=httpx.Request('POST', url))
    def streaming(self, method, url, **kwargs):
        model = kwargs['json']['model']; sent.append(model)
        return _Stream(200 if model == 'backup' else 503, (b'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n') if model == 'backup' else b'outage')
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    monkeypatch.setattr(httpx.AsyncClient, 'stream', streaming)
    body = {'model': 'auto', 'stream': stream}
    if protocol == 'chat':
        path = '/v1/chat/completions'; body['messages'] = [{'role': 'user', 'content': 'Refactor this Python parser.'}]
    else:
        path = '/v1/responses'; body['input'] = 'Refactor this Python parser.'
    for _ in range(2):
        response = c.post(path, json=body)
        assert response.status_code == 200, response.text
        if protocol == 'responses' and stream:
            assert 'response.completed' in response.text
    assert sent == ['cheap-coder', 'backup', 'backup']
    assert c.app.state.capability_router.provider_circuits.snapshot(DEFAULTS)['openrouter']['state'] == 'open'


def test_open_circuit_is_not_bypassed_when_no_other_provider(monkeypatch):
    c = _client(monkeypatch)
    c.store.set_setting('provider_circuit_enabled', 'true')
    circuits = c.app.state.capability_router.provider_circuits
    circuits.finish('openrouter', circuits.acquire('openrouter', 60), 1, False)
    result = c.post('/v1/chat/completions', json={'model': 'auto', 'messages': [{'role': 'user', 'content': 'Refactor this Python parser.'}]})
    assert result.status_code == 503
    assert c.sent == []


def test_status_requires_admin_and_never_exposes_credentials(monkeypatch):
    c = _client(monkeypatch)
    assert c.get('/api/provider-circuits').status_code == 403
    # Authenticated fixture uses a process-local synthetic admin key.
    monkeypatch.setenv('SMART_ROUTER_API_KEYS', 'test-admin')
    from smart_ai_router.api.app import create_app
    from fastapi.testclient import TestClient
    admin_client = TestClient(create_app(c.app.state.capability_router))
    response = admin_client.get('/api/provider-circuits', headers={'Authorization': 'Bearer test-admin'})
    assert response.status_code == 200
    assert response.json()['scope'] == 'process'
    assert 'test-admin' not in response.text


def test_stream_disconnect_releases_probe_without_counting_failure():
    from contextlib import asynccontextmanager
    now = [0]
    c = ProviderCircuits(clock=lambda: now[0])
    c.finish('openrouter', c.acquire('openrouter', 60), 1, False)
    now[0] = 60
    @asynccontextmanager
    async def stream(*args, **kwargs):
        yield SimpleNamespace(status_code=200)
    wrapped = CircuitClient(SimpleNamespace(stream=stream), c, lambda: 'openrouter', {'openrouter': (1, 60)})
    async def check():
        try:
            async with wrapped.stream('POST', 'https://mock.invalid'):
                raise asyncio.CancelledError()
        except asyncio.CancelledError:
            pass
        assert not c.blocked('openrouter', 60)
        assert c.snapshot(DEFAULTS)['openrouter']['failed_attempts'] == 1
        c.acquire('openrouter', 60)  # next caller can take the recovery slot
    asyncio.run(check())


def test_midstream_transport_failure_opens_circuit():
    from contextlib import asynccontextmanager
    c = ProviderCircuits()
    @asynccontextmanager
    async def stream(*args, **kwargs):
        yield SimpleNamespace(status_code=200)
    wrapped = CircuitClient(SimpleNamespace(stream=stream), c, lambda: 'openrouter', {'openrouter': (1, 60)})
    async def check():
        with pytest.raises(httpx.ReadError):
            async with wrapped.stream('POST', 'https://mock.invalid'):
                raise httpx.ReadError('stream reset')
    asyncio.run(check())
    assert c.blocked('openrouter', 60)


@pytest.mark.parametrize("constraint", ["pinned", "scoped", "capability"])
@pytest.mark.parametrize("stream", [False, True])
def test_unrelated_outage_preserves_invalid_selection_status(monkeypatch, constraint, stream):
    from fastapi.testclient import TestClient
    from smart_ai_router.api.app import create_app
    c = _client(monkeypatch)
    cr = c.app.state.capability_router
    local = _spec("ollama/local", 1)
    local.provider = "ollama"
    c.store.upsert_model(local)
    headers = {}
    if constraint == "scoped":
        monkeypatch.setenv("SMART_ROUTER_API_KEYS", "test-admin")
        c = TestClient(create_app(cr))
        key = c.post("/api/keys", headers={"Authorization": "Bearer test-admin"},
                     json={"user": "local-only", "scope_models": '{"allow":["ollama/"]}'}).json()["key"]
        headers = {"Authorization": "Bearer " + key}
    body = {"model": "smart-ollama" if constraint == "pinned" else "auto",
            "stream": stream,
            "messages": [{"role": "user", "content": "Refactor this Python parser."}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "answer", "schema": {"type": "object"}}}}
    baseline = c.post("/v1/chat/completions", json=body, headers=headers)
    assert baseline.status_code == 422
    cr._store.set_setting("provider_circuit_enabled", "true")
    cr.provider_circuits.finish("openrouter", cr.provider_circuits.acquire("openrouter", 60), 1, False)
    result = c.post("/v1/chat/completions", json=body, headers=headers)
    assert result.status_code == 422, result.text


def test_blocked_valid_pinned_pool_returns_unavailable(monkeypatch):
    c = _client(monkeypatch)
    local = _spec("ollama/local", 1)
    local.provider = "ollama"
    local.structured_outputs = True
    c.store.upsert_model(local)
    c.store.set_setting("provider_circuit_enabled", "true")
    circuits = c.app.state.capability_router.provider_circuits
    circuits.finish("ollama", circuits.acquire("ollama", 30), 1, False)
    response = c.post("/v1/chat/completions", json={"model": "smart-ollama",
        "messages": [{"role": "user", "content": "Refactor this Python parser."}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "answer", "schema": {"type": "object"}}}})
    assert response.status_code == 503
    assert c.sent == []


def test_recovery_metrics_and_transition_logs(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="smart_ai_router.circuit_breaker")
    now = [0]
    c = ProviderCircuits(clock=lambda: now[0])
    c.finish("ollama", c.acquire("ollama", 30), 1, False)
    c.record_fallback("ollama")
    now[0] = 30
    c.finish("ollama", c.acquire("ollama", 30), 1, True)
    state = c.snapshot(DEFAULTS)["ollama"]
    assert (state["recovery_attempts"], state["recoveries"], state["fallbacks"]) == (1, 1, 1)
    assert "closed->open" in caplog.text
    assert "open->half_open" in caplog.text
    assert "half_open->closed" in caplog.text


def test_enabled_circuit_agent_falls_back_across_providers(monkeypatch):
    c = _client(monkeypatch)
    c.store.set_setting("provider_circuit_enabled", "true")
    c.store.set_setting("circuit_openrouter_failures", "1")
    local = _spec("ollama/backup", 6)
    local.provider = "ollama"
    c.store.upsert_model(local)
    sent = []
    def stream(self, method, url, **kw):
        sent.append(kw["json"]["model"])
        return _Stream(503, b"outage") if len(sent) == 1 else _Stream(200,
            b'data: {"choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    monkeypatch.setattr(httpx.AsyncClient, "stream", stream)
    response = c.post("/v1/chat/completions", json={"model": "auto", "agent": True, "messages": []})
    assert response.status_code == 200
    assert '"error"' not in response.text
    assert sent == ["cheap-coder", "backup"]
    assert c.app.state.capability_router.provider_circuits.snapshot(DEFAULTS)["openrouter"]["fallbacks"] == 1


def test_asgi_disconnect_releases_recovery_lease(monkeypatch):
    import json
    c = _client(monkeypatch)
    c.store.set_setting("provider_circuit_enabled", "true")
    circuits = c.app.state.capability_router.provider_circuits
    now = [0]
    circuits.clock = lambda: now[0]
    circuits.finish("openrouter", circuits.acquire("openrouter", 60), 1, False)
    now[0] = 60
    closed = []
    class WaitingStream(_Stream):
        async def aiter_raw(self):
            yield b'data: {"choices":[{"index":0,"delta":{"content":"hello"}}]}\n\n'
            await asyncio.Event().wait()
        async def __aexit__(self, *args):
            closed.append(True)
    monkeypatch.setattr(httpx.AsyncClient, "stream", lambda *a, **kw: WaitingStream(200, b""))
    async def run():
        emitted = asyncio.Event()
        first = True
        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": json.dumps({"model": "auto", "stream": True,
                    "messages": [{"role": "user", "content": "Refactor this Python parser."}]}).encode(), "more_body": False}
            await emitted.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            if message["type"] == "http.response.body" and b"hello" in message.get("body", b""):
                emitted.set()
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
                 "http_version": "1.1", "method": "POST", "scheme": "http",
                 "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
                 "query_string": b"", "headers": [(b"content-type", b"application/json")],
                 "client": ("127.0.0.1", 1234), "server": ("testserver", 80)}
        await asyncio.wait_for(c.app(scope, receive, send), timeout=2)
        assert closed
        assert not circuits.blocked("openrouter", 60)
        assert circuits.snapshot(DEFAULTS)["openrouter"]["failed_attempts"] == 1
        circuits.acquire("openrouter", 60)
    asyncio.run(run())
