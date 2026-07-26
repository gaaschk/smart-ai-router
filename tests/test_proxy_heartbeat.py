"""Tests for the SSE keepalive wrapper (_with_heartbeat) in the proxy.

A slow model round yields nothing for seconds; without a heartbeat the client's
chat bubble looks frozen. _with_heartbeat injects an SSE comment during silence
and must pass real chunks through unchanged, in order, and terminate cleanly.
"""
import asyncio

from smart_ai_router.api.proxy import _with_heartbeat


async def _drain(gen):
    out = []
    async for chunk in gen:
        out.append(chunk)
    return out


def test_passes_chunks_through_without_heartbeat_when_fast():
    async def src():
        yield b"a"
        yield b"b"
        yield b"c"

    # Large interval → no silence long enough to fire a keepalive.
    out = asyncio.run(_drain(_with_heartbeat(src(), interval=100.0)))
    assert out == [b"a", b"b", b"c"]


def test_injects_keepalive_during_silence():
    async def src():
        await asyncio.sleep(0.05)
        yield b"first"
        await asyncio.sleep(0.05)
        yield b"second"

    # Tiny interval → each sleep outlasts it, so keepalives appear between the
    # real chunks, and the real chunks still arrive in order.
    out = asyncio.run(_drain(_with_heartbeat(src(), interval=0.01)))
    assert b": keepalive\n\n" in out
    real = [c for c in out if c != b": keepalive\n\n"]
    assert real == [b"first", b"second"]


def test_empty_source_terminates_cleanly():
    async def src():
        return
        yield  # pragma: no cover — makes this an async generator

    out = asyncio.run(_drain(_with_heartbeat(src(), interval=0.01)))
    assert out == []
