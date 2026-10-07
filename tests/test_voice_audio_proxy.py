"""OpenRouter passthroughs for /v1/audio/speech and /v1/audio/transcriptions.

Three things these routes have to get right, mirroring the voice suite:

1. Reachable by the admin identity only — every turn bills provider audio tokens.
2. A malformed body is refused *before* forwarding (the upstream bills whether
   it can use a file or not).
3. The multipart transcription request does not set Content-Type: application/json,
   which would break httpx's boundary generation — the exact bug that bites a
   naive copy of _headers() from proxy.py.
"""
import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ProviderConfig
from smart_ai_router.store.sqlite_store import SqliteStore

_ADMIN = "admin-secret"


def _client(cr) -> TestClient:
    return TestClient(create_app(cr))


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    cr = CapabilityRouter(store=SqliteStore(":memory:"))
    cr.upsert_provider(ProviderConfig(
        name="openrouter", kind="openrouter", api_key="or-test-key",
    ))
    return _client(cr), cr


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class _FakeUpstream:
    """Records requests sent to the provider, returns canned responses."""

    def __init__(self, *, tts_bytes=b"\x00\x01\x02\x03", stt_json=None, status=200):
        self.tts_bytes = tts_bytes
        self.stt_json = stt_json or {"text": "hello world", "task": "transcribe", "duration": 1.2}
        self.status = status
        self.tts_requests: list[dict] = []
        self.stt_requests: list[dict] = []

    def install(self, monkeypatch):
        outer = self

        async def fake_send(self, request, **kwargs):
            url = str(request.url)
            if "/audio/speech" in url:
                outer.tts_requests.append({
                    "url": url,
                    "headers": dict(request.headers),
                    "json": json.loads(request.content),
                })
                return httpx.Response(
                    outer.status, content=outer.tts_bytes, request=request,
                    headers={"Content-Type": "audio/mpeg"},
                )
            if "/audio/transcriptions" in url:
                content = await request.aread()
                outer.stt_requests.append({
                    "url": url,
                    "headers": dict(request.headers),
                    "content_type": request.headers.get("content-type", ""),
                    "content": content,
                })
                return httpx.Response(
                    outer.status, content=json.dumps(outer.stt_json), request=request,
                    headers={"Content-Type": "application/json"},
                )
            return httpx.Response(404, request=request)

        monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
        return self


# ── the gate ─────────────────────────────────────────────────────────────────

def test_tts_rejects_non_admin(admin):
    client, _ = admin
    made = client.post("/api/keys", json={"user": "alice"}, headers=_auth(_ADMIN))
    alice = made.json()["key"]
    r = client.post("/v1/audio/speech", json={"input": "hi", "model": "tts"},
                    headers=_auth(alice))
    assert r.status_code == 403


def test_stt_rejects_non_admin(admin):
    client, _ = admin
    made = client.post("/api/keys", json={"user": "bob"}, headers=_auth(_ADMIN))
    bob = made.json()["key"]
    r = client.post("/v1/audio/transcriptions",
                    files={"file": ("a.wav", io.BytesIO(b"RIFF"))},
                    headers=_auth(bob))
    assert r.status_code == 403


def test_anonymous_is_401_not_403(admin):
    """Not in _ANON_PATHS, so the middleware turns it away before the route runs."""
    client, _ = admin
    assert client.post("/v1/audio/speech", json={"input": "hi"}).status_code == 401
    assert client.post("/v1/audio/transcriptions",
                       files={"file": ("a.wav", io.BytesIO(b"RIFF"))}).status_code == 401


# ── the guards ───────────────────────────────────────────────────────────────

def test_tts_requires_input(admin, monkeypatch):
    client, _ = admin
    fake = _FakeUpstream().install(monkeypatch)
    r = client.post("/v1/audio/speech", json={"input": ""}, headers=_auth(_ADMIN))
    assert r.status_code == 422
    assert not fake.tts_requests, "empty input was forwarded to a metered provider"


def test_tts_requires_model_setting(admin, monkeypatch):
    client, cr = admin
    cr.set_setting("tts_model", "")
    fake = _FakeUpstream().install(monkeypatch)
    r = client.post("/v1/audio/speech", json={"input": "hi"}, headers=_auth(_ADMIN))
    assert r.status_code == 422
    assert "text-to-speech model" in r.json()["detail"].lower()
    assert not fake.tts_requests


def test_stt_requires_file(admin, monkeypatch):
    client, _ = admin
    fake = _FakeUpstream().install(monkeypatch)
    r = client.post("/v1/audio/transcriptions",
                    data={"model": "stt"}, headers=_auth(_ADMIN))
    assert r.status_code == 422
    assert not fake.stt_requests


