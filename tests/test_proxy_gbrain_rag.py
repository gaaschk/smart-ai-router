"""Tests for the proxy's GBrain RAG wiring: retrieval and save-back are both
scoped to the caller's own source_id, so one identity's chat never leaks
context into or out of another's.
"""
import asyncio
from unittest.mock import MagicMock, patch

from smart_ai_router.api.proxy import (
    _enrich_with_gbrain_context,
    _schedule_gbrain_save_back,
)


def test_enrich_passes_source_id_through_to_hybrid_query():
    fake_gbrain = MagicMock()
    fake_gbrain.hybrid_query.return_value = []
    with patch("smart_ai_router.api.proxy.get_gbrain", return_value=fake_gbrain):
        _enrich_with_gbrain_context(
            [], "a long enough prompt to search", source_id="kevin-gaasch"
        )
    fake_gbrain.hybrid_query.assert_called_once()
    _, kwargs = fake_gbrain.hybrid_query.call_args
    assert kwargs["source_id"] == "kevin-gaasch"


def test_enrich_defaults_to_the_shared_source_when_none_given():
    fake_gbrain = MagicMock()
    fake_gbrain.hybrid_query.return_value = []
    with patch("smart_ai_router.api.proxy.get_gbrain", return_value=fake_gbrain):
        _enrich_with_gbrain_context([], "a long enough prompt to search")
    _, kwargs = fake_gbrain.hybrid_query.call_args
    assert kwargs["source_id"] == ""


def test_enrich_prepends_context_from_results():
    fake_gbrain = MagicMock()
    fake_gbrain.hybrid_query.return_value = [
        {"title": "Doc", "chunk_text": "relevant fact"}
    ]
    with patch("smart_ai_router.api.proxy.get_gbrain", return_value=fake_gbrain):
        messages = _enrich_with_gbrain_context(
            [{"role": "user", "content": "hi"}],
            "a long enough prompt to search",
            source_id="kevin-gaasch",
        )
    assert messages[0]["role"] == "system"
    assert "relevant fact" in messages[0]["content"]


def test_enrich_skips_search_for_short_prompts():
    fake_gbrain = MagicMock()
    with patch("smart_ai_router.api.proxy.get_gbrain", return_value=fake_gbrain):
        messages = _enrich_with_gbrain_context([{"role": "user", "content": "hi"}], "hi")
    fake_gbrain.hybrid_query.assert_not_called()
    assert messages == [{"role": "user", "content": "hi"}]


def test_save_back_is_skipped_for_the_shared_default_source():
    # source_id == "" means admin/open-mode reading the shared brain -- saving
    # every chat turn into that shared space would pollute retrieval for
    # everyone, so save-back must no-op here.
    fake_gbrain = MagicMock()
    with patch("smart_ai_router.api.proxy.get_gbrain", return_value=fake_gbrain):
        _schedule_gbrain_save_back("admin", "", "question", "answer")
    fake_gbrain.remember.assert_not_called()


def test_save_back_is_skipped_for_empty_exchanges():
    fake_gbrain = MagicMock()
    with patch("smart_ai_router.api.proxy.get_gbrain", return_value=fake_gbrain):
        _schedule_gbrain_save_back("kevin-gaasch", "kevin-gaasch", "", "answer")
        _schedule_gbrain_save_back("kevin-gaasch", "kevin-gaasch", "question", "")
    fake_gbrain.remember.assert_not_called()


def test_save_back_ensures_the_source_then_remembers_with_it(monkeypatch):
    fake_gbrain = MagicMock()

    async def _run():
        with patch("smart_ai_router.api.proxy.get_gbrain", return_value=fake_gbrain):
            _schedule_gbrain_save_back(
                "kevin-gaasch", "kevin-gaasch", "What is X?", "X is Y."
            )
            # Let the scheduled task actually run.
            await asyncio.sleep(0.05)

    asyncio.run(_run())

    fake_gbrain.ensure_source.assert_called_once_with("kevin-gaasch")
    fake_gbrain.remember.assert_called_once()
    args = fake_gbrain.remember.call_args[0]
    assert args[3] == "kevin-gaasch"  # source_id positional arg
    assert "What is X?" in args[1]
    assert "X is Y." in args[1]
