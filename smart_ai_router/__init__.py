"""
smart-ai-router — vendor-agnostic LLM capability router.

Public API:
    CapabilityRouter   main façade
    MatrixStore        persistence interface
    SqliteStore        default SQLite implementation
    ModelSpec          per-model data container
    classify           optional role-agnostic prompt classifier
"""
from smart_ai_router.models import ModelSpec, ProviderConfig
from smart_ai_router.store.base import MatrixStore
from smart_ai_router.store.sqlite_store import SqliteStore
from smart_ai_router.classifier import classify
from smart_ai_router.facade import CapabilityRouter

__all__ = [
    "CapabilityRouter",
    "MatrixStore",
    "SqliteStore",
    "ModelSpec",
    "ProviderConfig",
    "classify",
]
