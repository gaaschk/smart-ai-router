# How routing decisions work

Routing matches a **prompt profile** against a **model profile**, over the same vocabulary of 16 fields. A single "how hard is this?" number can't express the thing that matters most — a cheap coding specialist and a frontier generalist can both score 0.9 on *something*, and the cheap one wins on price even when the prompt needs the other one's strength. So both sides are scored per field, and a model must clear the bar on **every** field the prompt reaches into.

## The prompt profile

Each prompt is described as:

- **domains** — up to 3 `(field, depth)` pairs. Fields are a closed set (`software_engineering`, `law_regulatory`, `medicine_health`, `math_formal`, `natural_science`, `finance_business`, `creative_writing`, … with `general_knowledge` as the residual).
- **depth** — how far into that field the answer must go, as one of four described tiers rather than a float, because a 3B classifier picks reliably between four tiers and cannot calibrate a continuous score:

  | Depth | Required score | Roughly |
  |-------|---------------|---------|
  | `surface` | 0.45 | any model that can hold a conversation |
  | `practitioner` | 0.68 | excludes the weakest models |
  | `specialist` | 0.85 | Sonnet / GPT-4o class and up |
  | `frontier` | 0.93 | Opus / Fable class only |

- **demands** — properties of the task that raise every bar: `factual_precision` (+0.05 — the hallucination axis: must name real statutes, standards, APIs, citations), `quantitative` (+0.03), `long_synthesis` (+0.03), `agentic` (+0.02).
- **stakes** — consequence of being wrong: `low` / `medium` (+0.02) / `high` (+0.05).

Two or more fields at specialist depth or deeper adds a further +0.04, because holding two specialist frames at once is where generalists start producing plausible nonsense. The sum of all bumps is capped at +0.08, and no requirement exceeds 0.97 — without those caps an ordinary high-stakes prompt would demand the priciest tier and undo the router's reason to exist.

## Two-speed classification

