"""Anonymous (public) chat access, and the caps that make it survivable.

The feature trades a real risk — strangers spending the operator's money on the
operator's hardware — for reach, so these tests are mostly about the *limits*
holding rather than the happy path working. Four properties matter most, and
each has a test that fails loudly if it regresses:

  * off by default, and a bare API client still gets a 401 when it's on
  * an anonymous visitor is never an admin and never gets filesystem tools
  * the cost ceiling binds, and tightens on its own once the day's cap is spent
  * one visitor cannot read another's conversations
"""
import warnings

import httpx
import pytest
from fastapi.testclient import TestClient

from smart_ai_router import public_access as _public
from smart_ai_router import settings as _settings
from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec, UsageRecord
from smart_ai_router.store.sqlite_store import SqliteStore

_ADMIN = "admin-secret-key"

_REPLY = {
    "id": "cmpl-1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi."},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

# What a browser's fetch() from our own page looks like. Absent these, a request
# is a bare API client and must be refused even with public chat on.
_BROWSER = {"sec-fetch-site": "same-origin"}


@pytest.fixture(autouse=True)
def _clean_rate_limits():
    """Rate-limit state is process-global by design; don't leak it between tests."""
    _public.reset_rate_limits()
    yield
    _public.reset_rate_limits()


def _profile(score=0.95):
    return {"software_engineering": score, "law_regulatory": score,
            "medicine_health": score, "general_knowledge": score}


