from unittest.mock import patch
import httpx
from smart_ai_router.facade import CapabilityRouter
from smart_ai_router.store.sqlite_store import SqliteStore
from tests.test_settings import _client, _ADMIN


def test_subscription_accounting_and_redaction(monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_API_KEYS", _ADMIN)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "private-voice-key")
    client = _client(CapabilityRouter(store=SqliteStore(":memory:")))
    auth = {"Authorization": f"Bearer {_ADMIN}"}
    with patch.object(httpx.Client, "get", return_value=httpx.Response(200, json={
        "tier": "creator", "character_count": 1234, "character_limit": 100000,
        "next_character_count_reset_unix": 1800000000,
        "current_overage": {"amount": "2.30", "currency": "usd"},
    })) as get:
        response = client.request("GET", "/api/voice-subscription", headers=auth)
    data = response.json()
    assert data["monthly_cost_usd"] == 19.62
    assert data["credits_remaining"] == 98766
    assert data["current_overage"]["amount"] == "2.30"
    assert "private-voice-key" not in response.text
    assert get.call_args.kwargs["headers"]["xi-api-key"] == "private-voice-key"
    with patch.object(httpx.Client, "get", return_value=httpx.Response(401, text="private-voice-key")):
        response = client.request("GET", "/api/voice-subscription", headers=auth)
    assert response.json()["available"] is False
    assert "private-voice-key" not in response.text
    assert "credits_remaining" not in response.json()
    assert client.get("/api/voice-subscription").status_code == 401


def test_subscription_cost_validation():
    import pytest
    from smart_ai_router import settings
    for value in (-1, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            settings.normalize('elevenlabs_annual_cost_usd', value)


def test_dashboard_shows_unknown_instead_of_zero():
    from tests.test_voice_speech_text import _js_function, _run
    source = _js_function('fetchVoiceSubscription')
    rendered = _run(source + '''
const API = '/api';
const panel = {style: {}};
const details = {style: {}};
const document = {getElementById: id => id === 'voice-subscription' ? panel : details};
const fetch = async () => ({ok: true, json: async () => ({
  monthly_cost_usd: 19.62, annual_cost_usd: 235.40, available: true
})});
fetchVoiceSubscription().then(() => console.log(JSON.stringify(details.textContent)));
''')
    assert '$19.62/month' in rendered
    assert 'Remaining: Unknown' in rendered
    assert 'charges: Unavailable' in rendered
