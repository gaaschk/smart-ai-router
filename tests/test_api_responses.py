"""Responses wire format plus integration with shared routing and authorization."""

import asyncio
import json
import warnings

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from smart_ai_router.api import responses_routes as api
from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec
from smart_ai_router.store.sqlite_store import SqliteStore


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    for key in (
        "MODEL_DENYLIST",
        "CLASSIFIER_MODEL",
        "CLASSIFIER_FALLBACK",
        "CLASSIFIER_REFINE_MODEL",
    ):
        monkeypatch.setenv("SMART_ROUTER_" + key, "")
    store = SqliteStore(":memory:")
    store.upsert_model(
        ModelSpec(
            value="openrouter/test",
            provider="openrouter",
            cost=1,
            ctx_k=200,
            tools=True,
            reliability=1.0,
            competence={"coding": 0.95, "general": 0.9},
        )
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return TestClient(create_app(CapabilityRouter(store=store)))


def reply(message=None, finish="stop"):
    return {
        "model": "openrouter/test",
        "choices": [
            {"message": message or {"content": "hello"}, "finish_reason": finish}
        ],
        "usage": {
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 2},
        },
    }


def test_shared_routing_and_usage(client, monkeypatch):
    sent = []

    async def post(self, url, **kwargs):
        sent.append(kwargs["json"])
        return httpx.Response(200, json=reply(), request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    result = client.post(
        "/v1/responses",
        json={
            "model": "auto",
            "input": "hello",
            "instructions": "Be brief",
            "store": False,
        },
    )
    assert result.status_code == 200
    data = result.json()
    assert data["object"] == "response" and data["status"] == "completed"
    assert data["output"][0]["content"][0]["text"] == "hello"
    assert data["usage"]["input_tokens"] == 7
    assert data["usage"]["input_tokens_details"]["cached_tokens"] == 2
    assert result.headers["x-routed-model"] == "openrouter/test"
    assert sent[0]["messages"][0] == {"role": "system", "content": "Be brief"}
    assert client.app.state.capability_router.usage_summary()["totals"]["requests"] == 1


def test_function_history_and_options():
    chat = api._chat_body(
        {
            "model": "auto",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read",
                    "arguments": "{}",
                },
                {
                    "type": "function_call",
                    "call_id": "call_2",
                    "name": "read",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
                {"type": "function_call_output", "call_id": "call_2", "output": "ok"},
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "read",
                    "strict": False,
                    "parameters": {"type": "object"},
                }
            ],
            "tool_choice": {"type": "function", "name": "read"},
            "max_output_tokens": 42,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "result",
                    "schema": {"type": "object"},
                }
            },
            "reasoning": {"effort": "low"},
        }
    )
    assert len(chat["messages"][0]["tool_calls"]) == 2
    assert chat["messages"][1]["tool_call_id"] == "call_1"
    assert chat["tools"][0]["function"]["name"] == "read"
    assert chat["tool_choice"]["function"]["name"] == "read"
    assert chat["max_tokens"] == 42 and chat["agent"] is False
    assert chat["reasoning_effort"] == "low"
    assert chat["response_format"]["json_schema"]["name"] == "result"


def test_images_and_output_replay():
    chat = api._chat_body(
        {
            "model": "auto",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "describe"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,AA==",
                        },
                    ],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "ok", "annotations": []}
                    ],
                },
            ],
        }
    )
    assert chat["messages"][0]["content"][1]["image_url"]["url"].startswith("data:")
    assert chat["messages"][1]["content"] == [{"type": "text", "text": "ok"}]


@pytest.mark.parametrize(
    "extra",
    [
        {"previous_response_id": 1},
        {"store": "true"},
        {"background": True},
        {"tools": [{"type": "web_search"}]},
        {"input": ["bad"]},
        {"stream": "false"},
        {"max_output_tokens": -1},
        {"text": {"format": "bad"}},
        {"reasoning": {"summary": "auto"}},
        {"unexpected": True},
        {"input": [{"type": "reasoning", "encrypted_content": "x"}]},
    ],
)
def test_invalid_requests_are_openai_errors(client, extra):
    result = client.post(
        "/v1/responses", json={"model": "auto", "input": "hello", **extra}
    )
    assert result.status_code == 400
    assert result.json()["error"]["type"] == "invalid_request_error"


def test_bad_json(client):
    assert client.post("/v1/responses", content="{").status_code == 400
    assert client.post("/v1/responses", json=[]).status_code == 400


