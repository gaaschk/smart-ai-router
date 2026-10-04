import json

from smart_ai_router.api.proxy import _sse_error


def test_stream_error_is_openai_shaped_and_terminated():
    first, done = _sse_error("boom", 429).decode().split("\n\n")[:2]
    err = json.loads(first.removeprefix("data: "))["error"]
    assert err["message"] == "boom" and err["code"] == 429
    assert done == "data: [DONE]"
