# Issue87 CI checkpoint

Stacked on coverage draft158 atd32c2fb. Added Python3.10/3.11/3.12 matrix and unique coverage artifacts in existing workflow. Fatal Ruff rules pass; formatting reports154files needing changes. Bandit1.9.4 reports53findings (2high/11medium/40low), including legacy SHA1persistent namespace IDs; no blanket suppressions or silent migration. pip-audit2.10.1 audits97packages with zero known vulnerabilities. Formatting/security scans remain explicitly report-only pending triage; owner branch protection/deployment unchanged. YAML/TOML parse; no new workflow token permissions. CI matrix results and independent review pending.

Review4417439930: added workflow-level contents:read, disabling unspecified token permissions. This reduces access; no branch protection/settings changed. Prior matrix CI40947790393 passed1343tests3skips on each Python3.10/3.11/3.12; coverage80.41/80.40/80.40%. Fresh CI pending for permission fix.
