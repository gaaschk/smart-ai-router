"""
CapabilityRouter — main façade wiring store + router + sync + pricing together.
"""
from __future__ import annotations

from pathlib import Path

from smart_ai_router.models import ApiKey, ModelSpec, ProviderConfig, UsageRecord
from smart_ai_router.scope import ModelScope
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
        needs_vision: bool = False,
        est_tokens: int = 0,
        exclude: set[str] | None = None,
        scope: ModelScope | None = None,
    ) -> str:
        """Return the optimal model string for the given hints.

        Raises RuntimeError if the matrix is empty (run sync() first).
        """
        return _router.route(
            self._store,
            domain=domain,
            complexity=complexity,
            needs_tools=needs_tools,
            needs_vision=needs_vision,
            est_tokens=est_tokens,
            exclude=exclude,
            scope=scope,
            thresholds=self._thresholds,
        )

    # ── Sync ─────────────────────────────────────────────────────────────────

    def sync(
        self,
        *,
        openrouter_key: str | None = None,
        ollama_base_url: str | None = None,
        bedrock_key: str | None = None,
        timeout: int = 15,
    ) -> SyncResult:
        """Fetch live model catalogs and upsert into the store.

        When called with no explicit credentials, falls back to enabled
        providers stored in the database.
        """
        explicit = openrouter_key or ollama_base_url or bedrock_key
        if explicit:
            return sync_from_providers(
                self._store,
                openrouter_key=openrouter_key,
                ollama_base_url=ollama_base_url,
                bedrock_key=bedrock_key,
                timeout=timeout,
            )

        # Use stored provider configs
        result = SyncResult()
        for cfg in self._store.all_providers():
            if not cfg.enabled:
                continue
            partial = sync_from_providers(
                self._store,
                openrouter_key=cfg.api_key if cfg.kind == "openrouter" else None,
                ollama_base_url=cfg.base_url if cfg.kind == "ollama" else None,
                bedrock_key=cfg.api_key if cfg.kind == "bedrock" else None,
                timeout=cfg.timeout,
            )
            result.added += partial.added
            result.updated += partial.updated
            result.errors.extend(partial.errors)
        return result

    # ── Provider config ───────────────────────────────────────────────────────

    def all_providers(self) -> list[ProviderConfig]:
        return self._store.all_providers()

    def get_provider(self, name: str) -> ProviderConfig | None:
        return self._store.get_provider(name)

    def upsert_provider(self, cfg: ProviderConfig) -> None:
        self._store.upsert_provider(cfg)

    def delete_provider(self, name: str) -> bool:
        return self._store.delete_provider(name)

    # ── API keys (per-user auth) ────────────────────────────────────────────────

    def all_api_keys(self) -> list[ApiKey]:
        return self._store.all_api_keys()

    def create_api_key(self, key: ApiKey) -> ApiKey:
        return self._store.create_api_key(key)

    def get_api_key_by_hash(self, key_hash: str) -> ApiKey | None:
        return self._store.get_api_key_by_hash(key_hash)

    def touch_api_key(self, key_hash: str) -> None:
        self._store.touch_api_key(key_hash)

    def set_api_key_enabled(self, key_prefix: str, enabled: bool) -> bool:
        return self._store.set_api_key_enabled(key_prefix, enabled)

    def delete_api_key(self, key_prefix: str) -> bool:
        return self._store.delete_api_key(key_prefix)

    # ── Usage log ────────────────────────────────────────────────────────────

    def record_usage(self, usage: UsageRecord) -> None:
        self._store.record_usage(usage)

    def recent_usage(self, user: str, since_ts: str) -> list[UsageRecord]:
        return self._store.recent_usage(user, since_ts)

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
