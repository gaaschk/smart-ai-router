"""
Provider sync — fetch the live model catalog from each provider and upsert
into a MatrixStore.  Role-agnostic. No pricing tables — rates come from
the provider catalog directly.

Returns a SyncResult with counts of added/updated models and any errors.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field

from smart_ai_router.competence import infer_competence
from smart_ai_router.models import ModelSpec
from smart_ai_router.store.base import MatrixStore


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.added + self.updated


def sync_from_providers(
    store: MatrixStore,
    *,
    openrouter_key: str | None = None,
    ollama_base_url: str | None = None,
    bedrock_key: str | None = None,
    timeout: int = 15,
) -> SyncResult:
    """Fetch model catalogs from configured providers and upsert into the store.

    Providers are skipped silently when their credentials/URLs are not supplied.
    """
    result = SyncResult()

    if ollama_base_url:
        _sync_ollama(store, ollama_base_url.rstrip("/"), result, timeout)

    if openrouter_key:
        _sync_openrouter(store, openrouter_key, result, timeout)

    if bedrock_key:
        _sync_bedrock(store, result)

    return result


# ── Bedrock (Claude) ────────────────────────────────────────────────────────
# Bedrock's OpenAI-compatible endpoint uses stable us.anthropic.* model IDs.
# We seed a curated set of Claude models with benchmark-informed competence and
# a HIGH cost tier so the router only picks them when no cheaper model clears
# the quality bar (the built-in fallback), or when orchestrator mode forces it.

_BEDROCK_CLAUDE_MODELS = [
    # (model_id, cost_tier, ctx_k, competence)
    ("us.anthropic.claude-haiku-4-5",   4, 200,
     {"coding": 0.80, "docs": 0.78, "reasoning": 0.80, "general": 0.80}),
    ("us.anthropic.claude-sonnet-4-6",  8, 1000,
     {"coding": 0.88, "docs": 0.89, "reasoning": 0.89, "general": 0.89}),
    ("us.anthropic.claude-opus-4-8",   12, 1000,
     {"coding": 0.92, "docs": 0.94, "reasoning": 0.94, "general": 0.94}),
]


def _sync_bedrock(store: MatrixStore, result: SyncResult) -> None:
    existing = {s.value for s in store.all_models()}
    for mid, cost, ctx_k, comp in _BEDROCK_CLAUDE_MODELS:
        value = f"bedrock/{mid}"
        spec = ModelSpec(
            value=value,
            provider="bedrock",
            cost=cost,
            ctx_k=ctx_k,
            tools=True,
            vision=True,
            reliability=0.95,
            cost_input=0.0,
            cost_output=0.0,
            competence=comp,
        )
        store.upsert_model(spec)
        if value in existing:
            result.updated += 1
        else:
            result.added += 1


# ── Ollama ────────────────────────────────────────────────────────────────────

def _sync_ollama(
    store: MatrixStore,
    base_url: str,
    result: SyncResult,
    timeout: int,
) -> None:
    try:
        req = urllib.request.Request(
            f"{base_url}/api/tags",
            headers={"User-Agent": "smart-ai-router"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            tags = json.load(r)
    except Exception as e:
        result.errors.append(f"Ollama: {e}")
        return

    existing = {s.value for s in store.all_models()}
    for m in tags.get("models", []):
        name = m.get("name", "")
        if not name:
            continue
        value = f"ollama/{name}"
        size_gb = (m.get("size", 0) or 0) / 1e9
        ctx_k = 128 if size_gb > 30 else (32 if size_gb > 10 else 8)
        spec = ModelSpec(
            value=value,
            provider="ollama",
            cost=0,
            ctx_k=ctx_k,
            tools=False,
            reliability=1.0,
            cost_input=0.0,
            cost_output=0.0,
            competence=infer_competence(value),
        )
        store.upsert_model(spec)
        if value in existing:
            result.updated += 1
        else:
            result.added += 1


# ── OpenRouter ────────────────────────────────────────────────────────────────

def _sync_openrouter(
    store: MatrixStore,
    api_key: str,
    result: SyncResult,
    timeout: int,
) -> None:
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={
                "User-Agent": "smart-ai-router",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            catalog = json.load(r)
    except Exception as e:
        result.errors.append(f"OpenRouter: {e}")
        return

    existing = {s.value for s in store.all_models()}
    for m in catalog.get("data", []):
        mid = m.get("id", "")
        if not mid or mid.startswith("openrouter/") or "/" not in mid:
            continue
        arch = (m.get("architecture") or {})
        modality = arch.get("modality", "text->text")
        if "text->text" not in modality and "text+image->text" not in modality:
            continue

        value = f"openrouter/{mid}"
        ctx_k = (m.get("context_length") or 0) // 1000

        pr = m.get("pricing") or {}
        try:
            cost_input = round(float(pr.get("prompt", 0)) * 1_000_000, 4)
        except Exception:
            cost_input = 0.0
        try:
            cost_output = round(float(pr.get("completion", 0)) * 1_000_000, 4)
        except Exception:
            cost_output = 0.0

        # Cost tier for router sorting
        if cost_input == 0:
            cost = 1 if mid.endswith(":free") else 0
        elif cost_input < 0.1:
            cost = 1
        elif cost_input < 0.5:
            cost = 2
        elif cost_input < 1:
            cost = 3
        elif cost_input < 3:
            cost = 5
        elif cost_input < 8:
            cost = 8
        elif cost_input < 15:
            cost = 12
        else:
            cost = 15

        supports = m.get("supported_parameters") or []
        tools = "tools" in supports
        vision = "text+image->text" in modality
        reliability = 0.5 if mid.endswith(":free") else 0.9

        spec = ModelSpec(
            value=value,
            provider="openrouter",
            cost=cost,
            ctx_k=ctx_k,
            tools=tools,
            vision=vision,
            reliability=reliability,
            cost_input=cost_input,
            cost_output=cost_output,
            competence=infer_competence(value),
        )
        store.upsert_model(spec)
        if value in existing:
            result.updated += 1
        else:
            result.added += 1
