from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.models import AssistantChatStreamRequest
from app.services.assistant_chat import AssistantChatConfigError, AssistantChatModelError, stream_assistant_image_chat
from app.postgres_database import PostgresUnavailableError
from app.services.assistant_orchestrator import stream_assistant_events
from app.services.postgres_smiles_to_iupac import find_iupac_smiles_matches_postgres
from app.services.image_recognition import validate_structure_image
from app.utils.exceptions import InvalidImageError


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])
POSTGRES_ONLY_DETAIL = "Postgres runtime is required; set STRUCTURED_DATA_BACKEND=postgres."


@router.post("/chat/stream")
def stream_chat(
    request_body: AssistantChatStreamRequest,
    request: Request,
) -> StreamingResponse:
    settings = request.app.state.settings
    if settings.structured_data_backend != "postgres":
        raise HTTPException(status_code=503, detail=POSTGRES_ONLY_DETAIL)

    def events() -> Iterable[str]:
        try:
            def iupac_match_finder(text: str):
                with request.app.state.postgres_connection_factory(settings.pi_postgres_dsn) as connection:
                    return find_iupac_smiles_matches_postgres(connection, text)

            for event in stream_assistant_events(
                messages=request_body.messages,
                modules=request_body.context.modules,
                active_module=request_body.context.active_module,
                api_key=settings.assistant_api_key,
                base_url=settings.assistant_base_url,
                model=settings.assistant_model,
                model_enabled=settings.model_enabled,
                model_dir=settings.model_dir_path,
                iupac_match_finder=iupac_match_finder,
            ):
                yield _sse(event.event, event.payload)
        except AssistantChatConfigError as exc:
            yield _sse("error", {"detail": str(exc)})
        except PostgresUnavailableError as exc:
            yield _sse("error", {"detail": str(exc)})
        except AssistantChatModelError as exc:
            yield _sse("error", {"detail": f"Assistant model call failed: {_safe_error_detail(str(exc))}"})
        except Exception:
            logger.exception("Assistant chat stream failed")
            yield _sse("error", {"detail": "Assistant chat failed. Check backend logs for details."})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/image-stream")
async def stream_image_chat(
    request: Request,
    payload: str = Form(...),
    image: UploadFile = File(...),
) -> StreamingResponse:
    settings = request.app.state.settings

    try:
        request_body = AssistantChatStreamRequest.model_validate_json(payload)
    except (ValidationError, ValueError) as exc:
        await image.close()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        image_bytes = await image.read(settings.assistant_image_max_bytes + 1)
        detected_type, _suffix = validate_structure_image(
            image_bytes,
            content_type=image.content_type,
            max_bytes=settings.assistant_image_max_bytes,
        )
    except InvalidImageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    finally:
        await image.close()

    def events() -> Iterable[str]:
        full_message: list[str] = []
        try:
            for token in stream_assistant_image_chat(
                messages=request_body.messages,
                modules=request_body.context.modules,
                active_module=request_body.context.active_module,
                image_bytes=image_bytes,
                content_type=detected_type,
                api_key=settings.assistant_api_key,
                base_url=settings.assistant_base_url,
                model=settings.assistant_model,
            ):
                full_message.append(token)
                yield _sse("token", {"content": token})
            yield _sse("done", {"message": "".join(full_message)})
        except AssistantChatConfigError as exc:
            yield _sse("error", {"detail": str(exc)})
        except PostgresUnavailableError as exc:
            yield _sse("error", {"detail": str(exc)})
        except AssistantChatModelError as exc:
            yield _sse("error", {"detail": f"Assistant model call failed: {_safe_error_detail(str(exc))}"})
        except Exception:
            logger.exception("Assistant image chat stream failed")
            yield _sse("error", {"detail": "Assistant image chat failed. Check backend logs for details."})

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
