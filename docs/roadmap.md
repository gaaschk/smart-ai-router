# Development roadmap

GitHub Issues hold scope and acceptance criteria; this roadmap holds order and outcome gates. The developer/assistant owns implementation and verification. Work proceeds consecutively when its gate is met, without artificial calendar waits. Merge and deployment remain separate decisions.

## Ordered stages

| Stage | Work | Outcome gate |
|---|---|---|
| Now | [#151: correct false short-final tool stalls](https://github.com/gaaschk/smart-ai-router/issues/151), following the [behavior spec](specs/valid-short-final-outputs.md) | Valid short answers stay healthy; true failures and exactly-once usage remain covered by regression tests and a before/after replay. |
| Next | [#152: replay a representative agent workflow](https://github.com/gaaschk/smart-ai-router/issues/152) | Reproducible success, failure attribution, latency and total-cost evidence; distinguish eventual success from clean success. Start with offline mocks/replay, not paid provider calls. |
| Later | [#153: evidence-based routing and capability eligibility](https://github.com/gaaschk/smart-ai-router/issues/153) | Tested endpoint capability combinations and task-family qualification justify a policy; compare reliable task cost/latency and define promotion/rollback gates. |
| Release readiness | [#154: reconcile backlog and release operations](https://github.com/gaaschk/smart-ai-router/issues/154) | Current backlog reconciled, reproducible release rehearsal, review evidence and explicit go/no-go decision. |

Fix measurement before tightening routing. Unknown orchestration scores remain eligible under current rules until evidence supports a separate reviewed policy change. A visual agent/crew/flow platform remains separate product discovery, not an assumed router feature or committed backlog.

## Lightweight working agreement

Use one Project, named **smart-ai-router delivery**, reusing an existing matching board if present. Statuses: **Ideas → Ready → In progress → Review → Done**. Keep at most one main task In progress (including its review until complete); select the next task only after its outcome gate. Ready requires bounded scope, acceptance criteria and a spec for behavior changes. Ideas can remain broad until promoted.

Done means linked spec, passing relevant tests/CI, independent review, and a reproducible demo; a draft PR alone is not Done. For documentation-only changes, document/link validation substitutes for runtime tests. Keep changes in focused branches and draft PRs. Use existing Actions; no additional tracking tool is required.

**Board setup pending:** on 2026-10-28, `gh project list --owner gaaschk --format json` failed because the existing token lacks `read:project`. No Project was inspected or created and no credential change was attempted. When Project access is available, inspect first, reuse/create one board, add #151–#154, put the active implementation in In progress and later stages in Ideas. Until then, these issue links and this ordered roadmap are the fallback; board access must not block development.

## Reconcile rather than replace

Review existing [#86–#91](https://github.com/gaaschk/smart-ai-router/issues?q=is%3Aissue+is%3Aopen) against actual files and tests before proposing closure. In particular, #87's missing-CI premise conflicts with the existing `.github/workflows/ci.yml`; test execution does not establish the coverage reporting requested in #91. Keep existing issues for their remaining scope.

[Installer draft #65](https://github.com/gaaschk/smart-ai-router/pull/65) needs fresh comparison with main and installer tests; do not blindly merge its older branch. No access/security, branch-protection, credentials, paid-feature, runtime or deployment changes belong in this planning setup.

Evidence baseline: main `dd90a6dcc978731b7fc05f3091a3106e26fb114d`, and the [October 14 investigation](investigations/agent-tool-reliability-2026-10-14.md). Historical benchmark results are evidence to reproduce, not current model certification.
