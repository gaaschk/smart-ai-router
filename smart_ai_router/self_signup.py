"""Self-issued API keys — an account with no identity behind it, bill capped.

An anonymous session lives in one browser (see ``public_access.py``). The moment
someone wants their chats on their phone too, or wants to point a real client at
``/v1``, they need a key — and the only way to get one today is to ask the
operator, who then has to know who they are. This module is the other option: a
button that mints a key, with no email, no name, and nothing to verify.

**The account is a random handle, not something the visitor types.** ``u:9f3a2b17``.
Not because a chosen name would be unsafe, but because a name field is an
invitation to type an email address into it — and then the operator is holding
personal information they went out of their way not to collect. There is
correspondingly nothing to recover an account *with*: the key is the account, and
losing it loses the chats behind it. That is the honest cost of knowing nothing
about someone, and the UI says so at the moment it hands the key over.

**Two dollar caps, and only one of them is the bill ceiling.** A per-account daily
cap is what keeps one person from spending the day's budget, but on its own it is
close to meaningless: signing up is free and scriptable, so N accounts buy N times
the cap. What actually bounds the operator's exposure is the *pool* cap — one
number over every self-issued account together, found the same way anonymous spend
is, by prefix-matching one identity namespace. Per-account is fairness; pooled is
the ceiling. An operator who only sets the first has set no limit at all.

**Reaching a cap degrades, it never refuses**, for the same reason it doesn't for
anonymous traffic: a site that answers on free and local models is still a site,
while one returning 402 for the rest of the day is indistinguishable from broken.
The ceiling is read live from settings on every request rather than frozen into
the key's row, which is also what lets an operator lower it and have it apply to
keys that already exist.

**A self-issued key is a stranger with a key, not a colleague.** It is
deliberately *not* equivalent to one the operator created by hand: agent mode
stays off (its tools are read/write/bash over a workspace on the operator's own
machine), output tokens are capped, and the tier ceiling binds inside the router's
eligibility filter so it holds for the fallback pick too. See ``api/proxy.py``.

File uploads *are* allowed, unlike for anonymous visitors, and that is a decision
rather than an omission: being able to hand the model a document is most of what
an account is for, and the existing per-upload size ceiling bounds the obvious
abuse. What it does not bound is total disk across many uploads — an operator
running this on a small volume should watch it.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass

from smart_ai_router import settings as _settings
from smart_ai_router.models import ApiKey
from smart_ai_router.public_access import utc_day_start
from smart_ai_router.scope import ModelScope

# Identity prefix for self-issued accounts. Doing double duty: it marks a key as
# self-issued (so the proxy knows to apply the ceiling below rather than trust the
# row) and it is how the pooled budget finds every such account's rows with one
# prefix match. Short and punctuated so it can't collide with a label an operator
# would plausibly type into the Keys page.
SIGNUP_PREFIX = "u:"

# Trip the budget check at this fraction of the cap. A call's cost is unknown
# until it returns, so concurrent requests can all read the same under-budget
# total; the slack is what keeps them from landing past the cap together. Same
# reasoning, and same value, as the anonymous cap.
_SOFT_FRACTION = 0.9


def enabled() -> bool:
    """Whether self-serve signup is turned on. Off unless an operator says so."""
    return _settings.get_bool("self_signup_enabled")


def is_signup_user(user: str) -> bool:
    return (user or "").startswith(SIGNUP_PREFIX)


# ── Guardrails on turning it on ────────────────────────────────────────────────

def lockout_reason(cr) -> str:
    """Why signup must be refused right now, or "" if it may proceed.

    The first case is not obvious and is unrecoverable without shell access. Auth
    in this app is *open* until at least one key exists, and the admin pages are
    gated on holding the admin identity — which, in open mode, is everybody. So on
    an install with no admin key, the first stranger to sign up flips auth on and
    silently takes the Settings and Keys pages away from the operator, who has no
    key to get them back with. Refusing is the recoverable direction: set
    SMART_ROUTER_API_KEYS, restart, done.

    The second is capacity. Accounts are free to create, so without a ceiling a
    script can fill the table; a number the operator picked is also how they say
    "I'll take fifty users, not five thousand".
    """
    if not os.environ.get("SMART_ROUTER_API_KEYS", "").strip():
        try:
            if not cr.all_api_keys():
                return (
                    "This deployment has no admin key, so creating the first "
                    "account would lock its operator out of their own settings. "
                    "Sign-ups are off until they set one."
                )
        except Exception:  # noqa: BLE001 — can't prove it's safe, so don't allow it
            return "Sign-ups are temporarily unavailable."
    cap = max(0, _settings.get_int("self_signup_max_accounts"))
    if cap:
        try:
            existing = sum(1 for k in cr.all_api_keys() if is_signup_user(k.user))
        except Exception:  # noqa: BLE001
            return "Sign-ups are temporarily unavailable."
        if existing >= cap:
            return "This deployment has reached its limit on self-serve accounts."
    return ""


# ── Spend caps ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BudgetStatus:
    """Today's spend for one account and for the whole self-issued pool.

    ``degraded`` is the only thing callers act on, and it is the OR of the two:
    either cap being reached drops the ceiling. Which one tripped is reported so a
    log line or a test can say why, but it is deliberately not surfaced to the
    visitor — how much budget an operator has left is nobody else's business.
    """

    account_spent_usd: float
    account_cap_usd: float
    pool_spent_usd: float
    pool_cap_usd: float
    degraded: bool
    reason: str = ""            # "account" | "pool" | "both" | "error" | ""


def _tripped(spent: float, cap: float) -> bool:
    """Whether a cap has bitten. A cap of 0 means "no paid spend", not "unlimited".

    Reading 0 as unlimited is the classic cap bug, and here it would turn the most
    cautious-looking configuration into an unbounded bill.
    """
    if cap <= 0:
        return True
    return spent >= cap * _SOFT_FRACTION


def budget_status(cr, user: str) -> BudgetStatus:
    """This account's daily spend and the pool's, against their caps."""
    account_cap = float(_settings.get("self_signup_daily_budget_usd") or 0.0)
    pool_cap = float(_settings.get("self_signup_pool_daily_budget_usd") or 0.0)
    since = utc_day_start()
    try:
        account_spent = cr.spend_for_user(user=user, since_ts=since)
        pool_spent = cr.spend_since(user_prefix=SIGNUP_PREFIX, since_ts=since)
    except Exception:  # noqa: BLE001
        # An accounting failure must not silently uncap the bill: assume both caps
        # are blown and serve free models until the store answers again.
        return BudgetStatus(
            account_spent_usd=0.0, account_cap_usd=account_cap,
            pool_spent_usd=0.0, pool_cap_usd=pool_cap,
            degraded=True, reason="error",
        )
    account_over = _tripped(account_spent, account_cap)
    pool_over = _tripped(pool_spent, pool_cap)
    reason = ""
    if account_over and pool_over:
        reason = "both"
    elif account_over:
        reason = "account"
    elif pool_over:
        reason = "pool"
    return BudgetStatus(
        account_spent_usd=account_spent, account_cap_usd=account_cap,
        pool_spent_usd=pool_spent, pool_cap_usd=pool_cap,
        degraded=account_over or pool_over, reason=reason,
    )


# ── Policy ─────────────────────────────────────────────────────────────────────

def tier_ceiling(cr, user: str) -> int:
    """The cost-tier ceiling this self-issued account gets right now.

    The *degraded* ceiling once either daily cap has reached its soft threshold,
    the configured one before that. This single value is the whole cost-control
    mechanism; everything above only decides which of the two applies.
    """
    key = (
        "self_signup_degraded_max_tier"
        if budget_status(cr, user).degraded
        else "self_signup_max_tier"
    )
    return max(0, _settings.get_int(key))


def signup_scope(cr, user: str) -> ModelScope:
    """Routing scope for a self-issued key — a hard cost-tier ceiling.

    Built directly rather than through ``parse_scope`` so a ceiling of 0 keeps its
    honest meaning (local models only) instead of the stored-key convention where
    0 means "unset".
    """
    return ModelScope(max_tier=tier_ceiling(cr, user))


def max_output_tokens() -> int:
    """Per-request output ceiling for a self-issued key (0 = no extra ceiling).

    What bounds the damage while the spend cap is blind: a call's cost isn't known
    until it returns, so the only protection available up front is a limit on how
    expensive one call can possibly be.
    """
    return max(0, _settings.get_int("self_signup_max_output_tokens"))


# ── Minting an account ─────────────────────────────────────────────────────────

def _handle() -> str:
    """A random account label. 8 hex chars — short enough to read in the admin's
    usage list, wide enough (2^32) that collisions need looking for, not luck."""
    return f"{SIGNUP_PREFIX}{secrets.token_hex(4)}"


def new_account(cr, *, key_hash: str, key_prefix: str) -> ApiKey:
    """An unsaved ``ApiKey`` for a fresh self-issued account.

    The rate limits are baked into the row because that is where the existing
    limiter reads them. The *tier ceiling* deliberately is not: it has to move with
    the day's spend, so it stays in settings and is applied per request — see
    ``signup_scope``. Leaving ``max_tier`` at 0 (the column's "unset") is therefore
    correct rather than an oversight, and is why ``api/proxy.py`` has to recognize
    the prefix instead of trusting the row.
    """
    taken = set()
    try:
        taken = {k.user for k in cr.all_api_keys()}
    except Exception:  # noqa: BLE001 — uniqueness is a nicety, not a guarantee
        pass
    user = _handle()
    for _ in range(8):
        if user not in taken:
            break
        user = _handle()
    return ApiKey(
        user=user,
        key_hash=key_hash,
        key_prefix=key_prefix,
        enabled=True,
        rl_window_s=max(0, _settings.get_int("self_signup_rl_window_s")),
        rl_max_req=max(0, _settings.get_int("self_signup_rl_max_req")),
        rl_max_tokens=0,  # the token budget here is per-request, not per-window
    )
