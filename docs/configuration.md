# Configuration

All configuration is stored in `~/.smart_ai_router.db` (SQLite). You can manage it via:
- The setup wizard: `smart-ai-router setup`
- The REST API: `PUT /api/providers/{name}`
- The web UI at `http://localhost:8001/`

## Environment variables

Most application *behavior* (the ⚙ marked rows below) is now managed from the
**Settings** page in the web UI — persisted in the database, applied live with no
restart. For those, the environment variable is only a fallback: the effective
value is **DB (set in the UI) → environment variable → built-in default**. The
unmarked rows are intrinsic to a machine/deployment (port, paths, the bootstrap
admin secret) and stay environment-only.

| Variable | Default | Purpose |
|----------|---------|---------|
| `SMART_ROUTER_PORT` | `8001` | Port the server listens on |
| `SMART_ROUTER_LABEL` | `com.smart-ai-router` | launchd service label. Only a fallback — the job is normally found by reading the installed plists ([Pull & Restart](operations.md#pull--restart)) |
| `SMART_ROUTER_URL` | `http://$(hostname):8001` | Used by `claudish-smart` to find the router |
| `SMART_ROUTER_API_KEYS` | *(empty)* | Comma-separated **admin** keys — unrestricted access, and the only keys allowed to manage per-user keys. Empty (with no DB keys) leaves the router open. |
| `SMART_ROUTER_OPTIONAL` | `0` | If `1`, `claudish-smart` falls back to plain claudish when unreachable |
| `SMART_ROUTER_CLASSIFIER_MODEL` ⚙ | `llama3.1:8b` | Primary (local Ollama) model that profiles each prompt. Prefer a small **non-reasoning** instruct model — thinking models burn the classifier's tiny output budget before emitting JSON. Empty string disables the local step. Unlike the two settings below it does **not** accept `auto` — routing triage is measured to pick the worst available triage model (see [The router's own calls](routing.md#the-routers-own-calls)), so the word is rejected instead of silently reinterpreted. Before changing it, bake off the candidate: `python scripts/bakeoff_classifier.py <model>` scores it on the real code path, and the script's docstring records what the current default was measured against. |
| `SMART_ROUTER_CLASSIFIER_FALLBACK` ⚙ | `nvidia/nemotron-nano-9b-v2:free` | Free OpenRouter model tried if the local classifier fails. Only used when an OpenRouter key is configured. Empty string disables it. |
| `SMART_ROUTER_CLASSIFIER_REFINE_MODEL` ⚙ | `auto` | Second-pass profiler, run only on prompts the local classifier flags as high-stakes, multi-specialist, or frontier-depth (see [Two-speed classification](routing.md#two-speed-classification)). `auto` routes it (see [The router's own calls](routing.md#the-routers-own-calls)); a model name pins it; empty string disables the second pass. |
| `SMART_ROUTER_MODEL_PROFILER_MODEL` ⚙ | `auto` | Model asked to rate each *model's* per-field shape (see [Refining model profiles](routing.md#refining-model-profiles-with-an-llm)). Off the request path — only runs when Refine is triggered. `auto` routes it, a model name pins it, empty string disables refinement. |
| `SMART_ROUTER_MODEL_PROFILER_LIMIT` ⚙ | `40` | Default ceiling on models rated per Refine run, cheapest first. Also caps the pass that runs after a sync. |
| `SMART_ROUTER_MODEL_PROFILER_ON_SYNC` ⚙ | `1` | Profile the models each sync adds (and any whose description was rewritten), so a new model doesn't route on a cue-table guess. Never re-profiles a model that only changed price or benchmark scores. |
| `SMART_ROUTER_WEB_SEARCH` ⚙ | `1` | Search the web when the profile says the prompt turns on current facts (see [Web search](api.md#web-search)). ~$0.007 per searched request, OpenRouter models only. |
| `SMART_ROUTER_WEB_SEARCH_MAX_RESULTS` ⚙ | `5` | Results put in front of the model. Up to 10 are in the per-search price; past 10 costs $0.001 each. |
| `SMART_ROUTER_MODEL_DENYLIST` ⚙ | *(empty)* | Comma-separated, case-insensitive substrings of model names to never route to (e.g. a broken local model). |
| `SMART_ROUTER_AGENT_DENYLIST` ⚙ | *(empty)* | Like the model denylist, but applied only in agent mode (models that advertise tools yet stall a tool-calling loop). |
| `SMART_ROUTER_WORKSPACE_DIR` | `~/.smart_ai_router_workspaces` | Root holding each user's private agent workspace (one subdir per identity). |
| `SMART_ROUTER_FILES_DIR` | `~/.smart_ai_router_files` | Root for uploaded/generated file blobs (metadata lives in SQLite). |
| `SMART_ROUTER_MAX_FILE_MB` ⚙ | `512` | Upload size ceiling in MB; larger uploads get `413`. |
| `SMART_ROUTER_OCR_MAX_PAGES` ⚙ | `10` | Max PDF pages rasterized for OCR text extraction. |
| `SMART_ROUTER_OCR_DPI` ⚙ | `150` | Rasterization resolution for OCR; higher is sharper but slower. |
| `SMART_ROUTER_ENABLE_BASH` ⚙ | `0` | If `1` (and `sandbox-exec` is present), the agent's `run_bash` tool is offered. Off by default — see the security note below. |
| `SMART_ROUTER_BASH_TIMEOUT_S` ⚙ | `30` | Wall-clock ceiling for a single `run_bash` call. |
| `SMART_ROUTER_PUBLIC_CHAT` ⚙ | `0` | If `1`, keyless visitors may use the chat page — see [Public (anonymous) access](api.md#public-anonymous-access). Exposes your router, and your bill, to the public internet. |
| `SMART_ROUTER_PUBLIC_DAILY_BUDGET` ⚙ | `1.00` | Ceiling on total anonymous spend per UTC day (USD). Past it, anonymous traffic drops to free/local models. `0` = no paid spend at all. |
| `SMART_ROUTER_PUBLIC_MAX_TIER` ⚙ | `3` | Cost-tier ceiling for anonymous traffic while budget remains (see [Cost tiers](routing.md#cost-tiers)). |
| `SMART_ROUTER_PUBLIC_DEGRADED_MAX_TIER` ⚙ | `1` | Cost-tier ceiling once the daily cap is reached (`1` = free + local, `0` = local only). |
| `SMART_ROUTER_PUBLIC_MAX_OUTPUT_TOKENS` ⚙ | `1024` | Hard `max_tokens` ceiling for an anonymous request. |
| `SMART_ROUTER_PUBLIC_RL_MAX_REQ` ⚙ | `30` | Anonymous requests allowed per window, per IP and per session (`0` = no cap). |
| `SMART_ROUTER_PUBLIC_RL_WINDOW_S` ⚙ | `3600` | Length of that rolling window. |
| `SMART_ROUTER_PUBLIC_MAX_CONCURRENT` ⚙ | `4` | Anonymous requests in flight at once, deployment-wide (`0` = unlimited). |

Rows marked ⚙ are editable from the Settings page (env value is the fallback).

**Agent (filesystem) mode.** The chat UI's 🛠 Agent toggle lets a tool-capable
model read, write, and edit files in the caller's own workspace (and, when
enabled, run shell commands). It's gated by capability negotiation just like
vision — the toggle only lights up when a reachable in-scope model supports
function calling. Each authenticated identity gets its own path-jailed
workspace directory; the tools cannot escape it (`..`, absolute paths, and
symlink escapes are all rejected).

> **`run_bash` security.** Shell access is opt-in (`SMART_ROUTER_ENABLE_BASH=1`)
> and, on macOS, runs under a `sandbox-exec` (seatbelt) profile that **denies
> all network**, blocks reads of the server's home directory (so `.env` keys,
> `~/.ssh`, the SQLite DB, and *other users'* workspaces are unreadable), and
> confines writes to the caller's workspace. If `sandbox-exec` is unavailable
> the tool is simply not offered — the router never runs an unsandboxed shell.
> On a box behind a public tunnel, keep this off unless you understand the
> shared-kernel blast radius; the read/write tools need no such flag.

**Prompt classification** is a fallback chain, tried in order:

1. **Local** — `SMART_ROUTER_CLASSIFIER_MODEL` via the Ollama provider (fast, private, no rate limit).
2. **Free remote** — `SMART_ROUTER_CLASSIFIER_FALLBACK` via OpenRouter, only if an OpenRouter key is stored (a resilience backstop; free tier is rate-limited and sends prompts off-box).
3. **Keyword** — the built-in deterministic classifier.

Each LLM step is skipped if its model is unset or provider unavailable, and any failure (network error, timeout, unparseable output) advances to the next step. **Classification never blocks or fails a request.** The `X-Classifier` response header reports which step succeeded: `llm` (local), `llm-free` (OpenRouter), `keyword`, or `default` (empty prompt).

That resilience has a cost worth knowing about: a misconfigured classifier looks *exactly* like a working one from the outside. Every request still succeeds, just routed on a coarser judgment than you think you're using. Two things make it visible instead:

- The classifier that ran is recorded per request, and the Usage page shows the mix as **By classifier**. A healthy local deployment is nearly all `llm`; a column of `keyword` means the configured model isn't answering. That's the number to look at after changing the setting or the host's pulled models.
- Settings flags a pin that can't work — a name absent from the model catalog (a typo, or never pulled here), or a model flagged `reasoning`. It's an advisory, not a block: the catalog can legitimately lag a model you just pulled, and refusing to start on a stale catalog would be worse than the warning.

