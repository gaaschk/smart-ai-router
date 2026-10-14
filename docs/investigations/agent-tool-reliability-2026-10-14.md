# Agent tool-calling reliability investigation

## Finding

The provider-independent orchestrator change removed the Claude-name restriction but did not replace it with demonstrated semantic agent reliability. The current admission rule is advertised tool support plus no failed tool probe. Unknown agentic scores pass. The shared health floor measures stalls rather than correct arguments, useful tool calls, attribution, or task completion. Price remains decisive among candidates clearing the prompt's field bars.

This explains how a model can generate many irrelevant calls, eventually answer correctly, and still look healthy to the router. It also explains why a safety-classification specialist can enter general-purpose orchestration.

No production routing, provider configuration, or live user conversations were changed during this investigation. The uncommitted report-transcript checkbox work was preserved. The additions are a repeatable benchmark, deterministic oracle/translation tests, and this report/results artifact.

## Evidence from the deployment

The catalog/usage snapshot was read from mini. The benchmark used deployed code at `1520ff127289d9bee19ff5eb2f185467fb26941e`. This is a current-state snapshot, not necessarily the code/settings used for every originally reported execution.

- Since October 13, observed `smart-orchestrator` selections included Qwen3.8 Flash (25 requests), GPT-OSS Safeguard 20B (21), Voxtral Small (8), Solar Mini 4 (7), and GPT-5 Nano (5). These totals include requests with no tools; they do not measure agent task success.
- `min_tool_health=70` and `min_tokens_per_second=30` were configured. The health guard was enabled, not missing.
- Qwen Flash had `agentic=0` (unknown), health `1.0`, and no probe verdict. Solar Mini had unknown agentic ability and health about `0.926`.
- Safeguard had advertised tools and structured output, a successful trivial probe, unknown agentic ability, health `1.0`, cost tier 1, and broad profile scores around `0.68–0.73`. Its recent routed prompts included surface math and practitioner finance. These profile defaults allow a specialized model to clear ordinary field bars.

The relevant code is `api/proxy.py::_orchestrator_capable`, `router.py::_eligible/_drives_loops`, `sync.py`'s OpenRouter capability/profile extraction, `tool_probe.py::classify`, and `api/responses_routes.py::_chat_body/_validate_output`.

## Repeatable benchmark

`scripts/bench_agent_reliability.py` runs three synthetic, bounded workflows:

1. Sales: two date-specific queries followed by subtract, divide, multiply, with exact dependency/value checks. Expected difference is 2,300 and increase is 18.4%.
2. Supervisor delegation: a nonempty array containing both ISO dates, then the actual retrieved values passed to the math agent.
3. Attribution: simulated weather must remain labeled simulated, preserve the returned temperature, and cite the exact fixture source rather than inventing a URL.

Every call is schema-validated and checked against the task's expected values and dependencies. Invalid calls receive a simulated error; no real tool or user system operation is executed. The harness counts every call, rejects repeats, flags parallel calls when disabled, tracks excess calls, checks the final JSON/schema/values, and distinguishes eventual task success from clean reliable success. A correct final answer does not erase earlier failures.

The native Chat baseline constructs messages independently of the Responses translator. The Responses path uses the real FastAPI app/adapter with a single catalog model in an in-memory DB snapshot, retaining routing guards. Classifiers, web search, GBrain and capture are disabled inside the isolated benchmark process. This tests the adapter, validation, and eligibility behavior without contaminating live usage/health data. It is not a request to the deployed public HTTPS endpoint and does not reproduce CrewAI's agent prompts, retries, delegation behavior, or streaming.

Two modes separate compatibility from model behavior:

- `combined`: tools and strict final JSON schema are sent together.
- `separate`: tools run without a final output schema; after all required calls complete, a tool-free request generates the structured final output.

The 120 main workflows (plus two targeted Qwen validation replays) cover five catalog models, two repeats, three tasks, two protocols, and two schema modes. Each table cell is clean reliable successes out of six workflows; transport/routing rejection counts are not treated as semantic model mistakes.

