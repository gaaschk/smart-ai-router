"""Self-issued API keys, and the caps that make handing them to strangers survivable.

The feature's premise is that spend limits are enough to let anyone mint a key. So
these tests are almost entirely about the limits, and about the ways the feature
could quietly *not* be limited:

  * off by default, and refused outright on a deployment where allowing it would
    lock the operator out of their own admin pages
  * the pooled cap is the real ceiling — N accounts must not buy N × the per-account
    cap, which is the mistake that makes per-account caps feel like protection
  * either cap tripping degrades the tier ceiling rather than refusing service, and
    that ceiling binds inside the router, not just at the edge
  * a self-issued key is a stranger with a key: no agent mode, no admin, capped
    output tokens
  * signing up carries an anonymous visitor's existing chats with them, and does
    not touch anyone else's
"""
import warnings
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from smart_ai_router import public_access as _public
from smart_ai_router import self_signup as _signup
from smart_ai_router import settings as _settings
from smart_ai_router.api.app import create_app
from smart_ai_router.api.proxy import _request_scope
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ApiKey, Conversation, ModelSpec, UsageRecord
from smart_ai_router.scope import ModelScope, parse_scope
from smart_ai_router.store.sqlite_store import SqliteStore

_ADMIN = "admin-secret-key"

_REPLY = {
    "id": "cmpl-1",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi."},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

# What a browser's fetch() from our own page looks like. Without these a request is
# a bare API client, and creating an account is not something a script may do.
_BROWSER = {"sec-fetch-site": "same-origin"}


@pytest.fixture(autouse=True)
def _clean_rate_limits():
    """The per-IP limiter is process-global by design; don't leak it between tests."""
    _public.reset_rate_limits()
    yield
    _public.reset_rate_limits()


def _profile(score=0.95):
    return {"software_engineering": score, "law_regulatory": score,
            "medicine_health": score, "general_knowledge": score}


def _base_env(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.delenv("SMART_ROUTER_MODEL_DENYLIST", raising=False)
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_MODEL", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_FALLBACK", "")
    monkeypatch.setenv("SMART_ROUTER_CLASSIFIER_REFINE_MODEL", "")
    # Signup on, with headroom so nothing trips accidentally; each test tightens
    # only the knob it is about.
    monkeypatch.setenv("SMART_ROUTER_SELF_SIGNUP", "true")
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_POOL_DAILY_BUDGET", "5.00")
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_DAILY_BUDGET", "1.00")
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_MAX_TIER", "8")
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_DEGRADED_MAX_TIER", "1")
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_MAX_OUTPUT_TOKENS", "256")
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_RL_MAX_REQ", "100")
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_RL_WINDOW_S", "3600")
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_MAX_ACCOUNTS", "50")
    # Anonymous chat off unless a test asks for it: the two features are meant to
    # be independent, and most of these tests are about signup alone.
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_CHAT", "false")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_RL_MAX_REQ", "100")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_RL_WINDOW_S", "3600")
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_MAX_CONCURRENT", "0")


def _seeded_store():
    store = SqliteStore(":memory:")
    # One model per tier that matters: local (0), free (1), pricey (8).
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
    return store


