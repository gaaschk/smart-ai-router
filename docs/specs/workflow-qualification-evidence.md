# Workflow qualification evidence contract (proposal)

Tracks #152 and #153. This is a data-collection and review proposal, not a routing
policy or model qualification. Unknown scores remain eligible under current rules.
No thresholds, name-based allowlists/denylists, or provider policy changes are made.

## Original incident evidence still needed

The October 14 investigation and repository history contain synthetic benchmarks,
not the original framework execution. Supply a sanitized, ordered request/response
trace including continuation messages, tool call IDs, tool arguments/results,
errors/retries, streaming boundaries and the expected outcome. Also record:

- framework/version and relevant configuration; agent prompt/tool-schema versions;
- router commit/configuration (without credentials), API protocol and streaming mode;
- concrete model revision and actual provider/backend identity, not just catalog alias;
- permitted tools, strict argument/final schemas, parallel constraints and caller
  provider restrictions; request IDs that can be safely shared;
- timestamps/durations, billed attempts including failures, missing usage indicators,
  stopping reason, and which calls were considered irrelevant with an explanation.

Replace private names/data consistently so dependencies and failures remain
reproducible. Never include bearer/provider keys or private customer material.
Do not label fabricated substitute traces as the original incident. Store sanitized
fixtures only after checking their provenance and suitability for publication.

## Proposed qualification record

Separate task families: single lookup, strict argument compliance, strict final
output, dependent multi-tool work, supervisor/delegation and attribution. For each
actual backend record `tested`, `unsupported` or `unknown` for the exact combination
of tools, strict arguments, final schema, streaming and parallel constraints. An
advertised capability or trivial probe is not a measured task-success score.

A future record must include evaluation/prompt/schema/fixture versions, sample
counts and exclusions, timestamps and freshness, training-versus-holdout split,
backend identity, eventual success versus clean reliable success, categorized
failures, excess/repeated calls, total task cost and end-to-end latency. Count retries
and rejected generated replies. Preserve missing cost as unknown, not zero. Report
uncertainty and sample sizes; choose any promotion floor only after representative
and held-out evidence is reviewed. Current synthetic 28-run data cannot set one.

Compare cost/latency only among candidates qualified for the caller's required
combination. Never drop required tools/schemas or relax caller restrictions to make
a candidate pass. Distinguish provider availability, adapter rejection, schema
compliance and semantic task failure rather than collapsing them into health.

## Exploration, promotion and rollback gates

Propose an opt-in, bounded evaluation path for unknown combinations using synthetic
or authorized sanitized data, explicit cost/turn/time budgets and isolated usage.
It does not silently exclude unknown models or send live paid requests. Backend or
prompt/schema changes invalidate comparisons until freshness is reviewed.

Before implementation: obtain original evidence, replay the source failure against
an independent baseline, define representative holdouts, compare full-task metrics,
and independently review a policy spec with regression tests and comparative demo.
Before release: approve measured thresholds, scope, observability and rollback.
Keep the prior policy/configuration recoverable; rollback criteria should include
caller-constraint violations, accounting regressions and worse held-out reliability.
No promotion or release decision is made by this document.
