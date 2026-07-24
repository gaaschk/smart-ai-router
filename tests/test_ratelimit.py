"""Tests for the pure rate-limit / quota logic."""
from datetime import datetime, timezone

from smart_ai_router.models import ApiKey, UsageRecord
from smart_ai_router.ratelimit import check_rate_limit, window_start_for


def _key(**kw):
    return ApiKey(key_hash="h", user="u", **kw)


def _usage(n, tokens_each=0):
    return [
        UsageRecord(user="u", prompt_tokens=tokens_each, completion_tokens=0)
        for _ in range(n)
    ]


def test_no_window_means_unlimited():
    status = check_rate_limit(_key(rl_max_req=1), _usage(100))
    assert status.allowed  # rl_window_s == 0 → limits off


def test_window_but_no_caps_is_unlimited():
    status = check_rate_limit(_key(rl_window_s=60), _usage(100))
    assert status.allowed


def test_request_cap_allows_under_limit():
    status = check_rate_limit(_key(rl_window_s=60, rl_max_req=5), _usage(4))
    assert status.allowed


def test_request_cap_blocks_at_limit():
    status = check_rate_limit(_key(rl_window_s=60, rl_max_req=5), _usage(5))
    assert not status.allowed
    assert "request quota" in status.reason
    assert status.limit == 5 and status.used == 5
    assert status.retry_after_s == 60


def test_token_cap_blocks_when_exceeded():
    # 3 requests * 500 tokens = 1500 >= 1000 cap
    status = check_rate_limit(
        _key(rl_window_s=60, rl_max_tokens=1000), _usage(3, tokens_each=500)
    )
    assert not status.allowed
    assert "token quota" in status.reason
    assert status.used == 1500


def test_token_cap_allows_under():
    status = check_rate_limit(
        _key(rl_window_s=60, rl_max_tokens=1000), _usage(1, tokens_each=500)
    )
    assert status.allowed


def test_window_start_is_before_now():
    key = _key(rl_window_s=60)
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
    start = window_start_for(key, now)
    assert start == "2026-07-24T11:59:00+00:00"
