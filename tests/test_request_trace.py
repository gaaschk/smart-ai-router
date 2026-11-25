import asyncio
import json

from smart_ai_router.api.request_trace import RequestTraceMiddleware, TracedUpstream


def test_disconnect_seen_while_upstream_pending(capsys):
    async def run():
        receive_queue = asyncio.Queue()
        pending = asyncio.Event()

        class Upstream:
            async def post(self):
                pending.set()
                await asyncio.Future()

        async def app(scope, receive, send):
            assert (await receive())["type"] == "http.request"
            trace = scope["state"]["request_trace"]
            task = asyncio.create_task(TracedUpstream(Upstream(), trace, "fixture").post())
            await pending.wait()
            assert (await receive())["type"] == "http.disconnect"
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        async def send(msg):
            raise AssertionError(msg)

        middleware = RequestTraceMiddleware(app)
        scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}
        await receive_queue.put({"type": "http.request", "body": b"private"})
        await receive_queue.put({"type": "http.disconnect"})
        await asyncio.wait_for(middleware(scope, receive_queue.get, send), timeout=2)

    asyncio.run(run())
    events = [json.loads(line.split(" ", 1)[1]) for line in capsys.readouterr().err.splitlines()]
    assert any(e["event"] == "client_disconnect" for e in events)
    end = next(e for e in events if e["event"] == "upstream_end")
    assert end["outcome"] == "cancelled" and end["client_disconnected"]
    assert events[-1]["outcome"] == "client_disconnected"
