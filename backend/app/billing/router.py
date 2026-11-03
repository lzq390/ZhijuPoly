from __future__ import annotations

import json
import re
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from app.auth.context import current_owner_id
from .providers import PaymentError
from .repository import BillingError, public_order

router = APIRouter(prefix="/api/v1/billing", tags=["membership"])


async def billing_error_handler(request, exc: BillingError):
    return JSONResponse({"detail": {"code": exc.code, "message": exc.message}}, status_code=exc.status,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def service(request):
    return request.app.state.billing


class OrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["wechat", "alipay"]


@router.get("/plans")
def plans(request: Request):
    return service(request).plans()


@router.get("/subscription")
def subscription(request: Request):
    return jsonable_encoder(service(request).repository.subscription(current_owner_id()))


@router.post("/orders", status_code=201)
def create_order(body: OrderBody, request: Request, idempotency_key: str = Header(alias="Idempotency-Key")):
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", idempotency_key):
        raise BillingError("order_key_required", "请提供有效的购买请求标识。", 400)
    return jsonable_encoder(service(request).create_order(current_owner_id(), body.provider, idempotency_key))


@router.get("/orders")
def orders(request: Request):
    return jsonable_encoder({"items": [public_order(row) for row in service(request).repository.list_orders(current_owner_id())]})


@router.get("/orders/{order_id}")
def order(request: Request, order_id: UUID):
    return jsonable_encoder(service(request).get_order(current_owner_id(), str(order_id)))


@router.post("/orders/{order_id}/simulate-payment")
def simulate_payment(request: Request, order_id: UUID):
    return jsonable_encoder(service(request).simulate_payment(current_owner_id(), str(order_id)))


async def simulation_token_body(request: Request):
    # Parse manually so validation errors never reflect a bearer token. The
    # authentication middleware checks trusted Origin before this public route.
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise BillingError("simulation_json_required", "请使用 JSON 提交模拟支付请求。", 415)
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 1024:
            raise BillingError("simulation_body_too_large", "模拟支付请求过大。", 413)
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
    except (ValueError, UnicodeError):
        raise BillingError("simulation_body_invalid", "模拟支付请求无效。", 400) from None
    if not isinstance(body, dict) or set(body) != {"token"} or not isinstance(body["token"], str):
        raise BillingError("simulation_body_invalid", "模拟支付请求无效。", 400)
    return body["token"]


@router.post("/simulation/checkout")
async def simulation_checkout(request: Request):
    token = await simulation_token_body(request)
    result = await run_in_threadpool(service(request).simulation_checkout, token)
    return JSONResponse(jsonable_encoder(result), headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/simulation/confirm")
async def confirm_simulation_checkout(request: Request):
    token = await simulation_token_body(request)
    result = await run_in_threadpool(service(request).confirm_simulation_checkout, token)
    return JSONResponse(jsonable_encoder(result), headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/webhooks/{provider}")
async def webhook(request: Request, provider: Literal["wechat", "alipay"]):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 65536:
            raise HTTPException(413, "Payment notification is too large")
        chunks.append(chunk)
    try:
        await run_in_threadpool(service(request).notification, provider, dict(request.headers), b"".join(chunks))
    except (PaymentError, BillingError):
        if provider == "alipay":
            return PlainTextResponse("failure", status_code=400)
        return JSONResponse({"code": "FAIL", "message": "Notification verification failed"}, status_code=400)
    return PlainTextResponse("success") if provider == "alipay" else Response(status_code=204)
