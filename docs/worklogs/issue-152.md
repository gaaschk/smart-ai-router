# Issue 152 offline replay checkpoint

Branch `replay/152-offline-agent-workflow` is stacked on PR #156 at `2107fae509e249787b5ceb9d152234ab6bfc5090`; no merge or deployment.

Issue inspected: original real sanitized framework trace/version/backend evidence is missing. This is explicitly a synthetic reconstruction of the investigation's supervisor sales workflow, plus an attribution control. It cannot diagnose the original unseen framework incident or certify a model.

Implemented a fixture-driven extension of the existing `run_case` benchmark. Seven scenarios run through independent native Chat and the real router Responses adapter, streaming and nonstreaming (28 runs). Every provider request is confined to MockTransport with an unexpected-URL rejection. No production DB is opened; each run uses an in-memory catalog. Proxy memory access and capture are disabled within the process.

Synthetic limits: six turns, six tool calls, two-second request timeout, 600ms fixture latency and $0.0002 synthetic task-cost ceiling. Costs include every dispatched successful fixture reply, including recovery and adapter-rejected replies; provider-error cost is explicitly unavailable. Local wall-clock timing is labeled separately. External spend is zero.

First checkpoint: 10 replay tests passed, including network tripwire, continuation payload comparison, oracle attribution, recovery accounting, protocol failures and turn/call budgets. No packages installed. Existing local SDK collection and nested sandbox limitations remain unchanged; the denied sandbox test will not be retried.

Next: publish baseline/fixed evidence, full available test results, stacked draft PR and CI; independent review pending. Missing real trace remains an issue-level gate. No routing-policy change is justified by synthetic fixtures.

Evidence checkpoint: source commit `8fcd190`; 10 focused tests passed; available suite 1319 passed / 1 deselected (same SDK exclusion and prior sandbox denial). Baseline/fixed 28-run artifacts and preserved issue-151 regression log are in `docs/investigations/agent-workflow-replay-2026-10-28/`. Clean Responses workflow health is 0.8 before / 1.0 after #151 in both modes, with unchanged task results and synthetic token/cost totals. All scenarios remain synthetic and original-source evidence is explicitly missing.
