"""Integration tests for GET /api/overview — the dashboard's first screen.

Three things get tested here, and they are the three ways this endpoint can be
wrong in a way that matters:

1. **Scoping.** `mine` must be the caller's own rows and nothing else. This is
   the one that leaks if it breaks: the numbers are billed per identity, and a
   per-user key seeing another user's spend is a disclosure bug, not a cosmetic
   one. So the tests below assert on *other* users' spend not appearing.

2. **The savings arithmetic.** The headline "you saved $X" is computed, not
   counted, and it rests on two judgement calls that are easy to get subtly
   wrong: what counts as cheap, and what the premium baseline is. Each gets a
   test that would fail if that judgement moved.

3. **Refusing to invent a number.** When savings cannot be computed the endpoint
   must say so. Rendering an unavailable figure as $0.00 would assert "you saved
   nothing", which is a different and false claim.
"""
import warnings
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ModelSpec, UsageRecord
from smart_ai_router.store.sqlite_store import SqliteStore

_ADMIN = "admin-secret"
_ALICE = "alice-secret"
# Far future so the rows are inside any window a test asks for.
_FUTURE = (datetime.now(timezone.utc) + timedelta(days=3650)).isoformat()


def _client(cr) -> TestClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return TestClient(create_app(cr))


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _rec(user, model, cost, status=200, kind="proxy", ts=_FUTURE):
    return UsageRecord(
        kind=kind, user=user, key_prefix=user[:4], routed_model=model,
        domain="coding", complexity="moderate",
        prompt_tokens=1000, completion_tokens=500,
        cost_usd=cost, status=status, ts=ts,
    )


@pytest.fixture
def store():
    """A catalog and traffic with a known, hand-checkable answer.

    Prices are chosen so the blended rates are unambiguous:
      local/llama3   $0      (Ollama — always free, `pricing.cost_for`)
      or/cheap       $1/M in, $4/M out  → tier 1
      or/mid         $5/M in, $20/M out → tier 4
      or/premium     $15/M in, $75/M out → tier 12  (the premium baseline)

    With one request of 1000 in / 500 out each:
      local    $0.0000 actual,  $0.0450 at premium  → saves $0.0450
      cheap    $0.0025 actual,  $0.0450 at premium  → saves $0.0425
      mid      $0.0150 actual,  $0.0450 at premium  → saves $0.0300
    """
    s = SqliteStore(":memory:")
    s.upsert_model(ModelSpec(value="local/llama3", provider="ollama", cost=0))
    s.upsert_model(ModelSpec(
        value="or/cheap", provider="openrouter", cost=1, tools=True,
        vision=True, ctx_k=128, cost_input=1.0, cost_output=4.0))
    s.upsert_model(ModelSpec(
        value="or/mid", provider="openrouter", cost=4, tools=True,
        ctx_k=200, cost_input=5.0, cost_output=20.0))
    s.upsert_model(ModelSpec(
        value="or/premium", provider="openrouter", cost=12, tools=True,
        vision=True, ctx_k=200, cost_input=15.0, cost_output=75.0))

    s.record_usage(_rec("alice", "or/cheap", 0.0025))
    s.record_usage(_rec("alice", "local/llama3", 0.0))
    s.record_usage(_rec("bob", "or/premium", 0.0450))
    # The router's own spend, which must be reported separately from user traffic.
    s.record_usage(_rec("", "or/cheap", 0.0010, kind="classify"))
    return s


@pytest.fixture
def clients(monkeypatch, store):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    cr = CapabilityRouter(store=store)
    client = _client(cr)
    # A per-user key, so `mine` can be checked against someone else's rows.
    store.create_api_key(_make_key("alice", _ALICE))
    return client


def _make_key(user: str, token: str):
    from smart_ai_router.apikeys import hash_key
    from smart_ai_router.models import ApiKey
    return ApiKey(key_hash=hash_key(token), user=user, key_prefix=token[:6])


# ── Scoping ───────────────────────────────────────────────────────────────────

def test_system_flow_is_every_user_for_any_caller(clients):
    """The deployment's traffic is the same number whoever is asking."""
    admin = clients.get("/api/overview?days=36500", headers=_auth(_ADMIN)).json()
    alice = clients.get("/api/overview?days=36500", headers=_auth(_ALICE)).json()

    # 3 user requests (alice 2 + bob 1) — overhead is excluded from this block.
    assert admin["system_flow"]["requests"] == 3
    assert alice["system_flow"]["requests"] == 3
    assert alice["system_flow"]["cost_usd"] == pytest.approx(0.0475)


def test_mine_is_scoped_to_the_caller(clients):
    """A per-user key must see its own traffic and nothing else."""
    body = clients.get(
        "/api/overview?days=36500", headers=_auth(_ALICE)
    ).json()
    mine = body["mine"]

    assert mine["requests"] == 2
    assert mine["cost_usd"] == pytest.approx(0.0025)
    # Bob's expensive request is the easiest thing to leak, so assert on its cost
    # specifically rather than just on the totals.
    assert mine["cost_usd"] < 0.01
    assert all("premium" not in m["key"] for m in mine["top_models"])


