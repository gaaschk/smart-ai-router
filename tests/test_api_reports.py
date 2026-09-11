"""Reporting a bad response: /api/reports.

The point of the feature is that the evidence is collected *for* the user — the
whole conversation and the routing decision — so the tests are mostly about the
evidence surviving: intact when it fits, trimmed from the oldest end when it
doesn't, and still there after the reported thread is deleted (which is why the
transcript is a snapshot and not a join back to chat_messages).

The rest is the trust boundary. Anyone may file a report, anonymous visitors
included; only the operator may read or clear one, because a report is somebody
else's chat.
"""
import json
import warnings

import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.app import _anon_path_allowed, create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.store.sqlite_store import SqliteStore

_ADMIN = "admin-secret"

_TURNS = [
    {"role": "user", "content": "What's the capital of Australia?"},
    {"role": "assistant", "content": "Sydney."},
]


def _client(cr) -> TestClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return TestClient(create_app(cr))


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def open_client(monkeypatch):
    """No keys configured: the caller is the operator, as on a first-run install."""
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    return _client(CapabilityRouter(store=SqliteStore(":memory:")))


@pytest.fixture
def scoped(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    client = _client(CapabilityRouter(store=SqliteStore(":memory:")))
    alice = client.post("/api/keys", json={"user": "alice"}, headers=_auth(_ADMIN)).json()["key"]
    return client, alice


def _file(client, headers=None, **body):
    payload = {"description": "It said Sydney; the capital is Canberra.",
               "transcript": _TURNS, **body}
    return client.post("/api/reports", json=payload, headers=headers or {})


# ── the context is gathered, not asked for ────────────────────────────────────

def test_a_report_keeps_the_whole_conversation_and_the_routing_decision(open_client):
    r = _file(open_client, conversation_id="conv-x",
              meta={"routed": "ollama/qwen3:8b", "profile": "General knowledge @ basic"})
    assert r.status_code == 200
    rep = r.json()
    assert rep["id"] > 0 and rep["ts"]

    listed = open_client.get("/api/reports").json()["data"]
    assert len(listed) == 1
    got = listed[0]
    # Every turn, verbatim — a report that paraphrased the conversation would be
    # useless for reproducing the answer that was wrong.
    assert got["transcript"] == _TURNS
    assert got["description"].startswith("It said Sydney")
    assert got["conversation_id"] == "conv-x"
    # Which model answered is nowhere else: routing is not stored per message, so
    # if the report doesn't carry it, nothing does.
    assert got["meta"]["routed"] == "ollama/qwen3:8b"


def test_the_snapshot_outlives_the_conversation_it_came_from(open_client):
    cid = open_client.post("/api/conversations", json={"title": "t"}).json()["id"]
    open_client.post(f"/api/conversations/{cid}/messages",
                     json={"role": "user", "content": "What's the capital of Australia?"})
    _file(open_client, conversation_id=cid)

    open_client.delete(f"/api/conversations/{cid}")
    # The reporter can delete their thread a second after filing. If the report
    # were a pointer at those rows, deleting them would erase the evidence.
    assert open_client.get("/api/reports").json()["data"][0]["transcript"] == _TURNS


def test_a_thread_that_was_never_saved_is_still_reportable(open_client):
    # A guest with chat persistence off, or a report filed before the first turn
    # persisted, has no conversation id to give. That must not block the report.
    r = _file(open_client, conversation_id="")
    assert r.status_code == 200
    assert open_client.get("/api/reports").json()["data"][0]["conversation_id"] == ""


def test_reports_come_back_newest_first(open_client):
    _file(open_client, description="first")
    _file(open_client, description="second")
    assert [r["description"] for r in open_client.get("/api/reports").json()["data"]] \
        == ["second", "first"]


# ── untrusted input: the body is whatever the browser sent ────────────────────

def test_a_report_with_nothing_written_in_it_is_refused(open_client):
    # The description is the one thing the feature actually asks the user for; an
    # empty one is a transcript nobody knows what to look at.
    assert _file(open_client, description="   ").status_code == 422
    assert _file(open_client, description="x" * 4001).status_code == 422
    assert open_client.get("/api/reports").json()["data"] == []


def test_an_oversized_transcript_keeps_its_most_recent_turns(open_client):
    # Trimming from the front, because the reported reply is at the end. Dropping
    # the tail would throw away the only turn the report is actually about.
    filler = [{"role": "user", "content": "x" * 20_000} for _ in range(40)]
    r = _file(open_client, transcript=filler + [{"role": "assistant", "content": "the bad answer"}])
    assert r.status_code == 200
    kept = open_client.get("/api/reports").json()["data"][0]["transcript"]
    assert kept[-1]["content"] == "the bad answer"
    assert 0 < len(kept) < 41
    assert len(json.dumps(kept)) <= 256_000


def test_one_enormous_turn_is_shortened_rather_than_dropped(open_client):
    # A pasted logfile or a base64 image in one message must not cost the report
    # the message it lives in — nor the ones around it.
    r = _file(open_client, transcript=[{"role": "user", "content": "y" * 500_000}])
    assert r.status_code == 200
    kept = open_client.get("/api/reports").json()["data"][0]["transcript"]
    assert len(kept) == 1
    assert "…[truncated]" in kept[0]["content"]
    assert len(kept[0]["content"]) < 40_000


def test_an_unreasonable_meta_blob_is_refused(open_client):
    # `meta` is the routing badge, not a second place to put a transcript.
    assert _file(open_client, meta={"why": "z" * 5000}).status_code == 422


# ── who may file, and who may read ───────────────────────────────────────────

def test_filing_is_reachable_without_a_key_and_reading_is_not(scoped):
    client, alice = scoped
    # Public chat's allow-list carries the exact path only, so an anonymous
    # visitor can file but never reach /api/reports/{id}.
    assert _anon_path_allowed("/api/reports")
    assert not _anon_path_allowed("/api/reports/1")

    assert _file(client, headers=_auth(alice)).status_code == 200
    # A per-user key filed it and still can't read it back: the transcript in
    # someone else's report is someone else's chat.
    assert client.get("/api/reports", headers=_auth(alice)).status_code == 403
    assert client.delete("/api/reports/1", headers=_auth(alice)).status_code == 403

    listed = client.get("/api/reports", headers=_auth(_ADMIN)).json()["data"]
    assert [r["user"] for r in listed] == ["alice"]   # attributed to the reporter


def test_admin_can_clear_a_report_it_has_acted_on(scoped):
    client, _ = scoped
    rid = _file(client, headers=_auth(_ADMIN)).json()["id"]
    assert client.delete(f"/api/reports/{rid}", headers=_auth(_ADMIN)).json()["deleted"] is True
    assert client.get("/api/reports", headers=_auth(_ADMIN)).json()["data"] == []
    # A second delete is not an error; it just has nothing to delete.
    assert client.delete(f"/api/reports/{rid}", headers=_auth(_ADMIN)).json()["deleted"] is False
