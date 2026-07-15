from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request


router = APIRouter(prefix="/internal/gpu", tags=["gpu-internal"])


@router.get("/status", include_in_schema=False)
def gpu_status(request: Request) -> dict[str, object]:
    registry = getattr(request.app.state, "gpu_runtime_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="GPU runtime registry is unavailable")
    snapshot = registry.snapshot()
    managed = getattr(request.app.state, "backend_gpu_residency_lease", None)
    if managed is None:
        snapshot["resource_broker"] = {"enabled": False, "lease": None}
    else:
        lease = managed.lease
        snapshot["resource_broker"] = {
            "enabled": True,
            "connectivity": managed.connectivity_status,
            "last_heartbeat_error": managed.last_heartbeat_error,
            "lease": {
                "lease_id": lease.lease_id,
                "fencing_token": lease.fencing_token,
                "broker_instance_id": lease.broker_instance_id,
                "gpu_index": lease.gpu_index,
                "gpu_uuid": lease.gpu_uuid,
                "memory_mib": lease.memory_mib,
                "thread_percent": lease.thread_percent,
                "status": (
                    managed.connectivity_status
                    if managed.connectivity_status != "healthy"
                    else lease.status
                ),
            },
        }
        if managed.connectivity_status != "healthy":
            snapshot["status"] = "degraded"
            snapshot["accepting_inferences"] = False
    return snapshot
