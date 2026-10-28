# Valid short final outputs must not count as tool stalls

Issue: [#151](https://github.com/gaaschk/smart-ai-router/issues/151). Status: ready for implementation.

## Problem and scope

`smart_ai_router/api/proxy.py::_log_usage` currently marks tool-enabled replies without tool calls as stalled when completion tokens are below `tool_stall_max_tokens`, even for valid final JSON. The characterization in `tests/test_agent_reliability_benchmark.py` reproduces this. The result can lower observed health and reject a capable model later.

Change this observation rule, not routing admission. Unknown orchestration scores remain eligible. Do not reset historical health, tune floors, change deployment settings, or silently relax protocol validation.

## Expected behavior

| Completed reply with optional tools | Observation |
|---|---|
| Short nonempty prose, such as `18.4%`, or valid final JSON | Not stalled, regardless of reported/estimated/absent token counts |
| Final answer after a tool-result continuation | Same rule; offering tools does not require another call |
| Empty or whitespace-only content without tool calls | Stalled, regardless of token count |
| Leaked/unexecutable tool-call text | Existing detection and failure accounting preserved |
| Actual tool call | Existing non-stall classification preserved; protocol checks still apply |
| No tools offered or incomplete/disconnected stream | Preserve current sampling rules |

Nonempty text establishes only observable progress, not semantic correctness. A short promise to act cannot be distinguished from a valid short answer using token count alone; evaluate semantic task completion in workflow replay. Existing leak detection still catches its recognized tool-text patterns.

Schema errors, ignored required/named tool choice, malformed calls and forbidden parallel calls must remain rejected by their existing validators. A schema-valid wrong answer remains a semantic benchmark failure. Do not broaden Chat validation in this fix; preserve protocol-specific contracts.

## Verification and Done

- Exercise Chat and Responses, each streaming and nonstreaming; include short prose, valid JSON and final tool-result continuation where applicable.
- Replace the false-positive characterization with the desired regression assertion; cover token-boundary, missing/estimated usage, empty/whitespace, leaked tool text and actual-call controls.
- Verify Responses defers success accounting until validation and records one usage row per request on both success and validation failure; preserve cancellation accounting and cost/tokens.
- Run existing tool-health/probe, streaming usage, Responses schema/required-tool/parallel-call and benchmark suites, then the full CI suite.
- Show a deterministic before/after replay with mocked providers; no paid external calls. Link spec, test results, independent review and demo in the fix PR before Done.
