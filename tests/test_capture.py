"""Turn capture: who gets captured, and whether a streamed reply survives it."""
import json

import pytest

from smart_ai_router import capture
from smart_ai_router.api.proxy import _StreamUsageScanner


@pytest.fixture
def capture_file(tmp_path, monkeypatch):
    path = tmp_path / "captures.jsonl"
    monkeypatch.setenv("SMART_ROUTER_CAPTURE_FILE", str(path))
    return path


def _on(monkeypatch, pct: int) -> None:
    monkeypatch.setenv("SMART_ROUTER_CAPTURE_TURN_PERCENT", str(pct))


def test_off_by_default(capture_file, monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_CAPTURE_TURN_PERCENT", raising=False)
    assert capture.percent() == 0
    assert capture.wanted("admin") is False


def test_only_admin_is_ever_captured(capture_file, monkeypatch):
    """The privacy guarantee: no percent lets someone else's prompt onto disk."""
    _on(monkeypatch, 100)
    assert capture.wanted("admin") is True
    for other in ("anon", "signup-7f3a", "kevin", ""):
        assert capture.wanted(other) is False


def test_percent_clamped(capture_file, monkeypatch):
    _on(monkeypatch, -5)
    assert capture.percent() == 0
    _on(monkeypatch, 500)
    assert capture.percent() == 100


def test_record_round_trips(capture_file, monkeypatch):
    _on(monkeypatch, 100)
    ok = capture.record(
        lane="orchestrator",
        routed_model="openrouter/anthropic/claude-haiku-4.5",
        messages=[{"role": "user", "content": "read setup.py"}],
        tools=[{"type": "function", "function": {"name": "Read"}}],
        tool_calls=[{"function": {"name": "Read",
                                  "arguments": '{"file_path": "setup.py"}'}}],
        content_len=0,
    )
    assert ok
    rows = capture.load()
    assert len(rows) == 1
    assert rows[0]["lane"] == "orchestrator"
    assert rows[0]["reference"]["tool_calls"][0]["function"]["name"] == "Read"


def test_load_skips_a_malformed_line(capture_file, monkeypatch):
    """A rotation mid-write truncates the last line; one bad row must not cost
    the rest of the corpus."""
    good = {"lane": "worker", "messages": [{"role": "user", "content": "hi"}],
            "tools": [], "reference": {"tool_calls": [], "content_len": 3}}
    capture_file.write_text(
        json.dumps(good) + "\n" + '{"messages": [{"role": "user"' + "\n",
        encoding="utf-8",
    )
    assert len(capture.load()) == 1


def test_record_never_raises_on_an_unwritable_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_CAPTURE_FILE",
                       str(tmp_path / "nope" / "captures.jsonl"))
    assert capture.record(lane="worker", routed_model="m", messages=[],
                          tools=None, tool_calls=[], content_len=0) is False


def test_scanner_reassembles_a_tool_call_split_across_chunks():
    """The reference for a streamed turn comes from the usage scanner, because the
    forwarded bytes are never buffered. Arguments arrive a few characters at a
    time, and a fragment can land mid-JSON."""
    scanner = _StreamUsageScanner()
    frames = [
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":'
        '{"name":"Read","arguments":""}}]}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":'
        '{"arguments":"{\\"file_"}}]}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"function":'
        '{"arguments":"path\\": \\"setup.py\\"}"}}]}}]}',
    ]
    blob = "".join(f"data: {f}\n\n" for f in frames) + "data: [DONE]\n\n"
    # Fed in small slices, so whole lines are assembled from partial ones.
    for i in range(0, len(blob), 17):
        scanner.feed(blob[i:i + 17].encode())

    calls = scanner.tool_calls()
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Read"
    assert json.loads(calls[0]["function"]["arguments"]) == {"file_path": "setup.py"}


def test_scanner_ignores_a_call_that_never_named_a_tool():
    """A stream cut off before the name arrived is not a reference."""
    scanner = _StreamUsageScanner()
    scanner.feed(b'data: {"choices":[{"delta":{"tool_calls":'
                 b'[{"index":0,"function":{"arguments":"{\\"a\\":1}"}}]}}]}\n\n')
    assert scanner.tool_calls() == []
    assert scanner.content_len == 0
