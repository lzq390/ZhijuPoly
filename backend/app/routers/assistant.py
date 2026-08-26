from __future__ import annotations

import json
import logging
import queue
from collections.abc import Iterable
import threading
from time import perf_counter
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.formparsers import MultiPartParser

from app.models import (
    AssistantChatStreamRequest,
    TgAssistantChatStreamRequest,
    TgAssistantGuideResponse,
    TgAssistantStatusResponse,
)
from app.services.assistant_chat import AssistantChatConfigError, AssistantChatModelError, stream_assistant_image_chat
from app.services.ai_client import clean_ai_provider_error
from app.postgres_database import PostgresUnavailableError
from app.services.assistant_orchestrator import stream_assistant_events
from app.services.postgres_smiles_to_iupac import find_iupac_smiles_matches_postgres
from app.services.image_recognition import validate_structure_image
from app.utils.exceptions import InvalidImageError
from app.services.tg_assistant import (
    TG_GUIDE_VERSION,
    TgAssistantEvent,
    TgAssistantImageInput,
    TgAssistantProviderError,
    build_tg_image_data_url,
    derive_tg_phase,
    get_tg_guide,
    sanitize_tg_context,
    stream_tg_assistant_events,
    tg_assistant_configured,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])
POSTGRES_ONLY_DETAIL = "Postgres runtime is required; set STRUCTURED_DATA_BACKEND=postgres."
TG_ASSISTANT_HEARTBEAT_SECONDS = 15.0
TG_ASSISTANT_IMAGE_MIME_TYPES = ("image/png", "image/jpeg", "image/webp")
TG_ASSISTANT_DEFAULT_IMAGE_MAX_BYTES = 5 * 1024 * 1024

# Starlette otherwise rolls multipart files over 1 MiB into a temporary file.
# Keep every accepted default-size assistant image in memory; handlers still
# enforce their own byte limits before any model request is started.
if hasattr(MultiPartParser, "spool_max_size"):
    MultiPartParser.spool_max_size = max(  # type: ignore[attr-defined]
        MultiPartParser.spool_max_size,  # type: ignore[attr-defined]
        TG_ASSISTANT_DEFAULT_IMAGE_MAX_BYTES,
    )
if hasattr(MultiPartParser, "max_file_size"):
    MultiPartParser.max_file_size = max(
        MultiPartParser.max_file_size,
        TG_ASSISTANT_DEFAULT_IMAGE_MAX_BYTES,
    )


@router.get("/tg/status", response_model=TgAssistantStatusResponse)
def get_tg_assistant_status(request: Request) -> TgAssistantStatusResponse:
    settings = request.app.state.settings
    return TgAssistantStatusResponse(
        enabled=settings.tg_assistant_enabled,
        configured=tg_assistant_configured(
            api_key=settings.tg_assistant_api_key,
            base_url=settings.tg_assistant_base_url,
            model=settings.tg_assistant_model,
        ),
        image={
            "supported": True,
            "max_files": 2,
            "max_canvas_snapshots": 1,
            "max_user_upload_files": 1,
            "max_bytes": getattr(
                settings,
                "tg_assistant_image_max_bytes",
                TG_ASSISTANT_DEFAULT_IMAGE_MAX_BYTES,
            ),
            "max_total_bytes": 2 * getattr(
                settings,
                "tg_assistant_image_max_bytes",
                TG_ASSISTANT_DEFAULT_IMAGE_MAX_BYTES,
            ),
            "accepted_mime_types": list(TG_ASSISTANT_IMAGE_MIME_TYPES),
        },
    )


@router.get("/tg/guide", response_model=TgAssistantGuideResponse)
def get_tg_assistant_guide() -> TgAssistantGuideResponse:
    return get_tg_guide()


@router.post("/tg/chat/stream")
def stream_tg_chat(
    request_body: TgAssistantChatStreamRequest,
    request: Request,
) -> StreamingResponse:
    return _stream_tg_chat_response(request_body=request_body, request=request)


