"""Compare Jev against the configured primary classifier without changing routing.

Run with PYTHONPATH=. python scripts/compare_jev_classifier.py --db ~/.smart_ai_router.db
Reads provider configuration using SQLite mode=ro; never initializes SqliteStore.
Only the synthetic labeled bakeoff prompts are sent; no user history is read.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sqlite3
import statistics
import time
from pathlib import Path

from bakeoff_classifier import CASES
from smart_ai_router.jev_classifier import classify_jev
from smart_ai_router.llm_classifier import classify_profile_llm, needs_refinement
from smart_ai_router.taxonomy import DEPTH_RANK


def configuration(path):
    with sqlite3.connect(Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        providers = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM providers WHERE enabled=1")}
        settings = dict(conn.execute("SELECT key,value FROM settings"))
    from smart_ai_router import settings as defaults
    model = settings.get("classifier_model", defaults.get_str("classifier_model"))
    primary = next((p for p in providers.values() if p["kind"] == "ollama"), None)
    if primary is None:
        raise ValueError("Configured classifier provider is unavailable")
    remote = next((p for p in providers.values() if p["kind"] == "openrouter"), {})
    if not remote.get("api_key"):
        raise ValueError("Enabled OpenRouter credentials are required")
    return model, primary["base_url"].rstrip("/") + (
        "" if primary["base_url"].rstrip("/").endswith("/v1") else "/v1"
    ), primary["api_key"], remote["api_key"]


def score(profile, case):
    _, field, depth, escalate, current = case
    got = next((d.depth for d in profile.domains if d.field == field), None)
    return {"field": got is not None,
            "depth_exact": got == depth,
            "depth_within_one": got is not None and abs(DEPTH_RANK[got] - DEPTH_RANK[depth]) <= 1,
            "missed_escalation": escalate and not needs_refinement(profile),
            "false_escalation": not escalate and needs_refinement(profile),
            "missed_search": current and not profile.needs_current_info(),
            "false_search": not current and profile.needs_current_info()}


async def run(args):
    model, url, primary_key, jev_key = configuration(args.db)
    cases = CASES + [
        ("Earlier user request: Debug my pytest async fixture hang.\nLatest user request: "
         "What should I check next?", "software_engineering", "practitioner", False, False),
        ("Earlier user request: Debug my pytest async fixture hang.\nLatest user request: "
         "New topic: what's the capital of France?", "general_knowledge", "surface", False, False),
        ("Earlier user request: Debug my pytest async fixture hang.\nLatest user request: "
         "Thanks!", "general_knowledge", "surface", False, False),
    ]
    rows = []
    for i, case in enumerate(cases[:args.limit] if args.limit else cases):
        start = time.monotonic()
        primary = await classify_profile_llm(case[0], base_url=url, model=model, api_key=primary_key)
        primary_ms = int((time.monotonic() - start) * 1000)
        jev = await classify_jev(case[0], api_key=jev_key)
        rows.append({"case": i, "prompt": case[0], "primary": {
            "profile": primary.to_dict() if primary else None, "latency_ms": primary_ms,
            "scores": score(primary, case) if primary else None}, "jev": {
            "profile": jev.profile.to_dict() if jev.profile else None, "latency_ms": jev.latency_ms,
            "scores": score(jev.profile, case) if jev.profile else None,
            "usage": jev.usage, "answers": jev.answers, "error": jev.error}})
        print(f"case {i + 1}/{len(cases)} primary={'ok' if primary else 'failed'} "
              f"jev={'ok' if jev.profile else jev.error}", flush=True)
        if jev.error.startswith("HTTP ") and not jev.profile:
            break  # don't keep paying/retrying an unavailable endpoint
    summary = {}
    for name in ("primary", "jev"):
        successes = [r[name] for r in rows if r[name]["scores"] is not None]
        latencies = sorted(r[name]["latency_ms"] for r in rows)
        summary[name] = {
            "parsed": len(successes), "total": len(rows),
            "p50_ms": statistics.median(latencies),
            "p95_ms": latencies[max(0, math.ceil(len(latencies) * .95) - 1)],
            "scores": {k: sum(r["scores"][k] for r in successes)
                       for k in successes[0]["scores"]} if successes else {}}
    costs = [u.get("cost") for r in rows for u in r["jev"]["usage"]]
    summary["jev"]["reported_cost_usd"] = sum(costs) if costs and all(
        isinstance(c, (int, float)) and math.isfinite(c) and c >= 0 for c in costs) else None
    summary["primary"]["provider_token_cost_usd"] = 0.0  # Ollama; excludes hardware/electricity
    result = {"primary_model": model, "summary": summary, "cases": rows,
              "limitations": "Synthetic fixtures; single pass; primary only (no refine/fallback); "
                              "probabilities retained, calibration not established; routing unchanged."}
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", default="/tmp/jev-classifier-comparison.json")
    parser.add_argument("--limit", type=int, default=0)
    asyncio.run(run(parser.parse_args()))
