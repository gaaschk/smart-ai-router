"""Shared pytest fixtures."""
import pytest

from smart_ai_router import settings as _settings


@pytest.fixture(autouse=True)
def _reset_settings_store():
    """Unbind the global settings store around every test.

    settings.bind_store() sets a module-global that create_app() populates. Left
    bound, a store from one test would leak into another (and env-fallback tests
    would unexpectedly read a prior test's saved DB value). Reset before and
    after so each test starts from clean env → default resolution unless it
    binds its own store.
    """
    _settings.bind_store(None)
    yield
    _settings.bind_store(None)
