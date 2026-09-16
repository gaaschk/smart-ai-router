# Quick Start — Unified Dashboard

## For Developers (5-minute local setup)

### Prerequisites
- Node.js 18+, Bun 1.0+, PostgreSQL 14+, Python 3.10+
- LLM API keys (OpenAI, Claude, etc.)

### Steps

1. **Start Smart-AI-Router** (terminal 1)
   ```bash
   cd /path/to/smart-ai-router
   python3 -m smart_ai_router.cli.api
   # Runs on http://localhost:8001
   ```

2. **Start Dashboard Backend** (terminal 2)
   ```bash
   cd dashboard/backend
   npm install
   npm run setup       # Creates DB & admin user (interactive)
   npm run dev
   # Runs on http://localhost:5050
   ```

3. **Start Dashboard Frontend** (terminal 3)
   ```bash
   cd dashboard/frontend
   npm install
   npm run dev
   # Runs on http://localhost:5173
   ```

4. **Open Browser**
   - http://localhost:5173
   - Sign in with admin credentials you created
   - Start chatting, viewing analytics, searching memory!

---

## For Mac Mini Deployment (Production)

See [DEPLOYMENT_GUIDE.md](./DEPLOYMENT_GUIDE.md) for full instructions.

Quick summary:
1. Install Node, Bun, PostgreSQL, Python
2. Configure `.env` files with API keys & database URL
3. Run `npm run setup` to initialize database
4. Start services (or use launchd for auto-start)
5. Access via http://your-mac-mini-ip:5173

---

## What You Get

✨ **Chat with AI**
- Messages routed through smart-ai-router for cost optimization
- Real-time display of model selection, cost, complexity

📊 **Cost Analytics**
- Per-user usage tracking
- Breakdown by model, date, domain
- Monthly cost trends

🧠 **Memory Search**
- Hybrid (vector + keyword) search across your GBrain knowledge base
- Quick page detail view with tags & backlinks

⚡ **Skills & Jobs**
- Run background maintenance jobs (sync, embed, lint, etc.)
- View integration recipes (email, calendar, voice, etc.)

👥 **Multi-User Management** (Admin)
- Create/manage user accounts
- Track per-user usage & costs
- Set roles (admin/user)

---

## Environment Variables

### Backend (.env)
```bash
NODE_ENV=production
PORT=5050
DATABASE_URL=postgresql://user:pass@localhost:5432/dashboard
SMART_ROUTER_URL=http://localhost:8001
GBRAIN_BIN=gbrain
AUTH_JWT_SECRET=your-secret-key-here
```

### Smart-AI-Router (.env in smart-ai-router/)
```bash
OPENAI_API_KEY=sk-...
CLAUDE_API_KEY=sk-ant-...
GEMINI_API_KEY=AIzaSy...
# Add other LLM provider keys
```

### Frontend (vite.config.ts)
- Proxies `/api` to `http://localhost:5050` (dev)
- Uses Socket.IO for real-time events

---

## Common Commands

```bash
# Dashboard Backend
npm run dev          # Start dev server (hot reload)
npm run build        # Build for production
npm start            # Run production build
npm run setup        # Initialize database & admin user
npm run typecheck    # TypeScript validation
npm run lint         # ESLint

# Dashboard Frontend
npm run dev          # Start dev server (hot reload)
npm run build        # Build for production
npm run preview      # Preview production build locally
npm run typecheck    # TypeScript validation
npm run lint         # ESLint

# GBrain
gbrain init          # Initialize brain
gbrain new-context   # Create context
gbrain list          # List pages
gbrain call get_stats  # Get brain statistics
gbrain serve         # Start MCP server (for editors)

# Smart-AI-Router
python3 -m smart_ai_router.cli.api  # Start API server
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Port 5050 already in use | `PORT=5051 npm start` |
| DB connection failed | Check PostgreSQL is running, DATABASE_URL is correct |
| GBrain timeout | Increase `GBRAIN_CLI_TIMEOUT_MS` in .env |
| Smart-AI-Router 401 | Check API keys in smart-ai-router .env |
| Frontend blank/errors | Check browser console & backend logs |
| Stuck on login | Database migration may not have run; try `npm run setup` again |

---

## Next Steps

- Read [DEPLOYMENT_GUIDE.md](./DEPLOYMENT_GUIDE.md) for production setup
- Check [PROJECT_STATUS.md](./PROJECT_STATUS.md) for architecture overview
- Explore GBrain at https://github.com/garrytan/gbrain
- Explore Smart-AI-Router at https://github.com/gaaschk/smart-ai-router

---

**Ready to go!** 🚀
