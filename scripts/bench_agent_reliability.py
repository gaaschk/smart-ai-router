"""Bounded, simulated agent workflows through direct Chat and the real Responses adapter.

Run from repo root: python -m scripts.bench_agent_reliability --db ~/.smart_ai_router.db
  --models openrouter/openai/gpt-5-nano openrouter/qwen/qwen3.8-flash
  --repeats 3 --output /tmp/agent-results.json

Uses an in-memory snapshot of the catalog; never updates the source DB, executes
real tools, contacts CrewAI, or writes benchmark scores into production routing.
Results contain synthetic prompts/tool arguments only. Live provider calls cost money.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import time
from unittest.mock import patch

import httpx
from jsonschema import Draft202012Validator, ValidationError

from smart_ai_router.api.app import create_app
from smart_ai_router.api.proxy import _resolve_provider
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.store.sqlite_store import SqliteStore

DATES = ["2026-10-01", "2026-10-02"]
SALES = dict(zip(DATES, [12500, 14800]))
CALCS = [("subtract", 14800, 12500, 2300), ("divide", 2300, 12500, .184),
         ("multiply", .184, 100, 18.4)]


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def tool(name, description, properties):
    return {"type": "function", "name": name, "description": description,
            "strict": True, "parameters": obj(properties)}


NUMBER = {"type": "number"}
TOOLS = {
    "sales": tool("get_sales", "Return simulated sales for one requested ISO date.",
                  {"date": {"type": "string"}}),
    "calculate": tool("calculate", "Perform exactly one requested arithmetic operation.",
                      {"operation": {"type": "string", "enum": ["add", "subtract", "divide", "multiply"]},
                       "a": NUMBER, "b": NUMBER}),
    "supervisor": tool("ask_sales_agent", "Retrieve simulated sales for the requested dates.",
                       {"dates": {"type": "array", "items": {"type": "string"}, "minItems": 1}}),
    "math": tool("ask_math_agent", "Return the difference and percent increase from first to second.",
                 {"first": NUMBER, "second": NUMBER}),
    "weather": tool("get_simulated_weather", "Return simulated weather, with its fixture source.",
                    {"city": {"type": "string"}}),
}
CASES = {
    "sales": {
        "prompt": "Compare simulated sales for October 1, 2026 and October 2, 2026. "
                  "Retrieve both dates with get_sales. You must use calculate for exactly these "
                  "three dependent steps: subtract second minus first, divide the returned "
                  "difference by first, then multiply the returned ratio by 100. Do not run "
                  "examples, unrelated operations, or repeat successful calls. Return JSON "
                  "with difference and percent_change, using the tool results.",
        "tools": [TOOLS["sales"], TOOLS["calculate"]],
        "schema": obj({"difference": NUMBER, "percent_change": NUMBER}),
        "expected": {"difference": 2300, "percent_change": 18.4}, "calls": 5,
    },
    "supervisor": {
        "prompt": "Compare simulated sales for October 1, 2026 and October 2, 2026. "
                  "Call ask_sales_agent once with both ISO dates, then ask_math_agent once "
                  "with the actual returned first and second sales values. Do not invent "
                  "numbers or dates. Return JSON with difference and percent_change.",
        "tools": [TOOLS["supervisor"], TOOLS["math"]],
        "schema": obj({"difference": NUMBER, "percent_change": NUMBER}),
        "expected": {"difference": 2300, "percent_change": 18.4}, "calls": 2,
    },
    "attribution": {
        "prompt": "Use get_simulated_weather once for Paris. This is simulated data, "
                  "not a real forecast. Return JSON with temperature_c, simulated=true, "
                  "and the exact source returned by the tool; never invent a URL.",
        "tools": [TOOLS["weather"], TOOLS["calculate"]],
        "schema": obj({"temperature_c": NUMBER, "simulated": {"type": "boolean"},
                       "source": {"type": "string"}}),
        "expected": {"temperature_c": 21, "simulated": True, "source": "fixture://weather/paris"},
        "calls": 1,
    },
}


@dataclass
class Workflow:
    case: str
    completed: set = field(default_factory=set)
    calculation: int = 0
    seen: set = field(default_factory=set)
    violations: Counter = field(default_factory=Counter)
    trace: list = field(default_factory=list)

    def execute(self, name, raw):
        """Validate every call's schema AND task semantics before simulating execution."""
        tools = {t["name"]: t for t in CASES[self.case]["tools"]}
        try:
            args = json.loads(raw)
            if name not in tools:
                return self.reject("wrong_tool", name, raw)
            Draft202012Validator(tools[name]["parameters"]).validate(args)
        except (ValueError, TypeError, ValidationError):
            return self.reject("invalid_arguments", name, raw)
        signature = (name, json.dumps(args, sort_keys=True))
        if signature in self.seen:
            return self.reject("repeated_call", name, args)
        self.seen.add(signature)
        if name == "get_sales" and args["date"] in SALES:
            self.completed.add(args["date"])
            result = {"date": args["date"], "sales": SALES[args["date"]], "simulated": True}
        elif name == "ask_sales_agent" and args["dates"] == DATES:
            self.completed.add(name)
            result = {"sales": SALES, "simulated": True}
        elif (name == "ask_math_agent" and "ask_sales_agent" in self.completed
              and args == {"first": 12500, "second": 14800}):
            self.completed.add(name)
            result = CASES["supervisor"]["expected"]
        elif (name == "get_simulated_weather" and args == {"city": "Paris"}):
            self.completed.add(name)
            result = CASES["attribution"]["expected"]
        elif name == "calculate" and self.case == "sales" and set(DATES) <= self.completed and self.calculation < 3:
            operation, a, b, expected = CALCS[self.calculation]
            if args["operation"] != operation or any(
                abs(Decimal(str(args[k])) - Decimal(str(v))) > Decimal("0.000000001")
                for k, v in (("a", a), ("b", b))
            ):
                return self.reject("irrelevant_or_wrong_arguments", name, args)
            self.calculation += 1
            result = {"result": expected}
        else:
            return self.reject("irrelevant_or_wrong_arguments", name, args)
        self.trace.append({"tool": name, "arguments": args, "result": result})
        return result

    def reject(self, violation, name, args):
        self.violations[violation] += 1
        result = {"error": violation, "message": "Use only the requested tools, dates, and returned values."}
        self.trace.append({"tool": name, "arguments": args, "result": result})
        return result

    def ready(self):
        return (set(DATES) <= self.completed and self.calculation == 3 if self.case == "sales"
                else {"ask_sales_agent", "ask_math_agent"} <= self.completed if self.case == "supervisor"
                else "get_simulated_weather" in self.completed)

    def finish(self, text):
        case = CASES[self.case]
        complete = self.ready()
        if not complete:
            self.violations["required_tool_bypassed"] += 1
        try:
            value = json.loads(text)
            Draft202012Validator(case["schema"]).validate(value)
        except (ValueError, TypeError, ValidationError):
            self.violations["invalid_structured_output"] += 1
            return False
        if value != case["expected"]:
            self.violations["wrong_answer_or_attribution"] += 1
        return value == case["expected"] and complete


