"""Assertions concern the replay harness and router, never a live model's ability."""
import asyncio
import copy
import json
import socket

import httpx
import pytest

from scripts.replay_agent_workflow import FIXTURE, ProviderReplay, replay, replay_one


@pytest.fixture(scope='module')
def results():
    # Hard network tripwire independent of the mock transport; any accidental
    # real socket fails the test rather than reaching a service/provider.
    from unittest.mock import patch
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')):
        data = asyncio.run(replay(json.loads(FIXTURE.read_text())))
    return data['results']


def select(results, name):
    return [r for r in results if r['scenario'] == name]


def test_clean_workflow_matches_native_and_adapter_in_both_modes(results):
    for row in select(results, 'clean'):
        assert row['task_success'] and row['reliable_success']
        assert row['tool_calls'] == 2 and row['turns'] == 3
        assert row['synthetic_cost_usd'] == .00006
        assert row['synthetic_latency_ms'] == 300
        assert row['within_synthetic_limits'] is True
        if row['protocol'] == 'responses':
            assert len(row['router_usage']) == 3
            assert all(r['status'] == 200 and not r['tool_stalled'] for r in row['router_usage'])
    # Compare the semantic fields actually sent to the mocked provider, not
    # merely two response summaries or the adapter against itself.
    rows = select(results, 'clean')
    for stream in (False, True):
        chat = next(r for r in rows if r['protocol'] == 'chat' and r['stream'] == stream)
        adapter = next(r for r in rows if r['protocol'] == 'responses' and r['stream'] == stream)
        assert len(chat['provider_requests']) == len(adapter['provider_requests'])
        for native, translated in zip(chat['provider_requests'], adapter['provider_requests']):
            native, translated = copy.deepcopy(native), copy.deepcopy(translated)
            # Prompt caching may use equivalent text content blocks. Keep all
            # call IDs, arguments, tool-result values and ordering intact.
            for payload in (native, translated):
                for message in payload['messages']:
                    if isinstance(message.get('content'), list):
                        assert all(p['type'] == 'text' for p in message['content'])
                        message['content'] = ''.join(p['text'] for p in message['content'])
            for field in ('messages', 'tools', 'parallel_tool_calls', 'response_format'):
                assert native[field] == translated[field]


def test_recovery_cost_and_eventual_success_do_not_hide_violations(results):
    for row in select(results, 'recovered_repeat_and_wrong_arguments'):
        assert row['task_success'] and not row['reliable_success']
        assert row['violations'] == {'repeated_call': 1, 'irrelevant_or_wrong_arguments': 1}
        assert row['tool_calls'] == 4 and row['excess_calls'] == 2
        assert row['synthetic_cost_usd'] == .0001
        assert row['synthetic_latency_ms'] == 500
        assert len(row['provider_ledger']) == 5


@pytest.mark.parametrize('scenario,violation', [
    ('schema_rejection', 'invalid_arguments'), ('parallel_rejection', 'parallel_constraint_ignored')])
def test_native_oracle_and_adapter_rejections_have_distinct_evidence(results, scenario, violation):
    for row in select(results, scenario):
        assert not row['reliable_success']
        if row['protocol'] == 'chat':
            assert row['violations'][violation] == 1
        else:
            assert not row['task_success'] and row.get('error')
            assert len(row['router_usage']) == 1
            assert row['router_usage'][0]['status'] == 502
            assert row['synthetic_cost_usd'] == .00002  # billed generation before validation
            assert row['router_usage'][0]['cost_usd'] == .00002


@pytest.mark.parametrize('scenario', ['wrong_final', 'invented_attribution'])
def test_semantic_failures_survive_valid_protocol(results, scenario):
    for row in select(results, scenario):
        assert not row['task_success'] and not row['reliable_success']
        assert row['violations']['wrong_answer_or_attribution'] == 1
        assert not row.get('error')


