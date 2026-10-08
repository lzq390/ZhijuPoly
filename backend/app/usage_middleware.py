"""Pure ASGI accounting inside authentication, without inspecting request bodies."""
from __future__ import annotations
from uuid import uuid4
import anyio
from starlette.responses import JSONResponse
from app.auth.context import current_identity, is_service_context
from app.usage_catalog import match_usage_route
from app.usage_context import (
    UsageCollector, UsageContext, UsageMeteringUnavailable, UsageSpan,
    _status_for_exception, usage_scope,
)


class UsageMeteringMiddleware:
    def __init__(self, app, *, collector: UsageCollector):
        self.app, self.collector = app, collector

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.collector.enabled:
            return await self.app(scope, receive, send)
        route = match_usage_route(scope["method"], scope["path"])
        identity = current_identity(required=False)
        if route is None or route.mode == "observe" or identity is None or is_service_context():
            return await self.app(scope, receive, send)
        context = (UsageContext(identity.user_id, route.module, "request:" + str(uuid4()))
                   if route.mode == "sync" else None)
        span = UsageSpan(context, self.collector)
        response_started, response_finished, disconnected = False, False, False
        status_code = 500

        async def metered_send(message):
            nonlocal response_started, response_finished, status_code
            if message["type"] == "http.response.start":
                response_started, status_code = True, message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_finished = True

        async def metered_receive():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
            return message

        terminal = "failed"
        with usage_scope(context, collector=self.collector):
            try:
                # No business execution or request body consumption precedes the
                # first durable event. The blocking repository runs off-loop.
                await anyio.to_thread.run_sync(span.start)
                await self.app(scope, metered_receive, metered_send)
                terminal = ("interrupted" if disconnected and not response_finished else
                            "completed" if response_finished and status_code < 400 else
                            "timed_out" if status_code in {408, 504} else
                            "rejected" if 400 <= status_code < 500 else "failed")
            except UsageMeteringUnavailable:
                terminal = "failed"
                if response_started:
                    raise
                await JSONResponse({"detail": "usage_metering_unavailable"}, status_code=503)(scope, receive, send)
            except BaseException as exc:
                terminal = "interrupted" if isinstance(exc, OSError) else _status_for_exception(exc)
                raise
            finally:
                # Waiting children/retentions defer the root terminal event until
                # the actual producer ends, even when HTTP has disconnected.
                with anyio.CancelScope(shield=True):
                    await anyio.to_thread.run_sync(lambda: span.finish(terminal))
