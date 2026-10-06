import asyncio
import json

import httpx
import pytest

from smart_ai_router.jev_classifier import URL, classify_jev


def install(monkeypatch, *, bad=None, status=200):
    calls = []

    async def post(self, url, **kwargs):
        payload = kwargs["json"]
        calls.append(payload)
        assert url == URL and "messages" not in payload
        assert "jev-router" not in payload["model"]
        if len(calls) == 1:
            answers = {k: {"type": "choice", "choice": v} for k, v in {
                "primary": "software_engineering", "secondary": "law_regulatory",
                "tertiary": "none"}.items()}
        else:
            answers = {"depth_software_engineering": {"type": "choice", "choice": "specialist"},
                       "depth_law_regulatory": {"type": "choice", "choice": "specialist"},
                       "stakes": {"type": "choice", "choice": "high"}}
            for key in payload["questions"]:
                if key.startswith("demand_"):
                    answers[key] = {"type": "noul", "noul": 0.9 if key == "demand_agentic" else 0.1}
            if bad:
                answers["demand_agentic"]["noul"] = bad
        return httpx.Response(status, content=json.dumps({"answers": answers,
            "usage": {"input_tokens": 300, "output_tokens": 50, "cost": 0.0001}}),
            request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    return calls


def test_preserves_multiple_domains_depth_demands_and_usage(monkeypatch):
    calls = install(monkeypatch)
    result = asyncio.run(classify_jev("Implement a regulatory compliance checker", api_key="test"))
    assert not result.error
    assert [(d.field, d.depth) for d in result.profile.domains] == [
        ("software_engineering", "specialist"), ("law_regulatory", "specialist")]
    assert result.profile.demands == {"agentic"}
    assert result.profile.stakes == "high"
    assert len(result.usage) == 2
    assert "depth_law_regulatory" in calls[1]["questions"]
    assert "test" not in json.dumps(result.__dict__, default=str)


@pytest.mark.parametrize("bad", ["0.9", 2, float("nan")])
def test_invalid_probability_does_not_produce_profile(monkeypatch, bad):
    install(monkeypatch, bad=bad)
    result = asyncio.run(classify_jev("prompt", api_key="test"))
    assert result.profile is None and result.error
    assert len(result.usage) == 2  # billed responses survive parsing failures


def test_http_error_stops_after_first_call(monkeypatch):
    calls = install(monkeypatch, status=403)
    result = asyncio.run(classify_jev("prompt", api_key="test"))
    assert result.profile is None and result.error == "HTTP 403"
    assert len(calls) == 1


def test_missing_key_does_not_call_provider(monkeypatch):
    calls = install(monkeypatch)
    assert asyncio.run(classify_jev("prompt", api_key="")).profile is None
    assert not calls