def test_auth_required(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", "secret")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = TestClient(create_app(CapabilityRouter(store=SqliteStore(":memory:"))))
    assert (
        client.post("/v1/responses", json={"model": "auto", "input": "hi"}).status_code
        == 401
    )


@pytest.mark.parametrize(
    "finish,status",
    [("stop", "completed"), ("length", "incomplete"), ("content_filter", "incomplete")],
)
def test_nonstream_tools_and_refusal(client, monkeypatch, finish, status):
    async def chat(request):
        return JSONResponse(
            reply(
                {
                    "refusal": "no",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "read", "arguments": "{}"},
                        }
                    ],
                },
                finish,
            )
        )

    monkeypatch.setattr(api, "chat_completions", chat)
    data = client.post(
        "/v1/responses",
        json={
            "model": "auto",
            "input": "hi",
            "tools": [{"type": "function", "name": "read", "strict": False}],
        },
    ).json()
    assert data["status"] == status
    assert data["output"][0]["content"][0] == {"type": "refusal", "refusal": "no"}
    assert data["output"][1]["call_id"] == "call_1"


def events(chunks):
    async def source():
        for chunk in chunks:
            yield chunk

    async def collect():
        return [
            json.loads(frame.decode().split("\ndata: ")[1])
            async for frame in api._stream(source(), api._response({"model": "auto"}))
            if frame.startswith(b"event:")
        ]

    return asyncio.run(collect())


def frame(delta=None, finish=None, usage=None):
    return (
        "data: "
        + json.dumps(
            {
                "choices": [
                    {"index": 0, "delta": delta or {}, "finish_reason": finish}
                ],
                "usage": usage,
            },
            ensure_ascii=False,
        )
        + "\n\n"
    ).encode()


def test_fragmented_stream_text_tools_usage():
    raw = (
        b": heartbeat\n\n"
        + frame({"content": "h\u00e9llo"})
        + frame(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "read", "arguments": "{"},
                    }
                ]
            }
        )
        + frame({"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]})
        + frame(finish="tool_calls")
        + frame(usage={"prompt_tokens": 7})
        + b"data: [DONE]\n\n"
    )
    output = events([raw[i : i + 1] for i in range(len(raw))])
    assert [e["sequence_number"] for e in output] == list(range(len(output)))
    assert output[0]["type"] == "response.created"
    assert (
        next(e for e in output if e["type"] == "response.output_text.delta")["delta"]
        == "h\u00e9llo"
    )
    final = output[-1]
    assert final["type"] == "response.completed"
    assert final["response"]["output"][1]["arguments"] == "{}"
    assert final["response"]["usage"]["input_tokens"] == 7
    assert "response.function_call_arguments.done" in [e["type"] for e in output]


@pytest.mark.parametrize(
    "chunks",
    [
        [frame({"content": "partial"})],
        [b'data: {"error":{"message":"provider failed"}}\n\ndata: [DONE]\n\n'],
        [b"data: invalid\n\n"],
    ],
)
def test_stream_failure_not_success(chunks):
    output = events(chunks)
    assert output[-1]["type"] == "response.failed"
    assert output[-1]["response"]["error"]["message"]


def test_stream_endpoint(client, monkeypatch):
    async def chat(request):
        body = await request.json()
        assert body["stream"] is True and body["agent"] is False

        async def source():
            yield frame({"content": "ok"})
            yield frame(finish="stop")

        return StreamingResponse(source(), headers={"X-Routed-Model": "test"})

    monkeypatch.setattr(api, "chat_completions", chat)
    result = client.post(
        "/v1/responses", json={"model": "auto", "input": "hi", "stream": True}
    )
    assert result.headers["x-routed-model"] == "test"
    assert "event: response.completed" in result.text
    assert "[DONE]" not in result.text


def test_upstream_http_error(client, monkeypatch):
    async def chat(request):
        raise HTTPException(429, "quota exceeded", headers={"Retry-After": "5"})

    monkeypatch.setattr(api, "chat_completions", chat)
    result = client.post("/v1/responses", json={"model": "auto", "input": "hi"})
    assert result.status_code == 429 and result.headers["retry-after"] == "5"
    assert result.json()["error"]["message"] == "quota exceeded"


def test_shared_stream_routing_and_usage(client, monkeypatch):
    class ProviderStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def aiter_raw(self):
            yield frame({"content": "routed stream"})
            yield frame(finish="stop")
            yield frame(usage={"prompt_tokens": 7, "completion_tokens": 3})
            yield b"data: [DONE]\n\n"

    monkeypatch.setattr(
        httpx.AsyncClient, "stream", lambda *args, **kwargs: ProviderStream()
    )
    result = client.post(
        "/v1/responses", json={"model": "auto", "input": "hi", "stream": True}
    )
    assert result.status_code == 200
    assert result.headers["x-routed-model"] == "openrouter/test"
    output = [
        json.loads(line[6:])
        for line in result.text.splitlines()
        if line.startswith("data: ")
    ]
    assert output[-1]["type"] == "response.completed"
    assert output[-1]["response"]["usage"]["output_tokens"] == 3
    assert client.app.state.capability_router.usage_summary()["totals"]["requests"] == 1


def test_split_tool_names_and_parallel_calls():
    output = events(
        [
            frame(
                {
                    "tool_calls": [
                        {"index": 0, "id": "call_", "function": {"name": "re"}}
                    ]
                }
            ),
            frame(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "1",
                            "function": {"name": "ad", "arguments": "{"},
                        },
                        {
                            "index": 1,
                            "id": "call_2",
                            "function": {"name": "write", "arguments": "{}"},
                        },
                    ]
                }
            ),
            frame({"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}),
            frame(finish="tool_calls"),
        ]
    )
    added = [e["item"] for e in output if e["type"] == "response.output_item.added"]
    assert added[0]["name"] == "read" and added[0]["call_id"] == "call_1"
    assert added[0]["arguments"] == ""
    assert [i["arguments"] for i in output[-1]["response"]["output"]] == ["{}", "{}"]


