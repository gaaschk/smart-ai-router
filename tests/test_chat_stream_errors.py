"""Exercise the shipped SSE reader with provider errors, including agent errors."""
import json

import pytest

from tests.test_voice_speech_text import _js_function, _run, pytestmark  # noqa: F401


@pytest.mark.parametrize("error, expected", [
    ({"message": "agent loop provider error: provider 429", "type": "upstream_error"},
     "agent loop provider error: provider 429"),
    ("connection failed", "connection failed"),
    ({"code": 503}, '{"code":503}'),
])
def test_stream_displays_error_detail(error, expected):
    frame = "data: " + json.dumps({"error": error}) + "\n\ndata: [DONE]\n\n"
    harness = _js_function("streamChatInto") + "\n" + f"""
const document = {{getElementById: () => ({{}})}};
const bubble = {{textContent: ''}};
let read = false;
const res = {{body: {{getReader: () => ({{read: async () => {{
  if (read) return {{done: true}};
  read = true;
  return {{done: false, value: new TextEncoder().encode({json.dumps(frame)})}};
}}}})}}}};
streamChatInto(res, bubble).then(result => console.log(JSON.stringify([result, bubble.textContent])));
"""
    result, displayed = _run(harness)
    assert displayed == f"\n[error: {expected}]"
    assert result["streamError"] is True
    assert result["text"] == displayed
