# Phase 4: Multi-User Infrastructure

## Status: Foundation Complete, Feature Implementation In Progress

### What Was Built (Backend Auth + Database Layer)

**Database Schema** (`dashboard/backend/migrations/001_initial_schema.sql`):
- `users` — multi-user accounts with email, password hash, role (admin/user)
- `sessions` — track issued JWTs with expiration and revocation
- `api_keys` — programmatic access (alternative auth method)
- `conversations` — chat history grouped by user + conversation ID
- `chat_messages` — individual messages with routing metadata, token counts, cost
- `cost_tracking` — denormalized cost data by model/domain/complexity for analytics
- `gbrain_access` — scope which users can access which knowledge bases (future)
- `audit_log` — compliance/debugging

**Services** (`dashboard/backend/src/services/`):
- `userService.ts` — user CRUD, password hashing (bcrypt), JWT token generation/verification, API key management
- `database.ts` — PostgreSQL pool management, migration runner, query wrapper

**Middleware** (`dashboard/backend/src/middleware/auth.ts`):
- `authRequired` — enforce JWT or API key; attach user to `req.user` and `req.userId`
- `adminRequired` — role-based access control (admin only)
- `authOptional` — attach user if authenticated, but don't error if not

**Routes** (`dashboard/backend/src/routes/auth.ts`):
- `POST /api/auth/register` — create new account (email, name, password)
- `POST /api/auth/login` — authenticate, return JWT token + user info
- `POST /api/auth/api-keys` — generate an API key for programmatic access
- `GET /api/auth/api-keys` — list the user's API keys
- `DELETE /api/auth/api-keys/:id` — revoke an API key
- `GET /api/auth/me` — get current logged-in user info

**Configuration**:
- Updated `.env.example` to include `DATABASE_URL` (PostgreSQL connection string)
- All dependencies installed: `pg`, `jsonwebtoken`, `bcryptjs`, `@types/*`
- TypeScript compilation clean

### What Remains (Next: Feature Implementation)

#### 1. Users Management Route (`phase4-users-route`)
Build `GET /api/users` (admin only) for user directory, with:
- Paginated list of all users
- Per-user total cost, message count, last activity
- Admin actions: role change, account disable, audit trail

#### 2. Conversation History (`phase4-conversation-history`)
Wire the chat route to:
- Save each message to `chat_messages` table (role, content, model_used, tokens, cost)
- Associate with a conversation (auto-create if none exists)
- Frontend: fetch conversation history on page load; append messages as they arrive
- Eventually: switch from in-memory to database-backed chat history

#### 3. Per-User Cost Scoping (`phase4-cost-scoping`)
Update analytics:
- Change `GET /api/analytics/usage` to return only the logged-in user's costs (not global)
- Add user breakdown to `GET /api/analytics/usage` (if admin: show all users; if user: only self)
- Frontend: add tabbed view in AnalyticsPage for per-user breakdown

#### 4. Frontend Auth (`phase4-frontend-auth`)
Build UI + local storage:
- Login page (email + password)
- Register page (email, name, password)
- Auth guard: redirect to login if not authenticated
- Store JWT in `localStorage` / `sessionStorage`
- Axios interceptor to attach JWT to all requests
- API key page (list, create, revoke) for power users

### Architecture Notes

**PostgreSQL Required**: Phase 4 needs a real PostgreSQL server (not the current PGLite-backed GBrain). GBrain can stay on PGLite for single-user use, but the dashboard needs Postgres for concurrent multi-user access.

**Database Migrations**: Run on server startup via `runMigrations()` in `server.ts`. Add new migrations as `.sql` files in `dashboard/backend/migrations/`, numbered sequentially (e.g., `002_add_conversation_titles.sql`).

**JWT Strategy**: 
- Token issued on login, stored in `sessions` table for potential revocation
- 7-day expiration (configurable)
- Can also use API keys (app-to-app) without JWT

**Cost Tracking**:
- Dashboard keeps denormalized `cost_tracking` table for fast analytics queries
- Smart-AI-Router (`POST /v1/chat/completions`) populates usage, dashboard inserts cost rows
- Per-user scoping: query `cost_tracking WHERE user_id = $1 AND created_at > ...`

### Running the Backend

**Setup**:
```bash
# Install PostgreSQL locally or point to existing instance
export DATABASE_URL="postgresql://user:password@localhost:5432/dashboard"

# Run migrations automatically on startup
cd dashboard/backend
npm run dev
```

**Creating the First Admin User** (via curl or psql):
```bash
# TODO: Add a CLI tool or first-run script to bootstrap an admin user
# For now, can be added via direct SQL or a temporary endpoint
```

### Testing

After implementing conversation history and cost scoping, test:
1. Register two users via `POST /api/auth/register`
2. Login as each, get JWT tokens
3. Send chat messages (should be saved per-user)
4. Check analytics per-user scoping
5. Admin user can see all users' data; regular user sees only theirs

### Known Gaps

- No password reset / forgot-password flow yet
- No email verification for registration
- No session management UI (logout, active sessions, revoke from everywhere)
- No audit trail UI (for compliance)
- GBrain `gbrain_access` scoping not yet enforced (all users currently access all brains)

These are Phase 5 (Polish) items.