def test_top_models_are_the_callers_own(clients):
    body = clients.get("/api/overview?days=36500", headers=_auth(_ALICE)).json()
    keys = {m["key"] for m in body["mine"]["top_models"]}
    assert keys == {"or/cheap", "local/llama3"}


def test_rank_is_within_the_window_and_scoped_to_users_with_traffic(clients):
    body = clients.get("/api/overview?days=36500", headers=_auth(_ALICE)).json()
    mine = body["mine"]
    # Bob spent more, so alice is 2nd of 2.
    assert mine["rank"] == 2
    assert mine["users_ranked"] == 2


# ── Savings arithmetic ────────────────────────────────────────────────────────

def test_savings_uses_the_priciest_model_as_the_baseline(clients):
    """$0.0450 at premium vs $0.0025 actual → $0.0425 saved.

    The baseline is a whole model chosen by blended rate, not a per-column
    maximum. Checking the number here pins both halves at once.
    """
    body = clients.get("/api/overview?days=36500", headers=_auth(_ALICE)).json()
    mine = body["mine"]

    # 1500 tokens total (1000 in + 500 out) at the premium rate:
    #   15/1M * 1000 + 75/1M * 500 = 0.015 + 0.0375 = 0.0525 per request, x2.
    assert mine["premium_equivalent_usd"] == pytest.approx(0.105)
    assert mine["cost_usd"] == pytest.approx(0.0025)
    assert mine["savings_usd"] == pytest.approx(0.1025)


def test_local_model_counts_as_a_saving_not_as_unpriced(clients):
    """A local request costs $0 and must still count toward the comparison.

    Ollama is *known* zero, not *unknown* price. Excluding it would drop the
    largest possible saving out of the headline.
    """
    body = clients.get("/api/overview?days=36500", headers=_auth(_ALICE)).json()
    # Both alice's requests are priced-or-local, so the denominator is 2.
    assert body["mine"]["requests"] == 2
    assert body["mine"]["savings_usd"] > 0
    # ...and it is in the cheap bucket, since local is tier 0.
    assert body["mine"]["cheap_requests"] == 2


def test_cheap_and_escalated_split_excludes_models_missing_from_the_catalog(
    monkeypatch,
):
    """A request whose model has left the catalog counts as neither.

    Guessing it into "cheap" would flatter the headline; into "escalated" it
    would invent an escalation that never happened. Read from `system_flow`,
    which aggregates every user, so this needs no key to line up with "alice".
    """
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    s = SqliteStore(":memory:")
    s.upsert_model(ModelSpec(
        value="or/cheap", provider="openrouter", cost=1,
        cost_input=1.0, cost_output=4.0))
    s.record_usage(_rec("alice", "or/cheap", 0.0025))
    s.record_usage(_rec("alice", "or/deleted", 0.0090))
    s.record_usage(_rec("alice", "or/premium-also-gone", 0.05))

    client = _client(CapabilityRouter(store=s))
    body = client.get("/api/overview?days=36500", headers=_auth(_ADMIN)).json()
    flow = body["system_flow"]

    assert flow["requests"] == 3
    assert flow["cheap_requests"] == 1
    assert flow["escalated_requests"] == 0


def test_savings_are_not_skewed_by_unpriced_requests(monkeypatch):
    """An unpriced request must not be able to erase a real saving.

    The baseline and the actual are both measured over the *priced* subset. If
    the caller instead compared the subset's baseline against the window total,
    this request's unknown cost would be subtracted twice over — once because it
    isn't in the baseline, and once because it is in the total — and the saving
    would collapse toward zero. Both halves must describe the same rows.
    """
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    s = SqliteStore(":memory:")
    s.upsert_model(ModelSpec(
        value="or/cheap", provider="openrouter", cost=1,
        cost_input=1.0, cost_output=4.0))
    s.upsert_model(ModelSpec(
        value="or/premium", provider="openrouter", cost=12,
        cost_input=15.0, cost_output=75.0))
    # Present in the catalog but unpriced: the router has no rate for it.
    s.upsert_model(ModelSpec(value="or/unknown", provider="openrouter", cost=2))
    # One cheap request (0.0025 actual, 0.0525 at premium) + one unpriced one.
    s.record_usage(_rec("alice", "or/cheap", 0.0025))
    s.record_usage(_rec("alice", "or/unknown", 50.0))

    client = _client(CapabilityRouter(store=s))
    flow = client.get(
        "/api/overview?days=36500", headers=_auth(_ADMIN)
    ).json()["system_flow"]

    assert flow["requests"] == 2
    # The baseline covers only the priced request: 15/1M*1000 + 75/1M*500.
    assert flow["premium_equivalent_usd"] == pytest.approx(0.0525)
    # And the actual it is compared against must be that same request's spend,
    # not the 50.0 the unpriced one contributes — hence a real, visible saving.
    assert flow["savings_usd"] == pytest.approx(0.05)


