from __future__ import annotations

import json
from collections.abc import Iterable

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.models import AssistantChatStreamRequest
from app.services.assistant_chat import (
    AssistantChatConfigError,
    AssistantChatModelError,
    stream_assistant_chat,
)


router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])


@router.post("/chat/stream")
def stream_chat(
    request_body: AssistantChatStreamRequest,
    request: Request,
) -> StreamingResponse:
    settings = request.app.state.settings

    def events() -> Iterable[str]:
        full_message: list[str] = []
        try:
            for token in stream_assistant_chat(
                messages=request_body.messages,
                modules=request_body.context.modules,
                active_module=request_body.context.active_module,
                api_key=settings.assistant_api_key,
                base_url=settings.assistant_base_url,
                model=settings.assistant_model,
            ):
                full_message.append(token)
                yield _sse("token", {"content": token})
            yield _sse("done", {"message": "".join(full_message)})
        except AssistantChatConfigError as exc:
            yield _sse("error", {"detail": str(exc)})
        except AssistantChatModelError as exc:
            yield _sse("error", {"detail": f"Assistant model call failed: {_safe_error_detail(str(exc))}"})
        except Exception:
            yield _sse("error", {"detail": "Assistant chat failed. Check backend logs for details."})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: str, payload: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _safe_error_detail(detail: str) -> str:
    text = " ".join(detail.split())
    return text[:500] if text else "Check the API key, Base URL, model, and provider access."