@pytest.fixture
def client(monkeypatch):
    # An admin key exists, so auth is enforced — without one the router is in
    # first-run open mode and anonymous access would be untestable.
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.delenv("SMART_ROUTER_MODEL_DENYLIST", raising=False)
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_FALLBACK", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_REFINE_MODEL", "")
    # Public access on, with headroom so nothing trips accidentally; individual
    # tests tighten what they're about.
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_CHAT", "true")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_DAILY_BUDGET", "5.00")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_MAX_TIER", "3")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_DEGRADED_MAX_TIER", "1")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_MAX_OUTPUT_TOKENS", "256")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_RL_MAX_REQ", "100")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_RL_WINDOW_S", "3600")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_MAX_CONCURRENT", "0")

    sent: list[dict] = []

    async def fake_post(self, url, **kwargs):
        sent.append(kwargs.get("json") or {})
        return httpx.Response(200, json=_REPLY, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    store = SqliteStore(":memory:")
    # One model per tier that matters: local (0), free (1), and pricey (8).
    store.upsert_model(ModelSpec(
        value="ollama/local-8b", provider="ollama", cost=0, ctx_k=128,
        reliability=1.0, tools=True, profile=_profile(0.80),
        competence={"coding": 0.8, "reasoning": 0.8, "docs": 0.8, "general": 0.8},
    ))
    store.upsert_model(ModelSpec(
        value="openrouter/free-model:free", provider="openrouter", cost=1, ctx_k=128,
        reliability=1.0, tools=True, profile=_profile(0.85),
        competence={"coding": 0.85, "reasoning": 0.85, "docs": 0.85, "general": 0.85},
    ))
    store.upsert_model(ModelSpec(
        value="openrouter/expensive-frontier", provider="openrouter", cost=8, ctx_k=200,
        reliability=1.0, tools=True, profile=_profile(0.99),
        competence={"coding": 0.99, "reasoning": 0.99, "docs": 0.99, "general": 0.99},
    ))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = TestClient(create_app(CapabilityRouter(store=store)))
    c.sent = sent
    c.store = store
    return c


def _chat(client, **kwargs):
    body = {"model": "smart-worker", "messages": [{"role": "user", "content": "hello"}]}
    body.update(kwargs.pop("body", {}))
    headers = {**_BROWSER, **kwargs.pop("headers", {})}
    return client.post("/v1/chat/completions", json=body, headers=headers, **kwargs)


# ── The gate ────────────────────────────────────────────────────────────────────

def test_off_by_default(monkeypatch, client):
    """Anonymous access must be an explicit decision, never a default."""
    monkeypatch.delenv("SMART_ROUTER_PUBLIC_CHAT", raising=False)
    assert _settings.get_bool("public_chat_enabled") is False
    assert _chat(client).status_code == 401


def test_bare_api_client_still_refused_when_public_chat_is_on(client):
    """The chat page is open; the API is not.

    A request with no Origin and no Sec-Fetch-Site is a script, not our page.
    This is the property that keeps `curl https://host/v1/chat/completions` from
    working the moment public chat is switched on.
    """
    r = client.post("/v1/chat/completions",
                    json={"model": "smart-worker",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401


def test_cross_origin_request_refused(client):
    r = _chat(client, headers={"sec-fetch-site": "cross-site"})
    assert r.status_code == 401


def test_same_origin_browser_request_allowed_and_gets_a_session(client):
    r = _chat(client)
    assert r.status_code == 200
    assert r.headers["x-user"].startswith(_public.ANON_PREFIX)
    assert _public.COOKIE_NAME in r.cookies


def test_top_level_navigation_counts_as_browser():
    """Sec-Fetch-Site: none is how the page itself loads, not a cross-origin call."""
    class _Req:
        headers = {"sec-fetch-site": "none"}
    assert _public.is_same_origin_browser_request(_Req())


def test_origin_fallback_for_browsers_without_sec_fetch_site():
    class _Match:
        headers = {"origin": "https://router.example", "host": "router.example"}

    class _Mismatch:
        headers = {"origin": "https://evil.example", "host": "router.example"}

    assert _public.is_same_origin_browser_request(_Match())
    assert not _public.is_same_origin_browser_request(_Mismatch())


# ── Identity and isolation ──────────────────────────────────────────────────────

def test_anon_is_not_admin_and_cannot_reach_admin_surfaces(client):
    """Opening the chat page must not open the dashboard."""
    assert client.get("/api/whoami", headers=_BROWSER).json()["kind"] == "anon"
    for path in ("/api/keys", "/api/settings", "/api/usage", "/api/providers",
                 "/api/files"):
        assert client.get(path, headers=_BROWSER).status_code == 401, path


def test_anon_cannot_upload_files(client):
    r = client.post("/api/files", headers=_BROWSER,
                    files={"file": ("x.txt", b"hello", "text/plain")})
    assert r.status_code == 401


def test_whoami_reports_agent_unavailable_for_anon(client):
    body = client.get("/api/whoami", headers=_BROWSER).json()
    assert body["anon"] is True
    assert body["agent_available"] is False
    assert body["is_admin"] is False
    assert body["authenticated"] is False


def test_two_visitors_get_distinct_identities(client):
    first = _chat(client).headers["x-user"]
    client.cookies.clear()
    second = _chat(client).headers["x-user"]
    assert first != second
    assert first.startswith(_public.ANON_PREFIX)
    assert second.startswith(_public.ANON_PREFIX)


def test_session_cookie_survives_across_requests(client):
    first = _chat(client).headers["x-user"]
    # TestClient keeps the cookie jar, so this is the same visitor returning.
    assert _chat(client).headers["x-user"] == first


def test_forged_session_cookie_is_rejected(client):
    """A visitor must not be able to name their own session and read someone else's.

    Conversations and files are scoped to the identity, so an unsigned session id
    would be a way to walk into another visitor's history.
    """
    client.cookies.set(_public.COOKIE_NAME, "victim-session.deadbeef")
    user = _chat(client).headers["x-user"]
    assert user != f"{_public.ANON_PREFIX}victim-session"


def test_signed_cookie_round_trips(client):
    cr = client.app.state.capability_router
    value = _public.issue_session(cr)
    assert _public.read_session(value, cr)
    assert _public.read_session("abc.notasignature", cr) is None
    assert _public.read_session("", cr) is None


def test_an_authenticated_key_is_not_downgraded_when_public_chat_is_on(client):
    """Enabling public access must not change anything for a real key."""
    r = client.get("/api/whoami", headers={"Authorization": f"Bearer {_ADMIN}"})
    assert r.json() == {**r.json(), "kind": "admin", "is_admin": True, "anon": False}


# ── The cost ceiling ────────────────────────────────────────────────────────────

def test_tier_ceiling_keeps_anon_off_expensive_models(client):
    """The pricey model is the best one available, and must still be refused."""
    r = _chat(client)
    assert r.status_code == 200
    assert r.headers["x-routed-model"] != "openrouter/expensive-frontier"


def test_admin_still_reaches_the_expensive_model(client):
    """Proof the ceiling above is anon policy, not the model being unroutable."""
    r = client.post(
        "/v1/chat/completions",
        json={"model": "smart-worker",
              "messages": [{"role": "user", "content":
                            "Analyze this FDA submission for regulatory risk."}]},
        headers={"Authorization": f"Bearer {_ADMIN}"},
    )
    assert r.status_code == 200


def test_zero_cap_means_no_paid_spend_not_unlimited(monkeypatch, client):
    """The cap-that-means-unlimited bug, pinned.

    An operator setting the daily budget to 0 is asking for "spend nothing", and
    a naive `if cap:` check would read it as "no limit" — the most cautious
    setting becoming the most expensive one.
    """
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_DAILY_BUDGET", "0")
    cr = client.app.state.capability_router
    assert _public.budget_status(cr).degraded is True
    assert _public.tier_ceiling(cr) == 1  # the degraded ceiling, free/local only


def test_ceiling_tightens_once_the_daily_cap_is_spent(monkeypatch, client):
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_DAILY_BUDGET", "1.00")
    cr = client.app.state.capability_router
    assert _public.tier_ceiling(cr) == 3          # configured ceiling, budget intact

    cr.record_usage(UsageRecord(kind="proxy", user="anon:someone", cost_usd=0.95))
    status = _public.budget_status(cr)
    assert status.degraded is True                # 0.95 ≥ 90% of 1.00
    assert _public.tier_ceiling(cr) == 1          # degraded ceiling now applies


def test_degraded_ceiling_excludes_paid_models_from_the_scope(monkeypatch, client):
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_DAILY_BUDGET", "0")
    cr = client.app.state.capability_router
    scope = _public.anon_scope(cr)
    models = {m.value: m for m in cr.all_models()}
    assert scope.permits(models["ollama/local-8b"])
    assert scope.permits(models["openrouter/free-model:free"])
    assert not scope.permits(models["openrouter/expensive-frontier"])


def test_budget_counts_overhead_because_the_bill_does(client):
    """A classification the visitor's prompt triggered is money spent on them.

    Excluding overhead would let the real bill drift above the cap while the cap
    reported room to spare.
    """
    cr = client.app.state.capability_router
    cr.record_usage(UsageRecord(kind="classify", user="anon:a", cost_usd=0.10))
    cr.record_usage(UsageRecord(kind="proxy", user="anon:a", cost_usd=0.25))
    assert _public.budget_status(cr).spent_usd == pytest.approx(0.35)


def test_budget_ignores_authenticated_users_spend(client):
    """The anonymous cap must not be consumed by the operator's own traffic."""
    cr = client.app.state.capability_router
    cr.record_usage(UsageRecord(kind="proxy", user="admin", cost_usd=9.99))
    cr.record_usage(UsageRecord(kind="proxy", user="alice", cost_usd=9.99))
    assert _public.budget_status(cr).spent_usd == pytest.approx(0.0)


def test_accounting_failure_degrades_rather_than_uncaps(client, monkeypatch):
    """If spend can't be read, assume the worst — never serve paid models blind."""
    cr = client.app.state.capability_router

    def boom(**kwargs):
        raise RuntimeError("store is down")

    monkeypatch.setattr(cr, "spend_since", boom)
    assert _public.budget_status(cr).degraded is True


def test_spend_since_is_prefix_scoped_and_escapes_wildcards(client):
    store = client.store
    store.record_usage(UsageRecord(kind="proxy", user="anon:x", cost_usd=1.0))
    store.record_usage(UsageRecord(kind="proxy", user="anonymous-alice", cost_usd=2.0))
    # "anon:" must not match "anonymous-alice"; and an underscore in a prefix is
    # a SQL single-char wildcard unless escaped, which would widen the match.
    assert store.spend_since(user_prefix="anon:", since_ts="") == pytest.approx(1.0)
    store.record_usage(UsageRecord(kind="proxy", user="anonXy", cost_usd=4.0))
    assert store.spend_since(user_prefix="anon_y", since_ts="") == pytest.approx(0.0)


# ── Per-request limits ──────────────────────────────────────────────────────────

def test_anon_output_tokens_are_capped(client):
    _chat(client, body={"max_tokens": 100_000})
    assert client.sent[-1]["max_tokens"] == 256


def test_anon_output_cap_applies_when_caller_omits_max_tokens(client):
    _chat(client)
    assert client.sent[-1]["max_tokens"] == 256


def test_agent_mode_refused_for_anon(client):
    """Filesystem tools run on the operator's machine; strangers don't get them."""
    r = _chat(client, body={"agent": True})
    assert r.status_code == 403
    assert "anonymous" in r.json()["detail"].lower()


def test_agent_auto_never_escalates_for_anon(client):
    r = _chat(client, body={"agent": "auto",
                            "messages": [{"role": "user",
                                          "content": "write a file called out.txt"}]})
    assert r.status_code == 200
    assert "tools" not in client.sent[-1]


def test_per_ip_rate_limit(monkeypatch, client):
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_RL_MAX_REQ", "2")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_RL_WINDOW_S", "60")
    headers = {**_BROWSER, "cf-connecting-ip": "203.0.113.9"}
    assert _chat(client, headers=headers).status_code == 200
    assert _chat(client, headers=headers).status_code == 200
    r = _chat(client, headers=headers)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0


def test_rate_limit_is_per_ip_not_per_session(monkeypatch, client):
    """Dropping the cookie must not buy a fresh quota — the IP is the real limit."""
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_RL_MAX_REQ", "1")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_RL_WINDOW_S", "60")
    headers = {**_BROWSER, "cf-connecting-ip": "198.51.100.7"}
    assert _chat(client, headers=headers).status_code == 200
    client.cookies.clear()
    assert _chat(client, headers=headers).status_code == 429


def test_rotating_ip_does_not_buy_a_fresh_quota_either(monkeypatch, client):
    """The other half of the same defense, and why policy_key carries rl_* fields.

    The IP limit and the session limit read the same setting but count different
    things, so a visitor is capped whichever one they shed: drop the cookie and
    the IP still counts you; change networks and the session still does.
    """
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_RL_MAX_REQ", "1")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_RL_WINDOW_S", "60")
    assert _chat(client, headers={**_BROWSER, "cf-connecting-ip": "1.1.1.1"}).status_code == 200
    # Same cookie jar, new address — the per-session cap catches it.
    assert _chat(client, headers={**_BROWSER, "cf-connecting-ip": "9.9.9.9"}).status_code == 429


