"""Browsing observation and summary routes used by both application entrypoints."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from task_usage import entitlements as entitlement_core
from app.services.recording_entitlements import prepare_summary, snapshot_version
from app.services.private_execution import PrivateStreamingResponse
from app.recording_models import ArticleObservation, FilterObservation, RecordingStart

router = APIRouter(prefix="/api/v1", tags=["browsing-recording"])


@router.post("/knowledge/recordings")
async def start_recording(body: RecordingStart, request: Request):
    return await request.app.state.browsing_recording.start_recording(body)


@router.post("/knowledge/recordings/{recording_id}/stop")
async def stop_recording(recording_id: str, request: Request):
    store = request.app.state.browsing_recording
    result = await store.stop_recording(recording_id)
    if entitlement_core.enabled():
        result["frozen_version"] = snapshot_version(store.get_recording(recording_id, active=False))
    return result


@router.post("/knowledge/recordings/{recording_id}/summary")
async def summarize_recording(recording_id: str, request: Request):
    store = request.app.state.browsing_recording
    entitled = await prepare_summary(request, recording_id)
    if entitled is not None:
        if "text/event-stream" in request.headers.get("accept", "").lower():
            class Frames:
                def __aiter__(self):
                    return self
                async def __anext__(self):
                    kind, payload = await entitled.__anext__()
                    return f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                async def aclose(self):
                    await entitled.aclose()
            return PrivateStreamingResponse(Frames(), media_type="text/event-stream", headers={
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Vary": "Accept", **entitled.headers})
        return JSONResponse(await entitled.json_result(), headers=entitled.headers)
    if "text/event-stream" in request.headers.get("accept", "").lower():
        events = store.stream_summary(recording_id)

        async def frames():
            try:
                async for kind, payload in events:
                    yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                await events.aclose()

        return PrivateStreamingResponse(frames(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Vary": "Accept",
        })
    return await store.summarize_recording(recording_id)


@router.post("/knowledge/observations")
async def observe_article(body: ArticleObservation, request: Request):
    return await request.app.state.browsing_recording.observe_article(body)


@router.post("/database-browser/property-filter/observations")
async def observe_filter(body: FilterObservation, request: Request):
    return await request.app.state.browsing_recording.observe_filter(body)
