from __future__ import annotations

import os
import re
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.services.dev_gpu_operator import (
    DevGpuOperatorClient,
    DevGpuOperatorError,
)


router = APIRouter(prefix="/api/v1/dev-gpu-session", tags=["dev-gpu-session"])


class DevGpuSessionStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    operator_available: bool
    phase: Literal[
        "stopped",
        "recovering",
        "queued",
        "starting",
        "ready",
        "failed",
        "unavailable",
    ]
    controller_status: str = Field(max_length=64)
    can_recover: bool
    operation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    message: str = Field(min_length=1, max_length=512)
    source_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    source_tree: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    updated_at: str | None = Field(default=None, max_length=64)


def _unavailable(message: str) -> DevGpuSessionStatusResponse:
    source_sha = os.getenv("BUILD_REVISION", "")
    source_tree = os.getenv("BUILD_SOURCE_TREE", "")
    return DevGpuSessionStatusResponse(
        operator_available=False,
        phase="unavailable",
        controller_status="unavailable",
        can_recover=False,
        message=message,
        source_sha=(
            source_sha if re.fullmatch(r"[0-9a-f]{40}", source_sha) else None
        ),
        source_tree=(
            source_tree if re.fullmatch(r"[0-9a-f]{40}", source_tree) else None
        ),
    )


def _operator_client(request: Request) -> DevGpuOperatorClient:
    client = getattr(request.app.state, "dev_gpu_operator_client", None)
    if client is None or not callable(getattr(client, "request", None)):
        raise HTTPException(status_code=404, detail="GPU operator is not enabled")
    return cast(DevGpuOperatorClient, client)


@router.get("/status", response_model=DevGpuSessionStatusResponse)
async def dev_gpu_session_status(request: Request) -> DevGpuSessionStatusResponse:
    client = _operator_client(request)
    try:
        value = await run_in_threadpool(client.request, "status")
    except DevGpuOperatorError:
        return _unavailable("GPU operator 当前不可用")
    return DevGpuSessionStatusResponse.model_validate(value)


@router.post(
    "/recover",
    response_model=DevGpuSessionStatusResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def recover_dev_gpu_session(
    request: Request,
) -> DevGpuSessionStatusResponse:
    client = _operator_client(request)
    try:
        value = await run_in_threadpool(client.request, "recover")
    except DevGpuOperatorError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return DevGpuSessionStatusResponse.model_validate(value)
