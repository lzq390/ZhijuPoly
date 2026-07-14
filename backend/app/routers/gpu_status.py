from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request


router = APIRouter(prefix="/internal/gpu", tags=["gpu-internal"])


@router.get("/status", include_in_schema=False)
def gpu_status(request: Request) -> dict[str, object]:
    registry = getattr(request.app.state, "gpu_runtime_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="GPU runtime registry is unavailable")
    return registry.snapshot()
