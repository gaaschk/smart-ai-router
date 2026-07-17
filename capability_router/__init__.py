"""
capability-router — vendor-agnostic LLM capability router.

Public API:
    CapabilityRouter   main façade
    MatrixStore        persistence interface
    SqliteStore        default SQLite implementation
    ModelSpec          per-model data container
    classify           optional role-agnostic prompt classifier
"""
from capability_router.models import ModelSpec
from capability_router.store.base import MatrixStore
from capability_router.store.sqlite_store import SqliteStore
from capability_router.classifier import classify
from capability_router.facade import CapabilityRouter

__all__ = [
    "CapabilityRouter",
    "MatrixStore",
    "SqliteStore",
    "ModelSpec",
    "classify",
]
