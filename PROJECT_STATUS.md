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

### Phase 2: Smart-AI-Router Integration 🔄
- Backend chat endpoint wired to smart-ai-router
- Backend analytics endpoint pulling real usage data
- Frontend ChatPage and AnalyticsPage showing real data
- Backend default port changed from 5000 → 5050 (macOS conflict fix)
- Status: **READY TO MERGE** (PR #96, includes GBrain setup docs)

### Phase 3: GBrain Integration ⏳
- GBrain installed and configured (v0.18.2)
- GBrain pointing to smart-ai-router for LLM calls
- GBrain MCP server running (stdio mode)
- Brain database: PGLite with 15 sample pages
- Brain directory: ~/gbrain-memory/ (people, companies, concepts, etc.)
- **Next**: Wire dashboard Memory/Skills pages to GBrain
- Status: **READY FOR IMPLEMENTATION**

## Services Running on Mac Mini

| Service | Port | Purpose | Status |
|---------|------|---------|--------|
| Smart-AI-Router | 8001 | LLM routing/classification | Running |
| Dashboard Backend | 5050 | API for chat, analytics, memory, skills | Ready to start |
| Dashboard Frontend | 5173 | React UI (dev) | Ready to start |
| GBrain MCP | stdio | Knowledge management API | Ready (use with dashboard backend) |
| GBrain Brain DB | PGLite | Embedded Postgres (~/. gbrain/brain.pglite) | Running |

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
1. Dashboard Frontend sends search query to Backend (/api/memory/search)
2. Backend calls GBrain MCP Server (search + synthesis)
3. GBrain hybrid-searches (keyword + vector) across ~/gbrain-memory/
4. GBrain calls Smart-AI-Router (/v1/chat/completions) to synthesize answer with sources
5. Smart-AI-Router picks optimal model for synthesis
6. GBrain returns synthesized answer with citations
7. Backend returns to Dashboard, which shows answer + sources
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
│   │   │   │   └── analytics.ts  (Phase 2)
│   │   │   ├── services/
│   │   │   │   └── smartRouterClient.ts  (Phase 2)
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
│       │   │   ├── MemoryPage.tsx      (Phase 3 — stub)
│       │   │   ├── SkillsPage.tsx      (Phase 3 — stub)
│       │   │   ├── UsersPage.tsx       (Phase 3 — stub)
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

**Terminal 2: GBrain MCP Server**
```bash
gbrain serve
# Connects via stdio to the dashboard backend
```

**Terminal 3: Dashboard Backend**
```bash
cd dashboard/backend
npm run dev
# Listens on http://localhost:5050
```

**Terminal 4: Dashboard Frontend**
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
✅ **Dashboard Phase 2** — Ready to merge, chat + analytics working
✅ **GBrain** — Installed, configured, MCP server ready
✅ **Port conflict fixed** — Backend now uses 5050 instead of 5000

## What's Next (Phase 3)

⏳ **Dashboard Memory Page** — Wire to GBrain search API
⏳ **Dashboard Skills Page** — Wire to GBrain skill discovery + execution
⏳ **Dashboard Entities Page** — Wire to GBrain knowledge graph
⏳ **Conversation Persistence** — Save chat history to PostgreSQL
⏳ **Real Authentication** — JWT instead of open dev mode

## PRs and Branches

| PR | Status | Description |
|----|--------|-------------|
| #94 | ✅ Merged | Phase 1: Dashboard shell |
| #96 | 🔄 Ready | Phase 2: Smart-AI-Router integration + GBrain setup docs |
| Phase 3 | ⏳ Pending | GBrain memory/skills/entities integration |

## Configuration Files

### Backend: `dashboard/backend/.env.example`
```bash
NODE_ENV=development
PORT=5050                              # Changed from 5000
SMART_ROUTER_URL=http://localhost:8001
GBRAIN_URL=http://localhost:8002      # Not used in Phase 2; will be used in Phase 3
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

None currently. The setup is clean and ready.

## Next Decision

**Do you want to:**
1. ✅ **Merge PR #96** (Phase 2) now — then start Phase 3?
2. ⏳ **Wait** — review the PR first?
3. 🚀 **Jump to Phase 3** — start wiring Memory/Skills pages to GBrain?

Recommend: Merge #96, then proceed to Phase 3.
