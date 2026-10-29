"""Actual OpenAI/Agents SDK clients against the ASGI app; provider IO is deterministic.

These test native Responses transport and the real Agents tool loop, not a fake SDK.
No paid external provider calls are made.
"""

import asyncio
import json

import httpx
import httpx2
import pytest
from openai import AsyncOpenAI
from agents import (
    Agent,
    ModelSettings,
    OpenAIResponsesModel,
    RunConfig,
    Runner,
    function_tool,
)

from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec
from smart_ai_router.store.sqlite_store import SqliteStore


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    for key in (
        "MODEL_DENYLIST",
        "CLASSIFIER_MODEL",
        "CLASSIFIER_FALLBACK",
        "CLASSIFIER_REFINE_MODEL",
    ):
        monkeypatch.setenv("SMART_ROUTER_" + key, "")
    from smart_ai_router.api import proxy

    monkeypatch.setattr(
        proxy, "_enrich_with_gbrain_context", lambda messages, *args, **kwargs: messages
    )
    monkeypatch.setattr(
        proxy, "_schedule_gbrain_save_back", lambda *args, **kwargs: None
    )
    store = SqliteStore(":memory:")
    store.upsert_model(
        ModelSpec(
            value="openrouter/anthropic/claude-sonnet-4.5",
            provider="openrouter",
            tools=True,
            structured_outputs=True,
            ctx_k=200,
            cost=1,
            reliability=1,
            agentic=0.95,
            competence={
                "coding": 0.99,
                "general": 0.99,
                "reasoning": 0.99,
                "docs": 0.99,
            },
        )
    )
    return create_app(CapabilityRouter(store=store))


def sdk(app):
    return AsyncOpenAI(
        base_url="http://router/v1",
        api_key="test-key",
        max_retries=0,
        _strict_response_validation=True,
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)),
    )


