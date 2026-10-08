"""Shared request identity helpers; no caller-selected owner, tier, policy or balance."""
from __future__ import annotations
import hashlib
import json
import re
from typing import Any
from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from task_usage import entitlements as core
from task_usage.entitlement_config import configured_policy

_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

def request_idempotency_key(request: Request) -> str:
    key = request.headers.get("idempotency-key", "")
    if not _KEY.fullmatch(key):
        raise HTTPException(400, detail={"code":"usage_idempotency_key_required",
            "message":"Provide a stable Idempotency-Key for this logical task."})
    return key

def request_fingerprint(payload: Any) -> str:
    try:
        canonical = json.dumps(jsonable_encoder(payload), sort_keys=True,
            separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, detail={"code":"usage_request_invalid",
                                       "message":"Request cannot be normalized."}) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def service_from_request(request: Request):
    if not core.enabled():
        return None, None
    repository = getattr(request.app.state, "entitlement_repository", None)
    policy = getattr(request.app.state, "entitlement_policy", None)
    if repository is None:
        raise HTTPException(503, detail={"code":"usage_entitlements_unavailable",
                                        "message":"Task entitlement service is unavailable."})
    if policy is None:
        policy = configured_policy()
    if policy is None:
        raise HTTPException(503, detail={"code":"usage_policy_unavailable"})
    return repository, policy

def public_entitlement_error(exc: core.EntitlementError) -> HTTPException:
    status = 429 if isinstance(exc, core.EntitlementLimitExceeded) else (
        409 if isinstance(exc, core.EntitlementConflict) else 503)
    return HTTPException(status, detail={"code":exc.code,
        "message": "Task allowance unavailable." if status == 429 else (
            "This task identity or state conflicts with an existing operation." if status == 409
            else "Task entitlement configuration is unavailable.")},
        headers={"Retry-After":"5"} if status == 429 else None)

def operation_header(operation):
    return {"X-Usage-Operation-ID": str(operation["operation_id"])}


async def entitlement_exception_handler(request: Request, exc: core.EntitlementError):
    """Map native transaction errors to the same bounded public API contract."""
    from fastapi.responses import JSONResponse
    failure = public_entitlement_error(exc)
    headers = dict(failure.headers or {})
    operation_id = getattr(request.state, "usage_operation_id", None)
    state = getattr(request.state, "usage_operation_state", None)
    if operation_id:
        headers["X-Usage-Operation-ID"] = str(operation_id)
    if state:
        headers["X-Usage-State"] = str(state)
    return JSONResponse({"detail":failure.detail},status_code=failure.status_code,headers=headers)
