# Issue 151 development checkpoint

- Isolated Mac mini clone: `~/Documents/Codex/2026-10-27/task-2/smart-ai-router`.
- Branch: `fix/151-valid-short-final-outputs`; fetched main is `dd90a6dcc978731b7fc05f3091a3106e26fb114d`.
- Planning checkpoint: `282a78700b3d2cfb327ef29e74ec8306d10425c6` (draft #155).
- Spec: [valid short final outputs](https://github.com/gaaschk/smart-ai-router/blob/282a78700b3d2cfb327ef29e74ec8306d10425c6/docs/specs/valid-short-final-outputs.md).
- Changed usage observation from token threshold to whitespace-aware empty content; leak detection and actual-call behavior retained. Legacy threshold remains readable but is labeled unused.
- Added 46 deterministic regression cases including both APIs, both response modes, missing/measured usage and tool-result continuation. All 46 passed.
- Existing Python 3.12.13 / pytest 9.1.1 dependencies are read from the installed checkout's virtual environment, with bytecode writes disabled; no packages installed.
- Broad tests require isolation from the installed GBrain CLI. An initial run reached a default memory query and was interrupted; no deliberate live workflow/provider replay was run. Temporary pytest plugin `/tmp/router_offline_tests.py` stubs proxy enrichment/save-back for test processes only. Full results pending.
- No production files, settings, credentials, routing floors, services or deployment changed. No merge authorized.
- Next: full test result, deterministic baseline replay, draft PR, independent review. Issue 152 remains gated on issue 151 review.

## Verification checkpoint (2026-10-28)

Implementation commit: `20b50a1` (subsequent test/evidence commit extends coverage).

- Final focused regression: **59 passed**. Includes threshold values 95/96/97, high token counts on empty output, and exactly one failed usage row when Responses rejects ignored required-tool choice, in both response modes.
- Initial focused health/probe/benchmark/persistence/scanner set: **99 passed** before the additional 13 cases.
- Before/after demo: run `tests/test_short_final_health.py` unchanged against an archive of baseline `dd90a6d`: **38 failed, 21 passed**. Current implementation: **59 passed**. Providers are canned HTTP/SSE mocks; no paid calls. Baseline failures demonstrate false stalls for short replies and missed stalls for high-token empty output.
- Full collection attempted: blocked by `ModuleNotFoundError: httpx2` in `tests/test_responses_sdk.py`; no dependencies installed.
- All collectable tests initially: **1 failed, 1296 passed**; sole failure `tests/test_tools.py::test_bash_runs_in_workspace_when_enabled`, because nested macOS `sandbox-exec` returns `sandbox_apply: Operation not permitted` (exit 71). No permission changes or escalation of this test attempted.
- Final available-suite run: **1309 passed, 1 deselected**, with SDK file excluded and the denied nested sandbox case deselected. This is not a claim that the full CI suite passed.
- `git diff --check` passed.

Reproduce using an existing development environment (the Mac interpreter used was `/Users/kevingaasch/ProjectHome/smart-ai-router/.venv/bin/python`):

```sh
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p scripts.offline_test_isolation tests/test_short_final_health.py
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p scripts.offline_test_isolation \
  --ignore=tests/test_responses_sdk.py \
  --deselect=tests/test_tools.py::test_bash_runs_in_workspace_when_enabled
```

For the baseline comparison, archive `dd90a6d` to a temporary directory, copy the new regression file and `scripts/offline_test_isolation.py` into that archive, and run the first command there. The isolation plugin disables only default proxy memory enrichment/save-back; tests may install their own mocks. Existing GBrain unit tests still run.

Remaining gates: SDK/full CI on an environment with its declared dependencies, permitted sandbox test environment, independent review. Draft PR is reviewable, not Done. The next issue's representative workflow replay remains deferred until this active issue's review gate; the narrow issue-151 before/after mocked replay is complete.
