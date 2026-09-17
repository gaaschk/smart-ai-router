# GBrain deployment (Mac Mini)

GBrain runs alongside smart-ai-router on `kevins-mac-mini.local`, giving the
router a persistent knowledge store for retrieval-augmented chat. This is the
current, as-deployed state — see [Configuration](configuration.md) and
[Service management](operations.md) for the router side.

## What GBrain is

GBrain is a personal knowledge-management CLI (`gbrain`, installed via Bun
from `github:garrytan/gbrain`) with no HTTP server of its own. It ships:

- `gbrain call <tool> '<json-args>'` — one-shot CLI invocations (~40 tools,
  see `gbrain --tools-json`).
- `gbrain serve` — a stdio-only MCP server for editor/agent integrations.

Neither listens on a port. `smart_ai_router/gbrain_client.py` talks to GBrain
by shelling out to the CLI per request — there's nothing to "start" beyond
having `gbrain` on `PATH`.

## Current configuration

- **Engine:** PostgreSQL 16 (Homebrew), database `gbrain`, with `pgvector`
  built from source against `postgresql@16` — Homebrew's `pgvector` bottle
  only ships for pg17/pg18:
  ```bash
  cd /tmp && git clone --branch v0.8.0 https://github.com/pgvector/pgvector.git pgvector-build
  cd pgvector-build
  make PG_CONFIG=/opt/homebrew/opt/postgresql@16/bin/pg_config
  make install PG_CONFIG=/opt/homebrew/opt/postgresql@16/bin/pg_config
  psql -d gbrain -c "CREATE EXTENSION vector;"
  ```
  Postgres was chosen over GBrain's default embedded PGLite because PGLite
  takes an exclusive file lock per CLI invocation (single-writer/
  single-reader) — a second concurrent `gbrain` call blocks until the first
  finishes and eventually times out. Postgres removes that limitation.
- **Embeddings:** OpenRouter (`openrouter:openai/text-embedding-3-small`,
  1536 dims), reusing the same `OPENROUTER_API_KEY` the router already uses
  for chat — no separate OpenAI/Voyage key needed. Persisted in
  `~/.gbrain/config.json` (`openrouter_api_key`), not an env var.
- **Gateway:** GBrain's own LLM calls (synthesis, enrichment) are routed
  through smart-ai-router rather than a direct provider, via
  `~/.gbrain/config.json`:
  ```json
  {
    "models": {
      "gateway": {
        "baseUrl": "http://localhost:8001/v1",
        "provider": "openai-compatible",
        "apiKey": ""
      },
      "chat": "auto-route",
      "rerank": "auto-route"
    }
  }
  ```
- **PATH:** `~/.bun/bin` (where `bun install -g` puts the `gbrain` binary)
  is exported in both `~/.zshenv` (all shells, including non-interactive
  SSH/launchd) and `~/.zshrc`. The system LaunchDaemon that runs the router
  (`/Library/LaunchDaemons/com.kevingaasch.smart-ai-router.plist`) needed its
  own `PATH` entry added separately, since daemons don't inherit shell
  profiles. `gbrain_client.py` also has a fallback: if `gbrain` isn't on
  `PATH`, it looks in `~/.bun/bin` directly, and if `bun` itself isn't on
  `PATH` either, it invokes `bun ~/.bun/bin/gbrain ...` explicitly so a
  stale daemon environment can't break it.

## Pages vs. facts — which one RAG actually reads

`gbrain import <dir>` writes to the `pages`/`content_chunks` tables. This is
what `gbrain query`/`search` — and therefore the router's RAG path — reads.

`gbrain remember "..."` writes to a **separate** `facts` table that `query`/
`search` do **not** read. Use `import` (or an equivalent page-authoring flow)
for anything that should be retrievable via chat RAG; `remember` is for
quick, ad hoc facts surfaced elsewhere.

## How the router uses GBrain (RAG)

`smart_ai_router/api/proxy.py` calls into GBrain on every chat request:

1. The user's latest message is queried against GBrain's hybrid search
   (`gbrain_client.hybrid_query`).
2. Any retrieved context is injected as a system message ahead of the user's
   messages.
3. After the response streams back, the Q&A exchange is asynchronously
   saved back into GBrain via `remember`.

This means every client of the router (built-in web UI, or any external
OpenAI-compatible client) automatically benefits from GBrain's knowledge base
with no client-side changes. The built-in web UI's **Memory** and **Skills**
tabs (`http://localhost:8001/`) call `/api/gbrain/*`
(`smart_ai_router/api/gbrain_routes.py`) to expose GBrain search, page
browsing, stats/health, integrations, and job submission directly in the
same UI as chat and usage — there is no separate dashboard app.

## Troubleshooting

**`gbrain: command not found`** — add `~/.bun/bin` to `PATH` and restart the
shell (or the daemon, if it's a launchd process — see PATH note above).

**`GBRAIN_DB_ACCESS <reason>`** — GBrain can't reach the database: run
`gbrain db-repair`.

**Vector search unavailable (`missing_env`)** — no embedding provider
configured. Point it at OpenRouter (reusing the router's existing key):
```bash
gbrain config set openrouter_api_key "$OPENROUTER_API_KEY"
gbrain init --force --url "$GBRAIN_DATABASE_URL" \
  --embedding-model openrouter:openai/text-embedding-3-small \
  --embedding-dimensions 1536
gbrain embed --stale
```

**`extension "vector" is not available`** — the Homebrew `pgvector` bottle
targets pg17/pg18; build it from source against pg16 headers (see above).

**`GBrain: Timed out waiting for PGLite lock.`** — another `gbrain` process
is holding the exclusive PGLite lock. Only relevant if still on PGLite;
switching to Postgres (as this deployment does) removes the limitation.

**Query/search returns nothing despite `remember` calls succeeding** — see
"Pages vs. facts" above; `remember` doesn't populate what `query`/`search`
read. Use `gbrain import`.