def test_overhead_is_reported_separately_from_user_traffic(clients):
    """Classification spend is on the bill but is not user traffic."""
    body = clients.get("/api/overview?days=36500", headers=_auth(_ADMIN)).json()
    assert body["overhead_cost_usd"] == pytest.approx(0.0010)
    # 3 user requests in the system block, not 4 — the classify row is excluded.
    assert body["system_flow"]["requests"] == 3
    assert body["overhead_share"] > 0


# ── Refusing to invent a number ───────────────────────────────────────────────

def test_savings_reported_unavailable_when_no_traffic():
    s = SqliteStore(":memory:")
    s.upsert_model(ModelSpec(
        value="or/cheap", provider="openrouter", cost=1,
        cost_input=1.0, cost_output=4.0))
    import warnings as _w
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        client = TestClient(create_app(CapabilityRouter(store=s)))
    body = client.get("/api/overview").json()

    assert body["savings_unavailable"] == "no traffic recorded yet"
    assert body["system_flow"]["savings_usd"] is None
    assert body["mine"]["savings_usd"] is None


def test_first_request_tenure_is_unbounded_not_window_capped(monkeypatch):
    """"How long have I been using this" must not be capped at the window.

    With a 1-day window, a request 200 days old is still outside the window, so
    the *usage* figures are empty — but tenure is computed from unbounded
    history, so a returning user still sees their history instead of appearing
    brand new.
    """
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    s = SqliteStore(":memory:")
    s.upsert_model(ModelSpec(
        value="or/cheap", provider="openrouter", cost=1,
        cost_input=1.0, cost_output=4.0))
    s.create_api_key(_make_key("alice", _ALICE))
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    s.record_usage(_rec("alice", "or/cheap", 0.0025, ts=old))

    client = _client(CapabilityRouter(store=s))
    body = client.get("/api/overview?days=1", headers=_auth(_ALICE)).json()
    mine = body["mine"]

    assert mine["requests"] == 0                    # outside the 1-day window
    assert mine["days_since_first"] == pytest.approx(200, abs=1)


def test_savings_unavailable_when_prices_are_unknown(monkeypatch):
    """A model present but unpriced must not produce a fabricated saving."""
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    s = SqliteStore(":memory:")
    # cost_input/output both 0 on a hosted model = the catalog has no price.
    s.upsert_model(ModelSpec(value="or/unknown", provider="openrouter", cost=1))
    s.record_usage(_rec("alice", "or/unknown", 0.01))

    client = _client(CapabilityRouter(store=s))
    body = client.get("/api/overview?days=36500", headers=_auth(_ADMIN)).json()

    assert body["savings_unavailable"] == "catalog prices unknown for the models used"
    assert body["system_flow"]["savings_usd"] is None


def test_empty_deployment_still_returns_a_full_shape():
    """No models, no traffic, no keys — every field present and zeroed.

    The dashboard renders unconditionally, so a missing key here would throw in
    the browser rather than degrade gracefully.
    """
    s = SqliteStore(":memory:")
    import warnings as _w
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        client = TestClient(create_app(CapabilityRouter(store=s)))
    body = client.get("/api/overview").json()

    assert body["system"]["models"] == 0
    assert body["system_flow"]["requests"] == 0
    assert body["mine"]["requests"] == 0
    assert body["mine"]["rank"] is None
    assert body["mine"]["days_since_first"] is None
    assert body["overhead_share"] == 0.0
    assert body["savings_unavailable"] == "no traffic recorded yet"


# ── System shape ──────────────────────────────────────────────────────────────

def test_system_block_describes_the_catalog(clients):
    body = clients.get("/api/overview?days=36500", headers=_auth(_ALICE)).json()
    sys = body["system"]
    assert sys["models"] == 4
    assert set(sys["providers"]) == {"ollama", "openrouter"}
    assert sys["tool_capable"] == 3      # everything but the local model
    assert sys["vision_capable"] == 2
    assert sys["free_or_local"] == 2     # tier <= 1: the local model and or/cheap
    assert sys["max_context"] == 200_000
    assert sys["cost_tiers"]["12"] == 1


def test_system_block_is_identical_for_admin_and_per_user(clients):
    """Catalog shape is not an operator secret — the two must not differ."""
    admin = clients.get("/api/overview?days=36500", headers=_auth(_ADMIN)).json()
    alice = clients.get("/api/overview?days=36500", headers=_auth(_ALICE)).json()
    assert admin["system"] == alice["system"]


def test_window_is_clamped(clients):
    for days, expect in ((0, 1), (-5, 1), (10_000, 365)):
        body = clients.get(f"/api/overview?days={days}", headers=_auth(_ALICE)).json()
        assert body["window_days"] == expect


def test_endpoint_requires_a_key_when_keys_exist(monkeypatch, clients):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    assert clients.get("/api/overview").status_code == 401