def test_stream_incomplete_and_refusal():
    output = events([frame({"refusal": "no"}), frame(finish="length")])
    assert "response.refusal.done" in [e["type"] for e in output]
    assert output[-1]["type"] == "response.incomplete"
    assert output[-1]["response"]["incomplete_details"]["reason"] == "max_output_tokens"


def test_stream_closes_on_cancellation():
    async def check():
        closed = []

        async def source():
            try:
                yield frame({"content": "hello"})
                yield frame(finish="stop")
            finally:
                closed.append(True)

        stream = api._stream(source(), api._response({"model": "auto"}))
        for _ in range(3):
            await anext(stream)
        await stream.aclose()
        assert closed == [True]

    asyncio.run(check())


def test_invalid_upstream_json_is_502(client, monkeypatch):
    async def chat(request):
        return JSONResponse({"choices": []})

    monkeypatch.setattr(api, "chat_completions", chat)
    result = client.post("/v1/responses", json={"model": "auto", "input": "hi"})
    assert result.status_code == 502
    assert result.json()["error"]["code"] == "invalid_upstream_response"


def test_file_id_uses_shared_owner_resolution(client):
    result = client.post(
        "/v1/responses",
        json={
            "model": "auto",
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_file", "file_id": "file-missing"}],
                }
            ],
        },
    )
    assert result.status_code == 404
    assert "No such file" in result.json()["error"]["message"]


def test_omitted_strict_normalizes_and_reports_resolution(client, monkeypatch):
    async def chat(request):
        body = await request.json()
        function = body["tools"][0]["function"]
        assert function["strict"] is True
        assert function["parameters"]["required"] == ["path"]
        assert function["parameters"]["additionalProperties"] is False
        return JSONResponse(
            reply(
                {
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "read", "arguments": '{"path":"a"}'},
                        }
                    ]
                },
                "tool_calls",
            )
        )

    monkeypatch.setattr(api, "chat_completions", chat)
    result = client.post(
        "/v1/responses",
        json={
            "model": "auto",
            "input": "hi",
            "tools": [
                {
                    "type": "function",
                    "name": "read",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                }
            ],
        },
    )
    assert result.status_code == 200
    assert result.json()["tools"][0]["strict"] is True
    fallback = api._chat_body(
        {
            "model": "auto",
            "input": "hi",
            "tools": [
                {
                    "type": "function",
                    "name": "read",
                    "parameters": {"type": "object", "allOf": [{"type": "object"}]},
                }
            ],
        }
    )
    assert fallback["tools"][0]["function"]["strict"] is False


def test_schema_refs_and_refusal_replay():
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        api._chat_body(
            {
                "model": "auto",
                "input": "hi",
                "tools": [
                    {
                        "type": "function",
                        "name": "read",
                        "parameters": {"type": "object", "$ref": "#/$defs/missing"},
                    }
                ],
            }
        )
    chat = api._chat_body(
        {
            "model": "auto",
            "input": [
                {"role": "assistant", "content": [{"type": "refusal", "refusal": "no"}]}
            ],
        }
    )
    assert chat["messages"][0]["refusal"] == "no"
