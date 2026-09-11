# API

The router exposes a REST API at `http://localhost:8001/api`:

## Routing

```bash
# Classify + route (returns the chosen model)
curl -X POST http://localhost:8001/api/route \
  -H 'Content-Type: application/json' \
  -d '{"domain":"coding","complexity":"moderate","needs_tools":true}'
```

## OpenAI-compatible proxy

```bash
# Full chat completions proxy — classifies the prompt, routes, and forwards
curl -X POST http://localhost:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer any-value' \
  -d '{
    "model": "smart-worker",
    "messages": [{"role":"user","content":"Fix this Python bug"}],
    "stream": true
  }'
```

The `model` field controls routing behavior. Both modes classify the prompt and
route on the resulting profile; they differ only in which models are candidates:
- `smart-orchestrator` — Claude models only (for reliable tool-calling), cheapest that qualifies. A mechanical turn can still land on Haiku; a hard one escalates to Opus.
- `smart-worker` or anything else — every model in scope, cheapest that qualifies

Response headers include routing metadata:
- `X-Routed-Model` — the actual model used
- `X-Domain` — classified domain
- `X-Complexity` — classified complexity
- `X-Escalated` — `true` if the task was escalated to a premium model
- `X-User` — the authenticated user the request was attributed to (empty in open/no-auth mode)
- `X-Dropped-Params` — request params the routed model couldn't take (see [Params vs. the pick](routing.md#params-vs-the-pick)); empty when nothing was dropped
- `X-Cache-Breakpoints` — how many prompt-cache markers this request was sent with (see [Prompt caching](routing.md#prompt-caching)); `0` for a local model, a first turn, or a short prompt
- `X-Web-Search` — whether the answer was searched (see [Web search](#web-search)); `false` covers "the prompt didn't need it" and "the routed model can't search", which is why it's reported at all
- `X-Classifier` — which classifier produced the profile: `llm`, `llm-free`, `llm-refined`, `keyword`, or `default`

`GET /v1/models` lists those two names and nothing else. Editors that speak only
OpenAI won't offer a model picker until that endpoint answers, and listing the
catalog there would promise a choice that doesn't exist — the `model` you send is
overwritten with the router's pick. `GET /api/models` is the real catalog, with
capability flags and prices.

Per-client setup lives in [Using it from a client](clients.md#using-it-from-a-client).

## API keys (per-user auth)

Authentication is optional until at least one key exists. There are two kinds of key:

- **Admin keys** — set via the `SMART_ROUTER_API_KEYS` env var (comma-separated). They authenticate with full, unrestricted access and are the only keys allowed to manage other keys. Use one to bootstrap.
- **Per-user keys** — minted through the API and stored (hashed) in SQLite. Each carries a `user` identity, so requests can be attributed in the usage log, and each can be revoked or rotated independently without touching anyone else's key or redeploying.

The wire protocol is unchanged: every client still sends `Authorization: Bearer <key>`, so `claudish-smart` and any OpenAI-compatible client work as-is.

```bash
# Mint a per-user key (admin only). The plaintext key is returned ONCE —
# only its SHA-256 hash is stored, so save it now; it can never be re-shown.
curl -X POST http://localhost:8001/api/keys \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"user":"alice"}'
# → {"user":"alice","key_prefix":"sk-smart-a1b2c3","key":"sk-smart-…","enabled":true, ...}

# List keys (metadata only — never the secret)
curl http://localhost:8001/api/keys -H "Authorization: Bearer $ADMIN_KEY"

# Revoke (disable) a key by its prefix — takes effect immediately, no redeploy
curl -X PUT http://localhost:8001/api/keys/sk-smart-a1b2c3/enabled \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' -d '{"enabled":false}'

# Rotate (recreate) a key — mints a new secret in place, keeping the user,
# scope, and limits; the old secret stops working immediately. Returns the
# new plaintext once, just like minting.
curl -X POST http://localhost:8001/api/keys/sk-smart-a1b2c3/recreate \
  -H "Authorization: Bearer $ADMIN_KEY"

# Delete a key
curl -X DELETE http://localhost:8001/api/keys/sk-smart-a1b2c3 \
  -H "Authorization: Bearer $ADMIN_KEY"

# Who am I? — report the identity the current key authenticates as
curl http://localhost:8001/api/whoami -H "Authorization: Bearer $SOME_KEY"
# → {"authenticated":true,"kind":"user","user":"alice","key_prefix":"sk-smart-a1b2c3","is_admin":false}
# kind is "admin" (env key), "user" (per-user key), "open" (no-auth mode),
# or "anon" (a keyless visitor, when public access is on)
```

The proxy adds an `X-User` response header identifying the authenticated user, and records each request (user, routed model, token counts, estimated cost) to a `usage_log` table for attribution.

### Router overhead

The router spends money on calls nobody requested: it profiles every prompt, escalates consequential ones to a stronger profiler, and rates catalog models after a sync or a Refine run. Those land in the same `usage_log`, tagged by `kind`:

| kind | what it is |
| --- | --- |
| `proxy` | a user request, forwarded to the routed model |
| `classify` | prompt profiling — the local classifier's triage pass |
| `classify-refine` | the second-pass profiler on a consequential prompt |
| `profile` | one model-shape rating during a sync or Refine run |

Only `proxy` rows are user traffic, so every existing figure keeps its meaning: the headline totals, the by-model / by-day / by-task / by-user tables, and the rate limiter's counter all count `proxy` alone. One prompt is one request against a key's quota however many calls the router made to route it. `GET /api/usage` reports the rest under `overhead` (totals plus a breakdown by kind and by model), and the Usage page shows it as a **Router Overhead** card with its share of total spend.

This matters because the overhead is not always small. A deployment whose prompts routinely trip the refine trigger can spend a meaningful fraction of its bill on deciding where to send things — and that is a fixable configuration problem (Settings → **Classifier**, **Model profiling**) only if it's visible. Overhead rows are attributed to the identity that caused them: the requesting user for classification, the admin who triggered the run for profiling.

Local classifier calls cost $0 and are still logged — the call count is what shows the classifier is being used at all. A `dry_run` Refine is logged too: it writes no ratings but makes exactly the same paid calls.

Each `proxy` row also records **which** classifier produced the profile that routed it (`llm`, `llm-free`, `llm-refined`, `keyword`, or `default`), reported as `by_classifier` and rendered as the Usage page's **By classifier** table. The per-request value is already in the `X-Classifier` header; the interesting figure is the *rate*, because that's the only form in which a silently-failing classifier is visible (see the classification fall-through chain under Configuration). The count is over `proxy` rows only — a `classify` overhead row is the classifier call itself, and counting it here would report two classifications per request. Rows written before this column existed read as blank, which they honestly are.

### Per-user scope and quotas

A per-user key can be constrained on three axes, all optional and all set at mint time (or via the API):

- **`scope_models`** — a JSON allow/deny list of case-insensitive substrings matched against a model's value and provider. `allow` is a whitelist (empty = all); `deny` overrides. Enforced inside routing, so a scoped user gets the best model *within scope* — never one outside it (the fallback pick respects scope too). Orchestrator mode returns `403` if the forced Claude model is out of scope.
- **`max_tier`** — a cost-tier ceiling; models above it are out of scope (`0` = no ceiling).
- **`rl_window_s` + `rl_max_req` / `rl_max_tokens`** — a rolling-window request and/or token quota, counted from the usage log. Over-quota requests get `429` with a `Retry-After` header, before any routing or forwarding.

```bash
# A key that may only use local Ollama models, capped at 100 requests/hour
curl -X POST http://localhost:8001/api/keys \
  -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"user":"alice",
       "scope_models":"{\"allow\":[\"ollama/\"]}",
       "rl_window_s":3600,"rl_max_req":100}'
```

Admin (env) keys are always unscoped and unlimited.

### Managing keys

Three ways, all equivalent (they share the SQLite store):

- **Web UI** — the **Keys** page at `http://localhost:8001/` (enter your admin key at the prompt to authenticate management calls).
- **REST API** — the `/api/keys` endpoints above.
- **CLI** — on the host machine, operating directly on the local store (no HTTP/auth needed):

```bash
smart-ai-router keys list
smart-ai-router keys add alice --scope '{"allow":["ollama/"]}' --window-s 3600 --max-req 100
smart-ai-router keys disable sk-smart-a1b2c3      # revoke (reversible)
smart-ai-router keys enable  sk-smart-a1b2c3      # re-enable a disabled key
smart-ai-router keys delete  sk-smart-a1b2c3      # permanent
```

Rotating a key (recreate) is available through the REST API and web UI; the CLI covers add/disable/enable/delete.

### Public (anonymous) access

Off by default. Turn on **Settings → Public access → Allow anonymous chat** and
visitors can use the chat page with no key and no signup — which means strangers
spending your OpenRouter balance and your GPU time, so every other setting in
that group is a ceiling on what that can cost you.

What "public" covers is narrow on purpose:

- **The chat page is open; the API is not.** Both use `POST /v1/chat/completions`,
  and the only thing distinguishing them is how the request presents itself, so an
  anonymous request is accepted only when it looks like a browser calling its own
  page (`Sec-Fetch-Site: same-origin`/`none`, or a matching `Origin`). A bare
  `curl` or an endpoint scanner still gets `401`. This stops drive-by scanning,
  not a determined abuser — the load-bearing limits are the ones below, which
  don't care who is calling.
- **A visitor is a real identity, not a shared one.** Each gets `anon:<session>`
  from an HMAC-signed, `HttpOnly`, `SameSite=strict` cookie. Conversations are
  scoped to that identity, so two visitors can't read each other's chats, and a
  forged cookie is treated as no cookie (a new session) rather than a way into
  someone else's history. Clearing cookies loses the history — that's the deal.
- **Only chat.** `/v1/chat/completions`, `/v1/models`, `/api/whoami`, and
  `/api/conversations` are reachable anonymously. Everything else — keys,
  settings, usage, providers, file uploads — still `401`s, and the UI hides the
  chrome that would only lead there.
- **No agent mode, ever.** Filesystem and shell tools run on your machine, so an
  anonymous `agent: true` is `403`, and `agent: "auto"` never escalates.
- **A spend cap that degrades instead of refusing.** Anonymous spend is totalled
  per UTC day (including the router's own classification calls, which are on the
  same bill). Past ~90% of the cap the cost-tier ceiling drops from
  `public_max_tier` to `public_degraded_max_tier` — free and local models — so
  the site keeps answering, slightly worse, instead of returning errors for the
  rest of the day. A cap of `0` means *no paid spend at all*, not "unlimited". If
  spend can't be read at all, it assumes the worst and serves free models.

The cap is enforced *after the fact* — a call's real cost is unknown until it
returns — so three things bound the overshoot: the 90% soft threshold, a hard
`max_tokens` ceiling per anonymous request, and a limit on how many anonymous
requests may be in flight at once. Rate limits and the concurrency gate are
in-process (they reset on restart, which is fine for abuse control); the budget
is read from the usage log and survives restarts.

Defaults, all editable on the Settings page:

| Setting | Default | What it bounds |
| --- | --- | --- |
| Allow anonymous chat | off | the whole feature |
| Anonymous daily spend cap (USD) | `1.00` | total paid spend per UTC day |
| Anonymous max cost tier | `3` (≈Haiku) | how expensive a model a visitor can reach |
| Anonymous max tier once budget is spent | `1` (free/local) | the degraded ceiling |
| Anonymous max output tokens | `1024` | cost of any single reply |
| Anonymous requests per window | `30` | per-IP *and* per-session request count |
| Anonymous rate-limit window (s) | `3600` | the window those counts cover |
| Anonymous concurrent requests | `4` | in-flight anonymous requests deployment-wide |

Behind a Cloudflare tunnel, `CF-Connecting-IP` is used as the visitor's address
(`request.client.host` is the tunnel, identical for everyone, which would turn a
per-IP limit into a global one). Because that header is trivially spoofable by
anything reaching the origin directly, this is only meaningful while the origin
isn't publicly reachable except through the tunnel. Rate limiting counts per IP
*and* per session against the same number, so a visitor is capped whichever one
they shed — dropping the cookie doesn't buy a fresh quota, and neither does
changing networks.

An authenticated key is never affected by any of this: a real key is matched
first, so enabling public access can't downgrade a paying caller to anonymous
limits.

## Provider management

```bash
# List providers
curl http://localhost:8001/api/providers

# Add/update a provider
curl -X PUT http://localhost:8001/api/providers/openrouter \
  -H 'Content-Type: application/json' \
  -d '{"name":"openrouter","kind":"openrouter","enabled":true,"api_key":"sk-or-..."}'

# Trigger a model sync
# Response counts models added / updated (only those that actually changed) /
# unchanged / removed. Models absent from a provider's fresh catalog are deleted.
# Also LLM-profiles the models it just added (see "Refining model profiles"),
# reporting that under `profiled`; pass {"profile": false} to skip it.
curl -X POST http://localhost:8001/api/sync -H 'Content-Type: application/json' -d '{}'
```

## Models

```bash
# List all synced models
curl http://localhost:8001/api/models

# Get a specific model
curl http://localhost:8001/api/models/openrouter/anthropic/claude-sonnet-4-6

# Refine model profiles with an LLM, and report what it would change about
# routing (admin only). dry_run=true writes nothing — see
# "Refining model profiles with an LLM" below.
curl -X POST http://localhost:8001/api/models/profile \
  -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"limit":20,"only_missing":true,"dry_run":true}'
```

## File uploads

An OpenAI-compatible Files API stores uploads on disk (metadata in SQLite) and scopes each file to the uploading identity — admin sees all files, a per-user key sees only its own.

```bash
# Upload a file (multipart). Returns an OpenAI-shaped file object.
curl -X POST http://localhost:8001/v1/files \
  -H "Authorization: Bearer $KEY" \
  -F 'file=@report.pdf' -F 'purpose=assistants'
# → {"id":"file-…","object":"file","bytes":12345,"filename":"report.pdf", ...}

curl http://localhost:8001/v1/files                    -H "Authorization: Bearer $KEY"  # list
curl http://localhost:8001/v1/files/file-XXXX          -H "Authorization: Bearer $KEY"  # metadata
curl http://localhost:8001/v1/files/file-XXXX/content  -H "Authorization: Bearer $KEY"  # download bytes
curl -X DELETE http://localhost:8001/v1/files/file-XXXX -H "Authorization: Bearer $KEY" # delete
```

Extractable-to-text types: **PDF**, **Word (.docx)**, **PowerPoint (.pptx)**, **Excel (.xlsx)**, and plain-text/code files (`text/*`, JSON, XML, YAML, TOML, JS, shell, Python). Legacy `.doc`/`.ppt`/`.xls` are not supported — save as the modern OpenXML format. Images aren't extracted here; they're inlined as base64 for vision-capable models at request time. Uploads over the size ceiling return `413`; unsupported types return `415`. See `SMART_ROUTER_MAX_FILE_MB` and `SMART_ROUTER_FILES_DIR` below.

## Web search

When the profile says a prompt depends on facts that move — a price, a version, a
count, a standing, which option is currently best — the request is forwarded with
OpenRouter's `web` plugin, so the retrieval happens provider-side and the model
sees results before it answers. About **$0.007 per searched request**, billed per
search rather than per token, with up to 10 results included in that price.

Two gates, and the second one is the one that surprises people:

- **Settings → Routing → "Search the web for time-sensitive prompts"** (on by default), and
- **the routed model has to be an OpenRouter model.** A local Ollama model and Bedrock's OpenAI-compatible endpoint both ignore `plugins` silently, so those answer unsearched.

`X-Web-Search: true|false` reports which happened on every response, and the chat
UI shows a `🌐 web` badge. Answering unsearched is the right fallback — better
than refusing a question the model can partly answer — but it's only honest if
the caller can tell.

**Whether a prompt needs search is the classifier's judgment, deliberately.** The
rubric asks it about the answer it would write rather than the wording of the
question, because the volatile prompts mostly don't announce themselves: "which
GPU should I buy", "what does an H100 go for", "which vector DB should we use"
contain no word like *current* or *latest*. It's also told today's date and that
its training data predates it — without that, "could this have changed since my
training" and "since now" are the same instant from the inside. Any keyword list
written for this has holes, so the durability comes from measuring instead:
`scripts/bakeoff_classifier.py` scores a candidate triage model on exactly this
judgment, reporting missed searches separately from false ones (a missed search
is a confidently stale answer; a false one costs $0.007).

## Voice

The chat page has a hands-free mode: click **🎙 Voice**, speak, and pause — the pause is what sends it. The reply is read back while it is still streaming, and Escape interrupts it and hands the turn back. A level bar and a status line under the composer show what it is doing, including whether any sound is reaching the page.

This is the browser's own speech recognition and synthesis, not a speech-to-speech model. The router still only ever sees text, so voice works with whichever model routing picks — including a local Ollama one — and adds nothing to the bill. The trade is prosody: you get the OS voice rather than something conversational.

Two requirements, both browser-side:

- **A secure context.** `http://localhost:8001` counts; `http://your-host.local:8001` does not, and the microphone is blocked there. Put the router behind TLS to use voice from another machine.
- **A browser with a speech engine.** Use **Safari** (Apple's recogniser) or **Chrome** (Google's). **Opera, Brave and Vivaldi are Chromium without that access** — they expose the `SpeechRecognition` interface, accept the request, and then never report anything at all. The page detects that by timing out and says so, rather than sitting on "Starting…" forever.

### Model-native voice (🔊 Talk — admin only)

The other trade. **🔊 Talk** sends your microphone audio to an audio-in/audio-out model and plays back its reply as speech, so the model hears your voice and answers in its own — the thing that makes ChatGPT's voice mode sound human rather than like a screen reader.

It needs no speech engine in the browser, so it works where 🎙 Voice doesn't (Opera included). What it gives up is the router's whole premise:

- **A spoken turn is not routed.** Of the models OpenRouter carries, a few dozen accept audio input and only `openai/gpt-audio` and `openai/gpt-audio-mini` emit audio at all. There is nothing to choose between on price, so `/v1/voice` bypasses `CapabilityRouter` entirely rather than pretending to route. The chat badge says so on every turn.
- **It bills audio tokens.** Roughly **$0.11 per hour** of conversation on `gpt-audio-mini`, roughly **$1.70** on `gpt-audio`. The usage page reads the real figure back from the provider rather than pricing it at the model's text rate, which would report an hour of talking as a rounding error. Pick the model under **Settings → Voice**.
- **Admin only, for now.** That per-turn cost on a model nobody chose on price is not something to hand a self-serve or anonymous visitor before the spend controls for it exist. The button is hidden unless the admin key is in use, and `/v1/voice` 403s for every other identity (and 401s before that for anonymous ones — it is not on the anonymous path allowlist).

Ceilings worth knowing, all of them the same one: OpenRouter exposes no realtime/WebSocket endpoint, only SSE.

- **Half-duplex.** You cannot talk over the reply; Escape interrupts it. Real barge-in needs a duplex transport.
- **Your words are not transcribed.** The model returns a transcript of its own reply but never of your audio, so the history it is sent carries only its side, and the chat log shows your turns as `🎙 (spoken)`. A question leaning on your exact earlier words may miss them. Fixing it means a transcription call alongside — a cascade, which this mode exists to avoid.
- **No tools.** Both gpt-audio models advertise function calling, so agent mode and web search are reachable from here later, but a tool call is a silent gap in the middle of a spoken sentence and covering it needs the model to say "let me look that up" first.
- **One turn per request**, endpointed locally on a 1.1s silence timer with a 25s cap — not by the model.

## Chat history (conversations)

Server-side conversation storage backs the web UI's chat, scoped per identity.
Every turn is stamped when it is stored (`ts`, UTC) and the transcript shows that
stamp under each bubble in the reader's own timezone — so reopening a thread says
when it happened, not when the page was loaded.

```bash
curl http://localhost:8001/api/conversations                       -H "Authorization: Bearer $KEY"  # list
curl -X POST http://localhost:8001/api/conversations \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"title":"Linezolid dosing"}'                                                                  # create
curl http://localhost:8001/api/conversations/CID                   -H "Authorization: Bearer $KEY"  # get (with messages)
curl -X PATCH  http://localhost:8001/api/conversations/CID \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"title":"New name"}'      # rename
curl -X DELETE http://localhost:8001/api/conversations/CID         -H "Authorization: Bearer $KEY"  # delete
curl -X POST http://localhost:8001/api/conversations/CID/messages \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"role":"user","content":"Hi"}'                                                                # append a message
```

## Bad-response reports

A router that picks the model for you owns the bad answers, and the answer alone
never says why one happened. So the ⚑ **Report** link under any reply in the chat
— and the ⚑ **Feedback** tab on the right edge of every page, for everything that
isn't one particular reply — opens a box that asks for the only thing the browser
can't work out (what was wrong) and attaches the rest by itself: the whole conversation exactly as
it was sent upstream, plus the routing decision behind the reply (routed model,
prompt profile, classifier, the `why` string). None of that routing detail is
stored per message anywhere else, so the report is the only place it survives.

The transcript is a **snapshot**, not a pointer at the stored thread: the reporter
can rename or delete their conversation the next minute, an anonymous visitor's
chat may never have been saved at all, and evidence that can change after it is
filed isn't evidence.

Anyone may file one — anonymous visitors included, since they are the callers most
likely to be handed a bad answer and least likely to have another way to say so.
Reading and clearing them is admin-only (a report is someone else's chat), on the
**Reports** tab or over the API. Oversized bodies are trimmed rather than refused:
a single huge turn is shortened, and a transcript past the cap loses its *oldest*
turns, because the reply being reported is the last one.

```bash
curl -X POST http://localhost:8001/api/reports \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"description":"It invented a function that does not exist.",
       "conversation_id":"conv-abc",
       "transcript":[{"role":"user","content":"..."},{"role":"assistant","content":"..."}],
       "meta":{"routed":"openrouter/anthropic/claude-haiku-4.5"}}'   # file (any caller)
curl http://localhost:8001/api/reports          -H "Authorization: Bearer $ADMIN_KEY"  # list (admin)
curl -X DELETE http://localhost:8001/api/reports/7 -H "Authorization: Bearer $ADMIN_KEY"  # clear one
```

**Reports as GitHub issues.** A report is only useful where the fix happens, so
the Settings page (**Feedback** group) can mirror each one into a repo's issue
tracker: turn on *File reports as GitHub issues*, give it `owner/name` and a
fine-grained token with **Issues: write** on that repo alone. Off until you do.

An issue is public and a report carries somebody's chat, so two things are true by
default: the **conversation is not published** (the issue cites the local report
id, and the transcript stays on the router), and the **reporter is never named**
in the issue at all — local reports keep the attribution. *Put the conversation in
the issue* publishes the transcript too; think about who filled that transcript
before you turn it on. Either way the modal tells the user it's going to a public
tracker before they type anything.

The report is stored first and mirrored second, and the mirror can never fail the
report: a revoked token or a GitHub outage costs you the issue, not the feedback.
The Reports tab shows the issue link, or the error in place of it — a token that
quietly stopped working is otherwise invisible until you wonder where the issues
went. Note that anonymous visitors can file, so an open router with this on lets
strangers open issues on your repo; the anonymous rate limits are what bound that.

## Agent mode & document creation

When agent mode is on (see the Configuration section), a tool-capable model can operate on the caller's private workspace via these tools: `list_dir`, `read_file`, `write_file`, `edit_file`, `create_document`, and (opt-in) `run_bash`. `create_document` renders a small Markdown subset (headings, bullets, pipe tables, bold) into **PDF**, **Word (.docx)**, **PowerPoint (.pptx)**, **Excel (.xlsx)**, or **Markdown/plain-text**, then registers it as a downloadable file via the Files API above.

The `agent` body flag is tri-state: `true` (always), `false` (never), and `"auto"` — the default when the key is absent — which enters agent mode only for an *actionable* prompt, when a tool-capable model is in scope, **and the caller sent no `tools` of its own**. That last condition means a client running its own tool loop is left alone: a coding agent (Claude Code through claudish, Codex, anything speaking the OpenAI tool protocol) sends its editor and shell tools expecting `tool_calls` back to execute locally, and its prompts are maximally actionable — so without the check, `"auto"` fired every turn and the router answered with its own filesystem loop over its own workspace. The caller's tools were never invoked and nothing errored. Send `agent: true` to ask for the router's loop even while advertising tools.

## Self-update

```bash
# Check for source updates
curl http://localhost:8001/api/updates

# Apply update (git pull + restart)
curl -X POST http://localhost:8001/api/updates/apply
```

