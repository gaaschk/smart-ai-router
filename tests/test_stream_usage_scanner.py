"""Tests for _StreamUsageScanner — recovering token usage from a streamed SSE
response without disturbing the forwarded bytes.

Providers that honor stream_options.include_usage emit a trailing chunk with a
top-level `usage` block; the scanner reports it as measured. Providers that
ignore the flag emit none, so the scanner estimates completion tokens from
accumulated delta content and prompt tokens from the request messages.
"""
from smart_ai_router.api.proxy import _StreamUsageScanner, _estimate_tokens


def _sse(obj):
    import json
    return f"data: {json.dumps(obj)}\n\n".encode()


def test_extracts_provider_usage_block_when_present():
    scanner = _StreamUsageScanner()
    scanner.feed(_sse({"choices": [{"delta": {"content": "hello"}}]}))
    scanner.feed(_sse({"choices": [], "usage": {
        "prompt_tokens": 42, "completion_tokens": 7}}))
    scanner.feed(b"data: [DONE]\n\n")

    usage, estimated = scanner.resolve([{"role": "user", "content": "hi"}])
    assert estimated is False
    assert usage == {"prompt_tokens": 42, "completion_tokens": 7}


def test_estimates_when_no_usage_block():
    scanner = _StreamUsageScanner()
    # 12 chars of streamed content → ~3 completion tokens (char/4).
    scanner.feed(_sse({"choices": [{"delta": {"content": "abcdef"}}]}))
    scanner.feed(_sse({"choices": [{"delta": {"content": "ghijkl"}}]}))
    scanner.feed(b"data: [DONE]\n\n")

    messages = [{"role": "user", "content": "x" * 40}]  # → ~10 prompt tokens
    usage, estimated = scanner.resolve(messages)
    assert estimated is True
    assert usage["completion_tokens"] == 3
    assert usage["prompt_tokens"] == 10


def test_handles_chunks_split_mid_line():
    """aiter_raw hands over arbitrary byte boundaries — a JSON line may arrive in
    pieces. The scanner must buffer partial lines until complete."""
    scanner = _StreamUsageScanner()
    full = _sse({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 9}})
    mid = len(full) // 2
    scanner.feed(full[:mid])
    scanner.feed(full[mid:])

    usage, estimated = scanner.resolve([])
    assert estimated is False
    assert usage["completion_tokens"] == 9


def test_ignores_malformed_json_lines():
    scanner = _StreamUsageScanner()
    scanner.feed(b"data: {not valid json\n\n")
    scanner.feed(_sse({"choices": [{"delta": {"content": "ok"}}]}))
    usage, estimated = scanner.resolve([])
    assert estimated is True  # no usage block seen; malformed line skipped


def test_estimate_tokens_helper():
    assert _estimate_tokens("") == 0
    assert _estimate_tokens("abcd") == 1
    assert _estimate_tokens("a" * 400) == 100
