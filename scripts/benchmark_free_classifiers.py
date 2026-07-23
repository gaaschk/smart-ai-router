#!/usr/bin/env python3
"""
Benchmark free OpenRouter models as prompt classifiers.

Run this ON the mac mini (where the OpenRouter key lives in .env). It reads the
key locally, sends a fixed set of prompts to each free candidate model, and
reports per-model latency and whether the reply parses into a valid
(domain, complexity) — the two things that decide if a model is usable as the
SMART_ROUTER_CLASSIFIER_FALLBACK.

Usage:
    cd ~/ProjectHome/smart-ai-router
    uv run python scripts/benchmark_free_classifiers.py

No arguments. Prints a table; picks nothing automatically — you choose the
winner from the numbers.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from pathlib import Path

from smart_ai_router.llm_classifier import _SYSTEM_PROMPT, _parse_classification

import httpx

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Free candidates worth testing (small / fast / instruction-tuned). The big
# nemotron models are omitted — too slow for a hot-path classifier.
CANDIDATES = [
    "nvidia/nemotron-nano-9b-v2:free",
    "openai/gpt-oss-20b:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "inclusionai/ling-3.0-flash:free",
    "cohere/north-mini-code:free",
]

# (prompt, expected_domain) — expected is a sanity hint, not a hard assertion.
PROMPTS = [
    ("Derive the formula for the electronic orbitals about a hydrogen atom", "reasoning"),
    ("fix the null pointer bug in the login handler", "coding"),
    ("write a readme explaining how to install this tool", "docs"),
    ("hello", "general"),
    ("design a distributed rate limiter that survives node failures", "reasoning"),
]

_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=30.0)


def _read_key() -> str:
    """Read the OpenRouter key: env var, then .env, then the provider store.

    The app itself stores the key in the provider DB (via the setup wizard), so
    that's the most reliable source; env/.env are checked first for overrides.
    """
    import os

    for var in ("OPENROUTER_API_KEY", "OPENROUTER_KEY"):
        if os.environ.get(var):
            return os.environ[var]
    env = Path(__file__).parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            for var in ("OPENROUTER_API_KEY", "OPENROUTER_KEY"):
                if line.startswith(f"{var}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    # Fall back to the provider store — where the app actually keeps the key.
    try:
        from smart_ai_router.facade import CapabilityRouter

        cr = CapabilityRouter()
        for p in cr.all_providers():
            if p.kind == "openrouter" and p.api_key:
                return p.api_key
    except Exception:  # noqa: BLE001 — best-effort; benchmark reports "no key"
        pass
    return ""


async def _classify(client: httpx.AsyncClient, key: str, model: str, prompt: str):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "temperature": 0,
        "max_tokens": 40,
    }
    t = time.monotonic()
    try:
        resp = await client.post(
            f"{_OPENROUTER_BASE}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
        )
        dt = time.monotonic() - t
        if resp.status_code >= 400:
            return dt, None, f"HTTP {resp.status_code}"
        content = resp.json()["choices"][0]["message"]["content"]
        parsed = _parse_classification(content)
        return dt, parsed, None if parsed else f"unparseable: {content[:40]!r}"
    except Exception as e:  # noqa: BLE001 — benchmark, report everything
        return time.monotonic() - t, None, f"{type(e).__name__}: {e}"


async def main():
    key = _read_key()
    if not key:
        print("No OpenRouter key found (env OPENROUTER_API_KEY or .env). Aborting.")
        return
    print(f"Testing {len(CANDIDATES)} free models on {len(PROMPTS)} prompts each.\n")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for model in CANDIDATES:
            lats, oks, errs = [], 0, []
            for prompt, expected in PROMPTS:
                dt, parsed, err = await _classify(client, key, model, prompt)
                lats.append(dt)
                if parsed:
                    oks += 1
                    flag = "" if parsed[0] == expected else f" (got {parsed[0]}, expected {expected})"
                    print(f"  [{model:42}] {dt:5.2f}s {str(parsed):26}{flag}")
                else:
                    errs.append(err)
                    print(f"  [{model:42}] {dt:5.2f}s FAIL: {err}")
            med = statistics.median(lats)
            print(f"  => {model}: {oks}/{len(PROMPTS)} valid, median {med:.2f}s\n")


if __name__ == "__main__":
    asyncio.run(main())
