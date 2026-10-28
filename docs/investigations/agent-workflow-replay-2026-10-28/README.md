# Offline supervisor workflow replay

This is a **synthetic reconstruction**, not the missing original CrewAI/user trace. It reuses the investigation's supervisor sales task: retrieve 2026-10-01/02 sales (12,500 / 14,800), pass those exact values to the math agent, then return difference 2,300 and percent change 18.4. An additional weather control verifies simulated-data attribution.

## Evidence and limits

- [Fixture](../../../tests/fixtures/agent_workflow_replay.json) pins prompts/schema reference, synthetic model/backend, turns and billing assumptions. Full prompt/schema snapshots and actual provider request bodies are included in results.
- [Baseline](baseline.json): router `dd90a6dcc978731b7fc05f3091a3106e26fb114d`.
- [Fixed](fixed.json): router `2107fae509e249787b5ceb9d152234ab6bfc5090` (PR #156).
- Both use the same replay source checkpoint `8fcd190` and fixture SHA256. Baseline was an archive, not the production checkout.
- [Preserved issue-151 baseline pytest output](issue-151-baseline-regression.txt): 38 failed / 21 passed, with synthetic test-key strings and home paths redacted and trailing whitespace normalized. Original local evidence remains preserved.

No real provider or user tool executes. All HTTP provider requests use `httpx.MockTransport`; unexpected URLs fail closed. Each workflow uses a new in-memory catalog and usage DB, with proxy memory and capture disabled. Tests additionally make real socket connections fail. No live credentials are read.

The original framework version, exact prompts, sanitized requests/responses, and concrete model/backend revision are **unavailable**. Therefore this closes only the offline harness/evidence portion of #152. It cannot attribute the original 13-call incident or justify routing-policy changes. The native baseline is independent of the router translator, but the fixture provider is scripted and does not reason over the prompt. The harness checks dependencies, actual forwarded continuation values, schema, constraints, and final outcomes.

## Results (each scenario runs Chat/Responses × streaming/nonstreaming)

| Scenario | Native Chat | Router Responses | Attribution |
|---|---|---|---|
| Clean | Eventual and clean success | Eventual and clean success | Scripted correct behavior |
| Repeat + wrong math values, then recovery | Eventual success, not clean | Eventual success, not clean | Semantic fixture errors; 2 excess calls retained |
| Empty date array | Oracle flags invalid arguments, fixture exhausts | One 502 usage row; rejection in both modes | Upstream schema violation, correctly rejected |
| Forbidden parallel calls | Eventual success, not clean | One 502 usage row; rejection in both modes | Upstream constraint violation, correctly rejected |
| Schema-valid wrong final | Task failure | Task failure despite protocol acceptance | Semantic failure, not translator failure |
| Invented source / simulated=false | Task failure | Task failure despite protocol acceptance | Attribution failure |
| Provider 503 | Transport failure | Transport failure | Synthetic provider failure; billed cost unavailable |

The clean supervisor task has the same correct result before and after #151. On both Responses modes, baseline falsely stalls its final reply and finishes with health **0.8**; fixed records no stall and finishes at **1.0**. This is measurement correction, not improved model reasoning. No floor is tightened and each run starts isolated. Native Chat bypasses the router, so its health value 0 means **unmeasured**, not bad health.

Synthetic successful-turn usage is 10 input / 5 output tokens, priced at $1/$2 per million. Clean task: **3 turns, $0.00006, 300ms**. Recovered task: **5 turns, $0.00010, 500ms**; every recovery turn counts. Adapter-rejected generated replies cost **$0.00002** in the synthetic ledger. A provider error has no supplied usage; `cost_unavailable_calls=1` and limit assessment is null, not a claim of free execution. No automatic transport retry is modeled.

Limits are 6 turns, 6 calls, 2 seconds per request, 600ms accumulated synthetic latency, and $0.0002 accumulated synthetic cost. Turn/call/time bounds govern execution; the latter two are reported fixture-budget checks, not live admission controls. **Actual external spend is zero.** `local_replay_latency_s` measures local mock/adapter overhead and varies; it is not model latency. Stable outcome/call/cost/health fields are deterministic. Full and fragmented provider streaming are covered elsewhere; this replay emits complete tool-call deltas plus a terminal event.

Provider nonstreaming errors currently yield no usage row, while streamed failures yield a failed row. The provider ledger preserves the attempt in both cases; this existing distinction is exposed, not silently counted as zero cost or changed here.

## Reproduce

Use the repository's existing development dependencies; no framework install is needed.

```sh
python -m scripts.replay_agent_workflow --output /tmp/workflow-fixed.json
python -m pytest -q tests/test_workflow_replay.py
```

For the baseline, export `git archive dd90a6d` into a temporary directory, copy `scripts/replay_agent_workflow.py` and `tests/fixtures/agent_workflow_replay.json` from this branch into the same paths there, then run the replay command from that directory. No source DB, provider key, or production endpoint arguments exist.

Local validation after timeout review: **26 replay tests passed**; available suite **1335 passed, 1 deselected**, excluding the already-known missing-`httpx2` SDK collection and denied nested macOS sandbox test. Neither local limitation was retried through expanded permissions. CI and independent review remain separate gates for this draft.


## Timeout lifecycle correction

Review reproduced a request surviving teardown in the original threaded harness: a 10ms timeout returned in 16ms with the provider thread still active. Commit `27fb268` replaces threaded synchronous clients with async HTTP clients and an in-process ASGI transport. The timeout cancels and awaits the request and provider stream before clients close, global patches restore, or the in-memory DB closes. The replay creates no request executor threads.

Sixteen regression combinations cover both protocols, both response modes, pre-header and mid-body waits, and finite-delay/indefinite cancellable fixtures. They assert provider finalization, stream closure, no surviving request tasks, and total termination under one second including event-loop shutdown for a 20ms request limit. This is an async cancellation contract for the controlled replay transport, not a claim to preempt arbitrary blocking Python code.

Both 28-run datasets were replayed with the corrected harness. Every stored result field except `local_replay_latency_s` matches the published artifacts exactly. The existing artifacts therefore remain the original source-`8fcd190` captures; they are not relabeled as fresh measurements. Outcome, accounting, health, request bodies, errors and fixture hash are unchanged. The earlier raw regression log is untouched.
