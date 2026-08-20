"""Anonymous (no-API-key) access to the chat UI, with the operator's bill capped.

The goal is a public site people can just *use* — no signup, no key — without
handing the internet an unmetered claim on the operator's OpenRouter balance or
the mini's GPU. Everything here is a ceiling, and the feature ships off.

Four ideas carry the design:

**An anonymous visitor is a user, not a hole in auth.** A request with no key
gets a real identity, ``anon:<session>``, derived from a signed cookie. That
matters beyond bookkeeping: conversations and files are scoped to
``request.state.user``, so the alternative — leaving it as the default empty
string — would put every visitor in one shared account, reading each other's chat
history and uploads. The signature is what stops a visitor from typing someone
else's session id into their cookie jar.

**Policy reuses the per-user machinery, so nothing downstream is special.**
``policy_key()`` returns an ``ApiKey`` that was never stored and can't
authenticate anything; it exists to carry the identity and the ``rl_*`` caps, so
the existing rate limiter and usage logging apply unchanged. The cost ceiling
rides alongside as a ``ModelScope`` (``anon_scope()``), which the router enforces
in its eligibility filter — the same filter that scopes a per-user key — so it
binds the fallback pick too, not just the happy path.

**A spent budget degrades, it doesn't refuse.** Past the daily cap the tier
ceiling drops to free/local models instead of returning an error. A site that
answers slightly worse is still a site; one that returns 503 for the rest of the
day is indistinguishable from broken, and the operator finds out from a stranger.

**The bill can only be measured after the fact, so the cap needs slack.** A
model's cost is unknown until its response arrives, so N concurrent requests can
all observe the same under-budget total before any of them record a cent. Three
things bound the overshoot: the check trips at a fraction of the cap
(``_SOFT_FRACTION``), anonymous ``max_tokens`` is capped, and only so many
anonymous requests may be in flight at once. Worst case is roughly
``max_concurrent × (cost of one capped response)`` past the soft threshold, which
the default settings keep well under the cap itself.

Rate limiting and concurrency are deliberately **in-process**: they reset on
restart, which is the right trade for abuse control (cheap, no write per request)
where the budget — the thing that costs real money — is DB-backed and survives.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from smart_ai_router import settings as _settings
from smart_ai_router.models import ApiKey
from smart_ai_router.scope import ModelScope

# Identity prefix for anonymous sessions. Every anonymous user is
# f"{ANON_PREFIX}{session_id}", which is also how the daily budget finds their
# rows: one prefix, one shared cap.
ANON_PREFIX = "anon:"

COOKIE_NAME = "sair_anon"
COOKIE_MAX_AGE_S = 30 * 24 * 3600  # 30 days — a returning visitor keeps their chats

# The store key holding the cookie-signing secret. Not a SettingSpec: it is a
# generated secret, not a tunable, and must never be rendered on the Settings
# page. Kept in the same table only because that is where this app keeps
# small persistent values.
_SECRET_KEY = "public_session_secret"

# Trip the budget check at this fraction of the cap, leaving room for in-flight
# requests whose cost isn't known yet to land without exceeding it.
_SOFT_FRACTION = 0.9


def enabled() -> bool:
    """Whether anonymous chat is turned on. Off unless an operator says otherwise."""
    return _settings.get_bool("public_chat_enabled")


def is_anon_user(user: str) -> bool:
    return (user or "").startswith(ANON_PREFIX)


# ── Session identity ────────────────────────────────────────────────────────────

def _secret(cr) -> bytes:
    """The cookie-signing secret, generated once and persisted.

    Generated rather than derived from the admin API key so that rotating the
    operator's key doesn't silently log out every visitor (and so the admin
    secret isn't stretched into a second job).
    """
    existing = cr.get_setting(_SECRET_KEY)
    if existing:
        return existing.encode()
    fresh = secrets.token_hex(32)
    cr.set_setting(_SECRET_KEY, fresh)
    return fresh.encode()


def _sign(session_id: str, secret: bytes) -> str:
    return hmac.new(secret, session_id.encode(), hashlib.sha256).hexdigest()[:32]


def issue_session(cr) -> str:
    """Mint a fresh signed cookie value for a new visitor."""
    session_id = secrets.token_urlsafe(16)
    return f"{session_id}.{_sign(session_id, _secret(cr))}"


def read_session(cookie_value: str, cr) -> str | None:
    """The session id inside a cookie value, or None if absent/forged.

    A bad signature is treated as no cookie at all (the caller mints a new
    session) rather than an error: a visitor whose cookie predates a secret
    change should get a working site, not a wall.
    """
    if not cookie_value or "." not in cookie_value:
        return None
    session_id, _, signature = cookie_value.rpartition(".")
    if not session_id:
        return None
    expected = _sign(session_id, _secret(cr))
    if not hmac.compare_digest(signature, expected):
        return None
    return session_id


# ── Same-origin check ───────────────────────────────────────────────────────────

def is_same_origin_browser_request(request) -> bool:
    """Whether this looks like the chat page calling its own server.

    This is what makes "the chat UI is open, the API is not" enforceable at all:
    both use ``/v1/chat/completions``, so the only difference available is how the
    request presents itself. Browsers send ``Sec-Fetch-Site`` on fetch() and
    attach ``Origin`` cross-origin; a bare ``curl`` — which is what an endpoint
    scanner uses — sends neither and is refused.

    Honest about its limits: any client can *set* these headers, so this stops
    drive-by scanning, not a determined abuser. The load-bearing protections are
    the per-IP limit and the spend cap, which don't care who is calling.
    """
    site = request.headers.get("sec-fetch-site", "").strip().lower()
    if site:
        # "none" is a top-level navigation (someone typing the URL), which is how
        # the page itself loads; "same-origin" is its fetch() calls.
        return site in ("same-origin", "none")

    # Pre-Sec-Fetch-Site browsers: fall back to comparing Origin to the host we
    # were reached on. Absent both headers we refuse.
    origin = request.headers.get("origin", "").strip()
    if not origin:
        return False
    host = request.headers.get("host", "").strip().lower()
    if not host:
        return False
    origin_host = origin.split("://", 1)[-1].rstrip("/").lower()
    return origin_host == host


def client_ip(request) -> str:
    """The visitor's IP, honoring the proxy this deployment actually runs behind.

    Cloudflare terminates the tunnel, so ``request.client.host`` is the tunnel's
    end — identical for every visitor on earth, which would turn a per-IP limit
    into a global one. ``CF-Connecting-IP`` is the real client; ``X-Forwarded-For``
    is the generic fallback, first hop being the originator.

    These headers are trivially spoofable by anything talking to the origin
    directly, so this is only meaningful because the origin is not publicly
    reachable except through the tunnel.
    """
    cf = request.headers.get("cf-connecting-ip", "").strip()
    if cf:
        return cf
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or "unknown"


# ── Spend cap ───────────────────────────────────────────────────────────────────

def _utc_day_start() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


@dataclass(frozen=True)
class BudgetStatus:
    """What anonymous traffic has cost today, and whether that has bitten yet."""

    spent_usd: float
    cap_usd: float
    degraded: bool          # True → tier ceiling drops to free/local
    remaining_usd: float


def budget_status(cr) -> BudgetStatus:
    """Today's anonymous spend against the cap.

    A cap of 0 means "no paid spend at all", which is *degraded from the start* —
    free and local models only — rather than "unlimited". Reading 0 as unlimited
    is the classic cap bug, and here it would be an unlimited bill.
    """
    cap = float(_settings.get("public_daily_budget_usd") or 0.0)
    try:
        spent = cr.spend_since(user_prefix=ANON_PREFIX, since_ts=_utc_day_start())
    except Exception:  # noqa: BLE001
        # An accounting failure must not silently uncap the bill: assume the cap
        # is blown and serve free models until the store answers again.
        return BudgetStatus(spent_usd=0.0, cap_usd=cap, degraded=True, remaining_usd=0.0)
    if cap <= 0:
        return BudgetStatus(spent_usd=spent, cap_usd=cap, degraded=True, remaining_usd=0.0)
    return BudgetStatus(
        spent_usd=spent,
        cap_usd=cap,
        degraded=spent >= cap * _SOFT_FRACTION,
        remaining_usd=max(0.0, cap - spent),
    )


# ── Policy as a synthetic key ───────────────────────────────────────────────────

def tier_ceiling(cr) -> int:
    """The cost-tier ceiling anonymous traffic gets right now.

    The *degraded* ceiling once today's spend has reached the soft threshold, the
    configured one before that. This single value is the whole cost-control
    mechanism: everything else here only decides which of the two applies.
    """
    key = (
        "public_degraded_max_tier"
        if budget_status(cr).degraded
        else "public_max_tier"
    )
    return max(0, _settings.get_int(key))


def anon_scope(cr) -> ModelScope:
    """Routing scope for an anonymous request — a hard cost-tier ceiling.

    Built directly rather than through ``parse_scope`` so a ceiling of 0 keeps its
    honest meaning (local models only) instead of the stored-key convention where
    0 means "unset". Enforced inside the router's eligibility filter, so it binds
    the fallback pick too: an anonymous visitor cannot be handed an expensive
    model by a code path that forgot to check.
    """
    return ModelScope(max_tier=tier_ceiling(cr))


def policy_key(cr, *, session_id: str) -> ApiKey:
    """An unstored ApiKey carrying this request's anonymous identity and limits.

    Returned so the proxy's existing machinery does the work — the rate limiter
    reads the ``rl_*`` fields, and usage logging reads ``user`` — rather than a
    second enforcement path that can drift from the first. The tier ceiling is
    *not* carried here: see ``anon_scope`` for why it can't be expressed in this
    field without ambiguity.

    ``key_hash``/``key_prefix`` are deliberately empty: this object must never
    look like something that could authenticate.
    """
    return ApiKey(
        user=f"{ANON_PREFIX}{session_id}",
        key_hash="",
        key_prefix="",
        enabled=True,
        rl_window_s=max(0, _settings.get_int("public_rl_window_s")),
        rl_max_req=max(0, _settings.get_int("public_rl_max_req")),
        rl_max_tokens=0,  # the token budget for anon is per-request, not per-window
    )


def max_output_tokens() -> int:
    """Per-request output ceiling for anonymous callers (0 = no extra ceiling)."""
    return max(0, _settings.get_int("public_max_output_tokens"))


# ── Per-IP rate limiting (in-process) ───────────────────────────────────────────

_ip_hits: dict[str, list[float]] = {}
# Never let the bookkeeping itself become the memory leak: an abuser cycling
# source addresses would otherwise grow this dict without bound.
_MAX_TRACKED_IPS = 10_000


def _prune(now: float, window: float) -> None:
    for ip in list(_ip_hits):
        kept = [t for t in _ip_hits[ip] if now - t < window]
        if kept:
            _ip_hits[ip] = kept
        else:
            del _ip_hits[ip]


def check_ip_rate(ip: str) -> tuple[bool, int]:
    """Record a hit for `ip`; return (allowed, retry_after_seconds).

    Sliding window over in-process timestamps. Counted per IP rather than per
    session because the session cookie is the visitor's to discard — a script
    that drops it gets a fresh identity every request, but not a fresh IP.
    """
    window = float(max(0, _settings.get_int("public_rl_window_s")))
    limit = max(0, _settings.get_int("public_rl_max_req"))
    if window <= 0 or limit <= 0:
        return True, 0

    now = time.monotonic()
    if len(_ip_hits) > _MAX_TRACKED_IPS:
        _prune(now, window)

    hits = [t for t in _ip_hits.get(ip, []) if now - t < window]
    if len(hits) >= limit:
        _ip_hits[ip] = hits
        oldest = min(hits)
        return False, max(1, int(window - (now - oldest)))
    hits.append(now)
    _ip_hits[ip] = hits
    return True, 0


def reset_rate_limits() -> None:
    """Drop all in-process rate-limit state (tests, and a manual reprieve)."""
    _ip_hits.clear()


# ── Concurrency ─────────────────────────────────────────────────────────────────

class _Concurrency:
    """Deployment-wide cap on in-flight anonymous requests.

    Protects the local GPU from being monopolized — one visitor with a loop can
    otherwise make the router feel dead for the operator — and bounds how far
    concurrent requests can push past the spend cap before any of them is
    recorded. A plain counter is safe here because increments happen in one event
    loop with no await between the check and the bump.
    """

    def __init__(self) -> None:
        self.active = 0

    def limit(self) -> int:
        return max(0, _settings.get_int("public_max_concurrent"))

    def try_acquire(self) -> bool:
        cap = self.limit()
        if cap and self.active >= cap:
            return False
        self.active += 1
        return True

    def release(self) -> None:
        self.active = max(0, self.active - 1)


inflight = _Concurrency()