def test_provider_error_cost_is_unknown_not_free(results):
    for row in select(results, 'provider_error'):
        assert not row['task_success'] and row.get('error')
        assert row['cost_unavailable_calls'] == 1
        assert row['within_synthetic_limits'] is None
        assert row['provider_ledger'][0]['status'] == 503
        assert not row['violations']


@pytest.mark.parametrize('budget,error', [('max_turns', 'turn_budget_exceeded'),
                                          ('max_calls', 'tool_call_budget_exceeded')])
def test_replay_honors_workflow_limits(budget, error):
    fixture = json.loads(FIXTURE.read_text())
    fixture['limits'][budget] = 1
    row = asyncio.run(replay_one(fixture, fixture['scenarios'][0], 'chat', False))
    assert row['error'] == error and not row['task_success']
    assert len(row['trace']) <= 1


def test_unexpected_provider_url_fails_closed():
    fixture = json.loads(FIXTURE.read_text())
    provider = ProviderReplay(fixture, fixture['scenarios'][0])
    with pytest.raises(ValueError, match='unexpected URL'):
        provider(httpx.Request('POST', 'https://other.invalid/chat/completions', json={}))
    assert provider.requests == []


@pytest.mark.parametrize('protocol', ['chat', 'responses'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('phase', ['headers', 'body'])
@pytest.mark.parametrize('stall', ['delay', 'hang'])
def test_timeout_cancels_provider_before_teardown(monkeypatch, protocol, stream, phase, stall):
    """No worker thread/task may outlive the clients, restored patches or store."""
    import time
    from scripts import replay_agent_workflow as module

    fixture = json.loads(FIXTURE.read_text())
    fixture['limits']['request_timeout_s'] = .02
    active, events = set(), []
    original_handler = ProviderReplay.handle_async
    original_bind = module.settings.bind_store
    original_close = httpx.AsyncClient.aclose
    previous_store = module.settings._store

    async def block():
        task = asyncio.current_task()
        active.add(task)
        events.append('provider_enter')
        try:
            if stall == 'delay':
                await asyncio.sleep(.2)
            else:
                await asyncio.Event().wait()  # only cancellation can release it
        finally:
            active.remove(task)
            events.append('provider_exit')

    class StalledBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n' if stream else b'{'
            await block()

        async def aclose(self):
            assert not active
            events.append('body_closed')

    async def stalled_handler(self, request):
        if phase == 'headers':
            await block()
        response = await original_handler(self, request)
        if phase == 'body':
            return httpx.Response(200, headers={'content-type': 'text/event-stream' if stream else 'application/json'},
                                  stream=StalledBody())
        return response

    async def checked_close(self):
        assert not active, 'provider still running when a client closes'
        events.append('client_closed')
        await original_close(self)

    def checked_restore(store):
        if store is previous_store:
            assert not active, 'provider still running when global state restores'
            events.append('restored')
        original_bind(store)

    monkeypatch.setattr(ProviderReplay, 'handle_async', stalled_handler)
    monkeypatch.setattr(httpx.AsyncClient, 'aclose', checked_close)
    monkeypatch.setattr(module.settings, 'bind_store', checked_restore)

    async def check():
        before = asyncio.all_tasks()
        row = await replay_one(fixture, fixture['scenarios'][0], protocol, stream)
        assert row.get('error', '').startswith('TimeoutError:')
        assert not row['task_success']
        assert not active
        await asyncio.sleep(0)  # allow cancellation callbacks to settle
        assert not (asyncio.all_tasks() - before), 'request task survived replay teardown'
        assert events.count('provider_enter') == events.count('provider_exit') == 1
        assert events.index('provider_exit') < events.index('restored')
        if phase == 'body':
            assert 'body_closed' in events

    start = time.monotonic()
    asyncio.run(check())
    # Includes event-loop/executor shutdown, not merely wait_for's return time.
    assert time.monotonic() - start < 1.0