def chat_payload(body, model):
    """Independent native-Chat baseline, so adapter translation is not its own oracle."""
    messages = []
    for item in body["input"]:
        kind = item.get("type", "message")
        if kind == "function_call":
            call = {"id": item["call_id"], "type": "function", "function": {
                "name": item["name"], "arguments": item["arguments"]}}
            if messages and messages[-1]["role"] == "assistant":
                messages[-1].setdefault("tool_calls", []).append(call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif kind == "function_call_output":
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
        else:
            content = item["content"]
            if isinstance(content, list):
                content = "".join(p["text"] for p in content if p["type"] in ("input_text", "output_text"))
            messages.append({"role": item["role"], "content": content})
    fmt = body["text"]["format"]
    payload = {"model": model, "messages": messages,
            "tools": [{"type": "function", "function": {k: v for k, v in t.items() if k != "type"}}
                      for t in body["tools"]], "parallel_tool_calls": False,
            "max_tokens": body["max_output_tokens"]}
    if fmt["type"] == "json_schema":
        payload["response_format"] = {"type": "json_schema", "json_schema": {k: v for k, v in fmt.items() if k != "type"}}
    return payload


async def run_case(request, case_name, *, max_turns=8, max_calls=16, schema_mode="combined", request_timeout=90):
    case, workflow = CASES[case_name], Workflow(case_name)
    inputs = [{"role": "user", "content": case["prompt"]}]
    result = {"case": case_name, "turns": 0, "tool_calls": 0, "input_tokens": 0,
              "output_tokens": 0, "cost_usd": 0.0, "models": [], "backends": [], "task_success": False}
    started = time.perf_counter()
    for _ in range(max_turns):
        body = {"model": "smart-orchestrator", "input": inputs, "store": False,
                "tools": case["tools"], "parallel_tool_calls": False,
                "max_output_tokens": 8000,
                "text": {"format": {"type": "json_schema", "name": "answer",
                                     "strict": True, "schema": case["schema"]}}}
        if schema_mode == "separate":
            if workflow.ready():
                body["tools"] = []
            else:
                body["text"] = {"format": {"type": "text"}}
        result["turns"] += 1
        try:
            reply = await asyncio.wait_for(request(body), timeout=request_timeout)
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as exc:
            result["error"] = type(exc).__name__ + ": " + str(exc)[:300]
            break
        usage = reply.get("usage") or {}
        for dest, source in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens")):
            result[dest] += usage.get(source, 0) or 0
        result["cost_usd"] += usage.get("cost", 0) or 0
        result["models"].append(reply.get("model"))
        result["backends"].append(reply.get("provider"))
        calls = [i for i in reply["output"] if i["type"] == "function_call"]
        if reply.get("status") != "completed":
            result["error"] = "incomplete_or_failed_response"
            break
        if not calls:
            result["final_text"] = "".join(
                p.get("text", "") for i in reply["output"] if i["type"] == "message"
                for p in i.get("content", []) if p["type"] == "output_text")
            result["task_success"] = workflow.finish(result["final_text"])
            break
        if len(calls) > 1:
            workflow.violations["parallel_constraint_ignored"] += 1
        inputs.extend(reply["output"])
        for call in calls:
            result["tool_calls"] += 1
            if result["tool_calls"] > max_calls:
                result["error"] = "tool_call_budget_exceeded"
                break
            output = workflow.execute(call["name"], call["arguments"])
            inputs.append({"type": "function_call_output", "call_id": call["call_id"],
                           "output": json.dumps(output)})
        if result.get("error"):
            break
    else:
        result["error"] = "turn_budget_exceeded"
    result.update(latency_s=round(time.perf_counter() - started, 3),
                  violations=dict(workflow.violations), trace=workflow.trace,
                  excess_calls=max(0, result["tool_calls"] - case["calls"]))
    result["reliable_success"] = result["task_success"] and not workflow.violations and not result.get("error")
    return result


def snapshot(path):
    store = SqliteStore(":memory:")
    source = sqlite3.connect(Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True)
    try:
        source.backup(store._conn)  # In-memory snapshot preserves observed columns and probe verdicts.
    finally:
        source.close()
    return store


async def benchmark(args):
    source = snapshot(args.db)
    results = []
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"SMART_ROUTER_API_KEYS": "benchmark-local-only"}))
        stack.enter_context(patch("smart_ai_router.api.proxy._enrich_with_gbrain_context", lambda m, *a, **k: m))
        stack.enter_context(patch("smart_ai_router.api.proxy._schedule_gbrain_save_back", lambda *a, **k: None))
        for model in args.models:
            store = SqliteStore(":memory:")
            source._conn.backup(store._conn)
            store._conn.execute("DELETE FROM models WHERE value != ?", (model,))
            store._conn.commit()
            cr = CapabilityRouter(store=store)
            spec = cr.get_model(model)
            if spec is None:
                raise ValueError(f"Model absent from catalog: {model}")
            for key, value in {"classifier_model": "", "classifier_fallback": "", "classifier_refine_model": "",
                               "web_search_enabled": "false", "capture_turn_percent": "0"}.items():
                store.set_setting(key, value)
            app = create_app(cr)
            base, key, real_model = _resolve_provider(model, cr)
            async with httpx.AsyncClient(timeout=args.timeout) as direct, httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://benchmark",
                headers={"Authorization": "Bearer benchmark-local-only"}, timeout=args.timeout,
            ) as adapter:
                async def request(body, protocol):
                    if protocol == "responses":
                        response = await adapter.post("/v1/responses", json=body)
                        if response.status_code >= 400:
                            raise ValueError(f"router HTTP {response.status_code}: {response.text[:300]}")
                        data = response.json()
                        # Responses exposes token usage; use catalog rates for comparable cost estimates.
                        u = data.get("usage") or {}
                        u["cost"] = ((u.get("input_tokens", 0) or 0) * spec.cost_input +
                                     (u.get("output_tokens", 0) or 0) * spec.cost_output) / 1e6
                        data["usage"] = u
                        data["model"] = response.headers.get("x-routed-model", data.get("model"))
                        return data
                    payload = chat_payload(body, real_model)
                    response = await direct.post(base.rstrip('/') + '/chat/completions', json=payload,
                                                 headers={"Authorization": f"Bearer {key}"} if key else {})
                    if response.status_code >= 400:
                        raise ValueError(f"provider HTTP {response.status_code}: {response.text[:300]}")
                    data = response.json()
                    choice = data["choices"][0]
                    msg = choice["message"]
                    output = [{"type": "function_call", "call_id": c["id"], **c["function"]}
                              for c in msg.get("tool_calls", [])]
                    if msg.get("content"):
                        output.append({"type": "message", "role": "assistant", "content": [
                            {"type": "output_text", "text": msg["content"]}]})
                    u = data.get("usage") or {}
                    cost = u.get("cost")
                    return {"output": output, "model": data.get("model", model), "provider": data.get("provider"),
                            "status": "completed" if choice.get("finish_reason") in ("stop", "tool_calls") else "incomplete",
                            "usage": {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0),
                                      "cost": cost if cost is not None else (u.get("prompt_tokens", 0)*spec.cost_input + u.get("completion_tokens", 0)*spec.cost_output)/1e6}}
                for protocol in args.protocols:
                    for repeat in range(args.repeats):
                        for case in args.cases:
                            row = await run_case(lambda body: request(body, protocol), case,
                                                 max_turns=args.max_turns, max_calls=args.max_calls,
                                                 schema_mode=args.schema_mode, request_timeout=args.timeout)
                            row.update(catalog_model=model, protocol=protocol, repeat=repeat, schema_mode=args.schema_mode,
                                       health_after=cr.get_model(model).observed_tool_health)
                            results.append(row)
                            args.output.write_text(json.dumps({"results": results, "complete": False}, indent=2) + "\n")
                            print(json.dumps({k: v for k, v in row.items() if k not in ("trace", "final_text")}), flush=True)
    return {"complete": True, "created_at": datetime.now(timezone.utc).isoformat(), "repeats": args.repeats,
            "limits": {"max_turns": args.max_turns, "max_calls": args.max_calls},
            "cost_note": "direct: provider cost when supplied; responses: catalog token estimate, excludes overhead",
            "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="~/.smart_ai_router.db")
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--protocols", nargs="+", choices=["chat", "responses"], default=["chat", "responses"])
    parser.add_argument("--cases", nargs="+", choices=list(CASES), default=list(CASES))
    parser.add_argument("--schema-mode", choices=["combined", "separate"], default="combined",
                        help="Combine tools + output schema, or use a tool-free schema-only final request")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--max-calls", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if min(args.repeats, args.max_turns, args.max_calls, args.timeout) <= 0:
        parser.error("repeats, budgets and timeout must be positive")
    # Checkpoint after each workflow so interruption retains completed measurements.
    result = asyncio.run(benchmark(args))
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
