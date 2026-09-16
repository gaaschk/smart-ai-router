# GBrain Setup for Mac Mini (Dashboard Integration)

This guide sets up GBrain on a Mac Mini to work alongside smart-ai-router as a unified AI backend for the Unified Dashboard.

## Architecture

```
Dashboard (http://localhost:5173)
    ↓
Dashboard Backend (http://localhost:5050)
    ├→ Smart-AI-Router (http://localhost:8001)
    │   └→ LLM Providers (OpenRouter, Ollama, Bedrock, etc.)
    │
    └→ GBrain (http://localhost:8002)
        └→ Smart-AI-Router (for synthesis/enrichment)
            └→ LLM Providers
```

GBrain stores knowledge (people, companies, facts, timeline) in PostgreSQL. When you ask the dashboard a question:
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

## Step 9: Set Up GBrain MCP Server

GBrain exposes a Model Context Protocol (MCP) server so external apps (like the dashboard backend) can call it.

```bash
# Start the MCP server (runs in foreground)
gbrain mcp-server --port 3000

# In another terminal, test it
# (The dashboard backend will make HTTP calls to http://localhost:3000)
```

For production (daemon mode):
```bash
gbrain autopilot --install

# This creates a systemd/launchd service that keeps the MCP server alive
```

Verify the MCP server is listening:
```bash
curl http://localhost:3000/health
```

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

## Step 11: Integrate with Dashboard Backend

The dashboard backend (`dashboard/backend`) will call GBrain's MCP server to:
- Search memory (`POST /api/memory/search`)
- List skills (`GET /api/skills`)
- Trigger skill execution (`POST /api/skills/{id}/execute`)

See **Phase 3** of the Unified Dashboard plan for implementation.

## Configuration Summary

| Component | Port | URL |
|-----------|------|-----|
| GBrain MCP Server | 3000 | `http://localhost:3000` |
| GBrain Daemon (autopilot) | - | systemd/launchd service |
| GBrain Brain Database | 5432 (Postgres) | `postgresql://postgres@localhost/gbrain` |
| GBrain Brain Files | - | `~/gbrain-memory/` |
| GBrain CLI | - | `gbrain` command |

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
GBrain can work without embeddings (keyword search only). If you want semantic search:
```bash
export VOYAGE_API_KEY=pa-...
gbrain embed --stale
```

### Port 5000/5050/8001/8002/3000 already in use

Find the process:
```bash
lsof -i :5000   # Show process using port 5000
```

Kill it or use a different port:
```bash
PORT=5051 npm run dev  # Dashboard backend on 5051 instead
gbrain mcp-server --port 3001  # GBrain MCP on 3001 instead
```

## Next Steps

1. ✅ Install Bun and GBrain
2. ✅ Set up PostgreSQL (or PGLite)
3. ✅ Initialize GBrain (`gbrain init`)
4. ✅ Create brain structure (`gbrain-memory/`)
5. ✅ Configure smart-ai-router gateway
6. ✅ Start GBrain MCP server (`gbrain mcp-server`)
7. ⏳ **Phase 3 Dashboard Integration** — wire dashboard backend to GBrain API

## Additional Resources

- GBrain README: `https://github.com/garrytan/gbrain`
- GBrain Installation Guide: `INSTALL_FOR_AGENTS.md` (in gbrain repo)
- Smart-AI-Router: `http://localhost:8001`
- Dashboard Backend: `dashboard/backend` in this repo
