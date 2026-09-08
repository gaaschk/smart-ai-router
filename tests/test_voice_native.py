"""Model-native speech-to-speech voice (/v1/voice) — the gate, the guards, the bill.

Three things this route has to get right, and they are the three things it is
tested for. It must be reachable by the admin identity and nobody else, because
every turn spends money on a model that was never chosen on price. It must refuse
a malformed or oversized body *before* forwarding it, because the upstream bills
audio tokens whether or not it can use them. And the cost it records must be the
one the provider reports, not the one the text rate implies — a voice conversation
priced at the text rate reads as almost free.
"""
import base64
import json
import warnings

import httpx
import pytest
from fastapi.testclient import TestClient

from smart_ai_router.api import voice_routes
from smart_ai_router.api.app import create_app
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.models import ProviderConfig
from smart_ai_router.store.sqlite_store import SqliteStore

_ADMIN = "admin-secret"
_WAV = base64.b64encode(b"RIFF....WAVEfmt fake pcm payload").decode()


def _client(cr) -> TestClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
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


def _sse(*objs: dict) -> bytes:
    body = "".join(f"data: {json.dumps(o)}\n\n" for o in objs)
    return (body + "data: [DONE]\n\n").encode()


class _FakeUpstream:
    """Stands in for the provider, recording the request it was handed.

    Patches httpx.AsyncClient.send rather than the transport so the assertions can
    read the exact JSON body the route built — which is the part with the audio
    modality flags in it, and the part that silently returns text-only if wrong.
    """

    def __init__(self, chunks: bytes, status: int = 200):
        self.chunks = chunks
        self.status = status
        self.sent: dict | None = None

    def install(self, monkeypatch):
        outer = self

        async def fake_send(self, request, **kwargs):  # noqa: ANN001
            outer.sent = json.loads(request.content)
            return httpx.Response(
                outer.status, content=outer.chunks, request=request,
                headers={"Content-Type": "text/event-stream"},
            )

        monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
        return self


# ── the gate ─────────────────────────────────────────────────────────────────

def test_a_per_user_key_cannot_spend_audio_tokens(admin):
    client, cr = admin
    made = client.post("/api/keys", json={"user": "alice"}, headers=_auth(_ADMIN))
    alice = made.json()["key"]
    r = client.post("/v1/voice", json={"audio": _WAV}, headers=_auth(alice))
    assert r.status_code == 403
    assert "admin" in r.json()["detail"].lower()


def test_no_key_at_all_never_reaches_the_route(admin):
    client, _ = admin
    # 401 from the middleware, not 403 from the route: /v1/voice is absent from the
    # anonymous path allowlist, so an anonymous visitor is stopped a layer earlier.
    assert client.post("/v1/voice", json={"audio": _WAV}).status_code == 401


def test_admin_gets_through(admin, monkeypatch):
    client, _ = admin
    _FakeUpstream(_sse({"choices": [{"delta": {"audio": {"transcript": "hi"}}}]})).install(monkeypatch)
    r = client.post("/v1/voice", json={"audio": _WAV}, headers=_auth(_ADMIN))
    assert r.status_code == 200, r.text


# ── the guards, all of them before a provider call ───────────────────────────

@pytest.mark.parametrize("body,status", [
    ({}, 422),                                    # no audio at all
    ({"audio": ""}, 422),                         # empty
    ({"audio": 42}, 422),                         # not a string
    ({"audio": "not!base64!"}, 422),              # undecodable
    ({"audio": _WAV, "format": "webm"}, 422),     # MediaRecorder's format
    ({"audio": "A" * 4_000_004}, 413),            # runaway recording
])
def test_a_bad_body_is_refused_without_spending_anything(admin, monkeypatch, body, status):
    client, _ = admin
    fake = _FakeUpstream(_sse()).install(monkeypatch)
    r = client.post("/v1/voice", json=body, headers=_auth(_ADMIN))
    assert r.status_code == status, r.text
    assert fake.sent is None, "a malformed body was forwarded to a metered provider"


def test_an_unconfigured_model_is_a_422_not_a_provider_call(admin, monkeypatch):
    client, cr = admin
    cr.set_setting("voice_native_model", "")
    fake = _FakeUpstream(_sse()).install(monkeypatch)
    r = client.post("/v1/voice", json={"audio": _WAV}, headers=_auth(_ADMIN))
    assert r.status_code == 422
    assert "Settings" in r.json()["detail"]
    assert fake.sent is None


# ── the request actually built ───────────────────────────────────────────────

def test_it_asks_for_audio_out_and_streams(admin, monkeypatch):
    client, _ = admin
    fake = _FakeUpstream(_sse()).install(monkeypatch)
    client.post("/v1/voice", json={"audio": _WAV}, headers=_auth(_ADMIN))
    sent = fake.sent
    # Without every one of these the model answers in text and the page plays
    # silence — the failure mode is a working request that returns the wrong
    # modality, so each flag is asserted rather than the shape as a whole.
    assert sent["modalities"] == ["text", "audio"]
    assert sent["audio"]["format"] == "pcm16"
    assert sent["stream"] is True
    assert sent["usage"] == {"include": True}
    assert sent["model"] == "openai/gpt-audio-mini"   # openrouter/ prefix stripped
    part = sent["messages"][-1]["content"][0]
    assert part["type"] == "input_audio"
    assert part["input_audio"] == {"data": _WAV, "format": "wav"}


