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


def test_client_disconnect_mid_read_closes_source_cleanly():
    """Regression for the production chat hang.

    Sequence, taken from the observed failure: the client disconnects while the
    upstream provider read is still in flight, Starlette closes the response
    generator, and the source generator is then finalized.

    The previous shield()-based implementation left an orphaned __anext__() task
    holding the source, so finalizing it raised

        RuntimeError: aclose(): asynchronous generator is already running

    reported as "Task exception was never retrieved" (it lands on the event
    loop's exception handler, not the caller). The upstream httpx stream was
    never released and the request wedged — the user's bubble simply stopped.

    Asserts both halves: no loop-level exception, and the source actually ran
    its finally block (connection released).
    """
    errors: list[dict] = []
    released: list[bool] = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, ctx: errors.append(ctx)
        )

        async def src():
            try:
                yield b"first"
                await asyncio.sleep(30)   # provider read still in flight
                yield b"never"            # pragma: no cover
            finally:
                released.append(True)     # stands in for releasing the stream

        source = src()
        gen = _with_heartbeat(source, interval=0.01)
        assert await gen.__anext__() == b"first"
        # Accumulate keepalives so a source read is definitely outstanding.
        for _ in range(3):
            assert await gen.__anext__() == b": keepalive\n\n"
        await gen.aclose()    # client disconnect
        await source.aclose() # later finalization — raised on the old impl
        await asyncio.sleep(0.05)  # give any orphaned task time to report

    asyncio.run(scenario())

    assert released == [True], "source was never closed — upstream stream leaked"
    assert not errors, f"event-loop exception during teardown: {errors}"


def test_source_error_propagates_to_consumer():
    """A provider blowing up mid-stream must surface, not hang or vanish."""
    async def src():
        yield b"partial"
        raise RuntimeError("provider exploded")

    async def scenario():
        gen = _with_heartbeat(src(), interval=5.0)
        assert await gen.__anext__() == b"partial"
        try:
            await gen.__anext__()
        except RuntimeError as exc:
            return str(exc)
        return None

    assert asyncio.run(scenario()) == "provider exploded"
