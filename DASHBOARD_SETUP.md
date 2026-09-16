# Unified Dashboard - Phase 1 Setup Guide

## Overview

Phase 1 of the unified dashboard is now ready to set up. This gives you:
- ✅ Backend server with Express and Socket.IO
- ✅ Frontend shell with React and TailwindCSS
- ✅ Navigation and page structure
- ✅ Configuration and environment setup
- ✅ Database and API foundation

## Project Structure

```
dashboard/
├── backend/
│   ├── src/
│   │   ├── config/
│   │   ├── middleware/
│   │   ├── types/
│   │   ├── app.ts
│   │   └── server.ts
│   ├── package.json
│   ├── tsconfig.json
│   └── .env.example
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   ├── components/
│   │   ├── App.tsx
│   │   ├── main.tsx
│   │   └── index.css
│   ├── package.json
│   ├── tsconfig.json
│   ├── vite.config.ts
│   └── index.html
└── docker-compose.yml (coming next)
```

## Getting Started

### 1. Install Backend Dependencies

```bash
cd dashboard/backend
npm install
cp .env.example .env
```

Update `.env` with your configuration:
```bash
NODE_ENV=development
PORT=5050
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/dashboard
SMART_ROUTER_URL=http://localhost:8001
GBRAIN_URL=http://localhost:8002
JWT_SECRET=your-development-secret
CORS_ORIGIN=http://localhost:5173
```

### 2. Install Frontend Dependencies

```bash
cd dashboard/frontend
npm install
```

### 3. Start Development Servers

**Terminal 1 - Backend:**
```bash
cd dashboard/backend
npm run dev
# Runs on http://localhost:5050
```

**Terminal 2 - Frontend:**
```bash
cd dashboard/frontend
npm run dev
# Runs on http://localhost:5173
```

**Terminal 3 - Smart-AI-Router:**
```bash
smart-ai-router
# Runs on http://localhost:8001
```

**Terminal 4 - GBrain:**
```bash
gbrain serve
# Runs on http://localhost:8002
```

## What's Included

### Backend Features
- ✅ Express.js server with TypeScript
- ✅ Socket.IO for real-time updates
- ✅ Configuration management
- ✅ Error handling middleware
- ✅ Request logging
- ✅ Health check endpoint
- ✅ Environment variable setup
- ✅ Database configuration (PostgreSQL)

### Frontend Features
- ✅ React with TypeScript
- ✅ TailwindCSS for styling
- ✅ Page shell structure
- ✅ Navigation component
- ✅ Responsive layout
- ✅ Page placeholders for all features

### Pages Included
1. **Chat** - Message interface with routing decisions
2. **Analytics** - Cost tracking and statistics
3. **Memory** - Knowledge search interface
4. **Skills** - GBrain skill library
5. **Users** - User management
6. **Settings** - Configuration interface

## Next Steps (Phase 2)

Phase 2 will implement:
- [ ] Smart-AI-Router integration
  - Chat endpoint connection
  - Routing decision display
  - Cost calculation
  - Model selection tracking
  
- [ ] Analytics implementation
  - Cost charts and trends
  - Model usage statistics
  - Request history
  - Savings calculations

- [ ] WebSocket integration
  - Real-time routing updates
  - Live cost tracking
  - User activity sync

## API Integration Points

The backend is ready to connect to:

### Smart-AI-Router (port 8001)
```
POST /v1/chat/completions
GET /health
GET /analytics/cost
GET /analytics/models
```

### GBrain (port 8002)
```
POST /query (memory search)
POST /ingest (save memory)
GET /skills (list skills)
POST /skills/{id}/execute (trigger skill)
```

## Database

The dashboard uses PostgreSQL with these tables:
- `users` - User accounts
- `sessions` - Active sessions
- `user_api_keys` - API key management
- `routing_decisions` - Routing history
- `skill_executions` - Skill execution logs

Create the database:
```bash
createdb dashboard
```

(Schema migrations will be added in Phase 2)

## Environment Variables

All configurable via `.env`:

```
# Server
NODE_ENV=development|production
PORT=5050
LOG_LEVEL=debug|info|warn|error

# Database
DATABASE_URL=postgresql://user:pass@host/db

# Integrations
SMART_ROUTER_URL=http://localhost:8001
GBRAIN_URL=http://localhost:8002

# Auth
JWT_SECRET=your-secret-key
SESSION_TIMEOUT=3600000

# CORS
CORS_ORIGIN=http://localhost:5173

# WebSocket
WS_PING_INTERVAL=30000
WS_PING_TIMEOUT=5000
```

## Testing the Setup

1. **Backend Health:**
   ```bash
   curl http://localhost:5050/health
   ```
   Should return: `{"status":"ok", ...}`

2. **Frontend Load:**
   Visit http://localhost:5173 in browser
   Should show dashboard shell with navigation

3. **Navigation:**
   Click through pages - all should load without errors

## Troubleshooting

### Port Already in Use
```bash
# Find process using port 5050
lsof -i :5050
# Kill it
kill -9 <PID>
```

### PostgreSQL Connection Error
```bash
# Ensure PostgreSQL is running
brew services list | grep postgres

# Or start it
brew services start postgresql
```

### Node Modules Issues
```bash
# Clear and reinstall
rm -rf node_modules package-lock.json
npm install
```

### Environment Variables Not Loading
```bash
# Verify .env exists in backend/
# Check that NODE_ENV is set
echo $NODE_ENV
```

## Architecture Diagram

```
User Browser (http://localhost:5173)
         │
         ├─→ Frontend (React, TailwindCSS)
         │   ├── Navigation
         │   ├── Chat Page
         │   ├── Analytics Page
         │   ├── Memory Page
         │   ├── Skills Page
         │   ├── Users Page
         │   └── Settings Page
         │
         └─→ Backend API (http://localhost:5050)
             ├── Express.js
             ├── Socket.IO (WebSocket)
             ├── PostgreSQL (Database)
             │
             ├─→ Smart-AI-Router (http://localhost:8001)
             │   └── Routing intelligence
             │
             └─→ GBrain (http://localhost:8002)
                 └── Knowledge management
```

## Development Workflow

1. **Make changes** to frontend (`src/pages/*.tsx`)
2. **Frontend auto-reloads** via Vite
3. **Make changes** to backend (`src/**/*.ts`)
4. **Backend auto-reloads** via ts-node-dev
5. **Test in browser** at http://localhost:5173

## Building for Production

### Frontend:
```bash
cd dashboard/frontend
npm run build
# Output in dist/
```

### Backend:
```bash
cd dashboard/backend
npm run build
# Output in dist/
npm run start
```

## What's Next

After Phase 1 is working:
- **Phase 2:** Implement smart-router integration
- **Phase 3:** Implement gbrain integration
- **Phase 4:** Add multi-user features
- **Phase 5:** Production deployment

## Questions or Issues?

Check the main `UNIFIED_DASHBOARD_PLAN.md` for detailed implementation strategy.

---

**Status:** Phase 1 setup complete ✅
**Next:** Start Phase 2 (Smart-router integration)