@router.post("/tg/chat/image-stream")
async def stream_tg_image_chat(
    request: Request,
    payload: str = Form(...),
    canvas_images: list[UploadFile] | None = File(default=None, alias="canvas_image"),
    images: list[UploadFile] | None = File(default=None, alias="image"),
) -> StreamingResponse:
    canvas_images = canvas_images or []
    images = images or []
    all_images = [*canvas_images, *images]
    raw_form = await request.form()
    unexpected_images = [
        value
        for key, value in raw_form.multi_items()
        if isinstance(value, StarletteUploadFile) and key not in {"canvas_image", "image"}
    ]
    if unexpected_images:
        for uploaded_image in [*all_images, *unexpected_images]:
            await uploaded_image.close()
        raise HTTPException(status_code=422, detail="unexpected image source field")
    if not all_images:
        raise HTTPException(status_code=422, detail="at least one image file is required")
    if len(canvas_images) > 1 or len(images) > 1 or len(all_images) > 2:
        for uploaded_image in all_images:
            await uploaded_image.close()
        raise HTTPException(
            status_code=422,
            detail="at most one canvas image and one user image are allowed",
        )
    try:
        request_body = TgAssistantChatStreamRequest.model_validate_json(payload)
    except (ValidationError, ValueError) as exc:
        for uploaded_image in all_images:
            await uploaded_image.close()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if canvas_images and request_body.page_context is None:
        for uploaded_image in all_images:
            await uploaded_image.close()
        raise HTTPException(
            status_code=422,
            detail="canvas_image requires page_context authorization",
        )

    settings = request.app.state.settings
    image_max_bytes = getattr(
        settings,
        "tg_assistant_image_max_bytes",
        TG_ASSISTANT_DEFAULT_IMAGE_MAX_BYTES,
    )
    validated: list[tuple[bytes, str]] = []
    try:
        for uploaded_image in all_images:
            image_bytes = await uploaded_image.read(image_max_bytes + 1)
            detected_type, _suffix = validate_structure_image(
                image_bytes,
                content_type=uploaded_image.content_type,
                max_bytes=image_max_bytes,
            )
            if detected_type not in TG_ASSISTANT_IMAGE_MIME_TYPES:
                raise InvalidImageError("unsupported image type", status_code=415)
            validated.append((image_bytes, detected_type))
    except InvalidImageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    finally:
        for uploaded_image in all_images:
            await uploaded_image.close()

    if sum(len(image_bytes) for image_bytes, _ in validated) > image_max_bytes * 2:
        raise HTTPException(status_code=413, detail="combined image payload is too large")

    canvas_image = validated[0] if canvas_images else None
    user_image = validated[-1] if images else None

    return _stream_tg_chat_response(
        request_body=request_body,
        request=request,
        canvas_image=canvas_image,
        user_image=user_image,
    )


