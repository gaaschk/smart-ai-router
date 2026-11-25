"""Safe lifecycle diagnostics for model requests, including streamed replies."""

import asyncio
from contextlib import asynccontextmanager, suppress
import json
import re
import sys
import time
from uuid import uuid4


_HEADER = b"x-request-id"
_ID = re.compile(rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")


class RequestTrace:
    def __init__(self, request_id):
        self.request_id = request_id
        self.started = time.monotonic()
        self.attempts = 0
        self.disconnect_seen = False

    def log(self, event, **fields):
        record = {
            "event": event, "request_id": self.request_id, "timestamp": time.time(),
            "elapsed_ms": round((time.monotonic() - self.started) * 1000),
            **fields,
        }
        print("[request_trace] " + json.dumps(record, separators=(",", ":")), file=sys.stderr, flush=True)


def trace_event(request, event, **fields):
    trace = getattr(request.state, "request_trace", None)
    if trace is not None:
        trace.log(event, **fields)


class TracedUpstream:
    """Wrap the existing client without changing retries, timeouts or streaming."""

    def __init__(self, client, trace, model):
        self.client = client
        self.trace = trace
        self.model = model

    @asynccontextmanager
    async def attempt(self):
        self.trace.attempts += 1
        fields = {"attempt": self.trace.attempts, "model": self.model}
        started = time.monotonic()
        self.trace.log("upstream_start", **fields)
        result = {"outcome": "completed"}

        def received(response, event):
            result["status"] = response.status_code
            if response.status_code >= 400:
                result["outcome"] = "upstream_http_error"
            self.trace.log(event, **fields, status=response.status_code,
                           attempt_ms=round((time.monotonic() - started) * 1000))

        try:
            yield received
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                outcome = "cancelled"
            elif isinstance(exc, GeneratorExit):
                outcome = "stream_closed"
            else:
                outcome = "failed"
            result.update(outcome=outcome, error_kind=type(exc).__name__)
            raise
        finally:
            self.trace.log("upstream_end", **fields, **result,
                           attempt_ms=round((time.monotonic() - started) * 1000),
                           client_disconnected=self.trace.disconnect_seen)

    async def post(self, *args, **kwargs):
        async with self.attempt() as received:
            response = await self.client.post(*args, **kwargs)
            received(response, "upstream_response")
            return response

    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        async with self.attempt() as received:
            async with self.client.stream(*args, **kwargs) as response:
                received(response, "upstream_headers")
                yield response


class RequestTraceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope["method"] != "POST"
                or scope["path"] not in {"/v1/chat/completions", "/v1/responses"}):
            await self.app(scope, receive, send)
            return

        offered = [value for key, value in scope.get("headers", []) if key == _HEADER]
        request_id = (offered[0].decode("ascii") if len(offered) == 1 and _ID.fullmatch(offered[0])
                      else str(uuid4()))
        trace = RequestTrace(request_id)
        scope.setdefault("state", {})["request_trace"] = trace
        trace.log("request_received", path=scope["path"])

        status = None
        finished = False
        first_body = False
        watcher = None
        disconnect = asyncio.Event()

        def mark_disconnect():
            if not finished and not trace.disconnect_seen:
                trace.disconnect_seen = True
                trace.log("client_disconnect")
            disconnect.set()

        async def watch_disconnect():
            # Only takes ownership of receive after the complete request body.
            message = await receive()
            if message["type"] == "http.disconnect":
                mark_disconnect()

        async def traced_receive():
            nonlocal watcher
            if watcher is not None:
                await disconnect.wait()
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.disconnect":
                mark_disconnect()
            elif message["type"] == "http.request" and not message.get("more_body", False):
                watcher = asyncio.create_task(watch_disconnect())
            return message

        async def traced_send(message):
            nonlocal status, finished, first_body
            event_type = message["type"]
            if event_type == "http.response.start":
                status = message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != _HEADER]
                message = {**message, "headers": headers + [(_HEADER, request_id.encode("ascii"))]}
                trace.log("response_headers", status=status)
            try:
                await send(message)
            except OSError:
                mark_disconnect()
                raise
            if event_type == "http.response.body":
                if message.get("body") and not first_body:
                    first_body = True
                    trace.log("first_response_bytes")
                if not message.get("more_body", False):
                    finished = True

        outcome = "completed"
        try:
            await self.app(scope, traced_receive, traced_send)
        except BaseException as exc:
            outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            trace.log("request_error", error_kind=type(exc).__name__)
            raise
        finally:
            if watcher is not None:
                watcher.cancel()
                with suppress(asyncio.CancelledError):
                    await watcher
            if outcome == "completed":
                if trace.disconnect_seen:
                    outcome = "client_disconnected"
                elif not finished:
                    outcome = "incomplete"
                elif status is not None and status >= 400:
                    outcome = "http_error"
            trace.log("request_end", status=status, outcome=outcome,
                      client_disconnected=trace.disconnect_seen, attempts=trace.attempts)
