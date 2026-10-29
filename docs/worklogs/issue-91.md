# Issue 91 coverage checkpoint

Base main `af7d089bc37043bf915b078558fc2293e92d4b97`; clean isolated branch `ci/91-coverage-baseline`. No AGENTS.md or .agents skills were present. Existing PR template and CI were read. A new local .venv contains development dependencies and coverage.py; production venv/config/secrets/services untouched. uv's default shared-cache write was unavailable; all install cache writes were confined to /tmp/router-coverage-uv.

Measured before configuration edits: 1345 passed,1 deselected; 6766/8273 statements,1896/2490 branches,80.47942023599369% combined. Python3.12.13,coverage7.17.0. Existing offline isolation plugin and prior denied sandbox deselection used. Selected80% whole-point floor below observed baseline; no artificial target or exclusions.

Extended existing CI only: reports, job summary, SHA-named90-day artifacts, failure-preserving gate. No paid service or expanded token permissions. Broader issue91 PR-comment/diff-gate/dashboard proposals remain explicitly outside baseline reporting.

Backlog: #86 closed after verifying all three license criteria, #152 reopened with synthetic completion preserved and original-trace gaps explicit. #151 remains closed. Expanded backlog work follows this coverage PR, using focused branches. Pending: final local validation, draft publication and full CI coverage evidence.
