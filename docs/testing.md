# Test coverage and local validation

Install the existing development extras in an isolated virtual environment (`pip install -e ".[dev]"`). Python 3.12 matches CI. Coverage.py is a development dependency; pytest remains the test runner.

```sh
python -m coverage erase
python -m coverage run -m pytest
python -m coverage report
python -m coverage html
python -m coverage xml
python -m coverage json
```

The configuration measures **all Python files in `smart_ai_router` and `scripts`**, including unimported modules. Tests, virtual environments and browser JavaScript are outside those source roots; this is not JavaScript or browser coverage. No new line exclusions or omitted application files were added. Coverage.py's existing/default exclusions remain in effect. Branch coverage is enabled; the headline percentage combines executed statements and branch destinations. Line and branch totals are also available separately in JSON/XML. Subprocess executions are not combined into the baseline; missed subprocess-only paths remain visible as uncovered.

## Measured baseline and gate

Base revision: `af7d089bc37043bf915b078558fc2293e92d4b97`. Measured on macOS / Python 3.12.13 / coverage.py 7.17.0 with the existing offline proxy-memory isolation plugin and the previously denied nested sandbox case deselected:

- 1,345 tests passed, one deselected.
- Statements: **6,766 / 8,273 (81.78%)**.
- Branch destinations: **1,896 / 2,490 (76.14%)**.
- Combined: **80.47942023599369%**, displayed **80.48%**.

The initial gate is **80% combined**, the whole percentage point immediately below the measured baseline. It gives 0.48 percentage points of headroom for platform differences; it does not claim zero regression tolerance, 80% branch coverage, or complete coverage of critical paths. Do not lower the gate or omit modules to make a change pass. Raise it through review after sustained improvement. The issue's proposed 90% new-code and 95% critical-path targets were not measured and are not claimed here.

The baseline is an observation of the current tests, not a code-quality certification. Security, cancellation, protocol, and cost correctness still require meaningful regression tests and review. macOS sandbox-only cases remain unverified on Linux and when local nested sandbox execution is denied.

## CI evidence and trends

The **existing** `CI` workflow runs the full suite once under coverage, then produces terminal text, HTML, XML, JSON and pytest JUnit XML. Coverage reports and the job summary run even after test failures; a test failure or coverage below the floor still fails CI. Missing coverage data also fails reporting/gating. Artifacts are named with the commit SHA and retained for 90 days. Open the PR's CI check → run summary → artifacts; download `htmlcov/index.html` with its neighboring assets for per-file missed lines/branches.

Compare `totals` in `coverage.json` from successive commit artifacts to track changes with consistent source roots, branch mode, Python version and test selection. Report partial/platform-filtered runs as such; do not compare them as though they were the Linux full suite. Retention is bounded, so download evidence needed longer than 90 days.

No third-party coverage service, upload token, PR-comment bot or additional workflow permissions are needed. The README badge is the CI/coverage **status**, not a numeric percentage badge. Job summaries and artifacts provide PR-visible reports without granting comment-write permissions. Historical service dashboards, automated PR comments and per-diff gates remain outside this baseline implementation.

## Mac development isolation

If this host has a live GBrain CLI installed, use the existing test-only isolation plugin to prevent default proxy enrichment/save-back:

```sh
python -m coverage run -m pytest -p scripts.offline_test_isolation \
  --deselect=tests/test_tools.py::test_bash_runs_in_workspace_when_enabled
python -m coverage report
```

The deselection above reflects a previously verified host denial (`sandbox_apply: Operation not permitted`), not an application skip to improve coverage. Do not retry by weakening the host sandbox. On a host where that case is permitted, run it normally. No provider keys, live provider calls or production DB are needed for the suite. A partial run's successful floor check does not substitute for the full CI run.
