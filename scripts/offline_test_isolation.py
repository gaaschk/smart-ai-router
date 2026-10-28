"""Mac development isolation: prevent default proxy memory integration access."""
import pytest

@pytest.fixture(autouse=True)
def isolate_default_proxy_memory(monkeypatch):
    from smart_ai_router.api import proxy
    monkeypatch.setattr(proxy, '_enrich_with_gbrain_context', lambda messages, *args, **kwargs: messages)
    monkeypatch.setattr(proxy, '_schedule_gbrain_save_back', lambda *args, **kwargs: None)
