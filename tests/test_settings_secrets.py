"""Write-only Voice credentials: persistence, live use, redaction and form behavior."""
import json
from unittest.mock import patch

import httpx
import pytest

from smart_ai_router import audio_tools, settings
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.store.sqlite_store import SqliteStore
from tests.test_settings import _client, _ADMIN
from tests.test_voice_speech_text import _js_function, _run


def test_saved_key_is_redacted_persistent_and_used_live(tmp_path, monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "env-secret")
    database = str(tmp_path / "settings.db")
    cr = CapabilityRouter(store=SqliteStore(database))
    client = _client(cr)
    auth = {"Authorization": f"Bearer {_ADMIN}"}
    for value in ("saved-secret", "replacement-secret", ""):
        response = client.put("/api/settings", headers=auth,
            json={"updates": {"elevenlabs_api_key": value}})
        assert response.status_code == 200
        rows = {row["key"]: row for row in response.json()["settings"]}
        assert rows["elevenlabs_api_key"]["value"] == ""
        assert rows["elevenlabs_api_key"]["secret"] is True
        assert rows["elevenlabs_api_key"]["configured"] is bool(value)
        assert "env-secret" not in response.text
        assert "saved-secret" not in response.text and "replacement-secret" not in response.text
        fetched = client.get("/api/settings", headers=auth)
        assert "saved-secret" not in fetched.text and "replacement-secret" not in fetched.text
        assert SqliteStore(database).get_setting("elevenlabs_api_key") == value
        if value:
            with patch.object(httpx.Client, "post", return_value=httpx.Response(200, json={})) as post:
                audio_tools._post("text-to-voice/design", {})
                assert post.call_args.kwargs["headers"]["xi-api-key"] == value
        else:
            with pytest.raises(ValueError, match="Settings"):
                audio_tools._post("text-to-voice/design", {})


def test_env_key_is_redacted_and_works_as_fallback(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "env-secret")
    row = next(row for row in settings.effective() if row["key"] == "elevenlabs_api_key")
    assert row["source"] == "env" and row["configured"] is True and row["value"] == ""
    assert settings.get_str("elevenlabs_api_key") == "env-secret"


@pytest.mark.parametrize("value", [None, 123, "x" * 513, "key\ninjected", "key\rinjected"])
def test_invalid_credential_error_does_not_echo_input(value):
    with pytest.raises(ValueError) as exc:
        settings.normalize("elevenlabs_api_key", value)
    assert "injected" not in str(exc.value)


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node is not installed")
def test_secret_form_keeps_blank_field_and_sends_only_explicit_changes():
    spec = next(row for row in settings.effective() if row["key"] == "elevenlabs_api_key")
    spec["configured"] = True
    harness = "\n".join(_js_function(name) for name in (
        "renderSettingField", "_readSettingInput", "_collectChangedSettings", "clearSecretSetting"))
    harness += f"""
const _settingsSpecs = [{json.dumps(spec)}];
const el = {{value: '', dataset: {{}}}};
const document = {{getElementById: () => el}};
const escapeHtml = s => String(s);
const onSettingChanged = () => {{}};
const rendered = renderSettingField(_settingsSpecs[0]);
const unchanged = _collectChangedSettings();
el.value = ' replacement-secret ';
const replacement = _collectChangedSettings();
clearSecretSetting('elevenlabs_api_key');
const removed = _collectChangedSettings();
console.log(JSON.stringify([rendered, unchanged, replacement, removed]));
"""
    rendered, unchanged, replacement, removed = _run(harness)
    assert 'type="password"' in rendered and 'value=""' in rendered
    assert "Configured" in rendered and "Remove key" in rendered
    assert unchanged == {}
    assert replacement == {"elevenlabs_api_key": "replacement-secret"}
    assert removed == {"elevenlabs_api_key": ""}