def completion(content):
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def test_python_sdk_storage_and_stateless(app, monkeypatch):
    sent = []

    async def post(self, url, **kwargs):
        sent.append(kwargs["json"])
        return httpx.Response(
            200, json=completion("hello"), request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    async def check():
        async with sdk(app) as client:
            first = await client.responses.create(
                model="smart-worker", input="first", instructions="Old instruction"
            )
            assert first.output_text == "hello" and first.store is True
            assert (await client.responses.retrieve(first.id)).id == first.id
            second = await client.responses.create(
                model="smart-worker",
                input="second",
                previous_response_id=first.id,
                instructions="New instruction",
                store=False,
            )
            assert second.output_text == "hello" and second.store is False
            page = await client.responses.input_items.list(
                first.id, order="asc", limit=1
            )
            assert page.data[0].content[0].text == "first"
            await client.responses.delete(first.id)
            from openai import NotFoundError

            with pytest.raises(NotFoundError):
                await client.responses.retrieve(first.id)
        assert sent[1]["messages"][0]["content"] == "New instruction"
        assert "Old instruction" not in json.dumps(sent[1])
        assert "first" in json.dumps(sent[1]) and "second" in json.dumps(sent[1])
        assert app.state.capability_router.usage_summary()["totals"]["requests"] == 2

    asyncio.run(check())


@pytest.mark.parametrize("stateful", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("lane", ["smart-worker", "smart-orchestrator"])
def test_agents_native_responses_multistep_tools(
    app, monkeypatch, streaming, lane, stateful
):
    paths, dispatched, executed, payloads = [], [], [], []

    @app.middleware("http")
    async def record_path(request, call_next):
        paths.append(request.url.path)
        payloads.append(await request.json())
        return await call_next(request)

    @function_tool
    def add(a: int, b: int) -> int:
        """Add two integers."""
        executed.append((a, b))
        return a + b

    def provider(body):
        dispatched.append(body)
        outputs = [m for m in body["messages"] if m["role"] == "tool"]
        if not outputs:
            args = [("call_a", {"a": 1, "b": 2}), ("call_b", {"a": 3, "b": 4})]
        elif len(outputs) == 2:
            assert [m["tool_call_id"] for m in outputs] == ["call_a", "call_b"]
            assert [m["content"] for m in outputs] == ["3", "7"]
            args = [("call_c", {"a": 3, "b": 7})]
        else:
            assert (
                outputs[-1]["tool_call_id"] == "call_c"
                and outputs[-1]["content"] == "10"
            )
            return completion("The total is 10.")
        return {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": "add",
                                    "arguments": json.dumps(arguments),
                                },
                            }
                            for call_id, arguments in args
                        ],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    async def post(self, url, **kwargs):
        return httpx.Response(
            200, json=provider(kwargs["json"]), request=httpx.Request("POST", url)
        )

    class Stream:
        status_code = 200

        def __init__(self, data):
            self.data = data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def aiter_raw(self):
            choice = self.data["choices"][0]
            message = choice["message"]
            deltas = []
            if message.get("content"):
                deltas.append({"content": message["content"]})
            for index, call in enumerate(message.get("tool_calls", [])):
                args = call["function"]["arguments"]
                deltas.append(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": call["id"],
                                "type": "function",
                                "function": {"name": "add", "arguments": args[:5]},
                            }
                        ]
                    }
                )
                deltas.append(
                    {
                        "tool_calls": [
                            {"index": index, "function": {"arguments": args[5:]}}
                        ]
                    }
                )
            for delta in deltas:
                yield (
                    "data: "
                    + json.dumps({"choices": [{"index": 0, "delta": delta}]})
                    + "\n\n"
                ).encode()
            yield (
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": choice["finish_reason"],
                            }
                        ]
                    }
                )
                + "\n\n"
            ).encode()
            yield (
                "data: "
                + json.dumps({"choices": [], "usage": self.data["usage"]})
                + "\n\n"
            ).encode()
            yield b"data: [DONE]\n\n"

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr(
        httpx.AsyncClient,
        "stream",
        lambda self, *args, **kwargs: Stream(provider(kwargs["json"])),
    )

    async def check():
        async with sdk(app) as client:
            agent = Agent(
                name="Calculator",
                model=OpenAIResponsesModel(lane, client),
                tools=[add],
                model_settings=ModelSettings(store=stateful, parallel_tool_calls=True),
            )
            config = RunConfig(tracing_disabled=True)
            if streaming:
                result = Runner.run_streamed(
                    agent,
                    "Add the numbers",
                    run_config=config,
                    auto_previous_response_id=stateful,
                )
                events = [event async for event in result.stream_events()]
                raw = [e.data.type for e in events if e.type == "raw_response_event"]
                assert "response.function_call_arguments.delta" in raw
                assert "response.output_text.delta" in raw
                assert raw.count("response.completed") == 3
            else:
                result = await Runner.run(
                    agent,
                    "Add the numbers",
                    run_config=config,
                    auto_previous_response_id=stateful,
                )
            assert result.final_output == "The total is 10."
            assert (
                app.state.capability_router.get_response(result.last_response_id, "")
                is not None
            ) == stateful
        assert executed == [(1, 2), (3, 4), (3, 7)]
        assert paths == ["/v1/responses"] * 3
        assert bool(payloads[1].get("previous_response_id")) == stateful
        if stateful:
            assert all(
                item["type"] == "function_call_output" for item in payloads[1]["input"]
            )
        assert len(dispatched) == 3
        assert all(
            body["tools"][0]["function"]["strict"] is True for body in dispatched
        )
        assert app.state.capability_router.usage_summary()["totals"]["requests"] == 3

    asyncio.run(check())


def test_agents_structured_output(app, monkeypatch):
    from pydantic import BaseModel

    class Plan(BaseModel):
        action: str

    async def post(self, url, **kwargs):
        assert kwargs["json"]["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(
            200, json=completion('{"action":"run"}'), request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    async def check():
        async with sdk(app) as client:
            agent = Agent(
                name="Planner",
                model=OpenAIResponsesModel("smart-worker", client),
                output_type=Plan,
            )
            result = await Runner.run(
                agent, "Make a plan", run_config=RunConfig(tracing_disabled=True)
            )
            assert result.final_output == Plan(action="run")

    asyncio.run(check())
