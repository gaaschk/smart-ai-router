"""
CapabilityRouter — main façade wiring store + router + sync + pricing together.
"""
from __future__ import annotations

from pathlib import Path

from smart_ai_router.models import ModelSpec
from smart_ai_router.store.base import MatrixStore
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router import router as _router
from smart_ai_router import pricing as _pricing
from smart_ai_router.sync import SyncResult, sync_from_providers


class CapabilityRouter:
    def __init__(
        self,
        store: MatrixStore | None = None,
        thresholds: dict | None = None,
    ):
        self._store = store or SqliteStore()
        self._thresholds = thresholds  # None → use DEFAULT_THRESHOLDS

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(
        self,
        domain: str,
        complexity: str,
        *,
        needs_tools: bool = False,
        est_tokens: int = 0,
        exclude: set[str] | None = None,
    ) -> str:
        """Return the optimal model string for the given hints.

        Raises RuntimeError if the matrix is empty (run sync() first).
        """
        return _router.route(
            self._store,
            domain=domain,
            complexity=complexity,
            needs_tools=needs_tools,
            est_tokens=est_tokens,
            exclude=exclude,
            thresholds=self._thresholds,
        )

    # ── Sync ─────────────────────────────────────────────────────────────────

    def sync(
        self,
        *,
        openrouter_key: str | None = None,
        ollama_base_url: str | None = None,
        timeout: int = 15,
    ) -> SyncResult:
        """Fetch live model catalogs and upsert into the store."""
        return sync_from_providers(
            self._store,
            openrouter_key=openrouter_key,
            ollama_base_url=ollama_base_url,
            timeout=timeout,
        )

    # ── Pricing ───────────────────────────────────────────────────────────────

    def cost_for(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float | None:
        """Return USD cost estimate for a completed call, or None if unknown."""
        spec = self._store.get(model)
        if spec is None:
            return None
        return _pricing.cost_for(spec, prompt_tokens, completion_tokens)

    # ── Store access ─────────────────────────────────────────────────────────

    def all_models(self) -> list[ModelSpec]:
        return self._store.all_models()

    def get_model(self, value: str) -> ModelSpec | None:
        return self._store.get(value)

    def upsert_model(self, spec: ModelSpec) -> None:
        self._store.upsert_model(spec)
