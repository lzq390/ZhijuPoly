from __future__ import annotations

from threading import Lock

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.postgres_database import postgres_connection
from app.services.deployment_control import InflightApiWriteTracker, get_drain_state


WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PUBLIC_API_PREFIX = "/api/"
DRAIN_RETRY_AFTER_SECONDS = 60
DRAIN_DETAIL = "service is temporarily read-only while a deployment is in progress"
_TRACKER_CREATION_LOCK = Lock()


def _inflight_tracker_for_app(app) -> InflightApiWriteTracker:
    tracker = getattr(app.state, "inflight_api_writes", None)
    if tracker is not None:
        return tracker
    with _TRACKER_CREATION_LOCK:
        tracker = getattr(app.state, "inflight_api_writes", None)
        if tracker is None:
            tracker = InflightApiWriteTracker()
            app.state.inflight_api_writes = tracker
    return tracker


def _read_drain_state(connection_factory, dsn: str):
    """Read the persistent admission flag without blocking the event loop."""
    with connection_factory(dsn) as connection:
        return get_drain_state(connection)


class DeploymentDrainMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] not in WRITE_METHODS
            or not scope["path"].startswith(PUBLIC_API_PREFIX)
        ):
            await self.app(scope, receive, send)
            return

        # Register admission before reading the persistent drain flag. A release
        # controller can therefore never observe active_total=0 while this
        # request is between drain admission and its GPU/job-manager helper.
        request_app = scope["app"]
        tracker = _inflight_tracker_for_app(request_app)
        tracker.enter()
        tracking = True

        def release_tracking() -> None:
            nonlocal tracking
            if not tracking:
                return
            tracking = False
            tracker.exit()

        async def send_and_track_completion(message: Message) -> None:
            await send(message)
            if (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                release_tracking()

        try:
            settings = request_app.state.settings
            if not getattr(settings, "deployment_drain_enabled", False):
                await self.app(scope, receive, send_and_track_completion)
                return
            connection_factory = getattr(
                request_app.state,
                "deployment_control_connection_factory",
                postgres_connection,
            )
            try:
                drain = await run_in_threadpool(
                    _read_drain_state,
                    connection_factory,
                    settings.app_postgres_dsn,
                )
            except Exception:
                # A write must not enter while the deployment safety state cannot be read.
                response = JSONResponse(
                    status_code=503,
                    content={"detail": "deployment safety state is unavailable"},
                    headers={"Retry-After": str(DRAIN_RETRY_AFTER_SECONDS)},
                )
                await response(scope, receive, send_and_track_completion)
                return

            if not drain.enabled:
                await self.app(scope, receive, send_and_track_completion)
                return
            response = JSONResponse(
                status_code=503,
                content={"detail": DRAIN_DETAIL, "reason": drain.reason},
                headers={"Retry-After": str(DRAIN_RETRY_AFTER_SECONDS)},
            )
            await response(scope, receive, send_and_track_completion)
        finally:
            # The final body message is the normal release point. This fallback
            # covers application errors, client disconnects, and malformed ASGI
            # applications that return without completing a response body.
            release_tracking()
