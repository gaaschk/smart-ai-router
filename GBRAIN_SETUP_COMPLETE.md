# GBrain Setup Complete ✅

GBrain is now installed and configured on this Mac Mini.

## Status

| Component | Status | Details |
|-----------|--------|---------|
| **GBrain CLI** | ✅ Running | v0.18.2 installed via Bun |
| **Brain Database** | ✅ Running | PGLite embedded (`~/.gbrain/brain.pglite`) |
| **Brain Pages** | ✅ 15 pages | Imported from test fixtures |
| **Smart-AI-Router Gateway** | ✅ Configured | `http://localhost:8001/v1` |
| **MCP Server** | ✅ Running | `gbrain serve` (stdio mode) |
| **Brain Directory** | ✅ Created | `~/gbrain-memory/` with MECE structure |

## Configuration

### GBrain Home
```
~/.gbrain/
├── brain.pglite         (PGLite database)
├── config.json          (Models pointing to smart-ai-router)
└── env                  (Environment variables for daemon)
```

### Brain Storage
```
~/gbrain-memory/
├── README.md            (Brain guide)
├── people/              (Individual profiles)
├── companies/           (Company profiles)
├── concepts/            (Frameworks, insights)
├── meetings/            (Meeting notes)
├── decisions/           (Strategic decisions)
└── findings/            (Research, analysis)
```

### Model Configuration
GBrain is configured to route all LLM calls through smart-ai-router:

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

This means:
- ✅ GBrain synthesis queries go through smart-ai-router
- ✅ Automatic model selection via router's classifier
- ✅ No duplicate LLM provider keys needed
- ✅ Unified cost tracking across all LLM usage

## Quick Commands

```bash
# Add knowledge
gbrain remember "Alice runs engineering at Acme"

# Search knowledge
gbrain search "Alice"                    # Keyword search
gbrain query "What do I know about Alice?"  # LLM synthesis

# Manage pages
gbrain list                              # List all pages
gbrain get people/alice                  # Read a page
gbrain put-page people/alice < alice.md # Create/update

# Import markdown directory
gbrain import ~/gbrain-memory --no-embed

# Generate embeddings (for semantic search)
gbrain embed --stale

# Extract knowledge graph
gbrain extract links --source db
gbrain extract timeline --source db

# Health check
gbrain doctor

# View MCP tools
gbrain --tools-json
```

## Next Steps

### Phase 3: Dashboard Integration

The dashboard backend (`dashboard/backend`) will call GBrain's MCP server to:
1. **Search memory** — `POST /api/memory/search`
2. **Query & synthesize** — `POST /api/memory/query`
3. **List pages** — `GET /api/memory/pages`
4. **List available skills** — `GET /api/skills`
5. **Execute skills** — `POST /api/skills/{id}/execute`

### Running GBrain Services

For **development**:
```bash
# Terminal 1: Smart-AI-Router (if not already running)
smart-ai-router

# Terminal 2: GBrain MCP Server
gbrain serve

# Terminal 3: Dashboard Backend
cd dashboard/backend && npm run dev

# Terminal 4: Dashboard Frontend
cd dashboard/frontend && npm run dev
```

For **production** (Mac Mini daemon):
```bash
# Set up persistent sync daemon (optional)
gbrain sync --install-cron

# Set up autopilot (24/7 enrichment, optional)
gbrain autopilot --install
gbrain autopilot start

# Start MCP server as a systemd/launchd service
# (Create a .plist for launchd or systemd unit file)
```

## Testing

### Verify GBrain can reach smart-ai-router
```bash
gbrain query "Hello" --dry-run
```

This should attempt to call smart-ai-router (may fail if router isn't running, but confirms configuration).

### Verify MCP server is active
```bash
# The MCP server is running on stdio, not HTTP
# Dashboard backend will communicate with it via the MCP protocol
```

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│  Dashboard (React, http://localhost:5173)                   │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ↓
┌─────────────────────────────────────────────────────────────┐
│  Dashboard Backend (Node.js, http://localhost:5050)         │
│                                                             │
│  Routes:                                                    │
│  ├─ /api/chat           → smart-ai-router                  │
│  ├─ /api/analytics      → smart-ai-router                  │
│  ├─ /api/memory/*       → gbrain (MCP server via socket)   │
│  ├─ /api/skills/*       → gbrain                           │
│  └─ Socket.IO events    → real-time updates               │
└────────────────┬────────────────────────┬────────────────────┘
                 │                        │
                 ↓                        ↓
    ┌────────────────────┐    ┌──────────────────────┐
    │ Smart-AI-Router    │    │ GBrain MCP Server    │
    │ (localhost:8001)   │    │ (stdio mode)         │
    │                    │    │                      │
    │ Routes requests    │    │ Memory management:   │
    │ to optimal LLM     │    │ ├─ Search            │
    │ providers          │    │ ├─ Query & synthesize│
    └────────────────────┘    │ ├─ Import/export    │
             │                │ └─ Knowledge graph  │
             ↓                │                      │
    ┌────────────────────┐    │ Database:           │
    │ LLM Providers:     │    │ └─ PGLite           │
    │ ├─ OpenRouter      │    │    (~/.gbrain/)     │
    │ ├─ Ollama          │    │                      │
    │ ├─ Bedrock         │    │ Files:              │
    │ └─ Others          │    │ └─ Brain markdown   │
    └────────────────────┘    │    (~/gbrain-mem/)  │
                              └──────────────────────┘
```

## Troubleshooting

### "gbrain: command not found"
```bash
export PATH="$HOME/.bun/bin:$PATH"
```

### "GBRAIN_DB_ACCESS" error
GBrain can't reach its database:
```bash
gbrain db-repair
```

### "Could not connect to smart-ai-router"
Ensure smart-ai-router is running on `http://localhost:8001`:
```bash
curl http://localhost:8001/api/whoami
```

### Embeddings not working
GBrain can work without embeddings (keyword search only). If you want semantic search:
```bash
# Set embedding key (Voyage or OpenAI)
export VOYAGE_API_KEY=pa-...
# Generate embeddings
gbrain embed --stale
```

## Documentation

- **Setup Guide**: [GBRAIN_MAC_MINI_SETUP.md](./GBRAIN_MAC_MINI_SETUP.md)
- **GBrain GitHub**: https://github.com/garrytan/gbrain
- **GBrain Docs**: https://github.com/garrytan/gbrain/blob/master/README.md

## Summary

✅ **GBrain is ready for Phase 3 dashboard integration**

Next: Implement the dashboard backend routes to call GBrain's MCP server for memory search, synthesis, and skill execution.
