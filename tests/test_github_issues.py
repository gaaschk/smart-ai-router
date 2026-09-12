"""Filing a report as a GitHub issue.

Two things are being protected here. One is the operator's issue tracker being
worth reading: the issue has to carry the description, the routing decision, and
a way back to the local report. The other is the reporter — an issue is public,
so the conversation is withheld unless the operator opted in, and the reporter is
never named at all.

And the whole mirror is best-effort: the report is stored first, so a dead token
costs an issue, never someone's feedback.
"""
import json
import warnings

import httpx
import pytest
from fastapi.testclient import TestClient

from smart_ai_router import github_issues as _github
from smart_ai_router import settings as _settings
from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.store.sqlite_store import SqliteStore


@pytest.fixture
def client(monkeypatch):
    """A keyless install (so the caller is the operator) with settings bound."""
    monkeypatch.delenv("SMART_ROUTER_API_KEYS", raising=False)
    for spec in _github_specs():
        monkeypatch.delenv(spec.env, raising=False)
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    _settings.bind_store(cr)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        yield TestClient(create_app(cr))
    _settings.bind_store(None)   # don't leak the binding into the next test


def _github_specs():
    return [s for s in _settings.SPECS if s.key.startswith("github_")]


def _configure(client, **overrides):
    updates = {
        "github_issues_enabled": True,
        "github_repo": "gaaschk/smart-ai-router",
        "github_token": "ghp_test",
        **overrides,
    }
    r = client.put("/api/settings", json={"updates": updates})
    assert r.status_code == 200, r.text
    return r


class _Sent:
    """Records the one POST github_issues makes, and answers it."""

    def __init__(self, status=201, url="https://github.com/o/n/issues/7"):
        self.status, self.url, self.calls = status, url, []

    def __call__(self, target, **kwargs):
        self.calls.append({"url": target, **kwargs})
        return httpx.Response(
            self.status,
            json={"html_url": self.url} if self.status < 300 else {"message": "nope"},
            request=httpx.Request("POST", target),
        )

    @property
    def body(self):
        return self.calls[0]["json"]["body"]


@pytest.fixture
def sent(monkeypatch):
    spy = _Sent()
    monkeypatch.setattr(_github.httpx, "post", spy)
    return spy


_TURNS = [
    {"role": "user", "content": "What's the capital of Australia?"},
    {"role": "assistant", "content": "Sydney."},
]


