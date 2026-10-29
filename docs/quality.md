# CI quality checks

Issue #87's initial “no CI” premise is obsolete. The existing workflow now runs tests/coverage on Python 3.10, 3.11 and 3.12, with distinct artifacts, plus one quality job. Install tools locally with `pip install -e ".[dev,quality]"`.

```sh
ruff check smart_ai_router scripts tests --select E9,F63,F7,F82
ruff format --check smart_ai_router scripts tests
bandit -r smart_ai_router -f json -o bandit.json
pip-audit --local --skip-editable --format json --output dependency-audit.json
```

Fatal syntax/control-flow/undefined-name errors are a blocking gate. Formatting, Bandit and dependency scanning initially publish reports with their actual exit status visible in Actions; their `continue-on-error` steps do not block CI. A green job is **not** a clean security audit. Inspect reports and failed service/tool steps before release. This avoids concealing existing findings or introducing an unrelated whole-repository formatting rewrite. Promote individual checks to blocking only after review/triage; do not blanket-suppress application paths or issue types.

Local baseline (ruff 0.17.3, Bandit 1.9.4): fatal lint passed. Bandit reports 53 findings: 2 high, 11 medium and 40 low. The high findings are B324 at `gbrain_client.py`'s legacy SHA-1-derived source IDs. Those IDs persist as user-memory namespaces; replacing the hash changes lookup locations. Neither silently changing IDs nor claiming this security-sensitive use is non-security is appropriate. Review collision/normalization isolation and a migration before changing this mapping. The findings remain in the published report, not an allowlist.

`pip-audit` checks the installed public package/version list using the public vulnerability service; it skips the editable local project, whose dependency versions are still audited. No authentication token is needed. Reports reflect the resolved environment and advisory database at run time, not all possible deployments. A service outage is not a clean audit result.

Branch protection, required status-check configuration, paid security services and automatic deployment remain owner-controlled and unchanged. Issue #87 stays open until report findings and the remaining policy gates are resolved. Version-matrix success is verified by CI, not inferred from a single local Python installation.

Local dependency audit (pip-audit2.10.1,2026-10-29):97 packages,zero known vulnerabilities. Ruff formatting baseline:154 files would change,5 already formatted. These are explicit current observations, not exemptions or a clean Bandit verdict.

## Formatting follow-up

The formatter baseline has been applied mechanically across154files, with docstring whitespace as the only AST differences. Formatting is now blocking; security reports remain visible pending scoped triage. This change makes no intentional runtime behavior changes.
