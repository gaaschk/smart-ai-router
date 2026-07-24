"""Per-user rate limits / quotas, computed from the usage log.

A key may cap requests and/or tokens over a rolling time window:

  * rl_window_s  — window length in seconds (0 disables all limits for the key)
  * rl_max_req   — max requests in the window (0 = no request cap)
  * rl_max_tokens— max total tokens (prompt + completion) in the window (0 = none)

Enforcement is a read of the `usage_log` for the user since (now - window),
summed and compared to the caps. This is a sliding-window count over recorded
usage — approximate at the margins (a request is counted once its usage row is
written, which for streaming happens in a later phase), but sufficient to stop a
key that's over budget. It never blocks admin/open requests, which have no key.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from smart_ai_router.models import ApiKey, UsageRecord


@dataclass(frozen=True)
class RateLimitStatus:
    allowed: bool
    reason: str = ""                 # human-readable, for the 429 body
    limit: int = 0                   # the cap that was hit (req or tokens)
    used: int = 0                    # usage in the window (of the hit dimension)
    window_s: int = 0
    retry_after_s: int = 0           # seconds until the window frees up


def _window_start(window_s: int, now: datetime) -> str:
    return (now - timedelta(seconds=window_s)).isoformat()


def check_rate_limit(
    key: ApiKey,
    recent: list[UsageRecord],
    *,
    now: datetime | None = None,
) -> RateLimitStatus:
    """Decide whether a key may make another request, given its recent usage.

    `recent` should be the user's usage rows at/after the window start (the
    caller fetches them via store.recent_usage(user, window_start)). Passing the
    already-filtered list keeps this function pure and unit-testable.
    """
    window = key.rl_window_s or 0
    if window <= 0 or (not key.rl_max_req and not key.rl_max_tokens):
        return RateLimitStatus(allowed=True, window_s=window)

    req_count = len(recent)
    token_count = sum(r.prompt_tokens + r.completion_tokens for r in recent)

    if key.rl_max_req and req_count >= key.rl_max_req:
        return RateLimitStatus(
            allowed=False,
            reason=f"request quota exceeded: {req_count}/{key.rl_max_req} "
                   f"in {window}s window",
            limit=key.rl_max_req, used=req_count,
            window_s=window, retry_after_s=window,
        )
    if key.rl_max_tokens and token_count >= key.rl_max_tokens:
        return RateLimitStatus(
            allowed=False,
            reason=f"token quota exceeded: {token_count}/{key.rl_max_tokens} "
                   f"in {window}s window",
            limit=key.rl_max_tokens, used=token_count,
            window_s=window, retry_after_s=window,
        )
    return RateLimitStatus(allowed=True, window_s=window)


def window_start_for(key: ApiKey, now: datetime | None = None) -> str:
    """ISO timestamp marking the start of the key's current window."""
    now = now or datetime.now(timezone.utc)
    return _window_start(key.rl_window_s or 0, now)
