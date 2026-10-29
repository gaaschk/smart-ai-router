# Security policy

## Reporting and current limitations

Do not put exploitable vulnerability details, credentials, private prompts, or
customer data in public issues or pull requests.

This project has not yet published a verified private reporting contact or a
response-time commitment. The setup is tracked in [issue #88](https://github.com/gaaschk/smart-ai-router/issues/88).
If GitHub shows **Report a vulnerability** in the repository's
[Security tab](https://github.com/gaaschk/smart-ai-router/security), use that private
flow. Its availability has not been verified by this policy change. If it is
unavailable, ask the maintainer in #88 to establish a private channel, without
including vulnerability details. Do not substitute a public issue for a private
report. No email address, acknowledgement SLA, or disclosure deadline is promised.

Once a private channel is established, provide the affected commit/version,
configuration with secrets removed, minimal reproduction, expected and observed
behavior, impact, and any suggested fix. Use synthetic records and placeholder
credentials. Coordinate disclosure timing with the maintainer; do not assume a
fixed deadline or permission to test other people's systems.

## Version support

There is no published supported-version window or backport guarantee. Include the
exact affected version and commit in a report. Do not treat the existence of a
release or a passing CI run as a security-support promise. The maintainer must
approve a support policy and reporting process before #88 is complete.

## Safe use and contribution

- Keep provider/admin keys out of source control, screenshots, logs and reports.
  Use the documented environment configuration and narrowly scoped credentials.
- If a key is exposed, revoke or rotate it through its provider and inspect usage.
  Deleting the visible copy alone does not invalidate it.
- Restrict access to the router, use TLS for remote access, and review enabled tools
  and spend limits before granting access. Avoid logging sensitive request bodies.
- Validate untrusted input and authorization at boundaries. Add regression tests
  for security fixes with synthetic data and no live credentials.
- Treat dependency audits and static analysis as evidence with limitations. Their
  presence does not establish that code is vulnerability-free.

Repository security settings and private reporting availability require separate
owner verification. This document does not enable or change those settings.
