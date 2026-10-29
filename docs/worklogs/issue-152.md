# Issue 152 offline replay checkpoint

Branch `replay/152-offline-agent-workflow` is stacked on PR #156 at `2107fae509e249787b5ceb9d152234ab6bfc5090`; no merge or deployment.

Issue inspected: original real sanitized framework trace/version/backend evidence is missing. This is explicitly a synthetic reconstruction of the investigation's supervisor sales workflow, plus an attribution control. It cannot diagnose the original unseen framework incident or certify a model.

Implemented a fixture-driven extension of the existing `run_case` benchmark. Seven scenarios run through independent native Chat and the real router Responses adapter, streaming and nonstreaming (28 runs). Every provider request is confined to MockTransport with an unexpected-URL rejection. No production DB is opened; each run uses an in-memory catalog. Proxy memory access and capture are disabled within the process.

Synthetic limits: six turns, six tool calls, two-second request timeout, 600ms fixture latency and $0.0002 synthetic task-cost ceiling. Costs include every dispatched successful fixture reply, including recovery and adapter-rejected replies; provider-error cost is explicitly unavailable. Local wall-clock timing is labeled separately. External spend is zero.

First checkpoint: 10 replay tests passed, including network tripwire, continuation payload comparison, oracle attribution, recovery accounting, protocol failures and turn/call budgets. No packages installed. Existing local SDK collection and nested sandbox limitations remain unchanged; the denied sandbox test will not be retried.

Next: publish baseline/fixed evidence, full available test results, stacked draft PR and CI; independent review pending. Missing real trace remains an issue-level gate. No routing-policy change is justified by synthetic fixtures.

Evidence checkpoint: source commit `8fcd190`; 10 focused tests passed; available suite 1319 passed / 1 deselected (same SDK exclusion and prior sandbox denial). Baseline/fixed 28-run artifacts and preserved issue-151 regression log are in `docs/investigations/agent-workflow-replay-2026-10-28/`. Clean Responses workflow health is 0.8 before / 1.0 after #151 in both modes, with unchanged task results and synthetic token/cost totals. All scenarios remain synthetic and original-source evidence is explicitly missing.

## Timeout review correction

Independent review identified a blocking lifecycle defect: `asyncio.wait_for` could cancel an `asyncio.to_thread` waiter while the synchronous request survived teardown. Reproduced on head `10677c0` with a finite controlled provider wait: `TimeoutError`, elapsed 0.016s, `active_after_replay_teardown=true`. The reproduction released the thread explicitly; no live HTTP or indefinite process was used.

Replaced both native and adapter requests with async HTTP clients. The adapter uses a single explicitly allowed in-process ASGI transport; all provider clients remain confined to the mock transport. Request cancellation now propagates through provider awaits/stream iteration before async client cleanup, patch restoration and DB close. No request worker threads are created.

Added 16 lifecycle cases (both protocols × stream/nonstream × headers/body × finite delay/indefinite cancellable wait). They assert TimeoutError, provider finalization before restored state, closed response bodies, zero surviving request tasks, and bounded completion including `asyncio.run` shutdown. Focused suite: **26 passed**. Full available suite and evidence equivalence verification pending at this checkpoint.

Post-fix checkpoint: source `27fb268`. Focused suite 26 passed; full available suite **1335 passed, 1 deselected**, with the same SDK file exclusion and no retry of the denied sandbox action. Fresh 28-run baseline and fixed outputs match every previously published result field except measured local replay latency; original evidence artifacts remain unchanged and correctly labeled with their original source commit. No scenario semantics, billing, health or token outcome changed. Draft PR157 will be updated in place; CI and timeout-fix independent review pending.

CI run338 found eight test setup failures: the 20ms test deadline expired before Responses entered the deliberately stalled provider, while cleanup/remaining-task assertions passed. Increased only the test deadline to 500ms (delayed fixture waits 2s; full test termination bound 2s) to accommodate runner routing setup. Production replay timeout remains 2s; no cleanup assertions were removed.
