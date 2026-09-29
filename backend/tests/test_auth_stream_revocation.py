"""ASGI delivery races, with no database, provider, or wall-clock polling."""
from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest

from app.auth.context import Identity
from app.auth.middleware import AuthenticationMiddleware
import app.auth.middleware as middleware_module


@pytest.mark.parametrize("revocation", ["revoked", "unavailable", "replaced"])
def test_revocation_serializes_terminal_frame_after_pending_send(monkeypatch, revocation):
    async def scenario():
        check_session = anyio.Event()
        revocation_observed = anyio.Event()
        first_body_pending = anyio.Event()
        release_first_body = anyio.Event()
        request_done = anyio.Event()
        producer_closed = anyio.Event()
        session = {
            "session_id": "session-a",
            "user_id": "11111111-1111-1111-1111-111111111111",
            "must_change_password": False,
        }
        frames = []
        sending = False

        class Auth:
            settings = SimpleNamespace(cookie_name="sid", allowed_origins=())
            resolutions = 0

            def resolve(self, token):
                assert token == "test-token"
                self.resolutions += 1
                if self.resolutions == 1:
                    return session
                if revocation == "unavailable":
                    raise RuntimeError("authentication unavailable")
                if revocation == "replaced":
                    return {**session, "session_id": "session-b"}
                return None

            def identity(self, value):
                return Identity(value["user_id"], value["session_id"])

            def assert_application_ready(self):
                pass

        async def polling_interval(delay):
            assert delay == 5
            await check_session.wait()

        async def run_sync(function, *args, **kwargs):
            try:
                return await anyio.to_thread.run_sync(function, *args, **kwargs)
            finally:
                if getattr(function, "__name__", "") == "resolve" and auth.resolutions > 1:
                    revocation_observed.set()

        # Change only the middleware's clock, leaving scheduling and locks real.
        class ControlledAnyio:
            sleep = staticmethod(polling_interval)
            to_thread = SimpleNamespace(run_sync=run_sync)

            def __getattr__(self, name):
                return getattr(anyio, name)

        monkeypatch.setattr(middleware_module, "anyio", ControlledAnyio())

        async def send(message):
            nonlocal sending
            assert not sending, "revocation sent a terminal frame during an unfinished data send"
            sending = True
            try:
                if message.get("body") == b"data: first\n\n":
                    first_body_pending.set()
                    await release_first_body.wait()
                frames.append(message.copy())
            finally:
                sending = False

        async def receive():
            await anyio.sleep_forever()

        async def app(scope, receive, send):
            try:
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"text/event-stream")]})
                await send({"type": "http.response.body", "body": b"data: first\n\n", "more_body": True})
                # A producer may already have more data when revocation is seen.
                await send({"type": "http.response.body", "body": b"data: stale\n\n", "more_body": True})
                await anyio.sleep_forever()
            finally:
                producer_closed.set()

        auth = Auth()
        middleware = AuthenticationMiddleware(app, auth)
        scope = {
            "type": "http", "method": "GET", "path": "/api/v1/private-stream",
            "headers": [(b"cookie", b"sid=test-token"), (b"x-session-context", b"session-a")],
        }

        async def request():
            try:
                await middleware(scope, receive, send)
            finally:
                request_done.set()

        with anyio.fail_after(3):
            async with anyio.create_task_group() as group:
                group.start_soon(request)
                await first_body_pending.wait()
                check_session.set()
                await revocation_observed.wait()
                # The watcher has returned from resolve and reached the send lock.
                assert len(frames) == 1
                assert not request_done.is_set()
                release_first_body.set()
                await request_done.wait()

        assert auth.resolutions == 2
        assert producer_closed.is_set()
        assert [message.get("body") for message in frames] == [None, b"data: first\n\n", b""]
        assert frames[-1]["more_body"] is False
        assert (b"cache-control", b"private, no-store") in frames[0]["headers"]

    anyio.run(scenario)
