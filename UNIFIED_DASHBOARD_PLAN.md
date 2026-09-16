# Unified Dashboard Implementation Plan

## Overview

Build a single unified dashboard that combines:
- Smart AI Router routing intelligence & cost analytics
- GBrain knowledge management & skill capabilities  
- User management & multi-user support
- Seamless integration on Mac Mini

## Architecture

```
Mac Mini (Server)
├── PostgreSQL + pgvector (shared knowledge)
├── GBrain (multi-user, port 8002)
├── Smart-AI-Router (routing, port 8001)
└── Unified Dashboard (port 3000) ← NEW

Users' Devices
├── Web Browser → Dashboard (localhost or mac-mini.local:3000)
├── CLI clients → smart-ai-router (8001) + gbrain (8002)
└── IDE plugins → Same endpoints
```

## Tech Stack

**Backend (Node.js/TypeScript):**
- Express.js (HTTP server)
- Socket.io (real-time updates)
- TypeORM (database)
- PostgreSQL client

**Frontend (React):**
- React 18
- TypeScript
- TailwindCSS (styling)
- Tanstack Query (data fetching)
- Socket.io client (real-time)
- Recharts (analytics charts)

**Integration:**
- HTTP client for smart-ai-router API
- HTTP client for gbrain API
- User session management
- API key management

## Implementation Phases

### Phase 1: Core Dashboard Shell (Week 1)
**Goal:** Working dashboard that connects to both services

- [ ] Project setup (React + TypeScript)
- [ ] Dashboard shell with navigation
- [ ] User authentication/session management
- [ ] API integration layer (smart-router + gbrain)
- [ ] Real-time connection setup

**Deliverable:** Blank dashboard with working backend connections

### Phase 2: Smart Router Integration (Week 1-2)
**Goal:** Display routing intelligence and cost analytics

- [ ] Chat interface with routing decisions
- [ ] Cost analytics dashboard
- [ ] Model selection history
- [ ] Real-time routing logs
- [ ] User API key management

**Deliverable:** Full smart-router UI in dashboard

### Phase 3: GBrain Integration (Week 2-3)
**Goal:** Add memory and skill management

- [ ] Memory search interface
- [ ] Knowledge graph visualization
- [ ] Skill library and triggers
- [ ] Conversation history from gbrain
- [ ] Entity/relationship browser

**Deliverable:** Full gbrain capabilities in dashboard

### Phase 4: Multi-User Features (Week 3-4)
**Goal:** Support multiple concurrent users

- [ ] User management interface
- [ ] Per-user cost tracking
- [ ] User-scoped memory access
- [ ] Role-based access control
- [ ] Team collaboration features

**Deliverable:** Production-ready multi-user system

### Phase 5: Polish & Optimization (Week 4-5)
**Goal:** Production quality

- [ ] Performance optimization
- [ ] Error handling & recovery
- [ ] Comprehensive logging
- [ ] User onboarding
- [ ] Documentation

**Deliverable:** Deploy to production on Mac Mini

## File Structure

