"""Responses API adapter over the shared routing/accounting path."""
from __future__ import annotations

import codecs
from copy import deepcopy
import json
import re
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from jsonschema import Draft202012Validator, ValidationError, SchemaError
from referencing import Registry
from referencing.jsonschema import DRAFT202012
from referencing.exceptions import Unresolvable

from smart_ai_router.api.proxy import chat_completions

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
        elif kind == "refusal" and isinstance(part.get("refusal"), str):
            continue  # Preserved in the assistant's refusal field by _chat_body.
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
    for key in ("background", "include"):
        if body.get(key):
            _invalid(f"{key} is not supported", key)
    if body.get("previous_response_id") is not None and not isinstance(body["previous_response_id"], str):
        _invalid("previous_response_id must be a string", "previous_response_id")
    if body.get("truncation", "disabled") != "disabled":
        _invalid("Only disabled truncation is supported", "truncation")
    if not isinstance(body.get("model"), str) or not body["model"]:
        _invalid("model is required", "model")
    for key in ("stream", "store", "background", "parallel_tool_calls"):
        if key in body and not isinstance(body[key], bool):
            _invalid(f"{key} must be a boolean", key)
    if "metadata" in body and (not isinstance(body["metadata"], dict) or len(body["metadata"]) > 16 or
                               any(not isinstance(k, str) or not isinstance(v, str) or len(k) > 64 or len(v) > 512
                                   for k, v in body["metadata"].items())):
        _invalid("metadata must be an object of strings", "metadata")
    for key in ("user", "prompt_cache_key", "safety_identifier", "service_tier"):
        if key in body and not isinstance(body[key], str):
            _invalid(f"{key} must be a string", key)
    if "service_tier" in body and body["service_tier"] not in {"auto", "default", "flex", "priority"}:
        _invalid("Invalid service_tier", "service_tier")
    if "include" in body and not isinstance(body["include"], list):
        _invalid("include must be an array", "include")
    for key in ("temperature", "top_p"):
        if key in body and (type(body[key]) not in (int, float) or
                            not 0 <= body[key] <= (2 if key == "temperature" else 1)):
            _invalid(f"Invalid {key}", key)
    messages = []
    pending_calls = set()
    seen_calls = set()
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
            if pending_calls:
                _invalid("Function outputs must follow their calls before another message", "input")
            message = {"role": item["role"], "content": _content(item.get("content"))}
            if isinstance(item.get("content"), list):
                refusals = [p["refusal"] for p in item["content"] if p.get("type") == "refusal"]
                if refusals:
                    if item["role"] != "assistant":
                        _invalid("Only assistant messages can contain refusals", "input")
                    message["refusal"] = "".join(refusals)
            messages.append(message)
        elif kind == "function_call":
            if not all(isinstance(item.get(k), str) for k in ("call_id", "name", "arguments")):
                _invalid("Function calls require call_id, name and arguments strings", "input")
            if not item["call_id"] or item["call_id"] in seen_calls:
                _invalid("Function call IDs must be nonempty and unique", "input")
            try:
                if not isinstance(json.loads(item["arguments"]), dict):
                    _invalid("Function arguments must be a JSON object", "input")
            except ValueError:
                _invalid("Function arguments must be valid JSON", "input")
            pending_calls.add(item["call_id"])
            seen_calls.add(item["call_id"])
            call = {"id": item["call_id"], "type": "function", "function": {
                "name": item["name"], "arguments": item["arguments"]}}
            if messages and messages[-1]["role"] == "assistant":
                messages[-1].setdefault("tool_calls", []).append(call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif kind == "function_call_output" and isinstance(item.get("output"), str) and isinstance(item.get("call_id"), str):
            if item["call_id"] not in pending_calls:
                _invalid("Function output has no matching pending call", "input")
            pending_calls.remove(item["call_id"])
            messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]})
        else:
            _invalid(f"Unsupported or invalid input item: {kind}", "input")
    if pending_calls:
        _invalid("Every function call needs an output before continuation", "input")
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
    tools = [dict(tool) if isinstance(tool, dict) else tool for tool in tools]
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("name"), str):
            _invalid("Only named function tools are supported", "tools")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool["name"]):
            _invalid("Invalid function name", "tools")
        if tool.keys() - {"type", "name", "description", "parameters", "strict"}:
            _invalid("Unsupported function tool fields", "tools")
        if "description" in tool and not isinstance(tool["description"], str):
            _invalid("Function description must be a string", "tools")
        if "strict" in tool and tool["strict"] is not None and not isinstance(tool["strict"], bool):
            _invalid("Function strict must be boolean or null", "tools")
        schema = tool.get("parameters") or {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        _schema(schema, "tools.parameters")
        if tool.get("strict") is None:
            normalized = deepcopy(schema)
            for node in _schema_nodes(normalized):
                if "object" in node.get("type", []):
                    node["additionalProperties"] = False
                    node["required"] = list(node.get("properties", {}))
            try:
                _schema(normalized, "tools.parameters", strict=True)
            except HTTPException:
                tool["strict"] = False
            else:
                schema, tool["strict"] = normalized, True
        _schema(schema, "tools.parameters", strict=tool.get("strict") is True)
        tool["parameters"] = schema
    if len({t["name"] for t in tools}) != len(tools):
        _invalid("Function names must be unique", "tools")
    if tools:
        chat["tools"] = [{"type": "function", "function": {
            **{k: v for k, v in t.items() if k != "type"},
            "strict": t.get("strict") is not False,
            "parameters": t.get("parameters") or {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        }} for t in tools]
    choice = body.get("tool_choice")
    if choice is not None:
        if isinstance(choice, dict) and choice.get("type") == "function" and isinstance(choice.get("name"), str):
            choice = {"type": "function", "function": {"name": choice["name"]}}
        elif choice not in ("auto", "none", "required"):
            _invalid("Unsupported tool_choice", "tool_choice")
        if choice == "required" and not tools:
            _invalid("required tool_choice needs tools", "tool_choice")
        if isinstance(choice, dict) and choice["function"]["name"] not in {t["name"] for t in tools}:
            _invalid("Selected function must be in tools", "tool_choice")
        chat["tool_choice"] = choice
    if "text" in body:
        text = body["text"]
        if not isinstance(text, dict) or text.keys() - {"format"}:
            _invalid("Only text.format is supported", "text")
        fmt = text.get("format", {"type": "text"})
        if not isinstance(fmt, dict) or fmt.get("type") not in {"text", "json_object", "json_schema"}:
            _invalid("Invalid text.format", "text.format")
        allowed = {"type", "name", "strict", "schema", "description"} if fmt["type"] == "json_schema" else {"type"}
        if fmt.keys() - allowed:
            _invalid("Unsupported format fields", "text.format")
        if fmt["type"] == "json_schema":
            if not isinstance(fmt.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", fmt["name"]):
                _invalid("json_schema requires a valid name", "text.format.name")
            if "strict" in fmt and fmt["strict"] is not None and not isinstance(fmt["strict"], bool):
                _invalid("strict must be boolean or null", "text.format.strict")
            _schema(fmt.get("schema"), "text.format.schema", strict=fmt.get("strict") is True)
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
            "model": body["model"], "output": [], "usage": None, "store": body.get("store", True),
            "previous_response_id": body.get("previous_response_id"),
            "temperature": body.get("temperature", 1), "top_p": body.get("top_p", 1),
            "max_output_tokens": body.get("max_output_tokens"),
            "text": body.get("text", {"format": {"type": "text"}}),
            "reasoning": body.get("reasoning", {"effort": None, "summary": None}),
            "truncation": "disabled", "background": False,
            "instructions": body.get("instructions"), "metadata": body.get("metadata", {}),
            "parallel_tool_calls": body.get("parallel_tool_calls", True),
            "tools": body.get("tools", []), "tool_choice": body.get("tool_choice", "auto")}


def _usage(usage):
    return {"input_tokens": usage.get("prompt_tokens", 0),
            "input_tokens_details": {"cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                                     "cache_write_tokens": (usage.get("prompt_tokens_details") or {}).get("cache_write_tokens", 0)},
            "output_tokens": usage.get("completion_tokens", 0),
            "output_tokens_details": {"reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)},
            "total_tokens": usage.get("total_tokens", usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))}


def _finish(response, reason):
    response["status"] = "incomplete" if reason in {"length", "content_filter"} else "completed"
    if response["status"] == "incomplete":
        response["incomplete_details"] = {"reason": "max_output_tokens" if reason == "length" else reason}
    for item in response["output"]:
        item["status"] = "incomplete" if response["status"] == "incomplete" else "completed"


def _message():
    return {"id": _id("msg_"), "type": "message", "role": "assistant", "status": "in_progress", "content": []}


def _part(kind, value=""):
    if not isinstance(value, str):
        raise ValueError("Provider content must be a string")
    if kind == "refusal":
        return {"type": kind, "refusal": value}
    return {"type": "output_text", "text": value, "annotations": []}


async def _stream(source, response, *, validate=None, save=None, sanitize=str):
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
            while not ended and "\n\n" in buffer:
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
                                    content_index=message["content"].index(part), delta=value,
                                    **({"logprobs": []} if kind == "output_text" else {}))
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
        decoder.decode(b"", final=True)
        if finish is None:
            raise ValueError("Upstream stream ended without a finish reason")
        if any(not c["call_id"] or not c["name"] or
               not any(i is c for i in response["output"]) for c in calls.values()):
            raise ValueError("Upstream returned an incomplete function call")
        _finish(response, finish)
        if validate:
            validate(response)
        if save:
            save(response)
        for index, item in enumerate(response["output"]):
            if item["type"] == "message":
                for content_index, part in enumerate(item["content"]):
                    kind = part["type"]
                    field = "refusal" if kind == "refusal" else "text"
                    yield event(f"response.{kind}.done", item_id=item["id"], output_index=index,
                                content_index=content_index, **{field: part[field]},
                                **({"logprobs": []} if kind == "output_text" else {}))
                    yield event("response.content_part.done", item_id=item["id"], output_index=index,
                                content_index=content_index, part=part)
            else:
                yield event("response.function_call_arguments.done", item_id=item["id"], output_index=index,
                            arguments=item["arguments"])
            yield event("response.output_item.done", output_index=index, item=item)
        yield event(f"response.{response['status']}", response=response)
    except Exception as exc:
        response.update(status="failed", error={"code": "server_error", "message": sanitize(str(exc))})
        if save:
            try:
                save(response)
            except Exception:
                pass  # The failure event still reaches the client when storage is unavailable.
        yield event("response.failed", response=response)
    finally:
        try:
            if hasattr(source, "aclose"):
                await source.aclose()
        finally:
            if response["status"] == "in_progress" and save:
                response.update(status="failed", error={"code": "server_error", "message": "Client disconnected"})
                save(response)


def _schema_nodes(node):
    if not isinstance(node, dict):
        return
    yield node
    for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
        for child in node.get(key, {}).values():
            yield from _schema_nodes(child)
    for key in ("items", "additionalProperties", "contains", "not", "if", "then", "else",
                "propertyNames", "unevaluatedProperties", "unevaluatedItems"):
        yield from _schema_nodes(node.get(key))
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        for child in node.get(key, []):
            yield from _schema_nodes(child)


def _schema(schema, param, *, strict=False):
    if not isinstance(schema, dict) or schema.get("type") != "object":
        _invalid("Schema must describe an object", param)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        _invalid("Invalid JSON Schema", param)
    resolver = Registry().with_resource("urn:responses:schema", DRAFT202012.create_resource(schema)).resolver("urn:responses:schema")
    for node in _schema_nodes(schema):
        ref = node.get("$ref")
        if ref is not None:
            if not ref.startswith("#"):
                _invalid("External schema references are unsupported", param)
            try:
                resolver.lookup(ref)
            except Unresolvable:
                _invalid("Unresolved schema reference", param)
        if strict:
            if node.keys() & {"allOf", "not", "dependentRequired", "dependentSchemas", "if", "then", "else"}:
                _invalid("Unsupported strict schema keyword", param)
            if "object" in node.get("type", []):
                if node.get("additionalProperties") is not False or set(node.get("required", [])) != set(node.get("properties", {})):
                    _invalid("Strict objects require additionalProperties=false and every property required", param)


def _input_items(value):
    if isinstance(value, str):
        value = [{"type": "message", "role": "user", "content": value}]
    if not isinstance(value, list) or not value:
        _invalid("input must be a string or nonempty array", "input")
    result = []
    for raw in value:
        if not isinstance(raw, dict):
            _invalid("Input items must be objects", "input")
        item = dict(raw)
        item.setdefault("type", "message")
        item.setdefault("id", _id("msg_" if item["type"] == "message" else "item_"))
        if not isinstance(item["id"], str) or not item["id"]:
            _invalid("Item IDs must be nonempty strings", "input")
        if item["type"] == "message":
            if isinstance(item.get("content"), str):
                item["content"] = [{"type": "input_text", "text": item["content"]}]
            item.setdefault("status", "completed")
        result.append(item)
    if len({i["id"] for i in result}) != len(result):
        _invalid("Input item IDs must be unique", "input")
    return result


def _validate_output(response, body):
    """Treat a provider ignoring required semantics as a failure, never success."""
    if response["status"] != "completed":
        return
    tools = {t["name"]: t for t in body.get("tools", [])}
    calls = [i for i in response["output"] if i["type"] == "function_call"]
    choice = body.get("tool_choice", "auto")
    if choice == "none" and calls:
        raise ValueError("Provider ignored tool_choice=none")
    if (choice == "required" or isinstance(choice, dict)) and not calls:
        raise ValueError("Provider did not return the required function call")
    if body.get("parallel_tool_calls") is False and len(calls) > 1:
        raise ValueError("Provider ignored parallel_tool_calls=false")
    if len({c["call_id"] for c in calls}) != len(calls):
        raise ValueError("Provider returned duplicate function call IDs")
    for call in calls:
        if not call["call_id"] or call["name"] not in tools:
            raise ValueError("Provider returned an unknown function")
        if isinstance(choice, dict) and call["name"] != choice["name"]:
            raise ValueError("Provider ignored named tool_choice")
        arguments = json.loads(call["arguments"])
        if not isinstance(arguments, dict):
            raise ValueError("Function arguments must be a JSON object")
        tool = tools[call["name"]]
        if tool.get("strict") is not False and tool.get("parameters"):
            Draft202012Validator(tool["parameters"]).validate(arguments)
    fmt = body.get("text", {}).get("format", {})
    if not calls and fmt.get("type") in {"json_schema", "json_object"}:
        parts = [p for i in response["output"] if i["type"] == "message" for p in i["content"]]
        if any(p["type"] == "refusal" for p in parts):
            return
        value = json.loads("".join(p["text"] for p in parts if p["type"] == "output_text"))
        if fmt["type"] == "json_schema":
            Draft202012Validator(fmt["schema"]).validate(value)
        elif not isinstance(value, dict):
            raise ValueError("Provider returned a non-object JSON value")


def _finalize_usage(request, status):
    callback = getattr(request.state, "responses_usage", None)
    if callback is not None:
        request.state.responses_usage = None
        request.state.responses_defer_usage = False
        callback(status=status)


def _safe_message(request, message):
    for provider in request.app.state.capability_router.all_providers():
        if provider.api_key:
            message = message.replace(provider.api_key, "[redacted]")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer ") and auth[7:]:
        message = message.replace(auth[7:], "[redacted]")
    message = re.sub(r"(?i)bearer\s+[^\s\"']+", "Bearer [redacted]", message)
    return re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", message)


def error_response(request, exc):
    error = dict(exc.detail) if isinstance(exc.detail, dict) else {
        "message": str(exc.detail), "type": "invalid_request_error" if exc.status_code < 500 else "server_error",
        "param": None, "code": None}
    if exc.status_code == 429:
        error["type"] = "rate_limit_error"
    error["message"] = _safe_message(request, error.get("message", "Request failed"))
    return JSONResponse({"error": error}, status_code=exc.status_code, headers=exc.headers)


def _stored(request, response_id):
    record = request.app.state.capability_router.get_response(
        response_id, getattr(request.state, "user", "") or "")
    if record is None:
        raise HTTPException(404, detail={"message": "Response not found", "type": "invalid_request_error",
                                        "param": "response_id", "code": "response_not_found"})
    return record


@responses_router.get("/v1/responses/{response_id}")
def retrieve_response(response_id: str, request: Request):
    return _stored(request, response_id)["response"]


@responses_router.delete("/v1/responses/{response_id}")
def delete_response(response_id: str, request: Request):
    deleted = request.app.state.capability_router.delete_response(
        response_id, getattr(request.state, "user", "") or "")
    if not deleted:
        _stored(request, response_id)  # Uniform 404 for missing, expired, or another owner's ID.
    return {"id": response_id, "object": "response.deleted", "deleted": True}


@responses_router.get("/v1/responses/{response_id}/input_items")
def list_input_items(response_id: str, request: Request, limit: int = 20,
                     order: str = "desc", after: str | None = None, before: str | None = None):
    if not 1 <= limit <= 100 or order not in {"asc", "desc"} or (after and before):
        _invalid("Invalid pagination parameters")
    items = _stored(request, response_id)["input"]
    items = list(reversed(items)) if order == "desc" else items
    cursor = after or before
    if cursor:
        index = next((i for i, item in enumerate(items) if item["id"] == cursor), None)
        if index is None:
            _invalid("Unknown input item cursor", "after" if after else "before")
        items = items[index + 1:] if after else items[:index]
    data = items[:limit]
    return {"object": "list", "data": data, "first_id": data[0]["id"] if data else None,
            "last_id": data[-1]["id"] if data else None, "has_more": len(items) > limit}


@responses_router.post("/v1/responses")
async def create_response(request: Request):
    try:
        try:
            body = await request.json()
        except ValueError:
            _invalid("Invalid JSON body")
        if not isinstance(body, dict):
            _invalid("Request body must be an object")
        original = body
        # Validate new input before expanding it with stored history.
        items = _input_items(body.get("input"))
        cr = request.app.state.capability_router
        owner = getattr(request.state, "user", "") or ""
        previous_id = body.get("previous_response_id")
        if previous_id is not None:
            if not isinstance(previous_id, str) or not previous_id:
                _invalid("previous_response_id must be a nonempty string", "previous_response_id")
            previous = _stored(request, previous_id)
            if previous["response"]["status"] not in {"completed", "incomplete"}:
                _invalid("Previous response must have finished successfully", "previous_response_id")
            items = previous["input"] + previous["response"]["output"] + items
            if len({item["id"] for item in items}) != len(items):
                _invalid("Continuation repeats an input item ID", "input")
        body = {**body, "input": items}
        chat = _chat_body(body)
        resolved_tools = [{"type": "function", **t["function"]} for t in chat.get("tools", [])]
        body = {**body, "tools": resolved_tools}
        original = {**original, "tools": resolved_tools}
        request.state.responses_api = True
        request.state.responses_defer_usage = True
        raw = json.dumps(chat).encode()

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        forwarded = Request(request.scope, receive)
        upstream = await chat_completions(forwarded)
    except HTTPException as exc:
        return error_response(request, exc)
    response = _response(original)
    response["model"] = getattr(request.state, "routed_model", original["model"])
    headers = {k: v for k, v in upstream.headers.items() if k.lower() not in {"content-length", "content-type"}}
    def save(result, *, create=False):
        result["model"] = getattr(request.state, "routed_model", result["model"])
        if response["store"]:
            cr.save_response(owner, result, items, create=create)
        if not create:
            status = 200 if result["status"] in {"completed", "incomplete"} else 502
            if (result.get("error") or {}).get("message") == "Client disconnected":
                status = 499
            _finalize_usage(request, status)

    if isinstance(upstream, StreamingResponse):
        save(response, create=True)
        return StreamingResponse(_stream(upstream.body_iterator, response,
                                 validate=lambda r: _validate_output(r, body), save=save,
                                 sanitize=lambda message: _safe_message(request, message)), media_type="text/event-stream",
                                 headers=headers, background=upstream.background)
    try:
        data = json.loads(upstream.body)
        if data.get("error"):
            raise ValueError("Provider returned an error instead of a completion")
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
        response["model"] = data.get("model", response["model"])
        response["usage"] = _usage(data.get("usage") or {})
        if choice.get("finish_reason") not in {"stop", "tool_calls", "length", "content_filter", "function_call"}:
            raise ValueError("Missing or invalid finish reason")
        _finish(response, choice["finish_reason"])
        _validate_output(response, body)
        save(response, create=True)
        _finalize_usage(request, 200)
        return JSONResponse(response, headers=headers)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, ValidationError, Unresolvable):
        return JSONResponse({"error": {"message": "Invalid chat completion from upstream provider",
                                      "type": "server_error", "param": None, "code": "invalid_upstream_response"}},
                            status_code=502, headers=headers)
    finally:
        _finalize_usage(request, 502)
