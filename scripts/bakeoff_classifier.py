"""Bake off local Ollama models as the triage prompt profiler.

Exercises the REAL code path — llm_classifier.classify_profile_llm — so the
strict json_schema response_format and the production max_tokens both apply. A
model that only works with a bigger budget or a looser schema is not a candidate.

What to look at, in priority order:
  1. parsed    — can it emit the profile schema at all? Below ~90% it's unusable
                 as triage: every miss silently falls through to the keyword
                 classifier.
  2. escalate  — does it flag the consequential prompts and only those? This is
                 the number the two-speed design rests on. Reported as two
                 separate errors because they cost differently: a MISSED
                 escalation answers a high-stakes prompt on a cheap model (the
                 fabricated-regulatory-answer failure), while a FALSE escalation
                 just spends a fraction of a cent on a refine call. Prefer a
                 model that errs toward false escalations.
  3. field     — did it name the right field? Credited if the expected field
                 appears anywhere in the profile, since naming an extra field
                 only raises the bar.
  4. depth     — within one tier of expected. Exact depth agreement from a 3B
                 model is not a realistic bar, and off-by-one is what the refine
                 pass exists to fix.

Last measured (2026-08-19, 17 cases × 2 repeats, live Ollama on the deploy host).
This is what set the shipped `classifier_model` default:

  model                 parsed  field  depth  escal  MISS  false     p50     p95
  llama3.1:8b            32/32     26     26     28     0      4   1.04s   1.83s
  qwen2.5:3b-instruct    32/32     19     18     29     2      1   0.56s   0.79s
  gemma4:12b             10/32     10     10     10     0      0  10.35s  11.87s
  qwen3:30b-a3b           0/32      0      0      0     0      0   4.23s   4.38s

Read in priority order that says llama3.1:8b: it misses no escalation where the
3B misses two, and buys that for ~0.5s. The 3B's losses are real reads, not parse
noise — it answers "write a regex that validates an RFC 5322 email address" with
`general_knowledge @ surface`. gemma4:12b parses only a third of the time and is
10× slower; qwen3:30b-a3b is a thinking model and returns empty content with
finish_reason=length every time, which is the budget failure named above.

Usage:  python scripts/bakeoff_classifier.py [model ...]
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import time

from smart_ai_router.llm_classifier import classify_profile_llm, needs_refinement
from smart_ai_router.taxonomy import DEPTH_RANK

BASE_URL = "http://127.0.0.1:11434/v1"

# (prompt, expected_field, expected_depth, should_escalate)
#
# Chosen so a right answer needs real judgment. Several deliberately defeat
# keyword matching: the physics one never says "reasoning"; the regex one is
# short but not trivial; the GDPR one uses heavy regulatory vocabulary for a
# question a 3B model can answer, which is the false-escalation trap.
CASES = [
    ("hi", "general_knowledge", "surface", False),
    ("what's the capital of France?", "general_knowledge", "surface", False),
    ("what does GDPR stand for?", "general_knowledge", "surface", False),
    ("how do I reverse a list in python?", "software_engineering", "surface", False),
    ("write a regex that validates an RFC 5322 email address",
     "software_engineering", "practitioner", False),
    ("my pytest suite hangs on an async fixture, how do I debug it?",
     "software_engineering", "practitioner", False),
    ("summarize the differences between HTTP/2 and HTTP/3 for our docs",
     "technical_writing", "practitioner", False),
    ("write a README section explaining how to install this CLI",
     "technical_writing", "surface", False),
    ("design a multi-region active-active postgres topology with failover",
     "systems_architecture", "specialist", False),
    ("derive the escape velocity of a body from conservation of energy",
     "natural_science", "practitioner", False),
    ("prove that the square root of 2 is irrational", "math_formal", "practitioner", False),
    ("refactor this 2000-line god class into cohesive modules",
     "software_engineering", "practitioner", False),
    # The escalation cases: high stakes, or two specialist fields at once. These
    # are the prompts the router used to answer with whatever cheap model sat at
    # exactly the old 0.88 bar.
    ("Analyze the legal, ethical, and technical implications of deploying an "
     "autonomous reactor control system, citing the specific regulatory "
     "provisions it would violate in each of 48 jurisdictions.",
     "law_regulatory", "frontier", True),
    ("My 4-year-old weighs 16kg and has a fever of 39.4C. What acetaminophen "
     "dose should I give, and when should I go to the ER?",
     "medicine_health", "specialist", True),
    ("Is our Delaware C-corp's revenue-share arrangement with an EU processor "
     "a GDPR joint-controller relationship, and what does that do to our "
     "Article 28 obligations?",
     "law_regulatory", "specialist", True),
    ("Derive the neutron flux distribution in a cylindrical reactor core and "
     "explain how the result constrains the licensing basis for the design.",
     "natural_science", "frontier", True),
]

REPEATS = 2  # temperature=0, but Ollama sampling is not bit-deterministic


def _depth_of(profile, want_field: str) -> str | None:
    """The depth this profile assigns `want_field`, or None if it never named it."""
    for need in profile.domains:
        if need.field == want_field:
            return need.depth
    return None


async def bench(model: str) -> dict:
    lat: list[float] = []
    parsed = field_ok = primary_ok = depth_ok = escalate_ok = 0
    missed_escalations = false_escalations = 0
    total = 0
    failures: list[str] = []

    for prompt, want_field, want_depth, want_esc in CASES:
        for _ in range(REPEATS):
            total += 1
            t0 = time.perf_counter()
            profile = await classify_profile_llm(prompt, base_url=BASE_URL, model=model)
            lat.append(time.perf_counter() - t0)
            if profile is None:
                failures.append(f"NONE          {prompt[:44]!r}")
                continue
            parsed += 1

            got_depth = _depth_of(profile, want_field)
            if got_depth is not None:
                field_ok += 1
                if abs(DEPTH_RANK[got_depth] - DEPTH_RANK[want_depth]) <= 1:
                    depth_ok += 1
                else:
                    failures.append(
                        f"depth {got_depth}/want {want_depth}  {prompt[:38]!r}"
                    )
            else:
                failures.append(
                    f"field {profile.primary_field()}/want {want_field}  {prompt[:34]!r}"
                )
            if profile.primary_field() == want_field:
                primary_ok += 1

            got_esc = needs_refinement(profile)
            if got_esc == want_esc:
                escalate_ok += 1
            elif want_esc:
                missed_escalations += 1
                failures.append(f"MISSED ESCALATION  {prompt[:40]!r}")
            else:
                false_escalations += 1
                failures.append(f"false escalation   {prompt[:40]!r}")

    return {
        "model": model,
        "total": total,
        "parsed": parsed,
        "field_ok": field_ok,
        "primary_ok": primary_ok,
        "depth_ok": depth_ok,
        "escalate_ok": escalate_ok,
        "missed": missed_escalations,
        "false": false_escalations,
        "p50": statistics.median(lat),
        "p95": sorted(lat)[int(len(lat) * 0.95) - 1],
        "max": max(lat),
        "failures": failures,
    }


async def main() -> None:
    models = sys.argv[1:] or ["qwen2.5:3b-instruct", "llama3.1:8b"]
    rows = []
    for m in models:
        print(f"\n=== {m}", flush=True)
        r = await bench(m)
        rows.append(r)
        print(f"  parsed    {r['parsed']}/{r['total']}")
        print(f"  field     {r['field_ok']}/{r['total']}  (primary {r['primary_ok']})")
        print(f"  depth±1   {r['depth_ok']}/{r['total']}")
        print(f"  escalate  {r['escalate_ok']}/{r['total']}  "
              f"(MISSED {r['missed']}, false {r['false']})")
        print(f"  latency   p50={r['p50']:.2f}s p95={r['p95']:.2f}s max={r['max']:.2f}s")
        for f in r["failures"][:10]:
            print(f"    {f}")

    print("\n" + "=" * 86)
    print(f"{'model':<30}{'parsed':>8}{'field':>7}{'depth':>7}{'escal':>7}"
          f"{'MISS':>6}{'false':>7}{'p50':>8}{'p95':>8}")
    # Sorted by missed escalations first: a model that answers a high-stakes
    # prompt cheaply is worse than a slow one, whatever its other numbers say.
    for r in sorted(rows, key=lambda x: (x["missed"], -x["escalate_ok"], x["p50"])):
        print(f"{r['model']:<30}{r['parsed']:>8}{r['field_ok']:>7}{r['depth_ok']:>7}"
              f"{r['escalate_ok']:>7}{r['missed']:>6}{r['false']:>7}"
              f"{r['p50']:>7.2f}s{r['p95']:>7.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
