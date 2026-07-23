"""
LLM-based prompt classifier — primary classification path.

Asks a small, fast local model (default: an Ollama model) to label a prompt's
(domain, complexity). This is more robust than keyword matching for prompts
whose vocabulary isn't in the deterministic classifier's hint sets (e.g. a
physics derivation that never uses the word "reasoning").

Design contract: this function NEVER raises and NEVER blocks the request for
long. On any failure — disabled, network error, timeout, malformed output, or
an unrecognized label — it returns None, and the caller falls back to the
deterministic classifier in classifier.py. Classification must never be the
reason a request fails.
"""
from __future__ import annotations

import json
import os

import httpx

# Valid label vocabularies — must match classifier.py exactly so the two paths
# are interchangeable and the router thresholds/competence keys line up.
_DOMAINS = frozenset({"coding", "docs", "reasoning", "general"})
_COMPLEXITIES = frozenset({"trivial", "moderate", "hard"})

# Default classifier model. Small + fast is the priority — classification is a
# trivial task on the hot path of every request, so avoid "thinking" models.
# Empty string (via env) disables the LLM path entirely → always fall back.
_DEFAULT_MODEL = "llama3.1:8b"

# Read budget covers a cold model load: Ollama unloads an idle model after a
# few minutes, and the first request then pays a one-time load cost (~8s for an
# 8B model on Apple silicon). Warm calls return in well under a second. A slow
# or truly hung model still degrades to the deterministic fallback rather than
# stalling the request indefinitely.
_TIMEOUT = httpx.Timeout(connect=3.0, read=20.0, write=3.0, pool=20.0)

_SYSTEM_PROMPT = (
    "You are a prompt classifier for an LLM router. Classify the user's prompt "
    "on two axes and reply with ONLY a compact JSON object, no prose, no code "
    "fences.\n"
    'Format: {"domain": <domain>, "complexity": <complexity>}\n'
    "domain is one of: coding, docs, reasoning, general\n"
    "  - coding: writing/fixing/reviewing code, APIs, databases, tests\n"
    "  - docs: documentation, guides, summaries, explanations, articles\n"
    "  - reasoning: analysis, planning, math/science, proofs, derivations, "
    "architecture, trade-offs\n"
    "  - general: anything else, small talk, simple lookups\n"
    "complexity is one of: trivial, moderate, hard\n"
    "  - trivial: one-liners, lookups, simple questions\n"
    "  - moderate: multi-step but well-scoped work\n"
    "  - hard: deep multi-step reasoning, derivations, system design, "
    "large/complex tasks\n"
    "Judge by the intellectual demand of the task, not its length."
)


def classifier_model() -> str:
    """The configured classifier model, or "" if the LLM path is disabled."""
    return os.environ.get("SMART_ROUTER_CLASSIFIER_MODEL", _DEFAULT_MODEL).strip()


def _parse_classification(text: str) -> tuple[str, str] | None:
    """Parse a model reply into a validated (domain, complexity), or None.

    Tolerant of code fences and surrounding prose: extracts the first {...}
    block. Returns None unless both labels are present and in-vocabulary.
    """
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    domain = str(obj.get("domain", "")).strip().lower()
    complexity = str(obj.get("complexity", "")).strip().lower()
    if domain in _DOMAINS and complexity in _COMPLEXITIES:
        return domain, complexity
    return None


async def classify_llm(
    prompt: str,
    *,
    base_url: str,
    model: str | None = None,
) -> tuple[str, str] | None:
    """Classify a prompt via a local LLM. Returns None on any failure.

    Args:
        prompt:   The user prompt text to classify.
        base_url: OpenAI-compatible base URL (e.g. "http://host:11434/v1").
        model:    Override the configured classifier model. If None, uses
                  classifier_model(); if that is "", the LLM path is disabled.
    """
    if not prompt or not prompt.strip():
        return None
    mdl = model if model is not None else classifier_model()
    if not mdl or not base_url:
        return None

    payload = {
        "model": mdl,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "temperature": 0,
        "max_tokens": 40,  # a tiny JSON object; no room needed for chatter
    }
    url = f"{base_url.rstrip('/')}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            return None
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return None
    return _parse_classification(content)
