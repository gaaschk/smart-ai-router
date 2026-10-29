"""Offline provider replay through the real Chat and Responses accounting paths."""
import json
from types import SimpleNamespace

import httpx
import pytest

from smart_ai_router.api.proxy import _log_usage
from tests.test_responses_persistence import setup  # shared isolated app/catalog fixture


@pytest.mark.parametrize('tokens', [0, 12, 95, 96, 97, 1000, None])
@pytest.mark.parametrize('content,stalled', [
    ('18.4%', False), ('{"difference":2300,"percent_change":18.4}', False),
    ('', True), (' \n\t', True), ('<tool_call>{}</tool_call>', True),
])
def test_observation_is_independent_of_token_count(tokens, content, stalled):
    records = []
    cr = SimpleNamespace(record_usage=records.append, cost_for=lambda *args: .01,
                         set_tool_probe=lambda *args: None)
    request = SimpleNamespace(state=SimpleNamespace(user='test'))
    usage = None if tokens is None else {'prompt_tokens': 10, 'completion_tokens': tokens}
    _log_usage(cr, request, routed_model='test', domain='general', complexity='basic',
               usage=usage, status=200, tools_offered=True, content=content)
    assert len(records) == 1
    assert records[0].tool_stalled is stalled
    assert records[0].completion_tokens == (tokens or 0)


@pytest.mark.parametrize('protocol,required', [('chat', False), ('responses', False), ('responses', True)])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('measured', [False, True])
@pytest.mark.parametrize('content', ['18.4%', '{"percent_change":18.4}'])
def test_short_final_continuation_accounted_once(setup, monkeypatch, protocol, required, stream, measured, content):
    client, cr, store, headers, _ = setup
    usage = {'prompt_tokens': 10, 'completion_tokens': 5}
    message = {'role': 'assistant', 'content': content}
    reply = {'choices': [{'index': 0, 'message': message, 'finish_reason': 'stop'}]}
    if measured:
        reply['usage'] = usage

    async def forward(self, url, **kwargs):
        return httpx.Response(200, json=reply, request=httpx.Request('POST', url))

    class Stream:
        status_code = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def aiter_raw(self):
            chunks = [{'choices': [{'index': 0, 'delta': message, 'finish_reason': None}]},
                      {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}]
            if measured:
                chunks.append({'choices': [], 'usage': usage})
            for chunk in chunks:
                yield ('data: ' + json.dumps(chunk) + '\n\n').encode()
            yield b'data: [DONE]\n\n'

    monkeypatch.setattr(httpx.AsyncClient, 'post', forward)
    monkeypatch.setattr(httpx.AsyncClient, 'stream', lambda *args, **kwargs: Stream())
    tool = {'name': 'calculate', 'parameters': {'type': 'object', 'properties': {}}, 'strict': False}
    body = {'model': 'smart-worker', 'stream': stream}
    if protocol == 'chat':
        path = '/v1/chat/completions'
        body.update(tools=[{'type': 'function', 'function': tool}], messages=[
            {'role': 'user', 'content': 'Calculate percent change'},
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'call_a', 'type': 'function', 'function': {'name': 'calculate', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'call_a', 'content': '18.4'}])
    else:
        path = '/v1/responses'
        body.update(tools=[{'type': 'function', **tool}], input=[
            {'role': 'user', 'content': 'Calculate percent change'},
            {'type': 'function_call', 'call_id': 'call_a', 'name': 'calculate', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'call_a', 'output': '18.4'}])
        if content.startswith('{'):
            body['text'] = {'format': {'type': 'json_schema', 'name': 'result', 'strict': True,
                'schema': {'type': 'object', 'properties': {'percent_change': {'type': 'number'}},
                           'required': ['percent_change'], 'additionalProperties': False}}}
    if required:
        body['tool_choice'] = 'required'
    result = client.post(path, headers=headers['alice'], json=body)
    assert result.status_code == (502 if required and not stream else 200), result.text
    if protocol == 'responses' and stream:
        assert ('response.failed' if required else 'response.completed') in result.text
        assert ('response.completed' if required else 'response.failed') not in result.text
    rows = cr.recent_usage('alice', '')
    assert len(rows) == 1
    row = rows[0]
    assert row.status == (502 if required else 200)
    assert row.tools_offered and not row.tool_stalled
    assert row.tokens_estimated is (stream and not measured)
    if measured:
        assert row.prompt_tokens == 10 and row.completion_tokens == 5
