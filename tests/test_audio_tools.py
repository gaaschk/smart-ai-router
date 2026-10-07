"""Voice design → selection → saved voice → authenticated downloadable MP3."""
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from smart_ai_router import audio_tools, tools
from smart_ai_router.api.app import create_app
from smart_ai_router.api.proxy import _agent_tool_schemas, _audio_followup
from smart_ai_router.classifier import is_actionable
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.store.sqlite_store import SqliteStore
from tests.test_proxy_failover import _spec, _Stream

_DESCRIPTION = "A deep gravelly Halloween skeleton voice with theatrical pauses."
_MP3 = b"ID3-test-audio"


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "private-test-key")
    calls, downloads = [], []
    original_post = httpx.Client.post

    def post(self, url, **kw):
        if not str(url).startswith("https://api.elevenlabs.io/"):
            return original_post(self, url, **kw)
        calls.append((url, kw))
        assert kw["headers"] == {"xi-api-key": "private-test-key"}
        if url.endswith("text-to-voice/design"):
            return httpx.Response(200, json={"text": "preview script", "previews": [{
                "generated_voice_id": "generated1", "media_type": "audio/mpeg",
                "audio_base_64": base64.b64encode(_MP3).decode(),
            }]})
        if url.endswith("text-to-voice"):
            return httpx.Response(200, json={"voice_id": "saved1"})
        return httpx.Response(200, content=_MP3, headers={"content-type": "audio/mpeg"})

    def register(data, name, mime):
        downloads.append((data, name, mime))
        return f"file-{len(downloads)}"

    monkeypatch.setattr(httpx.Client, "post", post)
    return calls, downloads, register


def test_design_save_reuse_and_generate(provider):
    calls, downloads, register = provider
    designed = json.loads(tools.execute_tool("admin", "design_voice",
        {"description": _DESCRIPTION}, register_file=register))
    preview = designed["previews"][0]
    assert "/v1/files/file-1/content" in preview["download"]
    listed = json.loads(tools.execute_tool("admin", "list_audio_voices", {}))
    assert listed["previews"][0]["preview_id"] == preview["preview_id"]
    assert listed["previews"][0]["preview_number"] == 1
    saved = tools.execute_tool("admin", "save_voice",
        {"preview_id": preview["preview_id"], "name": "Skeleton"})
    assert json.loads(saved)["voice_id"] == "saved1"
    assert tools.execute_tool("admin", "save_voice",
        {"preview_id": preview["preview_id"], "name": "Skeleton"}) == saved
    assert len(calls) == 2  # Repeated selection doesn't consume another voice slot.
    assert json.loads(tools.execute_tool("admin", "list_audio_voices", {}))["voices"] == [json.loads(saved)]
    assert not json.loads(tools.execute_tool("admin", "list_audio_voices", {}))["previews"]
    result = tools.execute_tool("admin", "generate_audio", {
        "voice_id": "saved1", "text": "[whispering] The living have arrived!", "path": "audio/skeleton.mp3",
    }, register_file=register)
    assert "[skeleton.mp3](/v1/files/file-2/content)" in result
    assert downloads[-1] == (_MP3, "skeleton.mp3", "audio/mpeg")
    assert calls[-1][0].endswith("text-to-speech/saved1")
    assert calls[-1][1]["json"] == {"text": "[whispering] The living have arrived!", "model_id": "eleven_v3"}


def test_audio_tools_are_admin_only_even_when_called_directly(provider):
    calls, _, register = provider
    for tool in ("design_voice", "save_voice", "list_audio_voices", "generate_audio"):
        assert tools.execute_tool("u:someone", tool, {}, register_file=register).startswith("Error: Audio tools")
    assert not calls
    admin_names = {s["function"]["name"] for s in _agent_tool_schemas("admin")}
    user_names = {s["function"]["name"] for s in _agent_tool_schemas("u:someone")}
    assert {"design_voice", "save_voice", "generate_audio", "list_audio_voices"} <= admin_names - user_names


@pytest.mark.parametrize("name,args", [
    ("design_voice", {"description": "short"}),
    ("save_voice", {"preview_id": "../../secret", "name": "Skeleton"}),
    ("generate_audio", {"voice_id": "../../voices", "text": "hello"}),
    ("generate_audio", {"voice_id": "saved1", "text": "hello", "path": "../escape.mp3"}),
    ("generate_audio", {"voice_id": "saved1", "text": "hello", "path": "fake.wav"}),
    ("generate_audio", {"voice_id": "saved1", "text": "x" * 5001}),
])
def test_invalid_inputs_do_not_call_provider(provider, name, args):
    calls, _, register = provider
    assert tools.execute_tool("admin", name, args, register_file=register).startswith("Error:")
    assert not calls


