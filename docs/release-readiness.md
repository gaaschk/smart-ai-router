# Release readiness checkpoint — 2026-10-29

Tracking issue: #154. Evaluated main `82bdea301735798e9f127524da80bc25f726b34e`.
**Decision: NO-GO for a release/deployment in this task.** Development drafts and
evidence collection continue. This is an offline rehearsal, not a deployed change.

## Backlog reconciliation

| Issue | Verified state | Remaining acceptance gate |
| --- | --- | --- |
| #86 license | MIT LICENSE, README reference and GitHub license detection verified; closed | None |
| #87 CI | Existing CI premise corrected; draft #159 adds three Python versions, fatal lint and report artifacts; explicit read-only token permissions | Triage report-only formatting/security findings, independent review/merge; owner controls branch protection |
| #88 security | Draft #162 supplies honest reporting guidance | Verified private channel, support/response/disclosure policy and settings review |
| #89 circuits | Draft #160 adds opt-in provider circuit states, tests and admin status | Review/merge; explicit operational enablement remains separate |
| #90 browser voice | Draft #161 adds matrix/warnings/troubleshooting | Real major-browser evidence; Mac locked during native inspection |
| #91 coverage | #158 merged; CI1343passed3skipped,80.40% combined coverage;80% floor | Original broader PR-comment/diff/dashboard requests remain unimplemented; agree scope before closure |
| #151 health | #156 merged, valid short-final regression corrected; closed | No new work identified |
| #152 replay | #157 merged synthetic replay/timeout fix; issue reopened | Original sanitized framework trace/version/prompts/concrete backend missing |
| #153 qualification | Evidence contract proposed in docs/specs/workflow-qualification-evidence.md | Depends on original/representative evidence; no new routing thresholds authorized |

#159's initial matrix ran1343tests3skips on each Python3.10/3.11/3.12 with
80.41/80.40/80.40% coverage. Quality reports are not clean gates: local formatting
reported154files, Bandit53findings(2high/11medium/40low), dependency audit97packages
and zero known vulnerabilities. SHA1 identity findings need migration/design review;
no silent namespace change or security allowlist was applied. Final PR CI evidence
is recorded in each PR because commits may advance after this snapshot.

## Older installer draft #65

Reviewed head `e08737015455b6b13abdc00c305d5e5fc11363df`, based on old
`c2d4289`, against current main. Do not blindly merge it. Main has a launchd installer
but not this proposed root bootstrap script. The draft's root script:

- checks that python3 exists, not that its version meets the project's requirement;
- updates an existing checkout with checkout/pull, without checking dirty state or
  requiring fast-forward-only updates;
- reads missing `--install-dir` value under `set -u`, producing an unbound-variable error;
- advertises piped execution but does not arrange terminal stdin for interactive setup;
- runs setup by default, which can configure providers/services outside this task's scope.

Safe checks on the exact proposed script: `bash -n` passed; `--help` returned usage;
`--install-dir` without a value reproduced `$2: unbound variable` before any install.
The clone/install/setup paths were not executed. A real install rehearsal requires
an isolated disposable target and explicit service/configuration scope. Keep #65
draft; these findings do not authorize installing or modifying production.

## Offline rehearsal evidence

Ran `python -m scripts.replay_agent_workflow --output /tmp/router-release-rehearsal.json`
at the main commit above. [Summary](investigations/release-rehearsal-2026-10-29/summary.json)
records28synthetic runs and zero external spend. All fields match the prior fixed
artifact except variable local latency and its manually added source/revision
annotations. Clean and deliberately failing cases retain their distinct outcomes.
Synthetic billing/latency are not measurements of a real model or the source incident.

Full main-equivalent local suite plus five browser-warning cases passed1350tests,
with one nested macOS sandbox case deselected after a prior sandbox denial. Linux
CI for #161 passed1348tests with3macOS-only skips and80.40% coverage. These environments
do not establish that the skipped/deselected cases passed. No deployment was rehearsed.

## Release checklist

- [ ] Pin release commit and linked behavior spec; enumerate included PRs and omissions.
- [ ] Verify final CI/test/coverage artifacts for that exact commit, including platform gaps.
- [ ] Independent review approves behavior, security findings and migration/compatibility risks.
- [ ] Run a reproducible comparative demo on isolated data and retain the evidence.
- [ ] Resolve owner-controlled security/reporting and browser verification gates.
- [ ] Write release notes covering defaults, opt-in features, known limitations and changes.
- [ ] Record rollback commit/configuration, data compatibility and a disposable rollback test.
- [ ] Owner records explicit go/no-go and separately authorizes any merge/deployment.

Do not equate a passing synthetic replay, green report-only quality job, draft PR,
or checklist document with release approval. Credentials, security settings,
branch protection and production services remain unchanged.
