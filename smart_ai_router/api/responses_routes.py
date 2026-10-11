"""Stateless Responses API adapter over the shared routing/accounting path."""
from __future__ import annotations

import codecs
import json
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from smart_ai_router.api.proxy import chat_completions

# shortcut: stateless Responses only; add owner-scoped storage when continuation is needed.
responses_router = APIRouter()


def _invalid(message, param=None):
    raise HTTPException(400, detail={"message": message, "type": "invalid_request_error",
                                     "param": param, "code": "unsupported_or_invalid_parameter"})


def _content(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        _invalid("Message content must be a string or array", "input")
    result = []
    for part in value:
        if not isinstance(part, dict):
            _invalid("Content parts must be objects", "input")
        kind = part.get("type")
        if kind in {"input_text", "output_text"} and isinstance(part.get("text"), str):
            result.append({"type": "text", "text": part["text"]})
        elif kind == "input_image" and isinstance(part.get("image_url"), str):
            result.append({"type": "image_url", "image_url": {
                "url": part["image_url"], "detail": part.get("detail", "auto")}})
        elif kind == "input_file" and isinstance(part.get("file_id"), str):
            result.append({"type": "file", "file": {"file_id": part["file_id"]}})
        else:
            _invalid(f"Unsupported or invalid content type: {kind}", "input")
    return result


def _chat_body(body):
    if not isinstance(body, dict):
        _invalid("Request body must be an object")
    supported = {"model", "input", "instructions", "stream", "store", "tools", "tool_choice",
                 "parallel_tool_calls", "temperature", "top_p", "max_output_tokens", "text",
                 "reasoning", "metadata", "user", "previous_response_id", "background",
                 "include", "truncation", "service_tier", "prompt_cache_key", "safety_identifier"}
    for key in body.keys() - supported:
        _invalid(f"Unsupported parameter: {key}", key)
    for key in ("previous_response_id", "background", "store", "include"):
        if body.get(key):
            _invalid(f"{key} is not supported; send complete history with store=false", key)
    if body.get("truncation", "disabled") != "disabled":
        _invalid("Only disabled truncation is supported", "truncation")
    if not isinstance(body.get("model"), str) or not body["model"]:
        _invalid("model is required", "model")
    for key in ("stream", "store", "background", "parallel_tool_calls"):
        if key in body and not isinstance(body[key], bool):
            _invalid(f"{key} must be a boolean", key)
    if "metadata" in body and (not isinstance(body["metadata"], dict) or
                               any(not isinstance(k, str) or not isinstance(v, str)
                                   for k, v in body["metadata"].items())):
        _invalid("metadata must be an object of strings", "metadata")
    for key in ("temperature", "top_p"):
        if key in body and (type(body[key]) not in (int, float) or
                            not 0 <= body[key] <= (2 if key == "temperature" else 1)):
            _invalid(f"Invalid {key}", key)
    messages = []
    if body.get("instructions") is not None:
        if not isinstance(body["instructions"], str):
            _invalid("instructions must be a string", "instructions")
        messages.append({"role": "system", "content": body["instructions"]})
    items = body.get("input")
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    if not isinstance(items, list) or not items:
        _invalid("input must be a nonempty string or array", "input")
    for item in items:
        if not isinstance(item, dict):
            _invalid("Input items must be objects", "input")
        kind = item.get("type", "message")
        if kind == "message":
            if item.get("role") not in {"system", "developer", "user", "assistant"}:
                _invalid("Invalid message role", "input")
            messages.append({"role": item["role"], "content": _content(item.get("content"))})
        elif kind == "function_call":
            if not all(isinstance(item.get(k), str) for k in ("call_id", "name", "arguments")):
                _invalid("Function calls require call_id, name and arguments strings", "input")
            call = {"id": item["call_id"], "type": "function", "function": {
                "name": item["name"], "arguments": item["arguments"]}}
            if messages and messages[-1]["role"] == "assistant" and messages[-1].get("tool_calls"):
                messages[-1]["tool_calls"].append(call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif kind == "function_call_output" and isinstance(item.get("output"), str) and isinstance(item.get("call_id"), str):
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
        else:
            _invalid(f"Unsupported or invalid input item: {kind}", "input")
    chat = {k: body[k] for k in ("model", "stream", "temperature", "top_p", "parallel_tool_calls", "user", "service_tier", "prompt_cache_key", "safety_identifier") if k in body}
    # Client tool execution must never turn into the router's filesystem agent.
    chat.update(messages=messages, agent=False)
    if "max_output_tokens" in body:
        value = body["max_output_tokens"]
        if type(value) is not int or value <= 0:
            _invalid("max_output_tokens must be a positive integer", "max_output_tokens")
        chat["max_tokens"] = value
    tools = body.get("tools", [])
    if not isinstance(tools, list):
        _invalid("tools must be an array", "tools")
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("name"), str):
            _invalid("Only named function tools are supported", "tools")
    if tools:
        chat["tools"] = [{"type": "function", "function": {k: v for k, v in t.items() if k != "type"}} for t in tools]
    choice = body.get("tool_choice")
    if choice is not None:
        if isinstance(choice, dict) and choice.get("type") == "function" and isinstance(choice.get("name"), str):
            choice = {"type": "function", "function": {"name": choice["name"]}}
        elif choice not in ("auto", "none", "required"):
            _invalid("Unsupported tool_choice", "tool_choice")
        chat["tool_choice"] = choice
    if "text" in body:
        text = body["text"]
        if not isinstance(text, dict) or text.keys() - {"format"}:
            _invalid("Only text.format is supported", "text")
        fmt = text.get("format", {"type": "text"})
        if not isinstance(fmt, dict) or fmt.get("type") not in {"text", "json_object", "json_schema"}:
            _invalid("Invalid text.format", "text.format")
        if fmt["type"] == "json_schema":
            chat["response_format"] = {"type": "json_schema", "json_schema": {k: v for k, v in fmt.items() if k != "type"}}
        else:
            chat["response_format"] = fmt
    if "reasoning" in body:
        reasoning = body["reasoning"]
        if not isinstance(reasoning, dict) or reasoning.keys() - {"effort", "summary"} or reasoning.get("summary"):
            _invalid("Only reasoning.effort is supported", "reasoning")
        if "effort" in reasoning:
            if reasoning["effort"] not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
                _invalid("Invalid reasoning effort", "reasoning.effort")
            chat["reasoning_effort"] = reasoning["effort"]
    return chat


def _id(prefix):
    return prefix + uuid.uuid4().hex


def _response(body):
    return {"id": _id("resp_"), "object": "response", "created_at": int(time.time()),
            "status": "in_progress", "error": None, "incomplete_details": None,
            "model": body["model"], "output": [], "usage": None, "store": False,
            "instructions": body.get("instructions"), "metadata": body.get("metadata", {}),
            "parallel_tool_calls": body.get("parallel_tool_calls", True),
            "tools": body.get("tools", []), "tool_choice": body.get("tool_choice", "auto")}


def _usage(usage):
    return {"input_tokens": usage.get("prompt_tokens", 0),
            "input_tokens_details": {"cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)},
            "output_tokens": usage.get("completion_tokens", 0),
            "output_tokens_details": {"reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)},
            "total_tokens": usage.get("total_tokens", usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))}


def _finish(response, reason):
    response["status"] = "incomplete" if reason in {"length", "content_filter"} else "completed"
    if response["status"] == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens" if reason == "length" else reason}
    for item in response["output"]:
        item["status"] = "completed"


def _message():
    return {"id": _id("msg_"), "type": "message", "role": "assistant", "status": "in_progress", "content": []}


def _part(kind, value=""):
    if kind == "refusal":
        return {"type": kind, "refusal": value}
    return {"type": "output_text", "text": value, "annotations": []}


async def _stream(source, response):
    sequence = 0

    def event(kind, **fields):
        nonlocal sequence
        payload = {"type": kind, "sequence_number": sequence, **fields}
        sequence += 1
        return f"event: {kind}\ndata: {json.dumps(payload)}\n\n".encode()

    yield event("response.created", response=response)
    yield event("response.in_progress", response=response)
    buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")()
    message = None
    calls = {}
    finish = None
    ended = False
    try:
        async for chunk in source:
            buffer += decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
            buffer = buffer.replace("\r\n", "\n")
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                data = "\n".join(line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:"))
                if not data:
                    yield b": keepalive\n\n"
                    continue
                if data == "[DONE]":
                    ended = True
                    break
                obj = json.loads(data)
                if obj.get("error"):
                    error = obj["error"]
                    raise ValueError(error.get("message", str(error)) if isinstance(error, dict) else str(error))
                if obj.get("usage"):
                    response["usage"] = _usage(obj["usage"])
                if obj.get("model"):
                    response["model"] = obj["model"]
                for choice in obj.get("choices", []):
                    if choice.get("index", 0) != 0:
                        continue
                    finish = choice.get("finish_reason") or finish
                    delta = choice.get("delta", {})
                    for key, kind in (("content", "output_text"), ("refusal", "refusal")):
                        value = delta.get(key)
                        if not value:
                            continue
                        if message is None:
                            message = _message()
                            response["output"].append(message)
                            yield event("response.output_item.added", output_index=response["output"].index(message), item=message)
                        index = response["output"].index(message)
                        part = next((p for p in message["content"] if p["type"] == kind), None)
                        if part is None:
                            part = _part(kind)
                            message["content"].append(part)
                            yield event("response.content_part.added", item_id=message["id"], output_index=index,
                                        content_index=message["content"].index(part), part=part)
                        field = "refusal" if kind == "refusal" else "text"
                        part[field] += value
                        yield event(f"response.{kind}.delta", item_id=message["id"], output_index=index,
                                    content_index=message["content"].index(part), delta=value)
                    for frag in delta.get("tool_calls", []):
                        tool_index = frag["index"]
                        if tool_index not in calls:
                            calls[tool_index] = {"id": _id("fc_"), "type": "function_call", "status": "in_progress",
                                                 "call_id": "", "name": "", "arguments": ""}
                        item = calls[tool_index]
                        item["call_id"] += frag.get("id") or ""
                        fn = frag.get("function") or {}
                        item["name"] += fn.get("name") or ""
                        # Providers can split the name/id across chunks; announce once arguments begin.
                        announced = any(i is item for i in response["output"])
                        if not announced and item["call_id"] and item["name"] and "arguments" in fn:
                            response["output"].append(item)
                            yield event("response.output_item.added", output_index=response["output"].index(item), item=dict(item))
                        args = fn.get("arguments") or ""
                        item["arguments"] += args
                        if args and any(i is item for i in response["output"]):
                            yield event("response.function_call_arguments.delta", item_id=item["id"],
                                        output_index=response["output"].index(item), delta=args)
            if ended:
                break
        if finish is None:
            raise ValueError("Upstream stream ended without a finish reason")
        if any(not c["call_id"] or not c["name"] or
               not any(i is c for i in response["output"]) for c in calls.values()):
            raise ValueError("Upstream returned an incomplete function call")
        _finish(response, finish)
        for index, item in enumerate(response["output"]):
            if item["type"] == "message":
                for content_index, part in enumerate(item["content"]):
                    kind = part["type"]
                    field = "refusal" if kind == "refusal" else "text"
                    yield event(f"response.{kind}.done", item_id=item["id"], output_index=index,
                                content_index=content_index, **{field: part[field]})
                    yield event("response.content_part.done", item_id=item["id"], output_index=index,
                                content_index=content_index, part=part)
            else:
                yield event("response.function_call_arguments.done", item_id=item["id"], output_index=index,
                            arguments=item["arguments"])
            yield event("response.output_item.done", output_index=index, item=item)
        yield event(f"response.{response['status']}", response=response)
    except Exception as exc:
        response.update(status="failed", error={"code": "server_error", "message": str(exc)})
        yield event("response.failed", response=response)
    finally:
        if hasattr(source, "aclose"):
            await source.aclose()


@responses_router.post("/v1/responses")
async def create_response(request: Request):
    try:
        try:
            body = await request.json()
        except ValueError:
            _invalid("Invalid JSON body")
        chat = _chat_body(body)
        raw = json.dumps(chat).encode()

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        forwarded = Request(request.scope, receive)
        upstream = await chat_completions(forwarded)
    except HTTPException as exc:
        detail = exc.detail
        error = detail if isinstance(detail, dict) else {"message": str(detail), "type": "invalid_request_error" if exc.status_code < 500 else "server_error", "param": None, "code": None}
        return JSONResponse({"error": error}, status_code=exc.status_code, headers=exc.headers)
    response = _response(body)
    headers = {k: v for k, v in upstream.headers.items() if k.lower() not in {"content-length", "content-type"}}
    if isinstance(upstream, StreamingResponse):
        return StreamingResponse(_stream(upstream.body_iterator, response), media_type="text/event-stream",
                                 headers=headers, background=upstream.background)
    try:
        data = json.loads(upstream.body)
        if data.get("error"):
            return JSONResponse(data, status_code=502, headers=headers)
        choice = data["choices"][0]
        msg = choice["message"]
        if msg.get("content") or msg.get("refusal"):
            message = _message()
            for key, kind in (("content", "output_text"), ("refusal", "refusal")):
                if msg.get(key):
                    message["content"].append(_part(kind, msg[key]))
            response["output"].append(message)
        for call in msg.get("tool_calls") or []:
            response["output"].append({"id": _id("fc_"), "type": "function_call", "status": "completed",
                                       "call_id": call["id"], **call["function"]})
        response["model"] = data.get("model", body["model"])
        response["usage"] = _usage(data.get("usage") or {})
        _finish(response, choice.get("finish_reason"))
        return JSONResponse(response, headers=headers)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return JSONResponse({"error": {"message": "Invalid chat completion from upstream provider",
                                      "type": "server_error", "param": None, "code": "invalid_upstream_response"}},
                            status_code=502, headers=headers)
