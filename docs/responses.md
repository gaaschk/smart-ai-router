# Responses API

The Responses adapter reuses the existing Chat Completions routing, classifier,
provider integrations, model scopes, cost restrictions, quotas, and accounting.
It does not run a separate router or execute client-provided functions.
Existing Chat Completions and internal filesystem agent behavior remain available.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/responses` | Generate a JSON response or Responses SSE stream |
| GET | `/v1/responses/{id}` | Retrieve a stored response |
| DELETE | `/v1/responses/{id}` | Delete a stored response |
| GET | `/v1/responses/{id}/input_items` | List the reconstructed input history |

All endpoints use the router's API key authentication. Stored objects are
private to the authenticated **user**, including across that user's keys. Admin
keys do not bypass ownership. Missing, deleted, expired, and other users' IDs
all return the same OpenAI-style 404. The existing keyless bootstrap mode has
one shared empty identity; configure API keys before accepting multiple users.

## Requests and external functions

Supported inputs include strings, system/developer/user/assistant messages,
function calls, and function results. Text, image URL/data URI, and uploaded
`input_file`/`file_id` content reuse the existing file/vision handling.

Supported options include instructions, function tools, tool choice
(`auto`, `none`, `required`, or a named function), parallel tool calls,
metadata, temperature/top-p, maximum output tokens, reasoning effort, and
`text.format` (`text`, `json_object`, or `json_schema`). Reasoning effort remains
an optional model preference and uses the shared proxy's capability filtering;
reasoning content is never fabricated. Unsupported fields/content types fail
explicitly.

Function tools are returned as `function_call` output items. The application
executes them, then sends `function_call_output` items with the same `call_id`.
Every pending call needs exactly one result before the next model turn.
Function names and call IDs survive translation, including parallel calls.
The Responses endpoint always disables the router's filesystem execution loop.

Tool requests require tool-capable models. Strict functions also require the existing `structured_outputs` capability.
Omitting `strict` attempts schema normalization into strict mode; incompatible
schemas resolve to `strict=false`, which is reflected in the response tools.
Explicit `strict=true` never silently falls back.
Strict object schemas require `additionalProperties=false` and every property
in `required`. JSON Schema requests similarly route only to models cataloged
with schema support. Required tool choice, named choice, serial tool constraints,
JSON output, and function arguments are checked against actual provider output.
Violations return 502 or a terminal `response.failed`, rather than success with
silently weakened semantics. Refusals and length-limited responses retain their
normal Responses semantics. Reasoning effort is a preference; providers may
reject unsupported optional parameters explicitly.

`jsonschema` is a runtime dependency for validating schemas and provider outputs;
no handwritten JSON Schema validator is used. External schema references are
unsupported. Strict function support can narrow the available provider pool;
Bedrock models currently cataloged without schema support require non-strict
functions or produce an explicit capability error.

## Stored and stateless continuation

`store` defaults to `true`. Responses and their reconstructed input history are
saved in SQLite separately from web UI conversations. Streaming responses become
retrievable as `in_progress`, then update to completed, incomplete, or failed.
Disconnects close the upstream iterator and mark stored work failed. Failed or
in-progress responses cannot be used as parents.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8001/v1", api_key="router-key")
first = client.responses.create(model="smart-worker", input="Remember the number 7.")
second = client.responses.create(
    model="smart-worker",
    previous_response_id=first.id,
    input="What number?",
    instructions="Answer briefly.",
)
print(second.output_text)
```

Continuation appends the parent's input and output to the new input. It does not
inherit the parent's `instructions`, tools, or other generation settings; supply
those on the new request. Instructions in explicit system/developer input
messages remain part of the supplied history. A continuation is a new billable
model request, including its reconstructed input tokens, not a second charge for
the parent's output generation.

Each object has a 30-day lifetime from creation. Expired rows are purged at
startup and during storage access; no background worker is required. Children
retain independent history snapshots: deleting or expiring a parent does not
delete its children. Deletion during a stream prevents finalization from
resurrecting that response. Retention is per object, not a cascading erasure of
all conversations derived from it.

With `store=false`, no Responses object or input snapshot is stored, and its ID
cannot be retrieved or used for continuation. The caller can still use a stored
parent with `store=false` for the new result. Existing usage accounting, configured
request capture, and GBrain policies are separate from Responses object storage.

Input listing accepts `limit` (1–100, default 20), `order` (`asc` or default
`desc`), and either `after` or `before` item ID. Pages contain `data`, `first_id`,
`last_id`, and `has_more`. Histories preserve item identifiers and ordering.

## SDK verification

Install development dependencies with `uv sync --extra dev` or
`pip install -e '.[dev]'`, then run:

```sh
python -m pytest tests/test_api_responses.py tests/test_responses_persistence.py tests/test_responses_sdk.py
```

Tests use the official OpenAI Python SDK and OpenAI Agents SDK against the ASGI
application with strict SDK response validation. They exercise retrieval,
continuation, pagination, deletion, stateless runs, structured output, and the
actual Agents SDK function loop on `smart-worker` and `smart-orchestrator` in
both JSON and streaming modes, with stateless history and stored
`previous_response_id` continuation. The agent executes two parallel calls, continues
with their results, executes a further call, and reaches a final answer using
only `/v1/responses`. Provider IO is mocked; no paid provider traffic occurs in
these tests. Tested SDK versions: OpenAI 3.29.0 and OpenAI Agents 0.23.1.

Usage preserves actual provider token counts when available and retains the
existing estimated-token flag otherwise. Accounting includes the requested
routing lane, selected model, classification, escalation, user, cost, and final
adapter status. Failed validation is recorded once with its billable usage.

## Limits

OpenAI-hosted execution tools (including code interpreter, file search and
computer use), hosted tool search, programmatic tool calling, background jobs,
Realtime, WebSocket Responses, image/audio/video generation, encrypted reasoning
replay, compaction, and the OpenAI Conversations API are not implemented.
Native Responses compatibility for the tested Agents SDK workflows does not
establish full Codex compatibility or live provider reliability. Deployment and
live-provider smoke verification remain separate steps.

Protocol references: [Responses migration](https://developers.openai.com/api/docs/guides/migrate-to-responses),
[conversation state](https://developers.openai.com/api/docs/guides/conversation-state),
[streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events).
