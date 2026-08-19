# smart-ai-router

A vendor-agnostic LLM capability router that classifies each prompt and routes it to the cheapest model that clears the quality bar. Sits in front of OpenRouter, Ollama, and AWS Bedrock as a single OpenAI-compatible endpoint.

## What it does

1. **Classifies** each incoming prompt by domain (`coding`, `docs`, `reasoning`, `general`) and complexity (`trivial`, `moderate`, `hard`).
2. **Routes** to the cheapest model whose competence score clears the threshold for that complexity tier — filtering by tool-calling support, vision, context length, and reliability.
3. **Falls back** to the highest-competence model (typically Claude via Bedrock) only when no cheaper model qualifies — and surfaces an escalation notice when it does.
4. **Streams** responses back in real-time via Server-Sent Events, with an immediate keepalive so the client knows the connection is alive while waiting for the provider's first token.

The model matrix is populated by syncing live catalogs from your configured providers (OpenRouter, Ollama, Bedrock). Competence scores are inferred from model name patterns using benchmark-informed priors, so newly-released models get reasonable defaults without manual curation.

Beyond the routing proxy, the built-in web UI at `http://localhost:8001/` is a full chat client:

- **Chat** with persistent, server-side conversation history — every message shows which model it was routed to and why.
- **File uploads** — PDF, Word, PowerPoint, Excel, and text/code files are extracted to text and fed to the model as context; images are inlined for vision-capable models.
- **Agent mode** — a tool-capable model can read, write, and edit files in your private, path-jailed workspace, and **create downloadable documents** (PDF, Word, PowerPoint, Excel, Markdown). Auto-enables when your request needs a file; can be forced on or off.
- **Per-user API keys** — mint, scope, rate-limit, revoke, and rotate keys from the Keys page; a signed-in badge shows which identity you're using.

## Quick start

```bash
# Clone and install
git clone https://github.com/gaaschk/smart-ai-router.git
cd smart-ai-router
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Run the setup wizard
smart-ai-router setup
```

The setup wizard will:
- Ask for your provider credentials (OpenRouter API key, Ollama URL, and/or AWS Bedrock)
- Save them to the local SQLite store
- Run an initial model sync
- Install a macOS LaunchAgent so the router starts at login (no sudo needed)
- Symlink `claudish-smart` to `~/.local/bin`

After setup, the router is available at `http://localhost:8001`.

## Using with Claude Code (claudish)

`claudish-smart` wraps Claude Code so every request routes through the smart-ai-router:

```bash
claudish-smart
```

Under the hood it sets `LITELLM_BASE_URL` to point at the router and configures Claude Code's model slots:

| Slot | Routed via | Purpose |
|------|-----------|---------|
| `--model-opus` | `smart-orchestrator` | Forces a capable Claude model for the main loop (skill/workflow/tool-calling) |
| `--model-sonnet` | `smart-orchestrator` | Same — orchestration needs Claude compliance |
| `--model-haiku` | `smart-orchestrator` | Same |
| `--model-subagent` | `smart-worker` | Classified + routed to cheapest capable model; Claude fallback only when needed |