The local classifier (a small Ollama model) profiles every prompt. When it reports **high stakes**, **two or more specialist-depth fields**, or **frontier depth** — the judgments a small local model gets wrong expensively — a second pass on a stronger model refines the profile before routing. That call fires only on prompts already headed for a costly model, and lowering the bar is as valid a correction as raising it. Which model runs that second pass is itself a routing decision (see [The router's own calls](#the-routers-own-calls)); set `SMART_ROUTER_CLASSIFIER_REFINE_MODEL` (or the **Classifier** group in Settings) to empty to disable it. If no LLM classifier is reachable, a keyword profiler runs instead. Both passes are billed and both appear on the Usage page as router overhead, so how often the second pass fires — and what it costs — is something you can look up rather than estimate.

## Model profiles

Model scores are inferred during sync from provider catalog metadata: Artificial Analysis benchmark indices (`intelligence_index`, plus `coding_index` as independent evidence for the software field), whether the model supports reasoning, and the vendor description. Narrow models — coders, roleplay models, math models — take a **specialist discount** on professional fields they don't advertise, which is what stops a cheap coding model from being the answer to a legal question. The legacy `coding`/`docs`/`reasoning`/`general` competence vector is derived from the profile, never tracked separately, so the two can't disagree.

**Tool-loop stamina is not a field.** `agentic_index` measures whether a model holds a multi-step tool loop together, which is not knowledge about a subject, so it lives on its own axis (the **Tool loop** column on the Models page) rather than in the taxonomy. It used to be written over the `operations_process` field, which conflated "knows about runbooks and workflow design" with "can drive twelve tool calls without losing the thread" — and since that field is one of two feeding the legacy `general` column, an agentic benchmark quietly became half of every model's reported general competence. On a 347-model catalog, 118 models carried the index and every one of them had that field dented by a median of 0.13.

The axis is consulted only when a request actually involves tool use — a `tools` array, agent mode, or the `agentic` demand — and only for models whose stamina was measured. Most of the catalog and *every* local model carry no index, and that reads as **unknown, not zero**: an unmeasured model competes on its fields exactly as before. What the floor excludes is the narrow case of a model with strong knowledge scores that was measured unable to finish a multi-step task.

## Refining model profiles with an LLM

The level in a model profile comes from measurement and is trustworthy. The
*shape* comes from reading a ~200-character vendor blurb through a fixed cue
table, and a blurb like "flagship model for enterprise workloads" matches no cue
at all — so the model gets a flat profile and looks equally competent at
software engineering and clinical medicine. Flat profiles are the thing profile
routing exists to eliminate, because the router picks the cheapest model that
clears every bar and a flat profile clears bars it never earned.

So there's an optional pass that asks a strong model what the blurb doesn't say:
that `qwen3-coder` is superb at code and should not be answering questions about
drug interactions. **Benchmarks own the level; the LLM only owns the shape.** It
is never asked for scores — asked for 16 numbers between 0 and 1, models return
0.8–0.9 for everything and re-flatten the profile. It's asked, per field,
whether the model is unusually strong or unusually bad *relative to its own
overall capability*:

| Rating | Offset | Means |
|--------|--------|-------|
| `specialty` | +0.04 | purpose-built for this; a headline capability |
| `capable` | 0.00 | about as good at this as its overall level suggests |
| `weak` | −0.10 | noticeably worse at this than its level suggests |
| `unsuited` | −0.20 | should not be relied on for this at all |

Those offsets are sized against the depth ladder above: `weak` is larger than
the specialist→frontier gap (0.85 → 0.93), so it genuinely disqualifies a model
from work it was passing on paper.

**The ratings are what's stored, not the adjusted numbers.** The profile is
composed on read — baseline + offsets — so a later sync with fresh benchmarks
re-levels every rated model automatically, with no second LLM call, and a sync
never erases a judgment you paid for.

Runs are bounded and inspectable, from the **Refine profiles with an LLM** panel
on the Models page:

- **cheapest first** — the router considers models cheapest-first, so an
  overstated *cheap* model is the one doing damage; a capped run fixes that end.
- **skips already-rated models** by default, so a large catalog can be worked
  through over several runs.
- **Preview** does the same rating work but writes nothing, and reports the
  effect on real traffic: it replays the prompt profiles this deployment has
  actually routed (recorded in the usage log) through the real selection
  function under both the current and the proposed profiles, and lists the flips
  — weighted by request count and labelled `qualifies` / `unqualifies` /
  `pricier` / `cheaper`. A diff of 16 floats can't tell a change that moves a
  tenth of your traffic from one that moves nothing; this can.
- Each rated model keeps a one-line note from the rater, shown in the Models
  table (`LLM` vs `rules` in the Profile column, filterable). An adjustment
  nobody can inspect is one nobody should trust.

A model that can't be rated (provider error, unparseable reply) keeps exactly
the profile sync gave it — nothing here can fail a run or a request.

**New models are profiled automatically.** A model starts taking traffic the
moment sync stores it, so waiting for someone to press Refine means routing on a
cue-table guess in the meantime. Every sync therefore profiles what it just
introduced, bounded by the same per-run ceiling and reported in the sync result
(`3 new, 3 profiled`). What counts as "just introduced" is deliberately narrow:

- **new models** — yes, always.
- **a rewritten vendor description** — yes. The description is the only shape
  evidence sync has, so a rewrite means the stored judgment rests on evidence
  that no longer stands.
- **a new price, context length, or benchmark index** — no. Those change the
  model's *level*, and the stored rating is relative, so a sync re-levels the
  profile for free. Re-rating would pay again for an answer that cannot change.

Turn it off with **Settings → Profile new models on sync**, or per-call with
`{"profile": false}` on `POST /api/sync`. Existing models are never swept up by
this — a catalog that predates refinement stays rules-only until you Refine it,
so enabling the feature can't produce a surprise bill for hundreds of models.

```bash
# Preview the effect of rating the 20 cheapest unrated models (admin only)
curl -X POST http://localhost:8001/api/models/profile \
  -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"limit":20,"only_missing":true,"dry_run":true,"audit_days":30}'
# → {"enrich":{"considered":20,"rated":20,"changed":7,"written":0, "changes":[…]},
#    "audit":{"requests":412,"flipped_requests":38,"flips":[…]}}
```

Configured under **Settings → Model profiling**
(`SMART_ROUTER_MODEL_PROFILER_MODEL`, `SMART_ROUTER_MODEL_PROFILER_LIMIT`). The
rater defaults to `auto` — routed, not pinned (see below) — and every run reports
which model it used and why, since a report of shifted profiles can't be judged
without knowing who shifted them.

Every rating call is logged to the usage log as `kind="profile"` overhead (see
[Router overhead](api.md#router-overhead)), including on a dry run — so what a run cost
is a number on the Usage page, not an estimate from the model count.

## The router's own calls

The router makes LLM calls of its own: the refine pass that re-profiles a
consequential prompt, and the rating pass that judges what a model is for. Both
used to be pinned to a hand-typed model name aimed at a hardcoded provider — so
the one part of the system nobody routed was the part the router runs itself. The
denylist didn't apply to it, neither did the reliability floor, a retired model
name failed silently instead of re-routing, and nothing noticed when a cheaper
qualified model appeared.

Both settings now take three values:

| Value | Meaning |
|-------|---------|
| `auto` *(default)* | Route it. Cheapest model clearing the bar for that task, same rules as user traffic. |
| a model name | Pin it, verbatim — the escape hatch for "the router's pick is wrong and I need it fixed now". Works even for a model absent from the catalog. |
| empty | Don't make the call at all. |

Each task carries a hand-written prompt profile rather than being classified,
because the workload is known: both ask for broad `general_knowledge`, and depth
is the only real difference. The refine pass demands **frontier** — it exists only
for prompts already headed to the top tier, so a model that doesn't clear the top
bar has nothing to add. The profiler demands **specialist**, because it runs one
call per model and a frontier bar would price a catalog-wide run out for judgment
specialist depth already covers.

Both also require `structured_outputs` (below), and both take only a *qualified*
pick. Ordinary routing falls back to the closest available model when nothing
clears every bar, which is right for a request — some answer beats none — and
wrong here: a rater too weak to judge a model, or a refine pass no better than the
triage model it second-guesses, spends money to make the decision worse. When
nothing qualifies, the call is skipped: routing falls back to the local
classifier's read, or a profiling run reports that it had no rater. A degraded
decision, never a failed request.

One caveat worth stating plainly: the profiler's own pick is chosen using the same
profiles the profiler exists to correct. That circularity is bounded by the things
that bound any Refine run — the dry-run preview, the routing audit, and the pin.

**Triage is deliberately not one of these.** The local classifier that profiles
every prompt stays a pinned name; `SMART_ROUTER_CLASSIFIER_MODEL` **rejects
`auto`** rather than quietly accepting a word that wouldn't work. Routing it was
measured against the live catalog and is worse than pinning:

- Routing means "cheapest model clearing the bar", and every local candidate is
  free — nine Ollama rows, all cost tier 0, reliability 1.0, `structured_outputs`.
  Cost discriminates nothing, so the sort falls through to competence margin and
  triage routes to the **biggest** local model. That's the opposite of what a
  hot-path JSON call wants.
- On the live host that pick was `qwen3:30b-a3b`, which `scripts/bakeoff_classifier.py`
  measures at **0 usable profiles out of 32**: it thinks, and `finish_reason` comes
  back `length` before any JSON appears. Every request's profiling would go to a
  model that cannot profile — and because the chain degrades silently, nothing
  would look broken.
- Excluding reasoning models doesn't rescue it. The next pick, `qwen2.5:3b-instruct`,
  outranks `llama3.1:8b` on competence — the exact inverse of the bakeoff, where
  the 3B misses 2 escalations to the 8B's 0 and names the right field 19 times to
  its 26.

The reason is structural, not a scoring bug. Competence measures what a model
*knows*; triage fitness is "does it emit strict JSON inside a 256-token budget
without thinking first". Only the second decides whether the call works at all,
and nothing in the catalog measures it. Until something does, the honest
configuration is a name someone benchmarked — which is what the bakeoff script is
for.

## Capability flags

Two facts about a model's *shape* (not its quality) are persisted alongside its
profile, derived on every sync:

- **`structured_outputs`** — honors `response_format: {"type": "json_schema"}`, a
  schema rather than merely valid JSON. The distinction is the whole reason it's
  stored: a model that accepts `json_object` but ignores the schema answers the
  prompt instead of filling in the requested shape, which parses as nothing and
  fails silently. On the live OpenRouter catalog, 336 of 415 models advertise
  `structured_outputs` while 359 advertise `response_format` — that 23-model gap
  is exactly the silent-failure population, which is why the flag reads the former.
  Ollama models are all flagged capable, since Ollama implements the constraint
  server-side as constrained decoding regardless of the weights. Bedrock is
  flagged *not* capable: unverified through its OpenAI-compatible endpoint, and
  the safe direction is to not use a model rather than to trust one.
- **`reasoning`** — emits thinking tokens before the answer. Not a quality signal
  in either direction. It's a shape signal: a thinking model handed a small output
  budget spends it reasoning and returns an empty message, which is why the local
  classifier wants a model without it.

Both are filter chips on the Models page (**JSON schema**, **Thinking**) and show
on hover over a model's name. `structured_outputs` is a hard routing filter for
the router's own calls above.

## Selection

1. **Filter** by hard constraints: tool-calling support, vision, context length, minimum reliability, key scope, denylists.
2. **Qualify** — keep models whose score clears the requirement for *every* field named in the profile.
3. **Sort** qualifying models by cost tier (ascending), then by their weakest required-field score (descending) as tiebreak, and take the cheapest.

If **nothing** qualifies, the pick is the *closest miss* — ranked by how far short it falls, with cost only as a tiebreak — and the response says so: `X-Qualified: false`, a `⚠ under-qualified` chip in the chat UI, and a caveat prepended to the answer telling the caller to treat specifics (citations, standards, figures) as unverified. This is the case the old single-bar router could not even detect; it returned a confident answer from an unqualified model with no indication anything was wrong.

Every response carries `X-Prompt-Profile` (the profile in words), `X-Routing-Why` (the binding constraint), `X-Qualified`, and the legacy `X-Domain` / `X-Complexity` derived from the profile.

## Earning a place in the orchestrator lane

`smart-orchestrator` admits a model on one test: whether its name contains
"claude". That string match decides where nearly all the spend goes — on this
router's first month, 99.9% of it — because a coding client pins its main loop to
that lane. Whether the rule is *right* is a measurement, and there are two ways
to make it, neither of which is "widen the rule and see":

**A bakeoff.** `python scripts/bakeoff_orchestrator.py` scores candidates on
tool-call reliability: did it call a tool when the turn needed one, were the
arguments well-formed, did it stay quiet on the turn that wanted prose, did it
avoid re-issuing a call it already had the answer to. `--pool` prints who each
candidate rule would admit, for free. The script's docstring records the last
run — including the result that the catalog's measured `agentic` index does *not*
predict this corpus, which is why the shipped filter has not been widened.

**A canary.** Set *Orchestrator canary model* and *Orchestrator canary share (%)*
on the Settings page and a named challenger — usually not a Claude — takes that
share of orchestrator turns. It is routed through the same selection as the pool,
so scope, capability requirements and the prompt's own bar all still apply: a
turn the canary doesn't qualify for goes to Claude as it would have. When it
fires, the response carries `X-Canary: true` and the usage row is billed against
the canary, so the Usage page answers what the experiment cost. Default 0 = off.

**Replaying real turns.** A synthetic corpus is a proxy for traffic, not a sample
of it — the first version of ours marked all three Claude incumbents down for
correctly reading a file before editing it. *Capture tool-loop turns (%)* samples
your own tool-bearing requests to `~/.smart_ai_router_captures.jsonl`, and
`bakeoff_orchestrator.py --replay` scores candidates against them, using the
incumbent's own tool calls as the reference for what each turn required. The
prompt is written to disk verbatim, so the capture is deliberately narrow: admin
traffic only (not configurable), requests that carry tools only, and of the reply
only its tool calls — never its prose.

## Prompt caching

Routing picks the cheapest capable model. Caching removes the tokens entirely,
and on this router's real traffic it is worth far more:

> **92% of lifetime spend was one five-minute coding session.** 52 requests,
> median 69,571 prompt tokens each, re-sending the same growing prefix, with
> `cached_tokens: 0` throughout. The router had already picked Haiku — the
> cheapest Claude there is — so routing had nothing left to give. $3.43 of a
> $3.71 lifetime bill, and at 80–95% prefix reuse that session costs $0.58–1.16.

Anthropic caches only what you mark, and a client's own `cache_control`
breakpoints don't survive an Anthropic→OpenAI translation layer (LiteLLM strips
every one), so by the time a body reaches the router the intent is gone and no
client can restore it. The router sets them itself — two, the standard shape for
a growing conversation:

1. **End of the system block.** Anthropic's cache prefix is ordered
   tools → system → messages, so this covers the tool definitions too — which is
   where the tokens actually are. (Measured on a real Claude Code request:
   146,296 of 153,507 bytes were tool schemas, ~40k tokens, against ~1.9k tokens
   of conversation.) A breakpoint on `tools` isn't expressible in the OpenAI wire
   shape; this makes one unnecessary.
2. **End of the history**, rolling. What this turn writes, the next turn reads.

A cache write costs 1.25× and a read 0.10×, so a marker nobody reads back is a
25% surcharge — which is what the guards are for. Breakpoints are set only:

- on **Claude models via OpenRouter** — every other family either caches long
  prefixes automatically (OpenAI, Grok, DeepSeek) or has no cache to mark
  (Ollama). Bedrock is excluded: it caches through its own `cachePoint` shape and
  whether our OpenAI-compatible path honors `cache_control` is unverified.
- from the **second turn onward** — an assistant turn in the history is the proof
  that the client re-sends its prefix. Agent mode is exempt: its tool loop
  re-sends the prefix every round, so even a first turn reads back what it writes.
- above **~2k tokens**, below which Anthropic ignores the marker anyway.
- never over a **caller that set its own breakpoints** — it knows its prefix
  better than this heuristic does.

Cache reads are billed at 10% of the input rate, and the usage log prices them
that way, so the Usage page shows the discount rather than a bill nobody was sent.
A hit also logs one line to stderr (`[proxy] cache hit: 62000 of 69000 prompt
tokens read at 10%`), which is how you confirm it's working. Turn the whole thing
off from **Settings → Routing** if a provider ever errors on a breakpoint.

## Params vs. the pick

A caller names a model *class* (`smart-worker`, `auto`) and never learns which
model answered, so every model-specific param in its body is a guess about a pick
it can't see. A wrong guess isn't ignored — it's a provider 400, which turns a
routed request into no answer at all:

```
reasoning_effort + a coding prompt → ollama/qwen3-coder:30b
→ 400 '"qwen3-coder:30b" does not support thinking'
```

So the proxy reconciles the body with the model it chose, three different ways
depending on what the param is:

| Param | Treatment | Why |
|---|---|---|
| `reasoning_effort`, `reasoning`, `include_reasoning`, `thinking` | **dropped** when the pick has `reasoning: false`, and reported in `X-Dropped-Params` | A thinking budget is a preference; the answer survives without it, and the header keeps that visible rather than mysterious |
| `max_tokens` | **clamped** to the pick's `max_output` | Several providers reject an over-limit request rather than truncating the reply |
| `response_format: {"type": "json_schema"}` | **routed on** — the pick must have `structured_outputs` | Dropping it is the silent failure: the model answers in prose, the caller's parse finds nothing, and nothing anywhere errors. `json_object` is not a schema and imposes no requirement |

Params the catalog tracks no capability flag for are left alone rather than
guessed at. The agent loop is seeded with the same reconciled body, since every
round of it hits the same model.

## Cost tiers

Models are assigned cost tiers during sync based on their per-million-token pricing:

| Tier | Input cost ($/M tokens) | Examples |
|------|------------------------|----------|
| 0 | Unknown | |
| 1 | Free or < $0.10 | Free-tier models, tiny local models |
| 2 | $0.10–$0.50 | Haiku-class |
| 3 | $0.50–$1.00 | |
| 5 | $1.00–$3.00 | Sonnet-class |
| 8 | $3.00–$8.00 | GPT-4o, mid-tier |
| 12 | $8.00–$15.00 | Opus-class |
| 15 | > $15.00 | Premium reasoning models |

Local Ollama models always have cost tier 0.

