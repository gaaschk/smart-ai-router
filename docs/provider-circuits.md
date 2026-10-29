# Provider availability circuits

The existing model failure cooldown and pre-response failover remain in place. The opt-in `provider_circuit_enabled` setting adds **process-local provider** protection around Chat/Responses dispatch and server-side agent rounds. It is off by default; this change does not enable it on a deployment.

Each provider has separate closed/open/half-open state. Defaults are OpenRouter5failures/60seconds, Ollama3/30, Bedrock10/120. Settings `circuit_<provider>_failures` and `circuit_<provider>_recovery_seconds` configure these independently; values below one are clamped to one. HTTP429, HTTP5xx and transport errors count as availability failures. Other HTTP4xx responses mean the endpoint is reachable and do not open a provider circuit. Output-schema/semantic failures do not feed this availability signal.

An open provider is excluded from ordinary and canary selection, including all its models. Existing scoped/capability-qualified failover chooses another provider where possible; if all eligible providers are open, the router returns503 instead of deliberately bypassing the circuit. Direct requests racing that decision fail at dispatch. After recovery time expires, at most one actual caller probes the provider. Success closes it; failure reopens it. No scheduled/live probe or paid background call is created. Concurrent stale responses cannot close a circuit opened by newer failures.

Streaming holds the recovery slot until the stream completes. A transport failure opens/counts once; cancellation or early closure releases the probe slot without recording a provider failure. The existing rule against failover after emitting reply bytes is unchanged. Circuit transitions do not write usage rows, alter tool health, reset historical observations, or change caller restrictions.

`GET /api/provider-circuits` is explicitly admin-only and returns enabled state, process scope, each provider state/thresholds and attempt counters. It contains no keys, URLs, prompts or error text. Counters reflect outcomes accepted in the current generation; late stale completions are ignored. State resets on process restart and is **not shared between worker processes**. This does not protect separate sync/classifier/media clients; it covers the routed Chat/Responses/agent dispatch path.

Demonstration: `python -m pytest -q -p scripts.offline_test_isolation tests/test_provider_circuits.py tests/test_proxy_failover.py`. Tests use mocked providers and a controlled clock, including a one-probe concurrency check, both protocols/modes,503 failover, all-open rejection, cancellation and mid-stream failure. No production outage rehearsal or deployment occurred. Review before enabling this optional policy.

Selection failures return503 only when removing the circuit filter permits a valid
selection under the same pin/scope/capability constraints. Unrelated outages leave
invalid requests at422. This diagnostic selection never dispatches a provider call.

Status also exposes recovery_attempts, recoveries and fallbacks. Fallbacks count
actual reroutes away from that provider while circuits are enabled, including
same-provider model fallback; they do not count initial exclusion or failed reroute.
Structured logger events name only provider and transition/event, with no request
bodies or credentials. Operators can consume existing logs; no external alert
integration or notification destination is configured.
