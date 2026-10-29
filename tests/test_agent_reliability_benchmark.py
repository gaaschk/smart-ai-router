"""Deterministic benchmark oracles; these do not assert that a mocked model is reliable."""

import asyncio
import json

import pytest

from scripts.bench_agent_reliability import (
    Workflow,
    CASES,
    chat_payload,
    run_case,
    snapshot,
)
from smart_ai_router.api.responses_routes import _chat_body, _validate_output
from smart_ai_router.store.sqlite_store import SqliteStore


def test_sales_requires_exact_values_and_all_dependent_operations():
    w = Workflow("sales")
    for date in ("2026-10-02", "2026-10-01"):
        assert "error" not in w.execute("get_sales", json.dumps({"date": date}))
    for operation, a, b in [
        ("subtract", 14800, 12500),
        ("divide", 2300, 12500),
        ("multiply", 0.184, 100),
    ]:
        assert "error" not in w.execute(
            "calculate", json.dumps(dict(operation=operation, a=a, b=b))
        )
    assert w.finish(json.dumps(CASES["sales"]["expected"]))
    assert not w.violations


@pytest.mark.parametrize(
    "arguments", [{"dates": []}, {"dates": ["2025-10-01"]}, {"dates": ["2026-10-01"]}]
)
def test_empty_or_irrelevant_sales_dates_fail(arguments):
    w = Workflow("supervisor")
    assert "error" in w.execute("ask_sales_agent", json.dumps(arguments))
    assert w.violations


def test_irrelevant_operations_and_wrong_values_are_not_success():
    w = Workflow("sales")
    for date in ("2026-10-01", "2026-10-02"):
        w.execute("get_sales", json.dumps({"date": date}))
    assert "error" in w.execute("calculate", '{"operation":"subtract","a":15,"b":10}')
    assert "error" in w.execute("calculate", '{"operation":"multiply","a":0.184,"b":1}')
    assert w.violations["irrelevant_or_wrong_arguments"] == 2


def test_repeats_and_required_tool_bypass_fail_even_with_correct_answer():
    w = Workflow("supervisor")
    raw = json.dumps({"dates": ["2026-10-01", "2026-10-02"]})
    w.execute("ask_sales_agent", raw)
    assert "error" in w.execute("ask_sales_agent", raw)
    assert not w.finish(json.dumps(CASES["supervisor"]["expected"]))
    assert w.violations["repeated_call"] == 1
    assert w.violations["required_tool_bypassed"] == 1


def test_simulated_attribution_and_schema_are_checked():
    w = Workflow("attribution")
    w.execute("get_simulated_weather", '{"city":"Paris"}')
    assert not w.finish(
        '{"temperature_c":21,"simulated":false,"source":"https://invented.example"}'
    )
    assert w.violations["wrong_answer_or_attribution"] == 1
    assert not w.finish("It is sunny")
    assert w.violations["invalid_structured_output"] == 1


def test_native_chat_baseline_matches_responses_translation_without_losing_tool_values():
    body = {
        "model": "smart-orchestrator",
        "input": [
            {"role": "user", "content": "Compare sales"},
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "ask_sales_agent",
                "arguments": '{"dates":["2026-10-01","2026-10-02"]}',
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": '{"sales":{"2026-10-01":12500,"2026-10-02":14800}}',
            },
        ],
        "tools": CASES["supervisor"]["tools"],
        "parallel_tool_calls": False,
        "max_output_tokens": 8000,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "answer",
                "strict": True,
                "schema": CASES["supervisor"]["schema"],
            }
        },
    }
    expected = chat_payload(body, body["model"])
    actual = _chat_body(body)
    actual.pop("agent")
    assert actual == expected
    assert json.loads(actual["messages"][-1]["content"])["sales"]["2026-10-02"] == 14800


def test_responses_schema_validation_does_not_establish_semantic_correctness():
    # A schema-valid but wrong value passes the protocol adapter; the benchmark catches it.
    body = {"tools": CASES["supervisor"]["tools"]}
    response = {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "call_id": "x",
                "name": "ask_math_agent",
                "arguments": '{"first":15,"second":10}',
            }
        ],
    }
    _validate_output(response, body)
    w = Workflow("supervisor")
    assert "error" in w.execute("ask_math_agent", response["output"][0]["arguments"])


def test_bounded_loop_preserves_violations_when_model_eventually_answers():
    calls = iter(
        [
            {
                "type": "function_call",
                "call_id": "1",
                "name": "get_simulated_weather",
                "arguments": '{"city":"Paris"}',
            },
            {
                "type": "function_call",
                "call_id": "2",
                "name": "get_simulated_weather",
                "arguments": '{"city":"Paris"}',
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(CASES["attribution"]["expected"]),
                    }
                ],
            },
        ]
    )

    async def request(body):
        return {"status": "completed", "output": [next(calls)], "model": "test"}

    result = asyncio.run(run_case(request, "attribution"))
    assert result["task_success"] is True
    assert result["reliable_success"] is False
    assert result["violations"] == {"repeated_call": 1}
    assert result["excess_calls"] == 1


def test_snapshot_cannot_modify_source_database(tmp_path):
    path = tmp_path / "catalog.db"
    source = SqliteStore(str(path))
    source.set_setting("min_tool_health", "70")
    copy = snapshot(path)
    copy.set_setting("min_tool_health", "0")
    assert source.get_setting("min_tool_health") == "70"


def test_separate_schema_mode_only_requests_structured_final_after_tools_complete():
    sent = []

    async def request(body):
        sent.append(body)
        output = (
            {
                "type": "function_call",
                "call_id": "weather",
                "name": "get_simulated_weather",
                "arguments": '{"city":"Paris"}',
            }
            if len(sent) == 1
            else {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(CASES["attribution"]["expected"]),
                    }
                ],
            }
        )
        return {"status": "completed", "output": [output]}

    result = asyncio.run(run_case(request, "attribution", schema_mode="separate"))
    assert result["reliable_success"]
    assert sent[0]["tools"] and sent[0]["text"]["format"]["type"] == "text"
    assert "response_format" not in chat_payload(sent[0], "model")
    assert sent[1]["tools"] == [] and sent[1]["text"]["format"]["type"] == "json_schema"


def test_benchmark_bounds_waits_and_keeps_transport_errors_separate():
    async def request(body):
        await asyncio.sleep(1)

    result = asyncio.run(run_case(request, "sales", request_timeout=0.001))
    assert result["error"].startswith("TimeoutError")
    assert result["violations"] == {}
    assert result["tool_calls"] == 0


def test_short_valid_final_answer_is_not_a_stall():
    """Regression: optional tools permit a short structured final answer."""
    from types import SimpleNamespace
    from smart_ai_router.api.proxy import _log_usage

    records = []
    router = SimpleNamespace(record_usage=records.append, cost_for=lambda *args: 0)
    request = SimpleNamespace(state=SimpleNamespace(user="benchmark"))
    _log_usage(
        router,
        request,
        routed_model="test",
        domain="reasoning",
        complexity="basic",
        usage={"prompt_tokens": 10, "completion_tokens": 12},
        status=200,
        tools_offered=True,
        tool_calls=[],
        content='{"difference":2300,"percent_change":18.4}',
    )
    assert records[0].tool_stalled is False