| Catalog model | Combined: direct Chat | Combined: router Responses | Separate: direct Chat | Separate: router Responses |
|---|---:|---:|---:|---:|
| OpenAI GPT-5 Nano | 6/6 | 6/6 | 6/6 | 5/6 |
| Qwen3.8 Flash | 4/6 | 4/6 | 4/6 | 2/6 |
| Solar Mini 4 | 0/6 | 0/6 | 3/6 | 0/6 |
| `~anthropic/claude-haiku-latest` | 6/6 | 2/6 | 6/6 | 6/6 |
| GPT-OSS Safeguard 20B | 0/6 | 0/6 | 5/6 | 6/6 |

Interpretation:

- GPT-5 Nano's one separate-mode Responses violation was passing timestamp strings instead of the required ISO dates; it recovered and answered correctly. It is a promising candidate, not certified by this small sample.
- Qwen's direct runs answered all tasks correctly but ignored `parallel_tool_calls=false` on the sales task, twice in each schema mode. Responses runs included upstream-output validation failures. Two additional isolated sales replays logged the exact validator reason in both schema modes: `Provider ignored parallel_tool_calls=false`. This confirms a model/provider constraint violation rather than argument corruption by the Responses translator. Three later separate-mode workflows were rejected by the throughput floor. These outcomes must not be combined into one vague quality score.
- Solar bypassed the required tools and returned incorrect final values in all six combined direct workflows. In separate mode, three direct workflows passed and three hit provider 400 errors. The router rejected all Solar workflows at its configured throughput floor, before model execution.
- Haiku's combined Responses run developed health-floor rejections inside the isolated snapshot. The existing stall heuristic treats a short final answer as a stall even when valid. All separate-mode workflows passed, where final schema-only requests have no tools and therefore do not affect tool health. This is a router health-accounting defect, not evidence that Haiku cannot orchestrate.
- Safeguard hit Groq's tools + JSON-schema restriction in all twelve combined workflows. Separating the final request removed that restriction; it then passed most/all of these very small synthetic tasks. That does not establish general-purpose competence or justify its broad catalog priors.

The main artifact records about $0.0243 in provider-reported or catalog-estimated token costs. This is not a settled bill: Responses costs are estimates, failed-call costs may be unavailable, and overhead is excluded. Latency, tokens, backend/model labels where returned, violations, and synthetic call traces are retained in `agent-reliability-2026-10-14.json`. Latest aliases and unpinned OpenRouter backend endpoints can change; repeats are not paired deterministic executions.

## Attribution: model, router, provider, framework

### Underlying model behavior

Wrong dates/values, irrelevant calculator operations, ignored explicit tool requirements, repeated calls, and incorrect simulated-data attribution are semantic failures when already present in direct provider responses. The benchmark demonstrates required-tool bypass, parallel-call noncompliance and an argument-format violation without CrewAI. It did not reproduce every supplied example or establish their frequency.

### Responses translation and validation

A deterministic test compares the independent native Chat baseline with `_chat_body` for the sales-delegation continuation, including both dates, the returned 12,500/14,800 values, call IDs, strict tools, parallel constraints, and the output schema. They match. Existing SDK/Responses suites also pass.

The adapter validates function names, JSON arguments, strict schemas, required/named tool choice, call IDs and parallel constraints. A schema-valid but semantically wrong argument can still pass. For example, `first=15, second=10` satisfies two number fields. This is not proof that every framework-specific translation is correct. The original failing CrewAI request/response payload was requested to close that gap.

### Provider capability combinations

The catalog has independent `tools` and `structured_outputs` flags. It has no endpoint-level record that their combination works. The Responses adapter also uses the structured-output flag for strict function arguments, conflating two distinct capabilities. The underlying model label does not identify the final OpenRouter hosting endpoint.

Groq documents that tool use is not supported with structured outputs, and the benchmark reproduced the exact backend error. OpenRouter supports `provider.require_parameters=true`, which is a useful guard for individual parameters, but does not by itself establish compatibility of every combination. Do not silently drop a schema or a required tool to make a request succeed.

