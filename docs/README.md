# smart-ai-router documentation

A vendor-agnostic LLM capability router. Start at the [project README](../README.md)
for what it is and how to install it; this is the reference.

- **[Using it from a client](clients.md)** — Claude Code, Cursor, Codex CLI, aider,
  the OpenAI SDKs, and what to do when a client won't work.
- **[API](api.md)** — every endpoint: the OpenAI-compatible proxy, keys, files,
  web search, voice, conversations, reports, self-update.
- **[How routing decisions work](routing.md)** — the prompt profile, the two-speed
  classifier, model profiles, capability flags, prompt caching, cost tiers.
- **[Configuration](configuration.md)** — settings, the store, environment variables.
- **[Service management](operations.md)** — the macOS LaunchAgent, Pull & Restart.
