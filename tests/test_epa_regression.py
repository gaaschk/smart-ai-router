"""Source grounding and unfenced HTML regressions from the EPA conversation."""
import json

import pytest

from smart_ai_router.taxonomy import DomainNeed, PromptProfile
from tests.test_current_info import client, _chat, _UI  # noqa: F401
from tests.test_voice_speech_text import (
    _UI as UI_PATH, _js_function, _run, pytestmark as node_required,
)


@pytest.mark.parametrize("prompt", [
    "How does the EPA Method 1040 for oxidizers differ from Method 9045D?",
    "What about EPA SW-847 method 1040?",
    "What does EPA-846 method 1040 specifically test for?",
])
def test_epa_factual_precision_is_searched(client, monkeypatch, prompt):
    # Replay the actual LLM profiles' shared demand, without making a paid classifier call.
    async def classify(*args):
        return PromptProfile(
            domains=(DomainNeed("natural_science", "practitioner"),),
            demands=frozenset({"factual_precision"}),
        ), "llm"

    monkeypatch.setattr("smart_ai_router.api.proxy.classify_profile_two_speed", classify)
    response = _chat(client, prompt, headers=_UI)
    assert response.status_code == 200
    assert client.sent[-1]["plugins"] == [{"id": "web", "max_results": 5}]
    assert response.headers["X-Web-Search"] == "true"


def _render(src):
    functions = "\n".join(_js_function(name) for name in (
        "escapeHtml", "artifactDocument", "renderArtifact", "renderMarkdown",
        "parseList", "renderInline",
    ))
    ui = UI_PATH.read_text()
    constants = ui[ui.index("const ARTIFACT_LANGS"):ui.index("function artifactDocument")]
    return _run(
        constants + "\nlet _artifactInline = false;\n"
        + functions + "\nconsole.log(JSON.stringify([renderMarkdown("
        + json.dumps(src) + ")]));"
    )[0]


@node_required
def test_unfenced_html_document_is_a_sandboxed_preview():
    result = _render('Before\n<html>\n<head><style>td{padding:6px}</style></head>\n'
                     '<body><table><tr><td>Oxidizing solids</td></tr></table></body>\n'
                     '</html>\nAfter')
    assert '<iframe' in result
    assert 'sandbox="allow-scripts"' in result
    assert '&lt;table&gt;' in result
    assert '<table>' not in result  # markup stays inside the iframe's escaped srcdoc
    assert '<p>Before</p>' in result
    assert '<p>After</p>' in result


@node_required
@pytest.mark.parametrize('src', [
    '<html>\n<body>unfinished',
    '```text\n<html><body>example</body></html>\n```',
    'A literal <script>alert(1)</script> tag',
])
def test_incomplete_documents_and_literal_markup_stay_escaped(src):
    result = _render(src)
    assert '<iframe' not in result
    assert '<script>' not in result
    assert '&lt;' in result


@node_required
def test_doctype_and_fenced_html_keep_using_artifacts():
    for src in ('<!DOCTYPE html>\n<html><body>Table</body></html>',
                '```html\n<html><body>Table</body></html>\n```'):
        assert '<iframe' in _render(src)


@node_required
def test_unfenced_scripts_stay_inside_opaque_sandbox():
    result = _render('<html><body><script>parent.hacked=true</script>'
                     '<img src="https://example.com/pixel"></body></html>')
    assert 'sandbox="allow-scripts"' in result
    assert 'allow-same-origin' not in result
    assert 'Content-Security-Policy' in result
    assert '<script>' not in result
    assert '<img ' not in result
    assert '&lt;script&gt;' in result
