# smart-ai-router

A vendor-agnostic LLM capability router that classifies each prompt and routes it to the cheapest model that clears the quality bar. Sits in front of OpenRouter, Ollama, and AWS Bedrock as a single OpenAI-compatible endpoint.

## What it does

1. **Profiles** each incoming prompt: which fields answering it needs (`software_engineering`, `law_regulatory`, `medicine_health`, …), how deep into each, what it demands (tool use, current facts, factual precision) and what's at stake. An LLM classifier does this, with a keyword classifier as the backstop — see [The prompt profile](docs/routing.md#the-prompt-profile). `domain` and `complexity` still appear in the API and headers, derived from the profile for compatibility.
2. **Routes** to the cheapest model whose competence score clears the threshold for that complexity tier — filtering by tool-calling support, vision, context length, and reliability.
3. **Falls back** to the highest-competence model (typically Claude via Bedrock) only when no cheaper model qualifies — and surfaces an escalation notice when it does.
4. **Searches the web** first when the profile says the answer turns on facts that could have moved since training — see [Web search](docs/api.md#web-search).
5. **Streams** responses back in real-time via Server-Sent Events, with an immediate keepalive so the client knows the connection is alive while waiting for the provider's first token.

The model matrix is populated by syncing live catalogs from your configured providers (OpenRouter, Ollama, Bedrock). Competence scores are inferred from model name patterns using benchmark-informed priors, so newly-released models get reasonable defaults without manual curation.

Beyond the routing proxy, the built-in web UI at `http://localhost:8001/` is a full chat client:

- **Chat** with persistent, server-side conversation history — every message shows which model it was routed to and why.
- **File uploads** — PDF, Word, PowerPoint, Excel, and text/code files are extracted to text and fed to the model as context; images are inlined for vision-capable models.
- **Agent mode** — a tool-capable model can read, write, and edit files in your private, path-jailed workspace, and **create downloadable documents** (PDF, Word, PowerPoint, Excel, Markdown). Auto-enables when your request needs a file; can be forced on or off.
- **Voice** — talk instead of typing and have the reply read back to you, using the browser's own speech engine so routing is unaffected. Needs Safari or Chrome, and HTTPS or localhost — see [Voice](docs/api.md#voice).
- **Per-user API keys** — mint, scope, rate-limit, revoke, and rotate keys from the Keys page; a signed-in badge shows which identity you're using.
- **Feedback** — ⚑ on any reply, or the ⚑ tab on the right edge, files a report with the conversation and the routing decision attached; the operator can mirror those into a repo's issue tracker — see [Bad-response reports](docs/api.md#bad-response-reports).

## Quick start

**One-line install (macOS):**

```bash
curl -fsSL https://raw.githubusercontent.com/gaaschk/smart-ai-router/main/install.sh | bash
```

**Or manual install:**

```bash
git clone https://github.com/gaaschk/smart-ai-router.git
cd smart-ai-router
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
smart-ai-router setup
```

Either way, the setup wizard will:
- Ask for your provider credentials (OpenRouter API key, Ollama URL, and/or AWS Bedrock)
- Save them to the local SQLite store
- Run an initial model sync
- Install a macOS LaunchAgent so the router starts at login (no sudo needed)
- Symlink `claudish-smart` to `~/.local/bin` for immediate use

After setup, the router is available at `http://localhost:8001`.

## Documentation

| | |
|---|---|
| [Using it from a client](docs/clients.md) | Claude Code, Cursor, Codex CLI, aider, the OpenAI SDKs |
| [API](docs/api.md) | Every endpoint: the proxy, keys, files, web search, voice, reports |
| [How routing decisions work](docs/routing.md) | Prompt profiles, the two-speed classifier, caching, cost tiers |
| [Configuration](docs/configuration.md) | Settings, the store, environment variables |
| [Service management](docs/operations.md) | The macOS LaunchAgent, Pull & Restart |

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
| `smart_ai_router/taxonomy.py` | The prompt profile: field/depth vocabulary, demands, stakes |
| `smart_ai_router/llm_classifier.py` | Two-speed LLM profiler (triage → refine), the primary classifier |
| `smart_ai_router/classifier.py` | Keyword-based domain/complexity classification — the backstop when the LLM path is off or fails |
| `smart_ai_router/settings.py` | UI-managed settings registry (DB → env → default) |
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
| `smart_ai_router/public_access.py` | Anonymous chat policy: session identity, spend cap, per-IP limits |
| `smart_ai_router/keys_cli.py` | `smart-ai-router keys` command-line key management |
| `smart_ai_router/extract.py` | Extract text from uploaded PDF/Word/PowerPoint/Excel/text files |
| `smart_ai_router/docgen.py` | Render Markdown-ish text into PDF/Word/PowerPoint/Excel/Markdown documents |
| `smart_ai_router/files.py` | Filesystem-backed blob storage for uploads (metadata in SQLite) |
| `smart_ai_router/tools.py` | Agent filesystem tools (read/write/edit/list, create_document, run_bash) against a per-user workspace |
| `smart_ai_router/api/files_routes.py` | OpenAI-compatible Files API (`/v1/files`) |
| `smart_ai_router/api/conversations_routes.py` | Chat history API (`/api/conversations`) |
| `smart_ai_router/api/reports_routes.py` | Bad-response reports (`/api/reports`) |
| `smart_ai_router/github_issues.py` | Mirroring a report to the repo's issue tracker |
| `smart_ai_router/overhead.py` | Attributing the router's own helper calls to the request that caused them |

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
