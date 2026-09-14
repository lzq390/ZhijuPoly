"""Browsing observation and summary routes used by both application entrypoints."""

from fastapi import APIRouter, Request
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
    return await request.app.state.browsing_recording.summarize_recording(recording_id)


@router.post("/knowledge/observations")
async def observe_article(body: ArticleObservation, request: Request):
    return await request.app.state.browsing_recording.observe_article(body)


@router.post("/database-browser/property-filter/observations")
async def observe_filter(body: FilterObservation, request: Request):
    return await request.app.state.browsing_recording.observe_filter(body)