def test_history_is_replayed_as_text_and_never_as_audio(admin, monkeypatch):
    client, _ = admin
    fake = _FakeUpstream(_sse()).install(monkeypatch)
    client.post("/v1/voice", headers=_auth(_ADMIN), json={
        "audio": _WAV,
        "messages": [
            {"role": "assistant", "content": "Paris."},
            # An upload from the typed chat. Sent to an audio model this is at best
            # ignored and at worst a 400 halfway through a conversation.
            {"role": "user", "content": [
                {"type": "text", "text": "and this one?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
            ]},
            {"role": "tool", "content": "should not survive"},
            {"role": "assistant", "content": "   "},   # whitespace only
        ],
    })
    prior = fake.sent["messages"][:-1]
    assert prior == [
        {"role": "assistant", "content": "Paris."},
        {"role": "user", "content": "and this one?"},
    ]
    assert "image_url" not in json.dumps(fake.sent["messages"])


def test_only_the_last_turns_are_replayed(admin, monkeypatch):
    client, _ = admin
    fake = _FakeUpstream(_sse()).install(monkeypatch)
    client.post("/v1/voice", headers=_auth(_ADMIN), json={
        "audio": _WAV,
        "messages": [{"role": "assistant", "content": f"turn {i}"} for i in range(50)],
    })
    prior = fake.sent["messages"][:-1]
    assert len(prior) == 20
    assert prior[0]["content"] == "turn 30"


# ── the bill ─────────────────────────────────────────────────────────────────

def test_the_recorded_cost_is_the_providers_not_the_text_rate(admin, monkeypatch):
    client, cr = admin
    _FakeUpstream(_sse(
        {"choices": [{"delta": {"audio": {"transcript": "Paris."}}}]},
        {"choices": [], "usage": {"prompt_tokens": 480, "completion_tokens": 260,
                                  "cost": 0.0731}},
    )).install(monkeypatch)
    r = client.post("/v1/voice", json={"audio": _WAV}, headers=_auth(_ADMIN))
    assert r.status_code == 200
    r.read()   # the usage row is written when the stream finishes, not before

    rows = cr.recent_usage("admin", "1970-01-01")
    voice = [u for u in rows if u.domain == "voice"]
    assert len(voice) == 1, f"expected one voice row, got {rows}"
    row = voice[0]
    assert row.user == "admin"
    assert row.prompt_tokens == 480
    assert row.completion_tokens == 260
    # The number the provider reported. Pricing this off the model's text rate is
    # what would make an hour of conversation look like a rounding error.
    assert row.cost_usd == pytest.approx(0.0731)
    assert row.status == 200
    # Counted as user traffic, because that is what it is — a voice turn that does
    # not show up in the spend rollup is a voice turn nobody notices paying for.
    assert row.kind == "proxy"


def test_a_refusal_upstream_is_reported_and_logged(admin, monkeypatch):
    client, cr = admin
    _FakeUpstream(b'{"error":{"message":"no audio for you"}}', status=402).install(monkeypatch)
    r = client.post("/v1/voice", json={"audio": _WAV}, headers=_auth(_ADMIN))
    assert r.status_code == 402
    assert "no audio for you" in r.json()["detail"]
    assert [u for u in cr.recent_usage("admin", "1970-01-01") if u.status == 402]


def test_the_page_is_told_which_model_and_which_sample_rate(admin, monkeypatch):
    client, _ = admin
    _FakeUpstream(_sse()).install(monkeypatch)
    r = client.post("/v1/voice", json={"audio": _WAV}, headers=_auth(_ADMIN))
    assert r.headers["X-Voice-Model"] == "openrouter/openai/gpt-audio-mini"
    # The page builds AudioBuffers at exactly this rate; a wrong value plays the
    # reply pitch-shifted rather than failing, so it is sent rather than assumed.
    assert r.headers["X-Voice-Sample-Rate"] == str(voice_routes._OUT_RATE)


def test_the_stream_reaches_the_page_intact(admin, monkeypatch):
    """Both halves of every delta have to survive: `data` is played, `transcript`
    is displayed, and re-encoding either one on the way past would only add
    latency to the thing being streamed."""
    client, _ = admin
    _FakeUpstream(_sse(
        {"choices": [{"delta": {"audio": {"data": "AAECAw==", "transcript": "Pa"}}}]},
        {"choices": [{"delta": {"audio": {"data": "BAUGBw==", "transcript": "ris."}}}]},
    )).install(monkeypatch)
    r = client.post("/v1/voice", json={"audio": _WAV}, headers=_auth(_ADMIN))
    text = r.text
    assert "AAECAw==" in text and "BAUGBw==" in text
    assert "[DONE]" in text
