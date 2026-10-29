# Issue89 provider circuits checkpoint

Baseaf7d089; branchfix/89-provider-circuits. Existing code has per-model time-window cooldown and pre-byte failover, not provider-wide half-open state. Implemented opt-in per-provider registry/client wrapper, process-local state and admin-only inspection. Default disabled; separate provider thresholds/recovery settings. Existing usage/tool health paths unchanged. Mocked integration covers Chat/Responses streaming/nonstreaming, provider-wide fallback and no all-open bypass; state/cancellation/concurrency controls included. No production settings/services or keys touched. Focused/full tests and CI results recorded with draft publication.

Review follow-up: preserve422 for invalid pinned/scoped/capability requests during
unrelated provider outages;503 requires a valid circuit-free diagnostic selection.
Added recovery/fallback counters and provider-only transition logs.26circuit tests
pass, including enabled agent fallback and ASGI disconnect releasing recovery lease.
