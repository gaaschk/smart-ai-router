"""MatrixStore — interface for persisting the capability matrix and provider config."""
from __future__ import annotations
from abc import ABC, abstractmethod
from smart_ai_router.models import ApiKey, ModelSpec, ProviderConfig, UsageRecord


class MatrixStore(ABC):
    @abstractmethod
    def all_models(self) -> list[ModelSpec]: ...

    @abstractmethod
    def upsert_model(self, spec: ModelSpec) -> None: ...

    @abstractmethod
    def get(self, value: str) -> ModelSpec | None: ...

    # ── Provider config ───────────────────────────────────────────────────────

    @abstractmethod
    def all_providers(self) -> list[ProviderConfig]: ...

    @abstractmethod
    def get_provider(self, name: str) -> ProviderConfig | None: ...

    @abstractmethod
    def upsert_provider(self, cfg: ProviderConfig) -> None: ...

    @abstractmethod
    def delete_provider(self, name: str) -> bool: ...

    # ── API keys (per-user auth) ────────────────────────────────────────────────

    @abstractmethod
    def all_api_keys(self) -> list[ApiKey]: ...

    @abstractmethod
    def create_api_key(self, key: ApiKey) -> ApiKey: ...

    @abstractmethod
    def get_api_key_by_hash(self, key_hash: str) -> ApiKey | None: ...

    @abstractmethod
    def touch_api_key(self, key_hash: str) -> None:
        """Record that a key was just used (updates last_used_at)."""

    @abstractmethod
    def set_api_key_enabled(self, key_prefix: str, enabled: bool) -> bool:
        """Enable/disable a key by prefix. Returns False if no key matched."""

    @abstractmethod
    def delete_api_key(self, key_prefix: str) -> bool: ...

    # ── Usage log ────────────────────────────────────────────────────────────

    @abstractmethod
    def record_usage(self, usage: UsageRecord) -> None: ...

    @abstractmethod
    def recent_usage(self, user: str, since_ts: str) -> list[UsageRecord]:
        """Usage rows for a user at/after an ISO timestamp (for quota checks)."""
