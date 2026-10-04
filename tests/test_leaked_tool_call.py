from smart_ai_router.api.proxy import _leaks_tool_call


def test_text_formatted_tool_calls_are_detected():
    assert _leaks_tool_call('<function=Read>{"path":"deploy"}')
    assert _leaks_tool_call('{"name": "Shell", "parameters": {"command": "ls"}}')
    assert _leaks_tool_call("<|python_tag|>foo()")


def test_ordinary_prose_and_json_are_not():
    assert not _leaks_tool_call("")
    assert not _leaks_tool_call("Use the Read tool to list the directory.")
    assert not _leaks_tool_call('{"name": "Ada", "age": 36}')