def _file(client, **body):
    payload = {
        "description": "It said Sydney; the capital is Canberra.\nSecond line.",
        "transcript": _TURNS,
        "meta": {"routed": "ollama/qwen3:8b", "profile": "General knowledge @ basic"},
        **body,
    }
    r = client.post("/api/reports", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# ── off unless the operator says otherwise ────────────────────────────────────

def test_nothing_is_posted_anywhere_until_it_is_configured(client, sent):
    # The default has to be silence: this ships to installs whose operator never
    # heard of the feature, and the first surprise must not be a public issue.
    rep = _file(client)
    assert sent.calls == []
    assert rep["issue_url"] == "" and rep["github_error"] == ""


def test_a_repo_without_a_token_is_not_half_enabled(client, sent):
    _configure(client, github_token="")
    _file(client)
    assert sent.calls == []          # no token = no attempt, not a 401 per report


def test_a_pasted_url_is_refused_rather_than_404ing_later(client):
    r = client.put("/api/settings", json={
        "updates": {"github_repo": "https://github.com/gaaschk/smart-ai-router"}})
    assert r.status_code == 422
    assert "owner/name" in r.json()["detail"]


# ── what the issue says ───────────────────────────────────────────────────────

def test_the_issue_carries_the_words_the_routing_and_a_way_back(client, sent):
    _configure(client)
    rep = _file(client)

    assert sent.calls[0]["url"].endswith("/repos/gaaschk/smart-ai-router/issues")
    assert sent.calls[0]["headers"]["Authorization"] == "Bearer ghp_test"
    # Title is the first line only — a whole paragraph in the title is unreadable
    # in a list of issues, which is where an issue is mostly seen.
    assert sent.calls[0]["json"]["title"] == "[report] It said Sydney; the capital is Canberra."
    body = sent.body
    assert "Second line." in body                 # the rest survives in the body
    assert "ollama/qwen3:8b" in body              # which model answered
    assert f"#{rep['id']}" in body                # back to the local report
    assert rep["issue_url"] == "https://github.com/o/n/issues/7"
    assert rep["github_error"] == ""


def test_a_rambling_first_line_is_cut_to_fit_an_issue_title(client, sent):
    _configure(client)
    _file(client, description="the model " + "kept going and going " * 20)
    title = sent.calls[0]["json"]["title"]
    assert len(title) <= len("[report] ") + 72 and title.endswith("…")
    assert "kept going" in title            # cut, not replaced by a placeholder


def test_the_conversation_is_withheld_from_the_public_issue_by_default(client, sent):
    _configure(client)
    _file(client)
    # The reported reply is quoted (it is the thing being complained about), but
    # the thread the user typed is not published on a whim.
    assert "Sydney." not in sent.body
    assert "What's the capital of Australia?" not in sent.body
    assert "Reports page" in sent.body            # says where the transcript is


def test_the_conversation_is_published_only_when_the_operator_opts_in(client, sent):
    _configure(client, github_include_transcript=True)
    _file(client)
    assert "What's the capital of Australia?" in sent.body


def test_the_reporter_is_not_named_in_public(client, sent, monkeypatch):
    _configure(client)     # while the install is still keyless
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", "admin-secret")
    alice = client.post("/api/keys", json={"user": "alice"},
                        headers={"Authorization": "Bearer admin-secret"}).json()["key"]
    client.post("/api/reports",
                json={"description": "bad answer", "transcript": _TURNS},
                headers={"Authorization": f"Bearer {alice}"})
    assert "alice" not in json.dumps(sent.calls[0]["json"])
    # …but the local report still knows who filed it.
    listed = client.get("/api/reports",
                        headers={"Authorization": "Bearer admin-secret"}).json()["data"]
    assert listed[0]["user"] == "alice"


def test_an_oversized_transcript_is_trimmed_before_it_is_published(client, sent):
    _configure(client, github_include_transcript=True)
    _file(client, transcript=[{"role": "user", "content": "x" * 20_000}
                              for _ in range(40)] + _TURNS)
    # The issue gets the same trimmed snapshot the report kept, not the raw body:
    # GitHub rejects a body over ~64k, so the whole issue would be lost.
    assert len(sent.body) < 70_000


def test_the_ui_learns_it_is_public_before_anyone_writes_anything(client):
    # The modal warns the user, so the warning has to be knowable at open time —
    # which means whoami, the one call the UI already makes.
    me = client.get("/api/whoami").json()
    assert me["reports_public"] is False and me["reports_public_transcript"] is False

    _configure(client)
    me = client.get("/api/whoami").json()
    assert me["reports_public"] is True
    assert me["reports_public_transcript"] is False   # public issue, private chat

    _configure(client, github_include_transcript=True)
    assert client.get("/api/whoami").json()["reports_public_transcript"] is True


# ── GitHub failing is not the report failing ──────────────────────────────────

def test_a_rejected_issue_still_leaves_the_report_filed_and_says_why(client, monkeypatch):
    _configure(client)
    monkeypatch.setattr(_github.httpx, "post", _Sent(status=401))
    rep = _file(client)
    assert rep["issue_url"] == ""
    assert "401" in rep["github_error"]
    # Stored, readable, complete — the user's words were never at risk.
    assert client.get("/api/reports").json()["data"][0]["transcript"] == _TURNS


def test_github_being_unreachable_is_recorded_not_raised(client, monkeypatch):
    def _boom(*a, **k):
        raise httpx.ConnectError("no route to host")

    _configure(client)
    monkeypatch.setattr(_github.httpx, "post", _boom)
    rep = _file(client)
    assert "unreachable" in rep["github_error"]
