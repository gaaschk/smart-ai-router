"""MatrixStore — interface for persisting the capability matrix."""
from __future__ import annotations
from abc import ABC, abstractmethod
from capability_router.models import ModelSpec


class MatrixStore(ABC):
    @abstractmethod
    def all_models(self) -> list[ModelSpec]: ...

    @abstractmethod
    def upsert_model(self, spec: ModelSpec) -> None: ...

    @abstractmethod
    def get(self, value: str) -> ModelSpec | None: ...
