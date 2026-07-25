"""Agent tool-calling loop: executes tools and terminates on a plain answer."""
import asyncio
import json

import pytest

from smart_ai_router import agent_loop, tools


@pytest.fixture(autouse=True)
def _root(tmp_path, monkeypatch):
    monkeypatch.setenv("SMART_ROUTER_WORKSPACE_DIR", str(tmp_path / "ws"))


async def _collect(gen):
    """Drain an async generator of SSE bytes into a list of parsed frames + raw."""
    frames, raw = [], []
    async for chunk in gen:
        raw.append(chunk)
        text = chunk.decode()
        for line in text.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    frames.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
    return frames, b"".join(raw)


def _tool_call(name, args, cid="call_1"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": cid,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
    }


def _msg(content):
    return {"role": "assistant", "content": content}


def test_loop_executes_tool_then_answers(tmp_path):
    # Model turn 1: call write_file. Turn 2: answer plainly.
    scripted = [
        {"choices": [{"message": _tool_call("write_file", {"path": "a.txt", "content": "hi"})}]},
        {"choices": [{"message": _msg("Done, I wrote the file.")}]},
    ]
    calls = {"n": 0}

    async def call_model(body):
        r = scripted[calls["n"]]
        calls["n"] += 1
        return r

    gen = agent_loop.run_agent_loop(
        user="alice",
        body={"messages": [{"role": "user", "content": "make a.txt"}], "model": "x"},
        tool_schemas=tools.tool_schemas(allow_bash=False),
        call_model=call_model,
    )
    frames, raw = asyncio.run(_collect(gen))

    # The file was actually written to alice's workspace.
    assert tools.execute_tool("alice", "read_file", {"path": "a.txt"}) == "hi"
    # The final answer streamed through.
    text = "".join(f["choices"][0]["delta"].get("content", "") for f in frames if f.get("choices"))
    assert "Done, I wrote the file." in text
    assert b"data: [DONE]" in raw
    assert calls["n"] == 2


def test_loop_stops_at_max_rounds():
    # Model always calls a tool → loop must cap and terminate.
    async def call_model(body):
        return {"choices": [{"message": _tool_call("list_dir", {"path": ""})}]}

    gen = agent_loop.run_agent_loop(
        user="alice",
        body={"messages": [{"role": "user", "content": "loop"}], "model": "x"},
        tool_schemas=tools.tool_schemas(allow_bash=False),
        call_model=call_model,
    )
    frames, raw = asyncio.run(_collect(gen))
    text = "".join(f["choices"][0]["delta"].get("content", "") for f in frames if f.get("choices"))
    assert "maximum number of tool-call rounds" in text
    assert b"data: [DONE]" in raw


def test_loop_surfaces_provider_error():
    async def call_model(body):
        raise RuntimeError("boom")

    gen = agent_loop.run_agent_loop(
        user="alice",
        body={"messages": [], "model": "x"},
        tool_schemas=[],
        call_model=call_model,
    )
    frames, raw = asyncio.run(_collect(gen))
    assert any("boom" in json.dumps(f) for f in frames)
