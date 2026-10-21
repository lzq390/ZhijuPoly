"""Owner access tiers, request observations, and a bounded operational rate guard."""
from __future__ import annotations
from collections import deque
from datetime import datetime, timezone
from functools import partial
import logging
from threading import Lock
from time import monotonic
from uuid import uuid4

import anyio
from starlette.responses import JSONResponse

from app.auth.context import current_identity, is_service_context
from app.auth.policy import route_policy
from app.usage_catalog import match_usage_route
from task_usage import entitlements as core

logger = logging.getLogger("nexpoly.entitlements")

class RequestRateGuard:
    """Per-process safety guard, not the durable allowance authority."""
    def __init__(self):
        self._lock, self._hits = Lock(), {}
    def accept(self, owner, limit, now=None):
        now = monotonic() if now is None else now
        with self._lock:
            if len(self._hits) >= 10000:
                self._hits = {key: value for key,value in self._hits.items() if value and value[-1] > now-60}
            if owner not in self._hits and len(self._hits) >= 10000:
                return False
            hits = self._hits.setdefault(owner, deque())
            while hits and hits[0] <= now-60:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True

class EntitlementAccessMiddleware:
    def __init__(self, app, *, repository, policy=None):
        self.app, self.repository, self.policy = app, repository, policy
        self.rate = RequestRateGuard()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or is_service_context():
            return await self.app(scope, receive, send)
        identity = current_identity(required=False)
        if identity is None:
            return await self.app(scope, receive, send)
        path, method = scope["path"], scope["method"]
        state = scope.setdefault("state", {})
        route = match_usage_route(method, path)
        observed = core.enabled() and route is not None and route.module is not None and route.mode in {"sync","native"}
        request_id = str(uuid4())
        status, response_finished, received = 500, False, False
        operation_id = None
        entered_business = False

        async def call(name, **kwargs):
            return await anyio.to_thread.run_sync(partial(getattr(self.repository, name), **kwargs))

        async def reject(code, message, http_status):
            return await JSONResponse({"detail":{"code":code,"message":message}},status_code=http_status,
                                      headers={"Cache-Control":"private, no-store"})(scope,receive,tracked_send)

        async def tracked_send(message):
            nonlocal status, response_finished, operation_id
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                for key,value in headers:
                    if key.lower() == b"x-usage-operation-id":
                        operation_id = value.decode("ascii")
                present = {key.lower() for key,value in headers}
                if state.get("usage_operation_id") and b"x-usage-operation-id" not in present:
                    operation_id = str(state["usage_operation_id"])
                    headers.append((b"x-usage-operation-id",operation_id.encode("ascii")))
                if state.get("usage_operation_state") and b"x-usage-state" not in present:
                    headers.append((b"x-usage-state",str(state["usage_operation_state"]).encode("ascii")))
                message = {**message,"headers":headers}
                if observed:
                    headers.append((b"x-usage-request-id",request_id.encode("ascii")))
                    message = {**message,"headers":headers}
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body",False):
                response_finished = True

        try:
            if observed:
                await call("record_request", owner_user_id=identity.user_id, module=route.module,
                           request_id=request_id, phase="received", occurred_at=datetime.now(timezone.utc))
                received = True
            # This method deliberately works even while success limits are off:
            # switching collection off must not upgrade a free account.
            tier = await call("resolve_access_tier", owner_user_id=identity.user_id)
            state["entitlement_tier"] = tier
            private = route_policy(path,method) == "member"
            account_route = path.startswith(("/api/v1/auth/", "/api/v1/usage/"))
            if tier == "free" and private and not account_route:
                return await reject("membership_required","This feature requires a member account.",403)
            if observed and self.policy is not None:
                if not self.rate.accept(identity.user_id,self.policy["request_rate_per_minute"]):
                    return await reject("usage_request_rate_limited","Too many task requests; wait before retrying.",429)
            entered_business = True
            return await self.app(scope,receive,tracked_send)
        except Exception:
            if not entered_business:
                logger.error("entitlement_request_admission_unavailable")
                return await reject("usage_entitlements_unavailable","Task admission is unavailable.",503)
            raise
        finally:
            if received:
                values = dict(owner_user_id=identity.user_id,module=route.module,request_id=request_id,
                    phase="finished",occurred_at=datetime.now(timezone.utc),http_status=status,
                    outcome="response_finished" if response_finished else "delivery_interrupted",
                    operation_id=operation_id or state.get("usage_operation_id"))
                with anyio.CancelScope(shield=True):
                    try:
                        await call("record_request",**values)
                    except Exception:
                        scope["app"].state.entitlement_observation_degraded = True
                        logger.error("entitlement_request_terminal_observation_unavailable")