Sources: [Groq Structured Outputs](https://console.groq.com/docs/structured-outputs), [OpenRouter provider selection](https://openrouter.ai/docs/guides/routing/provider-selection).

### Specialized-model purpose

OpenAI describes GPT-OSS Safeguard as a safety reasoning model trained to label content against a supplied policy. That purpose is not evidence of general-agent reliability. Introduce purpose-aware eligibility and measured qualification by task family; do not classify every tool-capable safety/audio/coding specialist as a general orchestrator, and do not replace that mistake with a Claude whitelist. The safeguard model's separate-mode passes here also show why a name-only blacklist would be an incomplete definition.

Source: [OpenAI's safeguard model card](https://huggingface.co/openai/gpt-oss-safeguard-20b).

### Framework behavior remains unverified

The harness has no CrewAI dependency and does not reproduce the user's custom agent implementation. Framework prompts, examples, retries, tool schemas, timestamp/date normalization and delegation could contribute to the reported 13-call execution. Original sanitized payloads and request IDs are needed to attribute those exact calls. The independent failures above establish that a framework bug is not required for this class of failure, but do not exonerate every framework path.

## Recommended implementation sequence

1. **Correct the measurement before tightening floors.** Stop labeling valid short final JSON as a stall. Track empty/unexecutable output separately from schema failures, ignored required-tool/parallel constraints, and semantic feedback. The characterization test documents the current false positive; it intentionally does not endorse that behavior. Do not reset production health indiscriminately.
2. **Record endpoint capability combinations.** Separate function calling, strict tool arguments and strict text output. Store tested/unsupported/unknown states for combinations, including streaming and parallel constraints, by model and actual backend endpoint. Respect caller provider restrictions, and avoid known-incompatible endpoints before dispatch. Use `require_parameters` as an additional guard rather than as a complete solution.
3. **Add measured orchestration qualification.** Keep imported `agentic` and observed availability separate from a task-family reliability record. Store component metrics, sample counts, evaluation/prompt/schema version, concrete model/backend, timestamps and cost/latency. Require positive relevant evidence for complex production orchestration, while retaining an explicit exploration/canary path for unknown models. Two repeats of three synthetic tasks are insufficient for promotion or an exclusion policy.
4. **Distinguish task depth.** A single tool lookup, strict function/schema compliance, dependent multi-tool execution, and long supervisor/delegation loops need different evaluations and eligibility bars. Today any tool array activates the same loop floor; field/depth classification does not provide this distinction. Use a broader holdout suite before calibrating thresholds or declaring a measured score authoritative.
5. **Persist observable failures and accept trusted caller feedback.** The router can observe call counts, invalid/schema arguments, exact normalized repeats, call IDs, protocol constraints, tokens and latency. It cannot generally prove a call was useful, a value came from the right prior result, or a task succeeded. Add an authenticated, request-linked outcome channel for agent frameworks, separate from provider errors and with safeguards against biased/spoofed feedback. Avoid storing raw private arguments by default; normalized fingerprints and categorized outcomes are sufficient for many metrics.
6. **Rank by reliable task cost after qualification.** Compare total task cost and latency, including retries and excess calls, among qualified candidates. Cheap tokens are not cheap workflows when a model makes thirteen calls for a three-operation task. Enforce explicit turn/call budgets at the agent framework even when routing improves.

## Reproduction and validation

From a checkout with the development dependencies installed, on a host with the configured catalog/provider credentials:

```sh
python -m scripts.bench_agent_reliability \
  --db ~/.smart_ai_router.db \
  --models openrouter/openai/gpt-5-nano openrouter/qwen/qwen3.8-flash \
  --protocols chat responses --schema-mode combined \
  --repeats 3 --max-turns 8 --max-calls 16 --timeout 90 \
  --output /tmp/agent-combined.json
```

Repeat with `--schema-mode separate --output /tmp/agent-separated.json`. Each run makes real provider calls, but all tools are simulated. Results checkpoint after each workflow. Source DB connections are read-only; provider credentials are used in memory and not emitted into results. Authentication changes and telemetry suppression are scoped to the benchmark process. Run models in separate processes for parallel execution because the application's settings binding is process-global.

`tests/test_agent_reliability_benchmark.py` checks exact arithmetic/dependencies, empty/incorrect dates, wrong numerical arguments, repeated calls, bypassed required tools, simulated attribution, structure, eventual-success versus reliable-success accounting, the independent translation comparison, separated capability requests, source-DB immutability, timeout bounds and the short-answer health false positive.

Validation performed: 137 focused router/Responses/probe/health/benchmark tests passed; the final full suite on latest main passed 1,261 tests, including all 14 benchmark checks. No production routing code was changed, and the report-option changes are included in the same PR with separate tests. No deployment or live settings change was performed.
