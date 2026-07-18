"""MatrixStore — interface for persisting the capability matrix and provider config."""
from __future__ import annotations
from abc import ABC, abstractmethod
from smart_ai_router.models import ModelSpec, ProviderConfig


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
