"""GET /v1/models — the handshake an OpenAI-only editor makes before it will
let you pick anything.

Two things are worth protecting. The shape, because a client parses it with a
typed model and a missing field is a hard error, not a degraded list. And the
truth of it: the names offered here have to be names the completions endpoint
actually treats differently, or the list is decoration.
"""
import warnings

import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.proxy import _ORCHESTRATOR_MARKERS
from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.store.sqlite_store import SqliteStore


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        yield TestClient(create_app(CapabilityRouter(store=SqliteStore(":memory:"))))


def test_the_list_is_the_shape_an_openai_client_parses(client):
    r = client.get("/v1/models")
    assert r.status_code == 200
    payload = r.json()
    assert payload["object"] == "list"
    for m in payload["data"]:
        # openai-python builds a typed Model from each entry; every one of these
        # is required there, so a missing key is a client-side exception.
        assert set(m) == {"id", "object", "created", "owned_by"}
        assert m["object"] == "model"


def test_it_offers_the_two_names_that_change_the_routing(client):
    ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
    # Not the catalog: `model` in a completions body is overwritten with the
    # router's pick, so listing 200 model ids would promise a choice that does
    # not exist. Orchestrator mode is the one thing a caller really selects.
    assert any(_ORCHESTRATOR_MARKERS[0] in i for i in ids)
    assert any("worker" in i for i in ids)
    assert len(ids) == 2


def test_every_name_offered_is_one_the_proxy_understands(client):
    # The link this file exists for: an id here that the completions endpoint
    # doesn't recognize as orchestration would send tool-calling traffic to a
    # local model that can't tool-call.
    ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
    orchestrating = [i for i in ids
                     if any(m in i for m in _ORCHESTRATOR_MARKERS)]
    assert len(orchestrating) == 1


def test_an_anonymous_visitor_can_read_it(client):
    # It sits in _ANON_PATHS, which was true before the route existed. A public
    # install's chat page is unusable if the model handshake 401s.
    assert client.get("/v1/models").status_code == 200


def test_a_keyed_install_still_asks_for_a_key(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", "admin-secret")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        c = TestClient(create_app(CapabilityRouter(store=SqliteStore(":memory:"))))
    assert c.get("/v1/models").status_code == 401
    assert c.get("/v1/models",
                 headers={"Authorization": "Bearer admin-secret"}).status_code == 200