def test_stt_requires_model_setting(admin, monkeypatch):
    client, cr = admin
    cr.set_setting("stt_model", "")
    fake = _FakeUpstream().install(monkeypatch)
    r = client.post("/v1/audio/transcriptions",
                    files={"file": ("a.wav", io.BytesIO(b"RIFF"))},
                    headers=_auth(_ADMIN))
    assert r.status_code == 422
    assert "speech-to-text model" in r.json()["detail"].lower()
    assert not fake.stt_requests


# ── the forwarding ───────────────────────────────────────────────────────────

def test_tts_forwards_json_and_strips_prefix(admin, monkeypatch):
    client, _ = admin
    fake = _FakeUpstream().install(monkeypatch)
    r = client.post("/v1/audio/speech", json={
        "input": "Hello [whispering] world",
        "voice": "Rachel",
        "response_format": "mp3",
    }, headers=_auth(_ADMIN))
    assert r.status_code == 200, r.text
    sent = fake.tts_requests[0]
    assert sent["json"]["model"] == "elevenlabs/eleven-v3"   # openrouter/ stripped
    assert sent["json"]["input"] == "Hello [whispering] world"
    assert sent["json"]["voice"] == "Rachel"
    # No Content-Type mismatch — the JSON path sets it correctly.
    assert sent["headers"]["content-type"] == "application/json"
    # Binary passthrough — not a JSON blob.
    assert r.content == b"\x00\x01\x02\x03"
    assert r.headers["content-type"] == "audio/mpeg"


def test_stt_forwards_multipart_without_json_content_type(admin, monkeypatch):
    """The critical test: multipart must NOT carry Content-Type: application/json."""
    client, _ = admin
    fake = _FakeUpstream().install(monkeypatch)
    r = client.post("/v1/audio/transcriptions",
                    files={"file": ("a.wav", io.BytesIO(b"RIFF"), "audio/wav")},
                    data={"model": "stt", "language": "en"},
                    headers=_auth(_ADMIN))
    assert r.status_code == 200, r.text
    sent = fake.stt_requests[0]
    # Must be multipart/form-data with a boundary — httpx sets this only when the
    # caller does not pin Content-Type to application/json.
    assert sent["content_type"].startswith("multipart/form-data; boundary=")
    assert "application/json" not in sent["content_type"]
    # The file content is in the forwarded body.
    assert b"RIFF" in sent["content"]
    # Model prefix stripped.
    assert b"elevenlabs/scribe-v2" in sent["content"]
    # Response is the JSON transcription.
    assert r.json()["text"] == "hello world"


def test_stt_upstream_error_is_suraced(admin, monkeypatch):
    client, _ = admin
    _FakeUpstream(stt_json={"error": {"message": "unsupported file"}}, status=400).install(monkeypatch)
    r = client.post("/v1/audio/transcriptions",
                    files={"file": ("a.wav", io.BytesIO(b"RIFF"), "audio/wav")},
                    headers=_auth(_ADMIN))
    assert r.status_code == 400
    assert "unsupported file" in r.json()["detail"]


def test_tts_upstream_error_is_suraced(admin, monkeypatch):
    client, _ = admin
    _FakeUpstream(status=402).install(monkeypatch)
    r = client.post("/v1/audio/speech", json={"input": "hi"}, headers=_auth(_ADMIN))
    assert r.status_code == 402


def test_stt_unreachable_is_502(admin, monkeypatch):
    client, _ = admin

    async def fail(self, request, **kwargs):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx.AsyncClient, "send", fail)
    r = client.post("/v1/audio/transcriptions",
                    files={"file": ("a.wav", io.BytesIO(b"RIFF"), "audio/wav")},
                    headers=_auth(_ADMIN))
    assert r.status_code == 502


# ── usage tracking ───────────────────────────────────────────────────────────

def test_usage_rows_are_recorded(admin, monkeypatch):
    client, cr = admin
    _FakeUpstream().install(monkeypatch)
    client.post("/v1/audio/speech", json={"input": "hi"}, headers=_auth(_ADMIN))
    client.post("/v1/audio/transcriptions",
                files={"file": ("a.wav", io.BytesIO(b"RIFF"), "audio/wav")},
                headers=_auth(_ADMIN))
    rows = [u for u in cr.recent_usage("admin", "1970-01-01") if u.domain == "voice"]
    # One for tts, one for stt — both should be accounted for even though we can't
    # charge the text rate to an audio model's real cost.
    assert len(rows) == 2
    assert all(r.status == 200 for r in rows)
