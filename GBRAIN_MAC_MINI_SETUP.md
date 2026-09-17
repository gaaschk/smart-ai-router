# GBrain Setup for Mac Mini (Dashboard Integration)

This guide sets up GBrain on a Mac Mini to work alongside smart-ai-router as a unified AI backend for the Unified Dashboard.

## Current State (as deployed on kevins-mac-mini.local)

- **Engine:** PostgreSQL 16 (Homebrew), database `gbrain`, with `pgvector` built
  from source against `postgresql@16` (the Homebrew `pgvector` bottle only
  ships for pg17/pg18 — see "Building pgvector for Postgres 16" below).
- **Embeddings:** OpenRouter (`openrouter:openai/text-embedding-3-small`,
  1536 dims), reusing the same `OPENROUTER_API_KEY` the smart-ai-router
  already uses for chat. No separate OpenAI/Voyage key needed. The key is
  persisted in `~/.gbrain/config.json` (`openrouter_api_key`), not an env var.
- **PATH:** `~/.bun/bin` (where `bun install -g` puts the `gbrain` binary) is
  exported in both `~/.zshenv` (all shells, including non-interactive SSH/
  launchd) and `~/.zshrc`.
- **Knowledge:** Populated via `gbrain import <markdown-dir>` (writes to the
  `pages`/`content_chunks` tables, which is what `gbrain query`/`search` —
  and therefore the smart-ai-router's RAG path — actually reads). Note:
  `gbrain remember`/`recall` write to a **separate** `facts` table that is
  NOT read by `query`/`search` — use `import` (or the `remember` tool's
  sibling `extract_facts`/page-authoring flows) for anything that should be
  retrievable via chat RAG.
- Verified end-to-end: keyword search (`gbrain search`), vector/hybrid search
  (`gbrain query`, confirmed via `cosine`/`evidence` fields in JSON output),
  and full RAG through `POST /v1/chat/completions` on port 8001 (the model
  correctly answered a question using only facts from imported GBrain pages).

## Architecture

```
Dashboard (http://localhost:5173)
    ↓
Dashboard Backend (http://localhost:5050)
    ├→ Smart-AI-Router (http://localhost:8001, HTTP)
    │   └→ LLM Providers (OpenRouter, Ollama, Bedrock, etc.)
    │
    └→ GBrain CLI (`gbrain call <tool> '<json>'`, subprocess -- no HTTP server)
        └→ Smart-AI-Router (for synthesis/enrichment)
            └→ LLM Providers
```

**GBrain has no HTTP API.** It ships as a CLI (`gbrain call <tool> '<json-args>'`,
~40 tools -- see `gbrain --tools-json`) plus a stdio-only MCP server (`gbrain
serve`) meant for editor/agent integrations. There is no `gbrain mcp-server
--port N` HTTP mode. The dashboard backend talks to GBrain by shelling out to
the CLI and serializing calls (see "A note on concurrency" below), not by
making HTTP requests to it.

GBrain stores knowledge (people, companies, facts, timeline) in PostgreSQL (or
embedded PGLite). When you ask the dashboard a question:
1. Dashboard sends the query to GBrain
2. GBrain searches its memory (hybrid keyword + vector search)
3. GBrain synthesizes an answer with citations using an LLM (via smart-ai-router)
4. Dashboard displays the answer with sources

## Prerequisites

- **Bun 1.3.11+** (JavaScript runtime, replaces Node for this project)
- **PostgreSQL 14+** (optional — GBrain can use embedded PGLite, but Postgres is better for multi-user/Mac Mini)
- **smart-ai-router** already running on `http://localhost:8001`
- **Dashboard backend** running on `http://localhost:5050`

## Step 1: Install Bun

```bash
# Install Bun
curl -fsSL https://bun.sh/install | bash

# Add to PATH (add to ~/.zprofile or ~/.bash_profile)
export PATH="$HOME/.bun/bin:$PATH"

# Verify
bun --version
```

## Step 2: Install GBrain

```bash
# Install GBrain globally from GitHub (NOT from npm!)
bun install -g github:garrytan/gbrain

# Verify
gbrain --version
```

If installation hangs (Bun postinstall hook issue), recover with:
```bash
gbrain apply-migrations --yes
```

## Step 3: Set Up PostgreSQL (Optional but Recommended)

For a Mac Mini serving multiple users via the dashboard, Postgres is better than PGLite.

### Option A: Using Homebrew (Recommended)

```bash
# Install PostgreSQL
brew install postgresql@16

# Start service
brew services start postgresql@16

# Create gbrain database
createdb gbrain

# Verify connection
psql -d gbrain -c "SELECT version();"
```

### Option B: Using Docker

```bash
# Start a PostgreSQL container
docker run --name gbrain-postgres \
  -e POSTGRES_DB=gbrain \
  -e POSTGRES_PASSWORD=gbrain_dev \
  -p 5432:5432 \
  -v gbrain-pgdata:/var/lib/postgresql/data \
  -d postgres:16

# Test connection
psql -h localhost -U postgres -d gbrain -c "SELECT version();"
```

### Option C: Use Embedded PGLite (Zero Config)

Skip Steps 3–4 and go straight to Step 5. PGLite works for single-user/testing but doesn't support concurrent connections well.

## Step 4: Set Up Environment Variables

Create `~/.gbrain/env` (0600) for daemon access:

```bash
mkdir -p ~/.gbrain
cat > ~/.gbrain/env <<'EOF'
# PostgreSQL connection (if using Postgres)
export GBRAIN_DATABASE_URL="postgresql://postgres:gbrain_dev@localhost:5432/gbrain"

# LLM API keys (optional — GBrain can synthesize via smart-ai-router if configured)
# These are only needed if GBrain makes DIRECT calls to providers (not recommended for this setup)
# Instead, point GBrain to smart-ai-router's gateway (see Step 7)

# Embedding API key (optional — GBrain can use keyword-only search without it)
export VOYAGE_API_KEY="pa-..."  # or OPENAI_API_KEY, ANTHROPIC_API_KEY
EOF

chmod 600 ~/.gbrain/env
```

Also export to current shell:
```bash
export GBRAIN_DATABASE_URL="postgresql://postgres:gbrain_dev@localhost:5432/gbrain"
```

## Step 5: Initialize GBrain

```bash
# If using Postgres
gbrain init --prefer-postgres --local-postgres --allow-create-db --json

# If using embedded PGLite (no Postgres needed)
gbrain init

# Verify installation
gbrain doctor --json
```

When prompted, **confirm the search mode**. For Mac Mini multi-user setup, recommend **balanced** mode:

```
Per-query cost @ 10K queries/mo:
                  Haiku 4.5     Sonnet 4.6    Opus 4.7
  conservative    $40/mo        $120/mo       $200/mo
  balanced        $100/mo       $300/mo       $500/mo
  tokenmax        $200/mo       $600/mo       $1,000/mo
```

Set balanced:
```bash
gbrain config set search.mode balanced
```

## Step 6: Create Brain Structure

GBrain stores knowledge in a MECE (Mutually Exclusive, Collectively Exhaustive) directory structure:

```bash
mkdir -p ~/gbrain-memory/{people,companies,concepts,meetings,decisions,findings}

cat > ~/gbrain-memory/README.md <<'EOF'
# My Brain

Central knowledge repository for strategic decisions, relationships, and insights.

## Structure

- **people/** — Individual profiles with roles, relationships, recent interactions
- **companies/** — Company profiles, funding, leadership, partnerships
- **concepts/** — Frameworks, methodologies, industry insights
- **meetings/** — Meeting notes, decisions, action items
- **decisions/** — Strategic decisions with rationale and outcomes
- **findings/** — Research, analysis, conclusions

## Usage

- Use `gbrain put-page` or add .md files and `gbrain import` to ingest
- `gbrain query "who works at Acme?"` to search across all pages
- `gbrain search` for keyword-only; `gbrain query` for synthesis (with LLM answer)
- `gbrain remember "Bob joined Acme as CTO in 2026"` for quick facts
EOF

# Import the structure
gbrain import ~/gbrain-memory --no-embed

# Generate embeddings (if using semantic search)
gbrain embed --stale
```

## Step 7: Configure GBrain to Use Smart-AI-Router

By default, GBrain tries to call LLM providers directly. Instead, point it to smart-ai-router so all LLM calls go through the router's intelligent model selection.

Create/edit `~/.gbrain/config.json`:

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

Or set via CLI:

```bash
gbrain config set models.gateway.baseUrl http://localhost:8001/v1
gbrain config set models.gateway.provider openai-compatible
gbrain config set models.chat auto-route
gbrain config set models.rerank auto-route
```

Verify:
```bash
gbrain config get models
```

Now when GBrain synthesizes answers or enriches pages, it sends requests to smart-ai-router (with `model: "auto"`), and the router picks the optimal model.

## Step 8: Test GBrain

```bash
# Add a memory
gbrain remember "Alice runs engineering at Acme, a Series-B fintech"
gbrain remember "You met Alice on 2026-09-15 to discuss Q4 roadmap"

# Search
gbrain search "Alice Acme"

# Synthesize (with LLM answer)
gbrain query "What do I need to know about Alice and Acme?"

# List pages
gbrain list

# View one page
gbrain get-page people/alice
```

## Step 9: A Note on Concurrency (No Server to "Set Up")

There is no GBrain HTTP server to start. `gbrain call <tool> '<json>'` is a
one-shot CLI process, and `gbrain serve` is a stdio MCP server for editor/agent
clients -- neither listens on a port. The dashboard backend calls the CLI
directly (see `dashboard/backend/src/services/gbrainClient.ts`); nothing needs
to be started or kept running for this step.

The one thing that *does* matter operationally: GBrain's default embedded
store (PGLite) takes an **exclusive file lock** for the lifetime of each CLI
invocation. Two `gbrain call`/`gbrain <command>` processes cannot run
concurrently against the same brain -- the second one blocks on the lock and
eventually times out (`GBrain: Timed out waiting for PGLite lock.`). The
dashboard backend serializes its own calls through a small in-process queue,
but if you also run `gbrain` commands by hand (or a cron sync) while the
dashboard is live, expect occasional lock contention. Switching to Postgres
(Step 3, Option A/B) removes this limitation.

## Step 10: Optional — Set Up Autopilot (24/7 Enrichment)

GBrain can run a daemon that enriches pages, extracts facts, and consolidates memory while you sleep.

```bash
# Install autopilot daemon
gbrain autopilot --install

# Start it
gbrain autopilot start

# View logs
gbrain autopilot status
gbrain autopilot logs
```

The daemon will:
- Extract typed relationships (who works at where, invested in, etc.)
- Synthesize daily digest emails
- Consolidate contradictory facts
- Index pages for search

Skip this for initial setup — focus on getting the dashboard working first.

## Step 11: Dashboard Backend Integration (Phase 3 -- Done)

The dashboard backend (`dashboard/backend`) calls the GBrain CLI directly
(`src/services/gbrainClient.ts`), exposed to the frontend as:
- `GET /api/memory/stats`, `/search`, `/pages`, `/page`, `/graph` -- brain
  stats, hybrid/keyword search, page browsing, and link-graph traversal.
- `GET /api/skills/integrations` + `/integrations/:id/status` -- the
  integration recipes (email/calendar/X/voice/... senses) as GBrain's
  closest equivalent to a "skill library".
- `GET/POST /api/skills/jobs` -- list and submit GBrain's built-in
  background job types (sync, embed, lint, import, extract, backlinks,
  autopilot-cycle) via its Minions job queue.

## Configuration Summary

| Component | Port | URL |
|-----------|------|-----|
| GBrain CLI | - | `gbrain` command (must be on `PATH`) |
| GBrain Daemon (autopilot) | - | systemd/launchd service |
| GBrain Brain Database | 5432 (Postgres) or embedded PGLite | `postgresql://postgres@localhost/gbrain` |
| GBrain Brain Files | - | `~/gbrain-memory/` |

## Troubleshooting

### "gbrain: command not found"
Add `~/.bun/bin` to your `$PATH` and restart the shell.

### "GBRAIN_DB_ACCESS <reason>"
GBrain can't reach the database. Run:
```bash
gbrain db-repair
```

### "Error: could not connect to Postgres"
Check Postgres is running:
```bash
brew services list | grep postgres
psql -l  # List databases
```

### "No API key for embedding"
GBrain can work without embeddings (keyword search only). If you want semantic search
and you already have an `OPENROUTER_API_KEY` (as this deployment does — the
smart-ai-router already needs one), you don't need a separate Voyage/OpenAI key:
```bash
gbrain config set openrouter_api_key "$OPENROUTER_API_KEY"
gbrain init --force --url "$GBRAIN_DATABASE_URL" \
  --embedding-model openrouter:openai/text-embedding-3-small \
  --embedding-dimensions 1536
gbrain embed --stale
```
Otherwise, any supported provider works (see `gbrain providers list` and
`docs/integrations/embedding-providers.md` in the gbrain package):
```bash
export VOYAGE_API_KEY=pa-...
gbrain embed --stale
```

### Building pgvector for Postgres 16 (Homebrew bottle only supports pg17/pg18)

`brew install pgvector` installs a bottle built against `postgresql@17`/`@18`.
If the Mac Mini's Postgres is still on `postgresql@16` (e.g. because the
`dashboard` database already lives there and an in-place major-version
upgrade isn't worth the risk), `CREATE EXTENSION vector` fails with
`extension "vector" is not available`. Build it from source against the
`@16` headers instead:
```bash
cd /tmp
git clone --branch v0.8.0 https://github.com/pgvector/pgvector.git pgvector-build
cd pgvector-build
make PG_CONFIG=/opt/homebrew/opt/postgresql@16/bin/pg_config
make install PG_CONFIG=/opt/homebrew/opt/postgresql@16/bin/pg_config
psql -d gbrain -c "CREATE EXTENSION vector;"
```

### "GBrain: Timed out waiting for PGLite lock."
Another `gbrain` process is holding the exclusive PGLite lock (see Step 9).
Wait for it to finish, or switch to Postgres for concurrent access.

### Port 5000/5050/8001 already in use

Find the process:
```bash
lsof -i :5000   # Show process using port 5000
```

Kill it or use a different port:
```bash
PORT=5051 npm run dev  # Dashboard backend on 5051 instead
```

## Next Steps

1. ✅ Install Bun and GBrain
2. ✅ Set up PostgreSQL (or PGLite)
3. ✅ Initialize GBrain (`gbrain init`)
4. ✅ Create brain structure (`gbrain-memory/`)
5. ✅ Configure smart-ai-router gateway
6. ✅ Confirm the CLI works (`gbrain call get_stats '{}'`)
7. ✅ **Phase 3 Dashboard Integration** — dashboard backend calls the GBrain CLI directly

## Additional Resources

- GBrain README: `https://github.com/garrytan/gbrain`
- GBrain Installation Guide: `INSTALL_FOR_AGENTS.md` (in gbrain repo)
- Smart-AI-Router: `http://localhost:8001`
- Dashboard Backend: `dashboard/backend` in this repo
