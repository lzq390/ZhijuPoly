"""Browsing observation and summary routes used by both application entrypoints."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from app.recording_models import ArticleObservation, FilterObservation, RecordingStart

router = APIRouter(prefix="/api/v1", tags=["browsing-recording"])


@router.post("/knowledge/recordings")
async def start_recording(body: RecordingStart, request: Request):
    return await request.app.state.browsing_recording.start_recording(body)


@router.post("/knowledge/recordings/{recording_id}/stop")
async def stop_recording(recording_id: str, request: Request):
    return await request.app.state.browsing_recording.stop_recording(recording_id)


@router.post("/knowledge/recordings/{recording_id}/summary")
async def summarize_recording(recording_id: str, request: Request):
    store = request.app.state.browsing_recording
    if "text/event-stream" in request.headers.get("accept", "").lower():
        events = store.stream_summary(recording_id)

        async def frames():
            try:
                async for kind, payload in events:
                    yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                await events.aclose()

        return StreamingResponse(frames(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Vary": "Accept",
        })
    return await store.summarize_recording(recording_id)


@router.post("/knowledge/observations")
async def observe_article(body: ArticleObservation, request: Request):
    return await request.app.state.browsing_recording.observe_article(body)


@router.post("/database-browser/property-filter/observations")
async def observe_filter(body: FilterObservation, request: Request):
    return await request.app.state.browsing_recording.observe_filter(body)
