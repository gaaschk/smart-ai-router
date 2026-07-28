"""Bake off local Ollama models as the prompt classifier.

Exercises the REAL code path — llm_classifier.classify_llm, so the strict
json_schema response_format and the production max_tokens both apply. A model
that only works with a bigger budget or looser schema is not a candidate.

Usage:  python scripts/bakeoff_classifier.py [model ...]
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import time

from smart_ai_router.llm_classifier import classify_llm

BASE_URL = "http://127.0.0.1:11434/v1"

# (prompt, expected_domain, expected_complexity). Chosen so a right answer needs
# real judgment: several are deliberately worded to defeat keyword matching (the
# physics one never says "reasoning"; the regex one is short but not trivial).
CASES = [
    ("hi", "general", "trivial"),
    ("what's the capital of France?", "general", "trivial"),
    ("how do I reverse a list in python?", "coding", "trivial"),
    ("write a regex that validates an RFC 5322 email address", "coding", "moderate"),
    ("my pytest suite hangs on an async fixture, how do I debug it?", "coding", "moderate"),
    ("design a multi-region active-active postgres topology with failover",
     "reasoning", "hard"),
    ("derive the escape velocity of a body from conservation of energy",
     "reasoning", "hard"),
    ("summarize the differences between HTTP/2 and HTTP/3 for our docs",
     "docs", "moderate"),
    ("write a README section explaining how to install this CLI", "docs", "moderate"),
    ("prove that the square root of 2 is irrational", "reasoning", "hard"),
    ("refactor this 2000-line god class into cohesive modules", "coding", "hard"),
    ("what time zone is UTC-5?", "general", "trivial"),
]

REPEATS = 2  # temperature=0, but Ollama sampling is not bit-deterministic


async def bench(model: str) -> dict:
    lat: list[float] = []
    exact = domain_ok = parsed = 0
    total = 0
    failures: list[str] = []
    for prompt, want_d, want_c in CASES:
        for _ in range(REPEATS):
            total += 1
            t0 = time.perf_counter()
            got = await classify_llm(prompt, base_url=BASE_URL, model=model)
            lat.append(time.perf_counter() - t0)
            if got is None:
                failures.append(f"NONE  {prompt[:40]!r}")
                continue
            parsed += 1
            if got[0] == want_d:
                domain_ok += 1
            if got == (want_d, want_c):
                exact += 1
            else:
                failures.append(f"{got[0]}/{got[1]} want {want_d}/{want_c}  {prompt[:34]!r}")
    return {
        "model": model,
        "total": total,
        "parsed": parsed,
        "domain_ok": domain_ok,
        "exact": exact,
        "p50": statistics.median(lat),
        "p95": sorted(lat)[int(len(lat) * 0.95) - 1],
        "max": max(lat),
        "failures": failures,
    }


async def main() -> None:
    models = sys.argv[1:] or ["llama3.1:8b", "llama3.2:3b"]
    rows = []
    for m in models:
        print(f"\n=== {m}", flush=True)
        r = await bench(m)
        rows.append(r)
        print(f"  parsed   {r['parsed']}/{r['total']}")
        print(f"  domain   {r['domain_ok']}/{r['total']}")
        print(f"  exact    {r['exact']}/{r['total']}")
        print(f"  latency  p50={r['p50']:.2f}s p95={r['p95']:.2f}s max={r['max']:.2f}s")
        for f in r["failures"][:8]:
            print(f"    miss: {f}")

    print("\n" + "=" * 78)
    print(f"{'model':<34}{'parsed':>8}{'domain':>8}{'exact':>8}{'p50':>8}{'p95':>8}")
    for r in sorted(rows, key=lambda x: (-x["exact"], x["p50"])):
        print(f"{r['model']:<34}{r['parsed']:>8}{r['domain_ok']:>8}"
              f"{r['exact']:>8}{r['p50']:>7.2f}s{r['p95']:>7.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
