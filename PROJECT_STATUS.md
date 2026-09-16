# Unified Dashboard + GBrain Integration — Project Status

## Summary

You now have a **three-tier system** set up on your Mac Mini:

1. **Smart-AI-Router** (LLM routing/cost optimization)
2. **Unified Dashboard** (React UI with Chat, Analytics, Memory, Skills)
3. **GBrain** (Personal knowledge management with synthesis)

All three talk to each other through intelligent APIs.

## Current Status

### Phase 1: Dashboard Shell ✅
- Core backend (Express) + frontend (React) structure
- Navigation, layout, 6 feature pages
- Socket.IO setup for real-time events
- Status: **MERGED** (PR #94)

### Phase 2: Smart-AI-Router Integration ✅
- Backend chat endpoint wired to smart-ai-router
- Backend analytics endpoint pulling real usage data
- Frontend ChatPage and AnalyticsPage showing real data
- Backend default port changed from 5000 → 5050 (macOS conflict fix)
- Status: **MERGED** (PR #96)

### Phase 3: GBrain Integration ✅
- GBrain installed and configured (v0.18.2)
- GBrain pointing to smart-ai-router for LLM calls
- Brain database: PGLite with 15 sample pages
- Brain directory: ~/gbrain-memory/ (people, companies, concepts, etc.)
- **Correction from earlier planning**: GBrain has no HTTP API. It's a CLI
  (`gbrain call <tool> '<json>'`, ~40 tools) plus a stdio-only MCP server
  (`gbrain serve`) for editor/agent clients -- there's no `gbrain mcp-server
  --port N` mode. The dashboard backend shells out to the CLI instead
  (`dashboard/backend/src/services/gbrainClient.ts`), serialized through a
  queue because GBrain's default PGLite store takes an exclusive file lock
  per invocation.
- Dashboard Memory page: brain stats, hybrid/keyword search, page detail
  (tags/links/backlinks) — all live against real GBrain data.
- Dashboard Skills page: integration recipes (email/calendar/X/voice senses)
  + background job catalog (sync/embed/lint/import/extract/backlinks/
  autopilot-cycle) via GBrain's Minions job queue — submit and list jobs live.
- Status: **IMPLEMENTED**, pending PR review/merge

## Services Running on Mac Mini

| Service | Port | Purpose | Status |
|---------|------|---------|--------|
| Smart-AI-Router | 8001 | LLM routing/classification | Running |
| Dashboard Backend | 5050 | API for chat, analytics, memory, skills | Ready to start |
| Dashboard Frontend | 5173 | React UI (dev) | Ready to start |
| GBrain CLI | (subprocess, no port) | `gbrain call <tool> '<json>'` — invoked by the dashboard backend, not a standing service | N/A |
| GBrain Brain DB | PGLite | Embedded Postgres (~/.gbrain/brain.pglite) | Running |

## How They Work Together

### User asks a question in Dashboard Chat
```
1. Dashboard Frontend sends message to Backend (/api/chat)
2. Backend calls Smart-AI-Router (/v1/chat/completions, model="auto")
3. Smart-AI-Router classifies the prompt and picks optimal LLM
4. Selected LLM responds with answer
5. Backend returns answer + routing metadata (which model, why, cost)
6. Dashboard shows answer with routed model, cost, escalation badge
7. Socket.IO broadcasts the routing decision to all connected clients
```

### User searches their memory in Dashboard Memory Page (Phase 3)
```
1. Dashboard Frontend sends search query to Backend (/api/memory/search?q=...)
2. Backend shells out to the GBrain CLI (`gbrain call query '{"query": ...}'`)
3. GBrain hybrid-searches (keyword + vector, with query expansion) across the brain
4. Backend returns scored chunk results to the Dashboard
5. Dashboard shows results; clicking one loads full page detail
   (tags, outgoing links, backlinks) via /api/memory/page?slug=...
```

### User runs a maintenance job in Dashboard Skills Page (Phase 3)
```
1. Dashboard Frontend posts a job name to Backend (/api/skills/jobs)
2. Backend shells out to GBrain (`gbrain call submit_job ...`)
3. GBrain enqueues it on its Minions job queue and returns the job record
4. Backend emits a `skill_execution` Socket.IO event; Dashboard refreshes the job list
   (Note: PGLite has no worker daemon -- jobs stay "waiting" until run with
   `gbrain jobs work` against Postgres, or executed inline with `--follow`.)
```

## File Structure

```
smart-ai-router/
├── smart_ai_router/              (Original router code — untouched)
├── dashboard/                    (NEW)
│   ├── backend/                  (Phase 1+2)
│   │   ├── src/
│   │   │   ├── app.ts
│   │   │   ├── server.ts
│   │   │   ├── config/
│   │   │   ├── middleware/
│   │   │   ├── routes/
│   │   │   │   ├── chat.ts       (Phase 2)
│   │   │   │   ├── analytics.ts  (Phase 2)
│   │   │   │   ├── memory.ts     (Phase 3)
│   │   │   │   └── skills.ts     (Phase 3)
│   │   │   ├── services/
│   │   │   │   ├── smartRouterClient.ts  (Phase 2)
│   │   │   │   └── gbrainClient.ts        (Phase 3)
│   │   │   └── types/
│   │   ├── package.json
│   │   ├── tsconfig.json
│   │   └── .env.example
│   └── frontend/                 (Phase 1+2)
│       ├── src/
│       │   ├── App.tsx
│       │   ├── main.tsx
│       │   ├── lib/
│       │   │   ├── api.ts        (Phase 2)
│       │   │   └── socket.ts     (Phase 2)
│       │   ├── components/
│       │   │   └── Navigation.tsx
│       │   ├── pages/
│       │   │   ├── ChatPage.tsx        (Phase 2 — live)
│       │   │   ├── AnalyticsPage.tsx   (Phase 2 — live)
│       │   │   ├── MemoryPage.tsx      (Phase 3 — live)
│       │   │   ├── SkillsPage.tsx      (Phase 3 — live)
│       │   │   ├── UsersPage.tsx       (Phase 4 — stub)
│       │   │   └── SettingsPage.tsx
│       │   ├── index.css
│       │   └── main.tsx
│       ├── vite.config.ts
│       ├── tsconfig.json
│       ├── tailwind.config.js
│       └── package.json
├── GBRAIN_MAC_MINI_SETUP.md       (NEW — comprehensive setup guide)
├── GBRAIN_SETUP_COMPLETE.md       (NEW — installation status)
├── PROJECT_STATUS.md              (NEW — this file)
├── DASHBOARD_SETUP.md             (Phase 1 setup guide)
├── UNIFIED_DASHBOARD_PLAN.md      (Phase 1-5 plan)
└── .gitignore                     (Updated to include node_modules/)
```

## Running Everything

### Development Mode

**Terminal 1: Smart-AI-Router**
```bash
smart-ai-router
# Listens on http://localhost:8001
```

**No separate GBrain terminal needed** — the dashboard backend calls the
`gbrain` CLI directly as a subprocess per request. Just make sure `gbrain` is
on `PATH` (e.g. `export PATH="$HOME/.bun/bin:$PATH"`) before starting the
backend below.

**Terminal 2: Dashboard Backend**
```bash
cd dashboard/backend
npm run dev
# Listens on http://localhost:5050
```

**Terminal 3: Dashboard Frontend**
```bash
cd dashboard/frontend
npm run dev
# Opens at http://localhost:5173
```

Then visit **http://localhost:5173** in your browser.

### Port Summary
- `http://localhost:8001` — Smart-AI-Router
- `http://localhost:5050` — Dashboard Backend
- `http://localhost:5173` — Dashboard Frontend (Vite dev server)
- `~/.gbrain/brain.pglite` — GBrain Database (local file)

## What's Ready Now

✅ **Smart-AI-Router** — Already running, integrated with dashboard
✅ **Dashboard Phase 1** — Merged, structure complete
✅ **Dashboard Phase 2** — Merged, chat + analytics working
✅ **GBrain** — Installed, configured, CLI verified end-to-end
✅ **Dashboard Phase 3** — Memory + Skills pages wired to live GBrain data
✅ **Port conflict fixed** — Backend now uses 5050 instead of 5000

## What's Next (Phase 4)

⏳ **User Management** — Multi-user auth, per-user cost/memory scoping
⏳ **Conversation Persistence** — Save chat history to PostgreSQL
⏳ **Real Authentication** — JWT instead of open dev mode
⏳ **GBrain on Postgres** — Removes the PGLite single-writer lock for true
   concurrent access once multiple users are hitting Memory/Skills at once

## PRs and Branches

| PR | Status | Description |
|----|--------|-------------|
| #94 | ✅ Merged | Phase 1: Dashboard shell |
| #96 | ✅ Merged | Phase 2: Smart-AI-Router integration + GBrain setup docs |
| Phase 3 | 🔄 In review | GBrain memory/skills integration (this branch) |

## Configuration Files

### Backend: `dashboard/backend/.env.example`
```bash
NODE_ENV=development
PORT=5050                              # Changed from 5000
SMART_ROUTER_URL=http://localhost:8001
GBRAIN_BIN=gbrain                      # CLI on PATH -- no URL, GBrain has no HTTP API
DATABASE_URL=postgresql://...          # For Phase 4 (multi-user, conversation history)
JWT_SECRET=dev-secret-key-change-me
CORS_ORIGIN=http://localhost:5173
```

### GBrain: `~/.gbrain/config.json`
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

## Known Issues

- **PGLite single-writer lock**: only one `gbrain` CLI process can access the
  brain at a time. The dashboard backend serializes its own calls, but a
  manual `gbrain` command run at the same time can still cause a ~30s stall
  on the other side. Not an issue once GBrain is migrated to Postgres.
- **No PGLite job worker**: `gbrain jobs work` refuses to run against PGLite
  ("Worker daemon requires Postgres"), so jobs submitted via the Skills page
  stay `waiting` forever until either Postgres is set up or the job is run
  inline (`gbrain jobs submit <name> --follow`).

## Next Decision

Phase 3 is implemented on this branch. Next up is Phase 4 (multi-user
support) — or migrating GBrain to Postgres first, to remove the PGLite
concurrency/worker limitations above before more people start using it.