def test_missing_direct_key_explains_setup(provider, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    calls, _, register = provider
    error = tools.execute_tool("admin", "design_voice", {"description": _DESCRIPTION}, register_file=register)
    assert "ELEVENLABS_API_KEY" in error and "OpenRouter" in error
    assert not calls


def test_provider_failure_does_not_expose_key_or_create_download(provider, monkeypatch):
    _, downloads, register = provider
    monkeypatch.setattr(httpx.Client, "post", lambda *a, **kw:
        httpx.Response(429, text="rate limited private-test-key"))
    error = tools.execute_tool("admin", "design_voice", {"description": _DESCRIPTION}, register_file=register)
    assert "429" in error and "private-test-key" not in error
    assert not downloads


@pytest.mark.parametrize("body", [b"", b'{"error":"not audio"}'])
def test_invalid_speech_response_is_not_registered(provider, monkeypatch, body):
    _, downloads, register = provider
    monkeypatch.setattr(httpx.Client, "post", lambda *a, **kw:
        httpx.Response(200, content=body, headers={"content-type": "application/json"}))
    assert tools.execute_tool("admin", "generate_audio", {"voice_id": "saved1", "text": "hello"},
                              register_file=register).startswith("Error:")
    assert not downloads


def test_audio_requests_auto_enter_agent_mode():
    for prompt in ("Design a custom voice for a spooky skeleton", "Generate audio saying hello", "Save this as skeleton.mp3"):
        assert is_actionable(prompt)
    assert not is_actionable("Can ElevenLabs design custom voices?")


def test_preview_selection_keeps_audio_context_only():
    from smart_ai_router.agent_loop import _narration
    messages = [{"role": "assistant", "content": _narration("design_voice", {})}]
    assert _audio_followup(messages, "Use the second one")
    assert not _audio_followup(messages, "What is the capital of France?")
    assert not _audio_followup([{"role": "assistant", "content": "Hello"}], "Use the second one")


def test_admin_chat_designs_saves_and_downloads_mp3(provider, monkeypatch, tmp_path):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", "admin-secret")
    monkeypatch.setenv("SMART_ROUTER_FILES_DIR", str(tmp_path / "files"))
    for key in ("SMART_ROUTER_CLASSIFIER_MODEL", "SMART_ROUTER_CLASSIFIER_FALLBACK", "SMART_ROUTER_CLASSIFIER_REFINE_MODEL"):
        monkeypatch.setenv(key, "")
    store = SqliteStore(":memory:")
    store.upsert_model(_spec("openrouter/voice-worker", 1))
    client = TestClient(create_app(CapabilityRouter(store=store)))
    rounds = []

    def stream(self, method, url, **kw):
        request = kw["json"]
        rounds.append(request)
        assert "generate_audio" in {s["function"]["name"] for s in request["tools"]}
        if len(rounds) == 1:
            name, args = "design_voice", {"description": _DESCRIPTION}
        elif len(rounds) == 2:
            designed = json.loads(request["messages"][-1]["content"])
            name, args = "save_voice", {"preview_id": designed["previews"][0]["preview_id"], "name": "Skeleton"}
        elif len(rounds) == 3:
            saved = json.loads(request["messages"][-1]["content"])
            name, args = "generate_audio", {"voice_id": saved["voice_id"], "text": "Hello!"}
        if len(rounds) <= 3:
            call = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": f"audio{len(rounds)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]}}]}
            return _Stream(200, ("data: " + json.dumps(call) + "\n\ndata: [DONE]\n\n").encode())
        result = request["messages"][-1]["content"]
        return _Stream(200, ("data: " + json.dumps({"choices": [{"delta": {"content": result}}]}) + "\n\ndata: [DONE]\n\n").encode())

    monkeypatch.setattr(httpx.AsyncClient, "stream", stream)
    auth = {"Authorization": "Bearer admin-secret"}
    reply = client.post("/v1/chat/completions", headers=auth, json={
        "model": "auto", "messages": [{"role": "user", "content": "Design a custom voice, choose one yourself, and generate audio saying Hello!"}],
    })
    assert "Created audio:" in reply.text
    import re
    href = re.search(r"/v1/files/[^/]+/content", reply.text).group()
    download = client.get(href, headers=auth)
    assert download.status_code == 200 and download.content == _MP3
    assert download.headers["content-type"].startswith("audio/mpeg")
    assert client.get(href).status_code == 401
    preview_result = json.loads(rounds[1]["messages"][2]["content"])
    preview_href = re.search(r"/v1/files/[^/]+/content", preview_result["previews"][0]["download"]).group()
    assert client.get(preview_href, headers=auth).content == _MP3