def _stream_tg_chat_response(
    *,
    request_body: TgAssistantChatStreamRequest,
    request: Request,
    canvas_image: tuple[bytes, str] | None = None,
    user_image: tuple[bytes, str] | None = None,
) -> StreamingResponse:
    settings = request.app.state.settings
    configured = tg_assistant_configured(
        api_key=settings.tg_assistant_api_key,
        base_url=settings.tg_assistant_base_url,
        model=settings.tg_assistant_model,
    )
    if not settings.tg_assistant_enabled:
        raise HTTPException(status_code=503, detail="Tg assistant is disabled")
    if not configured:
        raise HTTPException(status_code=503, detail="Tg assistant is not configured")

    request_id = uuid4().hex
    request_bytes = len(request_body.model_dump_json().encode("utf-8"))
    image_inputs: list[TgAssistantImageInput] = []
    if canvas_image is not None:
        image_inputs.append(
            TgAssistantImageInput(
                source="canvas",
                data_url=build_tg_image_data_url(canvas_image[0], canvas_image[1]),
            )
        )
    if user_image is not None:
        image_inputs.append(
            TgAssistantImageInput(
                source="user",
                data_url=build_tg_image_data_url(user_image[0], user_image[1]),
            )
        )
    image_attached = bool(image_inputs)
    context_payload, trimmed_fields, context_bytes = sanitize_tg_context(
        request_body.page_context,
        image_inputs=image_inputs,
    )
    phase = derive_tg_phase(request_body.page_context)

    def events() -> Iterable[str]:
        started_at = perf_counter()
        first_token_at: float | None = None
        first_summary_at: float | None = None
        summary_started_at: dict[str, float] = {}
        summary_done_at: dict[str, float] = {}
        route_ms: float | None = None
        terminal = "disconnect"
        terminal_sent = False
        action_types: set[str] = set()
        event_queue: queue.Queue[TgAssistantEvent | BaseException | None] = queue.Queue()
        cancelled = threading.Event()

        def note_route_complete(elapsed_ms: float) -> None:
            nonlocal route_ms
            route_ms = round(elapsed_ms, 1)

        def worker() -> None:
            try:
                for event in stream_tg_assistant_events(
                    messages=request_body.messages,
                    page_context=request_body.page_context,
                    api_key=settings.tg_assistant_api_key,
                    base_url=settings.tg_assistant_base_url,
                    model=settings.tg_assistant_model,
                    proxy_url=settings.ai_proxy_url,
                    image_inputs=image_inputs,
                    reasoning_effort=getattr(
                        settings,
                        "tg_assistant_reasoning_effort",
                        "medium",
                    ),
                    transport=getattr(settings, "tg_assistant_transport", "auto"),
                    cancelled=cancelled.is_set,
                    on_route_complete=note_route_complete,
                ):
                    if cancelled.is_set():
                        break
                    event_queue.put(event)
            except BaseException as exc:  # passed back to the response generator
                if not cancelled.is_set():
                    event_queue.put(exc)
            finally:
                if not cancelled.is_set():
                    event_queue.put(None)

        yield _sse(
            "meta",
            {
                "request_id": request_id,
                "guide_version": TG_GUIDE_VERSION,
                "context_attached": context_payload is not None,
                "context_trimmed": trimmed_fields,
                "image_attached": image_attached,
                "image_count": len(image_inputs),
                "canvas_image_attached": canvas_image is not None,
                "user_image_attached": user_image is not None,
            },
        )
        thread = threading.Thread(target=worker, name=f"tg-assistant-{request_id[:8]}", daemon=True)
        thread.start()
        try:
            while True:
                try:
                    item = event_queue.get(timeout=TG_ASSISTANT_HEARTBEAT_SECONDS)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                if item is None:
                    if not terminal_sent:
                        terminal = "incomplete_stream"
                        terminal_sent = True
                        yield _sse(
                            "error",
                            {
                                "request_id": request_id,
                                "code": "incomplete_stream",
                                "message": "AI 响应未完整结束，请重试。",
                                "retryable": True,
                            },
                        )
                    break
                if isinstance(item, BaseException):
                    raise item
                if item.event == "token" and first_token_at is None:
                    first_token_at = perf_counter()
                if item.event == "reasoning_summary_delta" and first_summary_at is None:
                    first_summary_at = perf_counter()
                if item.event == "reasoning_summary_delta":
                    summary_phase = item.payload.get("phase")
                    if summary_phase in {"intent", "answer"}:
                        summary_started_at.setdefault(str(summary_phase), perf_counter())
                if item.event == "reasoning_summary_done":
                    summary_phase = item.payload.get("phase")
                    if summary_phase in {"intent", "answer"}:
                        summary_done_at[str(summary_phase)] = perf_counter()
                if item.event == "action_proposal":
                    operations = item.payload.get("operations")
                    if isinstance(operations, list):
                        action_types.update(
                            str(operation.get("type"))
                            for operation in operations
                            if isinstance(operation, dict) and operation.get("type")
                        )
                if item.event in {"done", "error"}:
                    if terminal_sent:
                        continue
                    terminal_sent = True
                    terminal = item.event
                yield _sse(item.event, item.payload)
                if terminal_sent:
                    break
        except TgAssistantProviderError:
            terminal = "provider_error"
            terminal_sent = True
            logger.warning("Tg assistant provider failure request_id=%s code=provider_error", request_id)
            yield _sse(
                "error",
                {
                    "request_id": request_id,
                    "code": "provider_error",
                    "message": "AI 服务暂时无法完成请求，请稍后重试。",
                    "retryable": True,
                },
            )
        except Exception:
            terminal = "internal_error"
            terminal_sent = True
            logger.exception("Tg assistant stream failed request_id=%s", request_id)
            yield _sse(
                "error",
                {
                    "request_id": request_id,
                    "code": "internal_error",
                    "message": "AI 助手发生内部错误，请稍后重试。",
                    "retryable": True,
                },
            )
        finally:
            cancelled.set()
            elapsed = perf_counter() - started_at
            first_token_ms = (
                round((first_token_at - started_at) * 1000, 1) if first_token_at is not None else None
            )
            first_summary_ms = (
                round((first_summary_at - started_at) * 1000, 1)
                if first_summary_at is not None
                else None
            )
            attached_images = [image for image in (canvas_image, user_image) if image is not None]
            intent_reasoning_ms = (
                round((summary_done_at["intent"] - summary_started_at["intent"]) * 1000, 1)
                if "intent" in summary_started_at and "intent" in summary_done_at
                else None
            )
            answer_reasoning_ms = (
                round((summary_done_at["answer"] - summary_started_at["answer"]) * 1000, 1)
                if "answer" in summary_started_at and "answer" in summary_done_at
                else None
            )
            logger.info(
                "Tg assistant request request_id=%s phase=%s request_bytes=%s "
                "context_bytes=%s context_attached=%s trimmed=%s image_attached=%s "
                "image_count=%s canvas_image_attached=%s user_image_attached=%s "
                "image_bytes=%s image_mime=%s route_ms=%s first_summary_ms=%s first_token_ms=%s "
                "intent_reasoning_ms=%s answer_reasoning_ms=%s "
                "total_ms=%.1f terminal=%s action_types=%s",
                request_id,
                phase,
                request_bytes,
                context_bytes,
                context_payload is not None,
                bool(trimmed_fields),
                image_attached,
                len(attached_images),
                canvas_image is not None,
                user_image is not None,
                sum(len(image[0]) for image in attached_images),
                ",".join(image[1] for image in attached_images) or "none",
                route_ms,
                first_summary_ms,
                first_token_ms,
                intent_reasoning_ms,
                answer_reasoning_ms,
                elapsed * 1000,
                terminal,
                ",".join(sorted(action_types)) or "none",
            )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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
                proxy_url=settings.ai_proxy_url,
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
                proxy_url=settings.ai_proxy_url,
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
    return clean_ai_provider_error(AssistantChatModelError(detail))
