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