@pytest.fixture
def client(monkeypatch):
    _base_env(monkeypatch)

    sent: list[dict] = []

    async def fake_post(self, url, **kwargs):
        sent.append(kwargs.get("json") or {})
        return httpx.Response(200, json=_REPLY, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    store = _seeded_store()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = TestClient(create_app(CapabilityRouter(store=store)))
    c.sent = sent
    c.store = store
    return c


def _do_signup(client, **kwargs):
    headers = {**_BROWSER, **kwargs.pop("headers", {})}
    return client.post("/api/signup", headers=headers, **kwargs)


def _chat(client, key, **kwargs):
    body = {"model": "smart-worker", "messages": [{"role": "user", "content": "hello"}]}
    body.update(kwargs.pop("body", {}))
    headers = {"Authorization": f"Bearer {key}", **kwargs.pop("headers", {})}
    return client.post("/v1/chat/completions", json=body, headers=headers)


def _spend(store, user, amount):
    """Record a cost against `user` as if a request had really been billed."""
    store.record_usage(UsageRecord(
        user=user, key_prefix="", routed_model="openrouter/expensive-frontier",
        domain="general", complexity="moderate",
        prompt_tokens=100, completion_tokens=100, cost_usd=amount, status=200,
    ))


# ── The gate ────────────────────────────────────────────────────────────────────

def test_off_by_default(monkeypatch, client):
    """Letting strangers mint keys must be an explicit decision, never a default.

    With it off and anonymous chat off too, the middleware never lets the request
    reach the route at all — a 401, not a 404. Either answer is a refusal; asserting
    the 401 documents that the outer gate is the one doing the work.
    """
    monkeypatch.delenv("SMART_ROUTER_SELF_SIGNUP", raising=False)
    assert _settings.get_bool("self_signup_enabled") is False
    assert _do_signup(client).status_code == 401


def test_the_status_endpoint_says_unavailable_rather_than_404(monkeypatch, guest_client):
    """The UI needs one call that always answers, instead of reading a 404 as data."""
    monkeypatch.delenv("SMART_ROUTER_SELF_SIGNUP", raising=False)
    r = guest_client.get("/api/signup", headers=_BROWSER)
    assert r.status_code == 200
    assert r.json() == {"available": False, "reason": ""}
    assert guest_client.post("/api/signup", headers=_BROWSER).status_code == 404


def test_a_bare_client_cannot_mint_a_key(client):
    """No Origin, no Sec-Fetch-Site: that is a script, not our page.

    This is what keeps `curl -X POST https://host/api/signup` in a loop from being
    the cheapest way to enumerate accounts.
    """
    assert client.post("/api/signup").status_code == 401


def test_signup_works_with_anonymous_chat_off(client):
    """The two features are independent.

    "Accounts instead of open access" is a perfectly sensible configuration, and in
    it this endpoint is the only way anyone gets a first key — so it must not be
    gated behind public_chat_enabled.
    """
    assert _settings.get_bool("public_chat_enabled") is False
    r = _do_signup(client)
    assert r.status_code == 201
    assert r.json()["key"]


def test_a_deployment_with_no_admin_key_refuses_to_hand_out_the_first_one(monkeypatch):
    """The lockout guard, which is unrecoverable without shell access.

    Auth here is open until a key exists, and the admin pages are gated on holding
    the admin identity — which, in open mode, is everyone. So the first stranger to
    sign up would flip auth on and take Settings and Keys away from the operator,
    who has no key to get back in with.
    """
    _base_env(monkeypatch)
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = TestClient(create_app(CapabilityRouter(store=_seeded_store())))
    status = c.get("/api/signup", headers=_BROWSER).json()
    assert status["available"] is False
    assert "admin key" in status["reason"]
    r = c.post("/api/signup", headers=_BROWSER)
    assert r.status_code == 503
    assert not [k for k in c.app.state.capability_router.all_api_keys()]


def test_account_ceiling_is_enforced(monkeypatch, client):
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_MAX_ACCOUNTS", "2")
    assert _do_signup(client).status_code == 201
    assert _do_signup(client).status_code == 201
    r = _do_signup(client)
    assert r.status_code == 503
    assert "limit" in r.json()["detail"]
    assert client.get("/api/signup", headers=_BROWSER).json()["available"] is False


def test_an_operator_issued_key_does_not_count_against_the_signup_ceiling(
    monkeypatch, client
):
    """The cap is on self-serve accounts, not on the deployment's keys."""
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_MAX_ACCOUNTS", "1")
    client.post("/api/keys", json={"user": "colleague"},
                headers={"Authorization": f"Bearer {_ADMIN}"})
    assert _do_signup(client).status_code == 201


def test_an_authenticated_caller_cannot_mint_a_second_identity(client):
    """A key is not a coupon for more quota."""
    first = _do_signup(client).json()["key"]
    r = client.post("/api/signup", headers={**_BROWSER,
                                           "Authorization": f"Bearer {first}"})
    assert r.status_code == 409


# ── The account it makes ────────────────────────────────────────────────────────

def test_the_account_handle_carries_no_information_about_the_person(client):
    """No name field, so nothing for someone to type their email into."""
    body = _do_signup(client).json()
    assert body["user"].startswith(_signup.SIGNUP_PREFIX)
    assert len(body["user"]) > len(_signup.SIGNUP_PREFIX)


def test_two_accounts_are_different_identities(client):
    a, b = _do_signup(client).json(), _do_signup(client).json()
    assert a["user"] != b["user"]
    assert a["key"] != b["key"]


def test_the_key_authenticates_and_the_plaintext_is_never_stored(client):
    body = _do_signup(client).json()
    assert client.get("/api/whoami",
                      headers={"Authorization": f"Bearer {body['key']}"}
                      ).json()["user"] == body["user"]
    stored = [k for k in client.store.all_api_keys() if k.user == body["user"]]
    assert len(stored) == 1
    assert body["key"] not in (stored[0].key_hash, stored[0].key_prefix)


def test_whoami_flags_a_self_serve_account_as_capped_and_agentless(client):
    body = _do_signup(client).json()
    me = client.get("/api/whoami",
                    headers={"Authorization": f"Bearer {body['key']}"}).json()
    assert me["kind"] == "user"
    assert me["self_serve"] is True
    assert me["agent_available"] is False
    assert me["is_admin"] is False


def test_the_rate_limit_is_baked_into_the_key(monkeypatch, client):
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_RL_MAX_REQ", "7")
    body = _do_signup(client).json()
    record = next(k for k in client.store.all_api_keys() if k.user == body["user"])
    assert record.rl_max_req == 7
    assert record.rl_window_s == 3600


# ── A stranger with a key is still a stranger ───────────────────────────────────

def test_a_self_serve_key_is_never_an_admin(client):
    key = _do_signup(client).json()["key"]
    auth = {"Authorization": f"Bearer {key}"}
    for path in ("/api/keys", "/api/settings"):
        assert client.get(path, headers=auth).status_code == 403, path


def test_a_self_serve_key_sees_only_its_own_usage(client):
    """Usage is scoped, not forbidden — but the scope has to be the caller.

    Reading your own spend is reasonable for any per-user key, so this endpoint is
    deliberately not admin-gated. What must not leak is *everyone else's*: the
    by_user breakdown is the admin-only part, and its absence is the assertion.
    """
    body = _do_signup(client).json()
    _spend(client.store, body["user"], 0.10)
    _spend(client.store, "someone-else", 7.00)
    r = client.get("/api/usage", headers={"Authorization": f"Bearer {body['key']}"})
    assert r.status_code == 200
    data = r.json()
    assert data["totals"]["cost_usd"] == pytest.approx(0.10)
    assert not data.get("by_user")


def test_agent_mode_is_refused_for_a_self_serve_key(client):
    """Its tools are read/write/bash on the operator's own machine."""
    key = _do_signup(client).json()["key"]
    r = _chat(client, key, body={"agent": True})
    assert r.status_code == 403
    assert "self-serve" in r.json()["detail"].lower()


def test_output_tokens_are_capped_over_what_the_caller_asked_for(monkeypatch, client):
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_MAX_OUTPUT_TOKENS", "128")
    key = _do_signup(client).json()["key"]
    assert _chat(client, key, body={"max_tokens": 99999}).status_code == 200
    assert client.sent[-1]["max_tokens"] == 128


def test_an_operator_issued_key_keeps_its_full_output_budget(monkeypatch, client):
    """The ceiling is for keys nobody vetted, not for every per-user key."""
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_MAX_OUTPUT_TOKENS", "128")
    made = client.post("/api/keys", json={"user": "colleague"},
                       headers={"Authorization": f"Bearer {_ADMIN}"}).json()
    assert _chat(client, made["key"], body={"max_tokens": 4000}).status_code == 200
    assert client.sent[-1]["max_tokens"] == 4000


# ── The spend caps ──────────────────────────────────────────────────────────────

def test_the_tier_ceiling_binds_while_budget_remains(client):
    """With budget left, the configured ceiling applies — the pricey model is legal.

    Asserting scope rather than the routed model on purpose: the router picks the
    *cheapest qualified* model, so "hello" lands on the local one no matter what the
    ceiling is. Whether the expensive model was permitted is the policy question;
    whether it was chosen is the router's judgement about the prompt.
    """
    body = _do_signup(client).json()
    assert _chat(client, body["key"]).status_code == 200
    assert _permitted(client, body["user"]) == {
        "ollama/local-8b", "openrouter/free-model:free", "openrouter/expensive-frontier"
    }


def test_reaching_the_per_account_cap_degrades_instead_of_refusing(client):
    body = _do_signup(client).json()
    _spend(client.store, body["user"], 0.95)      # cap is 1.00, soft at 0.90
    r = _chat(client, body["key"])
    assert r.status_code == 200                   # still answered, not 402
    assert r.headers["x-routed-model"] != "openrouter/expensive-frontier"
    # The degraded ceiling is 1: free and local stay, paid goes.
    assert _permitted(client, body["user"]) == {
        "ollama/local-8b", "openrouter/free-model:free"
    }


def test_the_pool_cap_is_what_actually_bounds_the_bill(monkeypatch, client):
    """Many accounts must not buy many times the per-account cap.

    This is the property that makes the feature safe at all: signing up is free
    and scriptable, so a per-account cap alone is not a limit on anything. Here
    account A spends the *pool* dry while staying under its own cap, and a
    brand-new account B — with zero spend of its own — is degraded on arrival.
    """
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_POOL_DAILY_BUDGET", "1.00")
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_DAILY_BUDGET", "10.00")
    a = _do_signup(client).json()
    b = _do_signup(client).json()
    _spend(client.store, a["user"], 0.95)
    assert _signup_status(client, a["user"]).account_spent_usd == pytest.approx(0.95)
    assert _signup_status(client, b["user"]).account_spent_usd == 0.0
    assert _signup_status(client, b["user"]).degraded is True
    assert _signup_status(client, b["user"]).reason == "pool"
    r = _chat(client, b["key"])
    assert r.status_code == 200
    assert r.headers["x-routed-model"] != "openrouter/expensive-frontier"
    assert "openrouter/expensive-frontier" not in _permitted(client, b["user"])


def _signup_status(client, user):
    return _signup.budget_status(client.app.state.capability_router, user)


def _permitted(client, user):
    """The catalog models this account may be routed to right now."""
    cr = client.app.state.capability_router
    scope = _signup.signup_scope(cr, user)
    return {m.value for m in cr.all_models() if scope.permits(m)}


def test_anonymous_spend_and_signup_spend_are_separate_budgets(client):
    """One prefix per pool. Anonymous traffic must not eat the accounts' budget."""
    a = _do_signup(client).json()
    _spend(client.store, "anon:somebody", 100.0)
    assert _signup_status(client, a["user"]).degraded is False


def test_a_zero_pool_cap_means_no_paid_spend_not_unlimited(monkeypatch, client):
    """Reading 0 as "unlimited" is the classic cap bug; here it would be the bill."""
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_POOL_DAILY_BUDGET", "0")
    body = _do_signup(client).json()
    assert _signup_status(client, body["user"]).degraded is True
    r = _chat(client, body["key"])
    assert r.status_code == 200
    assert r.headers["x-routed-model"] != "openrouter/expensive-frontier"
    assert "openrouter/expensive-frontier" not in _permitted(client, body["user"])


def test_an_accounting_failure_degrades_rather_than_uncapping(monkeypatch, client):
    body = _do_signup(client).json()
    cr = client.app.state.capability_router

    def boom(**_kwargs):
        raise RuntimeError("store is down")

    monkeypatch.setattr(cr, "spend_for_user", boom)
    status = _signup.budget_status(cr, body["user"])
    assert status.degraded is True
    assert status.reason == "error"


def test_the_ceiling_is_read_live_so_lowering_it_binds_existing_keys(
    monkeypatch, client
):
    """The reason the tier is not frozen into the key's row when it is created."""
    body = _do_signup(client).json()
    cr = client.app.state.capability_router
    assert _signup.tier_ceiling(cr, body["user"]) == 8
    monkeypatch.setenv("SMART_ROUTER_SIGNUP_MAX_TIER", "0")
    assert _signup.tier_ceiling(cr, body["user"]) == 0
    assert _permitted(client, body["user"]) == {"ollama/local-8b"}
    r = _chat(client, body["key"])
    assert r.status_code == 200
    assert r.headers["x-routed-model"] == "ollama/local-8b"


def test_spend_for_user_is_exact_so_one_account_is_not_billed_for_another(client):
    """`u:sam` must not be charged for `u:sammy`.

    A prefix match — which is right for the pool — would silently do exactly that,
    and the direction of the error is "your cap is already spent" for whoever
    registered the shorter name.
    """
    store = client.store
    _spend(store, "u:sam", 1.0)
    _spend(store, "u:sammy", 4.0)
    assert store.spend_for_user(user="u:sam", since_ts="") == pytest.approx(1.0)
    assert store.spend_for_user(user="u:sammy", since_ts="") == pytest.approx(4.0)
    assert store.spend_since(user_prefix="u:", since_ts="") == pytest.approx(5.0)


# ── Scope arithmetic ────────────────────────────────────────────────────────────

def test_capped_at_takes_the_stricter_ceiling_and_never_loosens():
    assert ModelScope(max_tier=5).capped_at(3).max_tier == 3
    assert ModelScope(max_tier=2).capped_at(7).max_tier == 2
    assert ModelScope().capped_at(3).max_tier == 3
    assert ModelScope(max_tier=4).capped_at(None).max_tier == 4
    # 0 is a real ceiling (local only), not "unset".
    assert ModelScope(max_tier=5).capped_at(0).max_tier == 0


def test_capped_at_leaves_allow_and_deny_alone():
    scope = parse_scope('{"allow": ["ollama/"], "deny": ["vision"]}', 0)
    tightened = scope.capped_at(2)
    assert tightened.allow == ("ollama/",)
    assert tightened.deny == ("vision",)
    assert tightened.max_tier == 2


def _fake_request(client, record):
    """The two attributes _request_scope actually reads, and nothing else."""
    return SimpleNamespace(
        state=SimpleNamespace(is_anon=False, api_key=record, user=record.user),
        app=SimpleNamespace(state=SimpleNamespace(
            capability_router=client.app.state.capability_router
        )),
    )


def test_request_scope_applies_the_signup_ceiling_to_a_self_serve_key(client):
    """The row says nothing about tiers, so without this the key is unscoped."""
    record = ApiKey(user="u:deadbeef", key_hash="h", key_prefix="p", enabled=True)
    assert parse_scope(record.scope_models, record.max_tier).max_tier is None
    scope = _request_scope(_fake_request(client, record))
    assert scope is not None and scope.max_tier == 8


def test_a_hand_tightened_row_is_not_loosened_by_the_signup_ceiling(client):
    """An operator who restricts one self-serve key by hand keeps that restriction,
    even though the signup ceiling is applied on top of it every request."""
    record = ApiKey(user="u:deadbeef", key_hash="h", key_prefix="p", enabled=True,
                    max_tier=1, scope_models='{"deny": ["expensive"]}')
    scope = _request_scope(_fake_request(client, record))
    assert scope.max_tier == 1                 # not raised to the setting's 8
    assert scope.deny == ("expensive",)        # and the substring axis survives


def test_an_operator_issued_key_is_untouched_by_the_signup_ceiling(client):
    """The prefix is the whole trigger; a normal per-user key must not be capped."""
    record = ApiKey(user="colleague", key_hash="h", key_prefix="p", enabled=True)
    assert _request_scope(_fake_request(client, record)) is None


# ── Carrying an anonymous visitor's history over ────────────────────────────────

@pytest.fixture
def guest_client(monkeypatch):
    """Both features on, which is the flow signup exists to complete."""
    _base_env(monkeypatch)
    monkeypatch.setenv("SMART_ROUTER_PUBLIC_CHAT", "true")

    async def fake_post(self, url, **kwargs):
        return httpx.Response(200, json=_REPLY, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    store = _seeded_store()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = TestClient(create_app(CapabilityRouter(store=store)))
    c.store = store
    return c


def test_signing_up_brings_the_guest_chats_that_led_to_it(guest_client):
    """The conversations that convinced someone to make an account are the ones
    they would most hate to lose at the moment they commit."""
    made = guest_client.post("/api/conversations", json={"title": "My thread"},
                             headers=_BROWSER)
    assert made.status_code in (200, 201)
    owner = made.json()["user"]
    assert owner.startswith(_public.ANON_PREFIX)

    body = guest_client.post("/api/signup", headers=_BROWSER).json()
    assert body["carried_over"] == 1

    listed = guest_client.get("/api/conversations",
                              headers={"Authorization": f"Bearer {body['key']}"}).json()
    assert [c["title"] for c in listed["data"]] == ["My thread"]
    assert listed["data"][0]["user"] == body["user"]


def test_signing_up_moves_nobody_elses_chats(guest_client):
    stranger = Conversation(id="conv-other", user="anon:someone-else", title="Theirs")
    guest_client.store.create_conversation(stranger)
    guest_client.post("/api/conversations", json={"title": "Mine"}, headers=_BROWSER)

    body = guest_client.post("/api/signup", headers=_BROWSER).json()
    assert body["carried_over"] == 1
    assert guest_client.store.get_conversation("conv-other").user == "anon:someone-else"


def test_reassign_is_a_no_op_when_there_is_nothing_to_move(client):
    store = client.store
    assert store.reassign_conversations(from_user="", to_user="u:abc") == 0
    assert store.reassign_conversations(from_user="u:abc", to_user="u:abc") == 0
    assert store.reassign_conversations(from_user="nobody", to_user="u:abc") == 0
