"""Scheme checks happen before invoking any network or local-file handler."""

import urllib.request

import pytest

from smart_ai_router.sync import _open_provider_http


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.invalid/catalog",
        "data:text/plain,private",
        "http:///missing-host",
    ],
)
def test_unsupported_provider_scheme_never_opens(monkeypatch, url):
    def forbidden(*a, **kw):
        pytest.fail("URL handler must not be invoked")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    with pytest.raises(ValueError, match="http or https"):
        _open_provider_http(urllib.request.Request(url), timeout=1)


@pytest.mark.parametrize(
    "url", ["http://localhost:11434/api/tags", "https://openrouter.ai/api/v1/models"]
)
def test_http_provider_request_preserves_timeout_and_identity(monkeypatch, url):
    request = urllib.request.Request(url)
    marker = object()

    def fake(req, *, timeout):
        assert req is request
        assert timeout == 3
        return marker

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert _open_provider_http(request, timeout=3) is marker
