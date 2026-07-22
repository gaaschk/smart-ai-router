# smart-ai-router

A vendor-agnostic LLM capability router that classifies each prompt and routes it to the cheapest model that clears the quality bar. Sits in front of OpenRouter, Ollama, and AWS Bedrock as a single OpenAI-compatible endpoint.

## What it does

1. **Classifies** each incoming prompt by domain (`coding`, `docs`, `reasoning`, `general`) and complexity (`trivial`, `moderate`, `hard`).
2. **Routes** to the cheapest model whose competence score clears the threshold for that complexity tier — filtering by tool-calling support, vision, context length, and reliability.
3. **Falls back** to the highest-competence model (typically Claude via Bedrock) only when no cheaper model qualifies — and surfaces an escalation notice when it does.
4. **Streams** responses back in real-time via Server-Sent Events, with an immediate keepalive so the client knows the connection is alive while waiting for the provider's first token.

The model matrix is populated by syncing live catalogs from your configured providers (OpenRouter, Ollama, Bedrock). Competence scores are inferred from model name patterns using benchmark-informed priors, so newly-released models get reasonable defaults without manual curation.

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

### Provider management

```bash
# List providers
curl http://localhost:8001/api/providers

# Add/update a provider
curl -X PUT http://localhost:8001/api/providers/openrouter \
  -H 'Content-Type: application/json' \
  -d '{"name":"openrouter","kind":"openrouter","enabled":true,"api_key":"sk-or-..."}'

# Trigger a model sync
curl -X POST http://localhost:8001/api/sync -H 'Content-Type: application/json' -d '{}'
```

### Models

```bash
# List all synced models
curl http://localhost:8001/api/models

# Get a specific model
curl http://localhost:8001/api/models/openrouter/anthropic/claude-sonnet-4-6
```

### Self-update

```bash
# Check for source updates
curl http://localhost:8001/api/updates

# Apply update (git pull + restart)
curl -X POST http://localhost:8001/api/updates/apply
```

## How routing decisions work

The router uses a **competence matrix** — each model has scores for `coding`, `docs`, `reasoning`, and `general` (0.0–1.0). The classifier determines which domain and complexity a prompt belongs to, then the router:

1. **Filters** models by hard constraints: tool-calling support, vision, context length, minimum reliability.
2. **Applies a competence bar** based on complexity:
   - `trivial` → 0.50 (almost any model qualifies)
   - `moderate` → 0.70 (filters out the weakest)
   - `hard` → 0.88 (only top-tier models)
3. **Sorts** qualifying models by cost tier (ascending), then competence (descending as tiebreaker).
4. **Returns the cheapest** that clears the bar.

If nothing clears the bar, it falls back to the highest-competence model regardless of cost (typically Claude Opus via Bedrock) and marks the response as escalated.

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

| Variable | Default | Purpose |
|----------|---------|---------|
| `SMART_ROUTER_PORT` | `8001` | Port the server listens on |
| `SMART_ROUTER_LABEL` | `com.smart-ai-router` | launchd service label |
| `SMART_ROUTER_URL` | `http://$(hostname):8001` | Used by `claudish-smart` to find the router |
| `SMART_ROUTER_OPTIONAL` | `0` | If `1`, `claudish-smart` falls back to plain claudish when unreachable |

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
