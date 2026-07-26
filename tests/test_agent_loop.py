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


# ── streaming path ───────────────────────────────────────────────────────────

def _deltas(*deltas):
    """A stream_model that yields the given delta dicts, once."""
    async def stream_model(body):
        for d in deltas:
            yield d
    return stream_model


def test_stream_plain_answer_passes_tokens_through():
    # A model that just streams text — no tools — should stream verbatim.
    stream_model = _deltas(
        {"content": "Hello"}, {"content": ", "}, {"content": "world."},
    )
    gen = agent_loop.run_agent_loop(
        user="alice",
        body={"messages": [{"role": "user", "content": "hi"}], "model": "x"},
        tool_schemas=tools.tool_schemas(allow_bash=False),
        stream_model=stream_model,
    )
    frames, raw = asyncio.run(_collect(gen))
    text = "".join(f["choices"][0]["delta"].get("content", "") for f in frames if f.get("choices"))
    assert "Hello, world." in text
    assert b"data: [DONE]" in raw


def test_stream_reassembles_fragmented_tool_call_then_answers():
    # Round 1: tool call split across fragments (id+name first, args in pieces).
    # Round 2: plain streamed answer.
    round1 = _deltas(
        {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                          "function": {"name": "write_file", "arguments": ""}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": '{"path": "a.txt"'}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": ', "content": "hi"}'}}]},
    )
    round2 = _deltas({"content": "Wrote it."})
    rounds = [round1, round2]
    calls = {"n": 0}

    async def stream_model(body):
        gen = rounds[calls["n"]](body)
        calls["n"] += 1
        async for d in gen:
            yield d

    gen = agent_loop.run_agent_loop(
        user="alice",
        body={"messages": [{"role": "user", "content": "make a.txt"}], "model": "x"},
        tool_schemas=tools.tool_schemas(allow_bash=False),
        stream_model=stream_model,
    )
    frames, raw = asyncio.run(_collect(gen))

    # The reassembled tool call actually executed against the workspace.
    assert tools.execute_tool("alice", "read_file", {"path": "a.txt"}) == "hi"
    text = "".join(f["choices"][0]["delta"].get("content", "") for f in frames if f.get("choices"))
    assert "Wrote it." in text
    assert calls["n"] == 2


def test_stream_multiple_tool_calls_in_one_round():
    # Two tool calls in a single assistant turn, interleaved by index.
    round1 = _deltas(
        {"tool_calls": [{"index": 0, "id": "c0", "function": {"name": "write_file", "arguments": '{"path":"a.txt",'}}]},
        {"tool_calls": [{"index": 1, "id": "c1", "function": {"name": "write_file", "arguments": '{"path":"b.txt",'}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": '"content":"A"}'}}]},
        {"tool_calls": [{"index": 1, "function": {"arguments": '"content":"B"}'}}]},
    )
    round2 = _deltas({"content": "Both done."})
    rounds = [round1, round2]
    calls = {"n": 0}

    async def stream_model(body):
        gen = rounds[calls["n"]](body)
        calls["n"] += 1
        async for d in gen:
            yield d

    gen = agent_loop.run_agent_loop(
        user="alice",
        body={"messages": [{"role": "user", "content": "make two"}], "model": "x"},
        tool_schemas=tools.tool_schemas(allow_bash=False),
        stream_model=stream_model,
    )
    asyncio.run(_collect(gen))
    assert tools.execute_tool("alice", "read_file", {"path": "a.txt"}) == "A"
    assert tools.execute_tool("alice", "read_file", {"path": "b.txt"}) == "B"


def test_stream_provider_error_surfaced():
    async def stream_model(body):
        raise RuntimeError("stream-boom")
        yield  # pragma: no cover — make it an async generator

    gen = agent_loop.run_agent_loop(
        user="alice",
        body={"messages": [], "model": "x"},
        tool_schemas=[],
        stream_model=stream_model,
    )
    frames, raw = asyncio.run(_collect(gen))
    assert any("stream-boom" in json.dumps(f) for f in frames)
