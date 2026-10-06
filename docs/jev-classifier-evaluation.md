# Jev classifier evaluation

Jev is an experimental classifier candidate. It does not currently participate
in production routing. `smart_ai_router/jev_classifier.py` calls
`typesafe/jev-1.13` through OpenRouter's Decisions API, not `typesafe/jev-router`.

The adapter first selects up to three distinct fields, then evaluates their
depths, the five demands, and stakes. It returns the existing `PromptProfile`
shape and retains the provider's answers, probabilities, usage and latency.
Malformed decisions produce no profile; no thresholds or production defaults
are changed. The 0.5 yes/no threshold is experimental and uncalibrated.

## Measured comparison: October 6, 2026

Executed on mini against its configured `llama3.1:8b` primary classifier, using
24 existing synthetic bakeoff cases and three additional conversation cases:
continuation, topic switch, and courtesy. Neither the database nor live routing
was changed. Provider credentials stayed on mini and are absent from results.

| Metric | llama3.1:8b | Jev |
| --- | ---: | ---: |
| Valid profiles | 27/27 | 27/27 |
| Expected field present | 18/27 | 22/27 |
| Exact depth in expected field | 13/27 | 15/27 |
| Depth within one tier in expected field | 18/27 | 22/27 |
| Missed required escalation | 0 | 0 |
| Unnecessary escalation | 2 | 1 |
| Missed current-information check | 3 | 0 |
| Unnecessary current-information check | 0 | 1 |
| Median latency | 1,079 ms | 473 ms |
| p95 latency | 1,491 ms | 1,088 ms |
| Provider token cost | $0 locally | $0.004125492 |

Jev handled all three conversation cases as labeled. The local model treated
the courtesy reply as a continuation of the earlier software task.

These are fixture agreement scores, not established real-world accuracy. Some
labels are debatable: the fixture expects general knowledge for "Who runs the
Fed?", whereas both models selected finance. All 27 were synthetic; this was
one pass with no randomization, warm/cold-load control or statistical confidence
intervals. Only the primary classifier was compared, not production fallback
and refinement. Jev's two sequential calls are included in its timing. Local
cost excludes hardware and electricity. Reported probabilities are retained,
but their calibration and usefulness for fallback have not been measured.

Recommendation: continue evaluation before changing the primary classifier.
In particular, add adjudicated multi-domain, tool-workflow, and context cases,
repeat measurements, and test uncertainty thresholds on held-out cases.

## Reproduce

From the repository root on a configured host:

```sh
PYTHONPATH=. .venv/bin/python scripts/compare_jev_classifier.py \
  --db ~/.smart_ai_router.db --output /tmp/jev-classifier-comparison.json
```

This reads SQLite with `mode=ro`, loads existing enabled provider configuration,
and sends synthetic prompts to the configured Ollama primary and paid Jev API.
Use `--limit 1` for an API smoke test. The output contains per-case profiles,
scores, raw Jev answers and usage, but no credentials. HTTP errors stop the run
early; inspect `parsed` and `total` rather than treating an incomplete run as a
full comparison. Costs are unavailable if the provider omits them.

API sources:
[Jev tutorial](https://openrouter.ai/docs/guides/community/jev-tutorial) and
[Decisions reference](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request).
