from __future__ import annotations

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
    return JSONResponse({"detail": {"code": exc.code, "message": exc.message}}, status_code=exc.status)


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