Environment overrides:
- `SMART_ROUTER_URL=http://other-host:8001` — change the router address
- `SMART_ROUTER_API_KEY=<key>` — API key for a router that requires auth (note: **singular**, the client-side variable; the server's admin key list is the plural `SMART_ROUTER_API_KEYS`). Sent as the health-check `Authorization` header and exported to LiteLLM so the proxy authenticates.
- `SMART_ROUTER_OPTIONAL=1` — fall back to plain `claudish` if the router is unreachable

## Architecture

```
claudish-smart
    │
    │  POST /v1/chat/completions
    ▼
┌──────────────────────────────────────┐
│          smart-ai-router             │
│                                      │
│  ┌───────────┐   ┌───────────────┐  │
│  │ Classifier│──▶│   Router      │  │
│  │           │   │               │  │
│  │ domain +  │   │ cheapest      │  │
│  │ complexity│   │ model that    │  │
│  └───────────┘   │ clears the    │  │
│                   │ quality bar   │  │
│                   └───────┬───────┘  │
│                           │          │
│                   ┌───────▼───────┐  │
│                   │  Provider     │  │
│                   │  Proxy        │  │
│                   └───────┬───────┘  │
└───────────────────────────┼──────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
         OpenRouter      Ollama       Bedrock
         (cloud)         (local)      (AWS)
```

### Key modules

| File | Purpose |
|------|---------|
| `smart_ai_router/classifier.py` | Keyword-based domain/complexity classification |
| `smart_ai_router/router.py` | Core routing: filter eligible models, pick cheapest above competence bar |
| `smart_ai_router/competence.py` | Infer competence scores from model name patterns |
| `smart_ai_router/sync.py` | Fetch live model catalogs from providers |
| `smart_ai_router/api/proxy.py` | OpenAI-compatible streaming proxy with classification + routing |
| `smart_ai_router/facade.py` | `CapabilityRouter` — main facade wiring everything together |
| `smart_ai_router/store/sqlite_store.py` | SQLite persistence for models + provider configs |
| `smart_ai_router/setup.py` | First-run setup wizard |
| `smart_ai_router/updates.py` | Self-update: git fetch/merge + launchd restart |
| `smart_ai_router/apikeys.py` | Per-user API key minting + hashing |
| `smart_ai_router/scope.py` | Per-user model scope (allow/deny + cost-tier ceiling) |
| `smart_ai_router/ratelimit.py` | Per-user request/token quotas from the usage log |
| `smart_ai_router/keys_cli.py` | `smart-ai-router keys` command-line key management |
| `smart_ai_router/extract.py` | Extract text from uploaded PDF/Word/PowerPoint/Excel/text files |
| `smart_ai_router/docgen.py` | Render Markdown-ish text into PDF/Word/PowerPoint/Excel/Markdown documents |
| `smart_ai_router/files.py` | Filesystem-backed blob storage for uploads (metadata in SQLite) |
| `smart_ai_router/tools.py` | Agent filesystem tools (read/write/edit/list, create_document, run_bash) against a per-user workspace |
| `smart_ai_router/api/files_routes.py` | OpenAI-compatible Files API (`/v1/files`) |
| `smart_ai_router/api/conversations_routes.py` | Chat history API (`/api/conversations`) |

## API

The router exposes a REST API at `http://localhost:8001/api`:

### Routing

```bash
# Classify + route (returns the chosen model)
curl -X POST http://localhost:8001/api/route \
  -H 'Content-Type: application/json' \
  -d '{"domain":"coding","complexity":"moderate","needs_tools":true}'
```

### OpenAI-compatible proxy

```bash
# Full chat completions proxy — classifies the prompt, routes, and forwards
curl -X POST http://localhost:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer any-value' \
  -d '{
    "model": "smart-worker",
    "messages": [{"role":"user","content":"Fix this Python bug"}],
    "stream": true
  }'
```

The `model` field controls routing behavior:
- `smart-orchestrator` — forces a Claude model (for reliable tool-calling)
- `smart-worker` or anything else — classifies the prompt and routes to the cheapest capable model

Response headers include routing metadata:
- `X-Routed-Model` — the actual model used
- `X-Domain` — classified domain
- `X-Complexity` — classified complexity
- `X-Escalated` — `true` if the task was escalated to a premium model
- `X-User` — the authenticated user the request was attributed to (empty in open/no-auth mode)

### API keys (per-user auth)

Authentication is optional until at least one key exists. There are two kinds of key:

- **Admin keys** — set via the `SMART_ROUTER_API_KEYS` env var (comma-separated). They authenticate with full, unrestricted access and are the only keys allowed to manage other keys. Use one to bootstrap.
- **Per-user keys** — minted through the API and stored (hashed) in SQLite. Each carries a `user` identity, so requests can be attributed in the usage log, and each can be revoked or rotated independently without touching anyone else's key or redeploying.

The wire protocol is unchanged: every client still sends `Authorization: Bearer <key>`, so `claudish-smart` and any OpenAI-compatible client work as-is.

```bash
# Mint a per-user key (admin only). The plaintext key is returned ONCE —
# only its SHA-256 hash is stored, so save it now; it can never be re-shown.
curl -X POST http://localhost:8001/api/keys \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"user":"alice"}'
# → {"user":"alice","key_prefix":"sk-smart-a1b2c3","key":"sk-smart-…","enabled":true, ...}

# List keys (metadata only — never the secret)
curl http://localhost:8001/api/keys -H "Authorization: Bearer $ADMIN_KEY"

# Revoke (disable) a key by its prefix — takes effect immediately, no redeploy
curl -X PUT http://localhost:8001/api/keys/sk-smart-a1b2c3/enabled \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' -d '{"enabled":false}'

# Rotate (recreate) a key — mints a new secret in place, keeping the user,
# scope, and limits; the old secret stops working immediately. Returns the
# new plaintext once, just like minting.
curl -X POST http://localhost:8001/api/keys/sk-smart-a1b2c3/recreate \
  -H "Authorization: Bearer $ADMIN_KEY"

# Delete a key
curl -X DELETE http://localhost:8001/api/keys/sk-smart-a1b2c3 \
  -H "Authorization: Bearer $ADMIN_KEY"

# Who am I? — report the identity the current key authenticates as
curl http://localhost:8001/api/whoami -H "Authorization: Bearer $SOME_KEY"
# → {"authenticated":true,"kind":"user","user":"alice","key_prefix":"sk-smart-a1b2c3","is_admin":false}
# kind is "admin" (env key), "user" (per-user key), or "open" (no-auth mode)
```

The proxy adds an `X-User` response header identifying the authenticated user, and records each request (user, routed model, token counts, estimated cost) to a `usage_log` table for attribution.

#### Per-user scope and quotas

A per-user key can be constrained on three axes, all optional and all set at mint time (or via the API):

- **`scope_models`** — a JSON allow/deny list of case-insensitive substrings matched against a model's value and provider. `allow` is a whitelist (empty = all); `deny` overrides. Enforced inside routing, so a scoped user gets the best model *within scope* — never one outside it (the fallback pick respects scope too). Orchestrator mode returns `403` if the forced Claude model is out of scope.
- **`max_tier`** — a cost-tier ceiling; models above it are out of scope (`0` = no ceiling).
- **`rl_window_s` + `rl_max_req` / `rl_max_tokens`** — a rolling-window request and/or token quota, counted from the usage log. Over-quota requests get `429` with a `Retry-After` header, before any routing or forwarding.

```bash
# A key that may only use local Ollama models, capped at 100 requests/hour
curl -X POST http://localhost:8001/api/keys \
  -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"user":"alice",
       "scope_models":"{\"allow\":[\"ollama/\"]}",
       "rl_window_s":3600,"rl_max_req":100}'
```

Admin (env) keys are always unscoped and unlimited.

#### Managing keys

Three ways, all equivalent (they share the SQLite store):

- **Web UI** — the **Keys** page at `http://localhost:8001/` (enter your admin key at the prompt to authenticate management calls).
- **REST API** — the `/api/keys` endpoints above.
- **CLI** — on the host machine, operating directly on the local store (no HTTP/auth needed):

```bash
smart-ai-router keys list
smart-ai-router keys add alice --scope '{"allow":["ollama/"]}' --window-s 3600 --max-req 100
smart-ai-router keys disable sk-smart-a1b2c3      # revoke (reversible)
smart-ai-router keys enable  sk-smart-a1b2c3      # re-enable a disabled key
smart-ai-router keys delete  sk-smart-a1b2c3      # permanent
```

Rotating a key (recreate) is available through the REST API and web UI; the CLI covers add/disable/enable/delete.

### Provider management

```bash
# List providers
curl http://localhost:8001/api/providers

# Add/update a provider
curl -X PUT http://localhost:8001/api/providers/openrouter \
  -H 'Content-Type: application/json' \
  -d '{"name":"openrouter","kind":"openrouter","enabled":true,"api_key":"sk-or-..."}'

# Trigger a model sync
# Response counts models added / updated (only those that actually changed) /
# unchanged / removed. Models absent from a provider's fresh catalog are deleted.
curl -X POST http://localhost:8001/api/sync -H 'Content-Type: application/json' -d '{}'
```

### Models

```bash
# List all synced models
curl http://localhost:8001/api/models

# Get a specific model
curl http://localhost:8001/api/models/openrouter/anthropic/claude-sonnet-4-6
```

### File uploads

An OpenAI-compatible Files API stores uploads on disk (metadata in SQLite) and scopes each file to the uploading identity — admin sees all files, a per-user key sees only its own.

```bash
# Upload a file (multipart). Returns an OpenAI-shaped file object.
curl -X POST http://localhost:8001/v1/files \
  -H "Authorization: Bearer $KEY" \
  -F 'file=@report.pdf' -F 'purpose=assistants'
# → {"id":"file-…","object":"file","bytes":12345,"filename":"report.pdf", ...}

curl http://localhost:8001/v1/files                    -H "Authorization: Bearer $KEY"  # list
curl http://localhost:8001/v1/files/file-XXXX          -H "Authorization: Bearer $KEY"  # metadata
curl http://localhost:8001/v1/files/file-XXXX/content  -H "Authorization: Bearer $KEY"  # download bytes
curl -X DELETE http://localhost:8001/v1/files/file-XXXX -H "Authorization: Bearer $KEY" # delete
```

Extractable-to-text types: **PDF**, **Word (.docx)**, **PowerPoint (.pptx)**, **Excel (.xlsx)**, and plain-text/code files (`text/*`, JSON, XML, YAML, TOML, JS, shell, Python). Legacy `.doc`/`.ppt`/`.xls` are not supported — save as the modern OpenXML format. Images aren't extracted here; they're inlined as base64 for vision-capable models at request time. Uploads over the size ceiling return `413`; unsupported types return `415`. See `SMART_ROUTER_MAX_FILE_MB` and `SMART_ROUTER_FILES_DIR` below.

### Chat history (conversations)

Server-side conversation storage backs the web UI's chat, scoped per identity.

```bash
curl http://localhost:8001/api/conversations                       -H "Authorization: Bearer $KEY"  # list
curl -X POST http://localhost:8001/api/conversations \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"title":"Linezolid dosing"}'                                                                  # create
curl http://localhost:8001/api/conversations/CID                   -H "Authorization: Bearer $KEY"  # get (with messages)
curl -X PATCH  http://localhost:8001/api/conversations/CID \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"title":"New name"}'      # rename
curl -X DELETE http://localhost:8001/api/conversations/CID         -H "Authorization: Bearer $KEY"  # delete
curl -X POST http://localhost:8001/api/conversations/CID/messages \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"role":"user","content":"Hi"}'                                                                # append a message
```

### Agent mode & document creation

When agent mode is on (see the Configuration section), a tool-capable model can operate on the caller's private workspace via these tools: `list_dir`, `read_file`, `write_file`, `edit_file`, `create_document`, and (opt-in) `run_bash`. `create_document` renders a small Markdown subset (headings, bullets, pipe tables, bold) into **PDF**, **Word (.docx)**, **PowerPoint (.pptx)**, **Excel (.xlsx)**, or **Markdown/plain-text**, then registers it as a downloadable file via the Files API above.

### Self-update

```bash
# Check for source updates
curl http://localhost:8001/api/updates

# Apply update (git pull + restart)
curl -X POST http://localhost:8001/api/updates/apply
```

## How routing decisions work

Routing matches a **prompt profile** against a **model profile**, over the same vocabulary of 16 fields. A single "how hard is this?" number can't express the thing that matters most — a cheap coding specialist and a frontier generalist can both score 0.9 on *something*, and the cheap one wins on price even when the prompt needs the other one's strength. So both sides are scored per field, and a model must clear the bar on **every** field the prompt reaches into.

### The prompt profile

Each prompt is described as:

- **domains** — up to 3 `(field, depth)` pairs. Fields are a closed set (`software_engineering`, `law_regulatory`, `medicine_health`, `math_formal`, `natural_science`, `finance_business`, `creative_writing`, … with `general_knowledge` as the residual).
- **depth** — how far into that field the answer must go, as one of four described tiers rather than a float, because a 3B classifier picks reliably between four tiers and cannot calibrate a continuous score:

  | Depth | Required score | Roughly |
  |-------|---------------|---------|
  | `surface` | 0.45 | any model that can hold a conversation |
  | `practitioner` | 0.68 | excludes the weakest models |
  | `specialist` | 0.85 | Sonnet / GPT-4o class and up |
  | `frontier` | 0.93 | Opus / Fable class only |

- **demands** — properties of the task that raise every bar: `factual_precision` (+0.05 — the hallucination axis: must name real statutes, standards, APIs, citations), `quantitative` (+0.03), `long_synthesis` (+0.03), `agentic` (+0.02).
- **stakes** — consequence of being wrong: `low` / `medium` (+0.02) / `high` (+0.05).

Two or more fields at specialist depth or deeper adds a further +0.04, because holding two specialist frames at once is where generalists start producing plausible nonsense. The sum of all bumps is capped at +0.08, and no requirement exceeds 0.97 — without those caps an ordinary high-stakes prompt would demand the priciest tier and undo the router's reason to exist.

### Two-speed classification

The local classifier (a small Ollama model) profiles every prompt. When it reports **high stakes**, **two or more specialist-depth fields**, or **frontier depth** — the judgments a 3B model gets wrong expensively — a second pass on a stronger model refines the profile before routing. That call fires only on prompts already headed for a costly model, and lowering the bar is as valid a correction as raising it. Set `SMART_ROUTER_CLASSIFIER_REFINE_MODEL` (or the **Classifier** group in Settings) to empty to disable it. If no LLM classifier is reachable, a keyword profiler runs instead.

### Model profiles

Model scores are inferred during sync from provider catalog metadata: Artificial Analysis benchmark indices (`intelligence_index`, plus `coding_index` and `agentic_index` as independent evidence for their fields), whether the model supports reasoning, and the vendor description. Narrow models — coders, roleplay models, math models — take a **specialist discount** on professional fields they don't advertise, which is what stops a cheap coding model from being the answer to a legal question. The legacy `coding`/`docs`/`reasoning`/`general` competence vector is derived from the profile, never tracked separately, so the two can't disagree.

### Selection

1. **Filter** by hard constraints: tool-calling support, vision, context length, minimum reliability, key scope, denylists.
2. **Qualify** — keep models whose score clears the requirement for *every* field named in the profile.
3. **Sort** qualifying models by cost tier (ascending), then by their weakest required-field score (descending) as tiebreak, and take the cheapest.

If **nothing** qualifies, the pick is the *closest miss* — ranked by how far short it falls, with cost only as a tiebreak — and the response says so: `X-Qualified: false`, a `⚠ under-qualified` chip in the chat UI, and a caveat prepended to the answer telling the caller to treat specifics (citations, standards, figures) as unverified. This is the case the old single-bar router could not even detect; it returned a confident answer from an unqualified model with no indication anything was wrong.

Every response carries `X-Prompt-Profile` (the profile in words), `X-Routing-Why` (the binding constraint), `X-Qualified`, and the legacy `X-Domain` / `X-Complexity` derived from the profile.

### Cost tiers

Models are assigned cost tiers during sync based on their per-million-token pricing:

| Tier | Input cost ($/M tokens) | Examples |
|------|------------------------|----------|
| 0 | Unknown | |
| 1 | Free or < $0.10 | Free-tier models, tiny local models |
| 2 | $0.10–$0.50 | Haiku-class |
| 3 | $0.50–$1.00 | |
| 5 | $1.00–$3.00 | Sonnet-class |
| 8 | $3.00–$8.00 | GPT-4o, mid-tier |
| 12 | $8.00–$15.00 | Opus-class |
| 15 | > $15.00 | Premium reasoning models |

Local Ollama models always have cost tier 0.

## Configuration

All configuration is stored in `~/.smart_ai_router.db` (SQLite). You can manage it via:
- The setup wizard: `smart-ai-router setup`
- The REST API: `PUT /api/providers/{name}`
- The web UI at `http://localhost:8001/`

### Environment variables

Most application *behavior* (the ⚙ marked rows below) is now managed from the
**Settings** page in the web UI — persisted in the database, applied live with no
restart. For those, the environment variable is only a fallback: the effective
value is **DB (set in the UI) → environment variable → built-in default**. The
unmarked rows are intrinsic to a machine/deployment (port, paths, the bootstrap
admin secret) and stay environment-only.

| Variable | Default | Purpose |
|----------|---------|---------|
| `SMART_ROUTER_PORT` | `8001` | Port the server listens on |
| `SMART_ROUTER_LABEL` | `com.smart-ai-router` | launchd service label |
| `SMART_ROUTER_URL` | `http://$(hostname):8001` | Used by `claudish-smart` to find the router |
| `SMART_ROUTER_API_KEYS` | *(empty)* | Comma-separated **admin** keys — unrestricted access, and the only keys allowed to manage per-user keys. Empty (with no DB keys) leaves the router open. |
| `SMART_ROUTER_OPTIONAL` | `0` | If `1`, `claudish-smart` falls back to plain claudish when unreachable |
| `SMART_ROUTER_CLASSIFIER_MODEL` ⚙ | `qwen2.5:3b-instruct` | Primary (local Ollama) model that profiles each prompt. Prefer a small **non-reasoning** instruct model — thinking models burn the classifier's tiny output budget before emitting JSON. Empty string disables the local step. |
| `SMART_ROUTER_CLASSIFIER_FALLBACK` ⚙ | `nvidia/nemotron-nano-9b-v2:free` | Free OpenRouter model tried if the local classifier fails. Only used when an OpenRouter key is configured. Empty string disables it. |
| `SMART_ROUTER_CLASSIFIER_REFINE_MODEL` ⚙ | `openai/gpt-5.6-luna` | Second-pass profiler, run only on prompts the local classifier flags as high-stakes, multi-specialist, or frontier-depth (see [Two-speed classification](#two-speed-classification)). Needs an OpenRouter key; empty string disables the second pass. |
| `SMART_ROUTER_MODEL_DENYLIST` ⚙ | *(empty)* | Comma-separated, case-insensitive substrings of model names to never route to (e.g. a broken local model). |
| `SMART_ROUTER_AGENT_DENYLIST` ⚙ | *(empty)* | Like the model denylist, but applied only in agent mode (models that advertise tools yet stall a tool-calling loop). |
| `SMART_ROUTER_WORKSPACE_DIR` | `~/.smart_ai_router_workspaces` | Root holding each user's private agent workspace (one subdir per identity). |
| `SMART_ROUTER_FILES_DIR` | `~/.smart_ai_router_files` | Root for uploaded/generated file blobs (metadata lives in SQLite). |
| `SMART_ROUTER_MAX_FILE_MB` ⚙ | `512` | Upload size ceiling in MB; larger uploads get `413`. |
| `SMART_ROUTER_OCR_MAX_PAGES` ⚙ | `10` | Max PDF pages rasterized for OCR text extraction. |
| `SMART_ROUTER_OCR_DPI` ⚙ | `150` | Rasterization resolution for OCR; higher is sharper but slower. |
| `SMART_ROUTER_ENABLE_BASH` ⚙ | `0` | If `1` (and `sandbox-exec` is present), the agent's `run_bash` tool is offered. Off by default — see the security note below. |
| `SMART_ROUTER_BASH_TIMEOUT_S` ⚙ | `30` | Wall-clock ceiling for a single `run_bash` call. |

Rows marked ⚙ are editable from the Settings page (env value is the fallback).

**Agent (filesystem) mode.** The chat UI's 🛠 Agent toggle lets a tool-capable
model read, write, and edit files in the caller's own workspace (and, when
enabled, run shell commands). It's gated by capability negotiation just like
vision — the toggle only lights up when a reachable in-scope model supports
function calling. Each authenticated identity gets its own path-jailed
workspace directory; the tools cannot escape it (`..`, absolute paths, and
symlink escapes are all rejected).

> **`run_bash` security.** Shell access is opt-in (`SMART_ROUTER_ENABLE_BASH=1`)
> and, on macOS, runs under a `sandbox-exec` (seatbelt) profile that **denies
> all network**, blocks reads of the server's home directory (so `.env` keys,
> `~/.ssh`, the SQLite DB, and *other users'* workspaces are unreadable), and
> confines writes to the caller's workspace. If `sandbox-exec` is unavailable
> the tool is simply not offered — the router never runs an unsandboxed shell.
> On a box behind a public tunnel, keep this off unless you understand the
> shared-kernel blast radius; the read/write tools need no such flag.

**Prompt classification** is a fallback chain, tried in order:

1. **Local** — `SMART_ROUTER_CLASSIFIER_MODEL` via the Ollama provider (fast, private, no rate limit).
2. **Free remote** — `SMART_ROUTER_CLASSIFIER_FALLBACK` via OpenRouter, only if an OpenRouter key is stored (a resilience backstop; free tier is rate-limited and sends prompts off-box).
3. **Keyword** — the built-in deterministic classifier.

Each LLM step is skipped if its model is unset or provider unavailable, and any failure (network error, timeout, unparseable output) advances to the next step. **Classification never blocks or fails a request.** The `X-Classifier` response header reports which step succeeded: `llm` (local), `llm-free` (OpenRouter), `keyword`, or `default` (empty prompt).

## Service management (macOS)

The setup wizard installs a LaunchAgent in `~/Library/LaunchAgents/com.smart-ai-router.plist`. Manage it with:

```bash
# Restart
launchctl kickstart -k gui/$(id -u)/com.smart-ai-router

# Stop
launchctl kill SIGTERM gui/$(id -u)/com.smart-ai-router

# Unload (disable)
launchctl unload ~/Library/LaunchAgents/com.smart-ai-router.plist

# View logs
tail -f /path/to/smart-ai-router/logs/server.log
tail -f /path/to/smart-ai-router/logs/server.err
```

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest

# Run the server directly (without launchd)
smart-ai-router
# or
python -m smart_ai_router
```

## Requirements

- Python 3.10+
- macOS (for LaunchAgent auto-start; the server itself runs anywhere)
- At least one provider: OpenRouter API key, local Ollama, or AWS Bedrock credentials

## License

MIT
