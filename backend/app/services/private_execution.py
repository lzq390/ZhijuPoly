"""Capture the creating identity and retain admission until execution really ends."""
from __future__ import annotations

from concurrent.futures import Executor, Future
from contextvars import ContextVar
from functools import wraps
from threading import RLock
from time import monotonic
from app.services.private_quotas import PrivateQuotaSettings
from app.services.in_memory_jobs import _deep_sizeof
from typing import Callable, Any
import anyio
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from app.auth.context import current_identity, current_owner_id, user_context, database_settings
from app.task_control import acquire_admission, authorize_memory_start
from app.task_observability import TaskExecutionContext, log_task_event

held_channel: ContextVar[str | None] = ContextVar("private_execution_channel", default=None)
held_task_context: ContextVar[TaskExecutionContext | None] = ContextVar("private_task_context", default=None)


def bounded_sync(channel: str):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with acquire_admission(channel, task_type=function.__name__) as lease:
                if not authorize_memory_start(current_owner_id()):
                    log_task_event(lease.context, "start_rejected", reason="account_disabled")
                    raise HTTPException(403, "Account disabled before execution")
                log_task_event(lease.context, "start_authorized")
                return function(*args, **kwargs)
        return call
    return decorate


def bounded_stream(channel: str):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            identity, settings = current_identity(), database_settings()
            lease = acquire_admission(channel, task_type=function.__name__)

            class PrivateStream:
                def __init__(self):
                    self.iterator = None
                    self.closed = False
                    self.lock = RLock()
                    self.quotas = PrivateQuotaSettings.from_environment()
                    self.started_at = None
                    self.output_bytes = 0

                def __iter__(self):
                    return self

                def __next__(self):
                    with self.lock:
                        if self.closed:
                            raise StopIteration
                        try:
                            # Each pull has its own context; Starlette may advance
                            # a synchronous iterator on different worker threads.
                            with user_context(identity, settings):
                                if self.iterator is None:
                                    if not authorize_memory_start(identity.user_id):
                                        log_task_event(lease.context, "start_rejected", reason="account_disabled")
                                        raise HTTPException(403, "Account disabled before execution")
                                    log_task_event(lease.context, "start_authorized")
                                    self.iterator = iter(function(*args, **kwargs))
                                    self.started_at = monotonic()
                                if monotonic() - self.started_at >= self.quotas.model_execution_seconds:
                                    raise HTTPException(504, "Model execution exceeded the time budget")
                                item = next(self.iterator)
                                self.output_bytes += _deep_sizeof(item)
                                if self.output_bytes > self.quotas.model_output_bytes:
                                    raise HTTPException(413, "Model output exceeded the response budget")
                                return item
                        except BaseException:
                            self.close()
                            raise

                def close(self):
                    # A concurrent close must wait for a provider pull to finish.
                    with self.lock:
                        if self.closed:
                            return
                        self.closed = True
                        try:
                            if self.iterator is not None and hasattr(self.iterator, "close"):
                                with user_context(identity, settings):
                                    self.iterator.close()
                        finally:
                            # Closing an unstarted stream also returns its permit.
                            lease.release()

                def __del__(self):
                    # Match generators' defensive cleanup; HTTP consumers still
                    # close explicitly so request cancellation is deterministic.
                    try:
                        self.close()
                    except Exception:
                        pass

            return PrivateStream()
        return call
    return decorate


def submit_private_job(executor: Executor, run: Callable[..., Any], *args: Any,
                       channel: str, on_disabled: Callable[[], None],
                       task_type: str | None = None, task_id: str | None = None) -> Future:
    identity, settings = current_identity(), database_settings()
    owner = current_owner_id()
    lease = acquire_admission(channel, owner_user_id=owner, task_type=task_type, task_id=task_id)
    try:
        # Submission validation never grants execution: a queued GPU task still
        # makes its actual start decision only after acquiring the registry slot.
        if not authorize_memory_start(owner):
            raise HTTPException(403, "Account disabled before submission")
    except BaseException:
        lease.release()
        raise

    def execute():
        with user_context(identity, settings):
            # GPU runners can wait in the registry after entering this thread.
            # Their sole start decision belongs after registry slot acquisition.
            if channel != "backend_gpu" and not authorize_memory_start(owner):
                log_task_event(lease.context, "start_rejected", reason="account_disabled")
                on_disabled()
                return
            if channel != "backend_gpu":
                log_task_event(lease.context, "start_authorized")
            token = held_channel.set(channel)
            context_token = held_task_context.set(lease.context)
            try:
                return run(*args)
            finally:
                held_task_context.reset(context_token)
                held_channel.reset(token)

    try:
        future = executor.submit(execute)
    except BaseException:
        lease.release()
        raise
    future.add_done_callback(lambda _: lease.release())
    return future


class PrivateStreamingResponse(StreamingResponse):
    """Close the original producer after disconnect before returning its quota."""

    def __init__(self, content, *args, **kwargs):
        self.private_content = content
        super().__init__(content, *args, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A cancelled Starlette iterator does not close a sync producer.
            # Wait for a pending pull to finish; never release its lease early.
            with anyio.CancelScope(shield=True):
                if hasattr(self.private_content, 'aclose'):
                    await self.private_content.aclose()
                elif hasattr(self.private_content, 'close'):
                    await anyio.to_thread.run_sync(self.private_content.close)