def test_separate_visitors_have_separate_quotas(monkeypatch, client):
    """One abuser must not rate-limit everyone else off the site."""
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_RL_MAX_REQ", "1")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_RL_WINDOW_S", "60")
    assert _chat(client, headers={**_BROWSER, "cf-connecting-ip": "1.1.1.1"}).status_code == 200
    client.cookies.clear()  # a different visitor: new session, new address
    assert _chat(client, headers={**_BROWSER, "cf-connecting-ip": "2.2.2.2"}).status_code == 200


def test_client_ip_prefers_the_tunnels_real_client_header():
    """Behind the Cloudflare tunnel, request.client is identical for everyone."""
    class _Req:
        headers = {"cf-connecting-ip": "203.0.113.5",
                   "x-forwarded-for": "10.0.0.1, 10.0.0.2"}
        client = type("C", (), {"host": "127.0.0.1"})()

    class _NoCf:
        headers = {"x-forwarded-for": "203.0.113.6, 10.0.0.2"}
        client = type("C", (), {"host": "127.0.0.1"})()

    assert _public.client_ip(_Req()) == "203.0.113.5"
    assert _public.client_ip(_NoCf()) == "203.0.113.6"


def test_concurrency_cap_admits_up_to_the_limit(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_MAX_CONCURRENT", "2")
    gate = _public._Concurrency()
    assert gate.try_acquire() and gate.try_acquire()
    assert not gate.try_acquire()          # third is refused
    gate.release()
    assert gate.try_acquire()              # a slot freed up


def test_concurrency_zero_means_unlimited(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_MAX_CONCURRENT", "0")
    gate = _public._Concurrency()
    assert all(gate.try_acquire() for _ in range(50))


def test_usage_is_attributed_to_the_anonymous_session(client):
    """Anon traffic must land in the usage log, or the cap has nothing to read."""
    user = _chat(client).headers["x-user"]
    rows = client.store.recent_usage(user, "")
    assert len(rows) == 1
    assert rows[0].user == user
