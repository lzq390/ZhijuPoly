from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.postgres_database import postgres_connection
from app.services.deployment_control import aggregate_active_jobs, get_drain_state


# Nginx only proxies /api and /health. This operational endpoint is intended for
# a loopback/container-network probe by the release controller, not public traffic.
router = APIRouter(prefix="/internal/deployment", tags=["deployment-internal"])


@router.get("/status", include_in_schema=False)
def deployment_status(request: Request) -> dict[str, object]:
    settings = request.app.state.settings
    connection_factory = getattr(
        request.app.state,
        "deployment_control_connection_factory",
        postgres_connection,
    )
    try:
        with connection_factory(settings.app_postgres_dsn) as connection:
            drain = get_drain_state(connection)
            jobs = aggregate_active_jobs(connection, request.app)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="deployment state is unavailable") from exc

    return {
        "active_jobs_schema_version": 1,
        "drain": {
            "enabled": drain.enabled,
            "reason": drain.reason,
            "release_sha": drain.release_sha,
            "activated_at": drain.activated_at.isoformat() if drain.activated_at else None,
            "activated_by": drain.activated_by,
            "updated_at": drain.updated_at.isoformat(),
        },
        "active_jobs": jobs.counts,
        "active_total": jobs.total,
    }
