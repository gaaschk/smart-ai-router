# Using it from a client

Three facts decide every client, so they're worth reading once instead of four
times:

**1. The endpoint is `http://your-host:8001/v1`, and it speaks OpenAI.**
`/v1/chat/completions`, `/v1/models`, `/v1/files`. There is no Anthropic
`/v1/messages` here, so a client that natively speaks Anthropic's protocol needs
a translator in front of it — which is what `claudish-smart` is for.

**2. Auth is off until the install has a key.** Once one exists, every client
sends it the same way — `Authorization: Bearer <key>`, whatever the client calls
that field (see [API keys](api.md#api-keys-per-user-auth)). Before that, a client that
insists on a non-empty key field will take `any-value`.

**3. The model name picks a lane, not a model.** The name you send is replaced by
the router's pick, and the only thing it decides is which pool the pick comes
from:

| Send this | Candidates |
|---|---|
| `smart-orchestrator` | Claude only — reliable tool-calling — then routed on the prompt *within* that pool, so a mechanical turn can still land on Haiku |
| `smart-worker` (or anything else) | every model in scope, cheapest that clears the bar |

Those two are exactly what `GET /v1/models` returns. **If the client sends tool
definitions of its own, use `smart-orchestrator`**: a tool call routed to a model
that can't make one comes back as a provider 400, not a graceful degrade. Sending
your own `tools` also keeps the router's [agent mode](api.md#agent-mode--document-creation)
out of your way, which is what you want from a client that runs its own loop.

And one thing to check before fighting a client's settings: **does it call the
endpoint from your machine, or from its vendor's servers?** Locally-run clients
(Claude Code, Codex, aider, Continue, Zed) reach `localhost:8001` fine. A hosted
client (Cursor) calls out from its own backend, so the router has to be publicly
reachable over HTTPS — and your prompts travel through that vendor either way.

## Claude Code

Claude Code speaks Anthropic's protocol; the router speaks OpenAI's. `claudish`
(installed separately) is the translator, and `claudish-smart` — symlinked into
`~/.local/bin` by the setup wizard — wires the two together:

```bash
claudish-smart            # any claudish / Claude Code arguments pass through
```

It health-checks the router first, exports `LITELLM_BASE_URL` and
`LITELLM_API_KEY` so claudish's proxy forwards here, then writes a
project-level `.claude/settings.local.json` naming the routed models — and
restores or removes that file on exit, including on an early one.

| Claude Code slot | Set via | Lane |
|---|---|---|
| main loop | `ANTHROPIC_MODEL` | `ll@smart-orchestrator` — the loop recognizes skills and emits tool calls, so it needs a Claude-compliant model |
| small/fast | `ANTHROPIC_SMALL_FAST_MODEL` | `ll@smart-orchestrator` |
| subagents | `CLAUDE_CODE_SUBAGENT_MODEL` | `ll@smart-worker` — fan-out work, routed to the cheapest capable model |

The `--model-opus/sonnet/haiku/subagent` flags it also passes are a fallback for
any request that still arrives under a Claude name. They can't carry the split on
their own: claudish's proxy reads `modelMap.opus/sonnet/haiku` but never
`modelMap.subagent`, so that mapping was always inert — hence the env vars.

Environment overrides:
- `SMART_ROUTER_URL=http://other-host:8001` — change the router address
- `SMART_ROUTER_API_KEY=<key>` — API key for a router that requires auth (note: **singular**, the client-side variable; the server's admin key list is the plural `SMART_ROUTER_API_KEYS`). Sent as the health-check `Authorization` header and exported to LiteLLM so the proxy authenticates.
- `SMART_ROUTER_OPTIONAL=1` — fall back to plain `claudish` if the router is unreachable

**If it fails with `[Route] No credentialed providers in chain`:** you have a
`modelOverrides` map in `~/.claude/settings.json` rewriting model names (e.g.
`claude-opus-5` → a Bedrock inference-profile ARN). claudish then sees the ARN,
matches no role, and gives up. `claudish-smart` avoids it by naming models
through `ANTHROPIC_MODEL`, which matches no override key —
`CLAUDE_CODE_USE_BEDROCK=0` does *not* help, because it disables the transport
and leaves the rewrite in place, and an empty `"modelOverrides": {}` can't clear
inherited entries either (settings merge key-wise).

## Cursor

Settings → Models → *Override OpenAI Base URL*. Set the base URL to
`https://your-host/v1`, put a router key in the OpenAI API key field, and add
`smart-orchestrator` to the model list by hand.

Cursor sends its requests from Cursor's own servers, not from your machine, so
`localhost:8001` will not work — this is the one client that needs the router
publicly reachable over HTTPS, and it means your repo context travels Cursor →
your host. A custom OpenAI key drives Chat and Cmd-K; Tab and agent mode stay on
Cursor's own models regardless.

## Codex CLI

In `~/.codex/config.toml`:

```toml
model = "smart-orchestrator"
model_provider = "smart-router"

[model_providers.smart-router]
name = "smart-ai-router"
base_url = "http://localhost:8001/v1"
env_key = "SMART_ROUTER_API_KEY"
wire_api = "chat"
```

`wire_api = "chat"` is not optional: Codex prefers OpenAI's Responses API, and
this router implements chat completions only. Codex runs its own tool loop, which
is why the model is the orchestrator lane.

## Anything with an OpenAI base-URL field

Continue, Zed, LibreChat, Open WebUI (Settings → Connections → OpenAI API), and
most of the long tail need the same three values:

| The field | The value |
|---|---|
| Base URL / API base / endpoint | `http://localhost:8001/v1` — if requests 404, try it without the `/v1`; clients disagree about which half they append |
| API key | a router key, or `any-value` on a keyless install |
| Model | `smart-orchestrator`, or `smart-worker` for plain chat |

aider wants the provider prefix, since it names models the LiteLLM way:

```bash
export OPENAI_API_BASE=http://localhost:8001/v1
export OPENAI_API_KEY=any-value
aider --model openai/smart-orchestrator
```

## The OpenAI SDKs

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8001/v1", api_key="any-value")
r = client.chat.completions.create(
    model="smart-worker",
    messages=[{"role": "user", "content": "Fix this Python bug"}],
)
print(r.model)          # the model that actually answered, not what you sent
```

The response body comes back from the provider untouched, so `r.model` is the
real pick. For the routing metadata, read the headers via
`client.chat.completions.with_raw_response.create(...)`.

## Claude Desktop

Not supported, and not for want of a feature here: Claude Desktop has no
base-URL or custom-model setting, so there is nowhere to point it. Its extension
point is MCP, and an MCP server supplies *tools* to the model Anthropic is
running — it can't replace that model, which is the whole job of a router.
(Nothing stops someone exposing the router as an MCP tool so Claude can consult
it as a sub-model; nothing here ships that.)

The nearest things that do work: the router's own web UI at
`http://localhost:8001/` is a full chat client — history, uploads, voice, agent
mode — and `claudish-smart` gives you the Claude-agent experience on the
terminal.

## Checking that it actually routed

```bash
# What names the endpoint will accept
curl -s http://localhost:8001/v1/models

# Which model answered, and why
curl -sD - -o /dev/null -X POST http://localhost:8001/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer any-value' \
  -d '{"model":"smart-worker","messages":[{"role":"user","content":"hi"}]}' \
  | grep -i '^x-'
```

For a client that hides its traffic, the UI's Usage page lists every request with
the model it landed on and what it cost.