```
smart-ai-router/
├── dashboard/                 # NEW - Unified dashboard
│   ├── backend/
│   │   ├── src/
│   │   │   ├── app.ts         # Express app
│   │   │   ├── server.ts      # Server entry
│   │   │   ├── config/
│   │   │   │   └── index.ts
│   │   │   ├── services/
│   │   │   │   ├── smart-router-client.ts
│   │   │   │   ├── gbrain-client.ts
│   │   │   │   ├── auth.ts
│   │   │   │   └── user.ts
│   │   │   ├── routes/
│   │   │   │   ├── chat.ts
│   │   │   │   ├── analytics.ts
│   │   │   │   ├── memory.ts
│   │   │   │   ├── skills.ts
│   │   │   │   └── users.ts
│   │   │   ├── middleware/
│   │   │   │   ├── auth.ts
│   │   │   │   └── logging.ts
│   │   │   └── websocket/
│   │   │       └── handlers.ts
│   │   ├── tsconfig.json
│   │   ├── package.json
│   │   └── .env.example
│   │
│   ├── frontend/
│   │   ├── src/
│   │   │   ├── App.tsx
│   │   │   ├── index.css
│   │   │   ├── pages/
│   │   │   │   ├── ChatPage.tsx
│   │   │   │   ├── AnalyticsPage.tsx
│   │   │   │   ├── MemoryPage.tsx
│   │   │   │   ├── SkillsPage.tsx
│   │   │   │   ├── UsersPage.tsx
│   │   │   │   └── SettingsPage.tsx
│   │   │   ├── components/
│   │   │   │   ├── Navigation.tsx
│   │   │   │   ├── ChatInterface.tsx
│   │   │   │   ├── RoutingDecision.tsx
│   │   │   │   ├── CostChart.tsx
│   │   │   │   ├── MemorySearch.tsx
│   │   │   │   ├── SkillLibrary.tsx
│   │   │   │   └── UserSelector.tsx
│   │   │   ├── hooks/
│   │   │   │   ├── useChat.ts
│   │   │   │   ├── useAnalytics.ts
│   │   │   │   ├── useMemory.ts
│   │   │   │   └── useUser.ts
│   │   │   ├── services/
│   │   │   │   └── api.ts
│   │   │   └── types/
│   │   │       └── index.ts
│   │   ├── tsconfig.json
│   │   ├── package.json
│   │   └── vite.config.ts
│   │
│   └── docker-compose.yml     # For local development

├── smart_ai_router/           # Existing code
│   ├── api/
│   │   ├── proxy.py           # Enhance with dashboard integration
│   │   └── ...
│   └── ...

└── docs/
    └── DASHBOARD_GUIDE.md     # User documentation
```

## Key Features by Section

### Chat Interface
- Send message → smart-ai-router for routing
- See which model was selected and why
- View cost of each request
- Save conversations to gbrain
- Search past conversations

### Analytics Dashboard
- Real-time cost tracking
- Model usage statistics
- Routing decision pie charts
- Cost trends over time
- Per-user cost breakdown

### Memory Management
- Full-text search of stored knowledge
- Knowledge graph visualization
- Entity browser (people, companies, topics)
- Recent memory additions
- Manual memory addition/editing

### Skill Library
- Browse 85+ gbrain skills
- Trigger skills from dashboard
- Skill configuration interface
- Execution history
- Skill-specific parameters

### User Management
- User list with roles
- Per-user cost tracking
- API key management
- Session monitoring
- User activity logs

### Settings
- Connection settings (smart-router, gbrain)
- Database configuration
- Display preferences
- Notification settings
- API key generation

## API Integration Layer

### Smart-AI-Router Client
```typescript
interface SmartRouterClient {
  chat(request: ChatRequest): Promise<ChatResponse>;
  getModels(): Promise<Model[]>;
  getRoutingHistory(): Promise<RoutingDecision[]>;
  getCostAnalytics(timeRange: TimeRange): Promise<CostData>;
  getUserKeys(): Promise<ApiKey[]>;
}
```

### GBrain Client
```typescript
interface GBrainClient {
  query(q: string): Promise<SearchResult[]>;
  triggerSkill(skillId: string, params: any): Promise<SkillResult>;
  getMemory(userId: string): Promise<MemoryItem[]>;
  saveMemory(content: string, metadata: any): Promise<MemoryItem>;
  getSkills(): Promise<Skill[]>;
  getEntities(): Promise<Entity[]>;
}
```

## Real-Time Features (WebSocket)

```typescript
// Server sends real-time updates to connected clients
onRequest() → broadcast routing decision
onComplete() → broadcast cost and result
onError() → broadcast error with recovery
onMemoryChange() → broadcast knowledge updates
onSkillExecution() → broadcast skill status
```

## Database Schema (PostgreSQL)

