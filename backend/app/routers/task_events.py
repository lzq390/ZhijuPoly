from typing import Literal

from fastapi import APIRouter, Request

from app.auth.context import current_identity, database_settings
from app.services.private_execution import PrivateStreamingResponse
from app.services.task_events import TaskEventHub, event_frames, read_snapshot

router = APIRouter(prefix="/api/v1/task-events", tags=["task-events"])


@router.get("")
async def task_events(request: Request, module: Literal["md", "dft"]):
    identity = current_identity()
    if not hasattr(request.app.state, "task_event_hub"):
        dsn = request.app.state.settings.app_postgres_dsn
        request.app.state.task_event_hub = TaskEventHub(
            lambda actor, settings, name: read_snapshot(dsn, actor, settings, name)
        )
    return PrivateStreamingResponse(
        event_frames(request.app.state.task_event_hub, identity, database_settings(), module),
        media_type="text/event-stream",
        headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"},
    )
