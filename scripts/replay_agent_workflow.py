"""Offline, synthetic workflow replay. Never reads a live catalog or calls a provider.

python -m scripts.replay_agent_workflow --output /tmp/workflow-replay.json
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack, ExitStack
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx

from scripts.bench_agent_reliability import CASES, chat_payload, run_case
from smart_ai_router import settings
from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec, ProviderConfig
from smart_ai_router.store.sqlite_store import SqliteStore

FIXTURE = (
    Path(__file__).resolve().parents[1] / "tests/fixtures/agent_workflow_replay.json"
)
BASE = "https://replay.invalid/v1"
MODEL = "openrouter/replay/supervisor-v1"


def sse(data):
    return ("data: " + json.dumps(data) + "\n\n").encode()


class ProviderReplay:
    def __init__(self, fixture, scenario):
        self.fixture, self.scenario = fixture, scenario
        self.requests, self.ledger = [], []

    async def handle_async(self, request):
        # Awaitable transport boundary: cancellation reaches the provider and
        # its response stream before the replay closes clients or the DB.
        return self(request)

    def __call__(self, request):
        if str(request.url) != BASE + "/chat/completions":
            raise ValueError("Offline replay rejected an unexpected URL")
        body = json.loads(request.content)
        index = len(self.requests)
        if index >= len(self.scenario["turns"]):
            raise ValueError("Fixture exhausted without a final answer")
        self.requests.append(body)
        turn = self.scenario["turns"][index]
        billing = self.fixture["billing"]
        status = turn.get("http_status", 200)
        usage = {
            "prompt_tokens": billing["input_tokens_per_reply"],
            "completion_tokens": billing["output_tokens_per_reply"],
        }
        self.ledger.append(
            {
                "status": status,
                "usage": usage if status == 200 else None,
                "synthetic_latency_ms": billing["latency_ms_per_reply"],
            }
        )
        if status != 200:
            return httpx.Response(
                status, json={"error": {"message": "synthetic provider failure"}}
            )
        message = {"role": "assistant", "content": turn.get("text")}
        if turn.get("calls"):
            message["tool_calls"] = turn["calls"]
        finish = "tool_calls" if turn.get("calls") else "stop"
        if not body.get("stream"):
            return httpx.Response(
                200,
                json={
                    "model": "replay/supervisor-v1",
                    "provider": "synthetic",
                    "choices": [
                        {"index": 0, "message": message, "finish_reason": finish}
                    ],
                    "usage": usage,
                },
            )
        delta = dict(message)
        if turn.get("calls"):
            delta["tool_calls"] = [
                {"index": i, **call} for i, call in enumerate(turn["calls"])
            ]
        data = sse({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
        data += sse({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        data += sse({"choices": [], "usage": usage}) + b"data: [DONE]\n\n"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(data),
        )


def native_reply(response, stream):
    """Parse native Chat independently of the router's Responses translator."""
    response.raise_for_status()
    if not stream:
        data = response.json()
        choice = data["choices"][0]
        message, finish, usage = (
            choice["message"],
            choice["finish_reason"],
            data["usage"],
        )
    else:
        message, finish, usage = {"content": "", "tool_calls": []}, None, {}
        for line in response.text.splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            data = json.loads(line[6:])
            usage = data.get("usage", usage)
            for choice in data["choices"]:
                delta = choice["delta"]
                message["content"] += delta.get("content") or ""
                # Fixture emits complete call deltas; production chunk assembly is
                # exercised by existing scanner/Responses tests, not this parser.
                message["tool_calls"].extend(delta.get("tool_calls", []))
                finish = choice.get("finish_reason") or finish
    output = [
        {"type": "function_call", "call_id": c["id"], **c["function"]}
        for c in message.get("tool_calls", [])
    ]
    if message.get("content"):
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": message["content"]}],
            }
        )
    return {
        "status": "completed" if finish in ("stop", "tool_calls") else "incomplete",
        "model": MODEL,
        "provider": "synthetic",
        "output": output,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


async def replay_one(fixture, scenario, protocol, stream):
    provider = ProviderReplay(fixture, scenario)
    transport = httpx.MockTransport(provider.handle_async)
    adapter_transport = None
    original_init = httpx.AsyncClient.__init__

    def offline_init(self, *args, **kwargs):
        # Only our exact in-process adapter transport may bypass the provider
        # mock. All other clients (including proxy-created clients) stay offline.
        if (
            adapter_transport is None
            or kwargs.get("transport") is not adapter_transport
        ):
            kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    limits = fixture["limits"]
    store = SqliteStore(":memory:")
    cr = CapabilityRouter(store=store)
    billing = fixture["billing"]
    store.upsert_model(
        ModelSpec(
            value=MODEL,
            provider="openrouter",
            tools=True,
            structured_outputs=True,
            ctx_k=200,
            cost=1,
            reliability=1,
            cost_input=billing["usd_per_million_input"],
            cost_output=billing["usd_per_million_output"],
            competence={
                "general": 0.99,
                "reasoning": 0.99,
                "coding": 0.99,
                "docs": 0.99,
            },
        )
    )
    cr.upsert_provider(
        ProviderConfig(name="openrouter", kind="openrouter", base_url=BASE)
    )
    for key, value in {
        "classifier_model": "",
        "classifier_fallback": "",
        "classifier_refine_model": "",
        "web_search_enabled": "false",
        "capture_turn_percent": "0",
    }.items():
        store.set_setting(key, value)
    previous_store = settings._store
    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch.dict(
                    os.environ,
                    {
                        "SMART_ROUTER_API_KEYS": "synthetic-replay-key",
                        "SMART_ROUTER_MODEL_DENYLIST": "",
                    },
                )
            )
            stack.enter_context(patch("httpx.AsyncClient.__init__", offline_init))
            stack.enter_context(
                patch("smart_ai_router.api.proxy._OPENROUTER_BASE", BASE)
            )
            stack.enter_context(
                patch(
                    "smart_ai_router.api.proxy._enrich_with_gbrain_context",
                    lambda m, *a, **k: m,
                )
            )
            stack.enter_context(
                patch(
                    "smart_ai_router.api.proxy._schedule_gbrain_save_back",
                    lambda *a, **k: None,
                )
            )
            adapter_transport = httpx.ASGITransport(app=create_app(cr))

            async def request(body):
                if protocol == "chat":
                    payload = chat_payload(body, "replay/supervisor-v1")
                    payload["stream"] = stream
                    response = await direct.post(
                        BASE + "/chat/completions", json=payload
                    )
                    return native_reply(response, stream)
                response = await client.post(
                    "/v1/responses",
                    headers={"Authorization": "Bearer synthetic-replay-key"},
                    json={**body, "stream": stream},
                )
                if response.status_code >= 400:
                    raise ValueError(
                        f"router HTTP {response.status_code}: {response.text[:300]}"
                    )
                if not stream:
                    return response.json()
                for line in response.text.splitlines():
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        if event.get("type") in (
                            "response.completed",
                            "response.failed",
                            "response.incomplete",
                        ):
                            return event["response"]
                raise ValueError("No terminal Responses event")

            async with AsyncExitStack() as clients:
                direct = await clients.enter_async_context(
                    httpx.AsyncClient(transport=transport)
                )
                client = await clients.enter_async_context(
                    httpx.AsyncClient(
                        transport=adapter_transport, base_url="http://replay-adapter"
                    )
                )
                row = await run_case(
                    request,
                    scenario["case"],
                    max_turns=limits["max_turns"],
                    max_calls=limits["max_calls"],
                    request_timeout=limits["request_timeout_s"],
                )
            rows = cr.recent_usage("admin", "")
            row.update(
                scenario=scenario["name"],
                protocol=protocol,
                stream=stream,
                provider_requests=provider.requests,
                provider_ledger=provider.ledger,
                router_usage=[
                    {
                        "status": r.status,
                        "tool_stalled": r.tool_stalled,
                        "input_tokens": r.prompt_tokens,
                        "output_tokens": r.completion_tokens,
                        "cost_usd": r.cost_usd,
                    }
                    for r in rows
                ],
                health_after=cr.get_model(MODEL).observed_tool_health,
            )
    finally:
        settings.bind_store(previous_store)
        store._conn.close()
    # Include all dispatched replies, including adapter rejections and recovery turns.
    known = [entry["usage"] for entry in provider.ledger if entry["usage"] is not None]
    row["synthetic_cost_usd"] = round(
        sum(
            u["prompt_tokens"] * billing["usd_per_million_input"]
            + u["completion_tokens"] * billing["usd_per_million_output"]
            for u in known
        )
        / 1e6,
        8,
    )
    row["cost_unavailable_calls"] = sum(
        entry["usage"] is None for entry in provider.ledger
    )
    row["synthetic_latency_ms"] = sum(
        entry["synthetic_latency_ms"] for entry in provider.ledger
    )
    row["within_synthetic_limits"] = (
        None
        if row["cost_unavailable_calls"]
        else (
            row["synthetic_cost_usd"] <= limits["max_synthetic_cost_usd"]
            and row["synthetic_latency_ms"] <= limits["max_synthetic_latency_ms"]
        )
    )
    row["local_replay_latency_s"] = row.pop("latency_s")
    row.pop(
        "cost_usd"
    )  # run_case's missing cost default must not be presented as measured zero
    return row


async def replay(fixture):
    rows = []
    for scenario in fixture["scenarios"]:
        for protocol in ("chat", "responses"):
            for stream in (False, True):
                rows.append(await replay_one(fixture, scenario, protocol, stream))
    return {
        "fixture_version": fixture["fixture_version"],
        "provenance": fixture["provenance"],
        "limits": fixture["limits"],
        "billing": fixture["billing"],
        "prompt_and_schema": {
            key: CASES[key] for key in {s["case"] for s in fixture["scenarios"]}
        },
        "actual_external_spend_usd": 0,
        "results": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = FIXTURE.read_bytes()
    result = asyncio.run(replay(json.loads(raw)))
    result["fixture_sha256"] = hashlib.sha256(raw).hexdigest()
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {len(result['results'])} synthetic offline runs to {args.output}")


if __name__ == "__main__":
    main()