```sql
-- Dashboard-specific tables (in addition to gbrain's existing tables)

CREATE TABLE dashboard_sessions (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL,
  token VARCHAR NOT NULL UNIQUE,
  created_at TIMESTAMP DEFAULT NOW(),
  last_activity TIMESTAMP DEFAULT NOW(),
  expires_at TIMESTAMP NOT NULL
);

CREATE TABLE user_api_keys (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL,
  key_hash VARCHAR NOT NULL,
  name VARCHAR NOT NULL,
  last_used TIMESTAMP,
  created_at TIMESTAMP DEFAULT NOW(),
  revoked_at TIMESTAMP
);

CREATE TABLE routing_decisions (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL,
  model_requested VARCHAR NOT NULL,
  model_selected VARCHAR NOT NULL,
  cost DECIMAL(10, 6) NOT NULL,
  reasoning TEXT NOT NULL,
  timestamp TIMESTAMP DEFAULT NOW(),
  FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE skill_executions (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL,
  skill_id VARCHAR NOT NULL,
  parameters JSONB NOT NULL,
  status VARCHAR NOT NULL, -- pending, running, completed, failed
  result JSONB,
  error TEXT,
  started_at TIMESTAMP DEFAULT NOW(),
  completed_at TIMESTAMP,
  FOREIGN KEY (user_id) REFERENCES users(id)
);
```

## Environment Configuration

**Mac Mini deployment (.env):**
```bash
# Backend
NODE_ENV=production
PORT=3000
DATABASE_URL=postgresql://user:pass@localhost:5432/gbrain_dashboard

# Smart-AI-Router
SMART_ROUTER_URL=http://localhost:8001
SMART_ROUTER_API_KEY=optional

# GBrain
GBRAIN_URL=http://localhost:8002
GBRAIN_DATABASE_URL=postgresql://user:pass@localhost:5432/gbrain_shared

# Authentication
JWT_SECRET=your-secret-key-here
SESSION_TIMEOUT=3600

# Frontend
VITE_API_URL=http://localhost:3000
VITE_WS_URL=ws://localhost:3000
```

## Deployment on Mac Mini

```bash
# 1. Install dependencies
cd dashboard/backend && npm install
cd ../frontend && npm install
cd ../..

# 2. Build frontend
cd dashboard/frontend
npm run build
cd ../..

# 3. Start all services (one command)
# Create start-all.sh
#!/bin/bash
smart-ai-router &
gbrain serve &
cd dashboard/backend && npm run start &
wait
```

## Development Workflow

```bash
# Terminal 1: Backend
cd dashboard/backend
npm run dev  # Runs on 5000 with hot reload

# Terminal 2: Frontend  
cd dashboard/frontend
npm run dev  # Runs on 5173 with hot reload

# Terminal 3: Smart-AI-Router
smart-ai-router

# Terminal 4: GBrain
gbrain serve
```

## Testing Strategy

- **Unit tests:** Services and utilities
- **Integration tests:** API endpoints
- **E2E tests:** Full user workflows
- **Performance tests:** Dashboard under load
- **Multi-user tests:** Concurrent user scenarios

## Success Criteria

✅ Dashboard loads and connects to both services
✅ Users can chat and see routing decisions
✅ Cost analytics display correctly
✅ Memory search works
✅ Skills can be triggered
✅ Multiple users can use simultaneously
✅ Real-time updates work via WebSocket
✅ User management functional
✅ All data properly isolated per user
✅ Performance acceptable (< 2s load time)

## Rollout Plan

**Week 1:** Deploy to Mac Mini, single user test
**Week 2:** Multi-user testing with team
**Week 3:** Gather feedback, iterate
**Week 4:** Production deployment

## Documentation

- **User Guide:** How to use dashboard features
- **Admin Guide:** Managing users and settings
- **API Reference:** Dashboard API endpoints
- **Architecture Guide:** System design overview
- **Troubleshooting:** Common issues and solutions

## Next Steps

1. Start Phase 1 implementation (shell + setup)
2. Build backend API layer
3. Build React frontend
4. Integrate with smart-ai-router
5. Integrate with gbrain
6. Deploy to Mac Mini
