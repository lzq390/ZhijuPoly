"""Frozen browsing-summary snapshots and durable owner-scoped delivery."""
from __future__ import annotations

import json
from contextlib import aclosing
from fastapi import HTTPException
from task_usage import entitlements as core
from app.auth.context import current_owner_id
from app.task_control import admission, authorize_memory_start
from app.services.assistant_entitlements import lookup_request, prepare_operation, _stored
from app.services.assistant_completion import mark_assistant_result
from app.services.entitlement_service import request_fingerprint
from app.services.knowledge_poc_summary import generate_knowledge_summary, stream_knowledge_summary
from app.usage_context import mark_current_usage_outcome, _status_for_exception


def snapshot_version(record):
    return request_fingerprint({"started_at": record.started_at, "ended_at": record.ended_at, "events": record.events})


async def prepare_summary(request, recording_id):
    if not core.enabled():
        return None
    frozen = request.query_params.get("frozen_version")
    payload = {"recording_id": recording_id, "frozen_version": frozen}
    operation = lookup_request(request, "recording_summary", payload)
    if operation is not None:
        return SummaryExecution(operation, recording_id)
    store = request.app.state.browsing_recording
    record = store.get_recording(recording_id, active=False)
    if record.ended_at is None:
        raise HTTPException(409, "Stop the recording before requesting a summary")
    await record.summary_lock.acquire()
    try:
        version = snapshot_version(record)
        if frozen is not None and frozen != version:
            raise HTTPException(409, {"code": "recording_snapshot_changed"})
        # A same-process cached delivery retains its original operation, snapshot
        # and charge. Never reserve another paid summary merely to read it.
        previous = getattr(record, "summary_operation", None)
        if previous is not None:
            repository = request.app.state.entitlement_repository
            saved = repository.get(owner_user_id=current_owner_id(), operation_id=previous["operation_id"])
            if saved is None:
                raise HTTPException(409, {"code": "summary_operation_missing"})
            from app.services.assistant_entitlements import AssistantOperation
            operation = AssistantOperation(repository, None, "recording_summary", None, saved, replay=_stored(saved))
            record.summary_lock.release()
            return SummaryExecution(operation, recording_id)
        if record.summary is not None:
            # A cache created before this feature has no durable operation. Read
            # the already-owned result without a reservation or historical backfill.
            result = {"recording_id": recording_id, **record.summary, "frozen_version": version}
            record.summary_lock.release()
            return LegacySummaryExecution(result)
        frozen_events = json.loads(json.dumps(record.events, ensure_ascii=False))
        spec = {"evidence_characters": len(json.dumps(frozen_events, ensure_ascii=False, sort_keys=True)),
            "max_output_tokens": store.quotas.summary_max_tokens, "provider_attempt_limit": 1}
        operation = prepare_operation(request, "recording_summary", payload, spec)
        if operation.replay is not None:
            record.summary_lock.release()
            return SummaryExecution(operation, recording_id)
        return SummaryExecution(operation, recording_id, store, record, frozen_events, version)
    except BaseException:
        record.summary_lock.release()
        raise


class SummaryExecution:
    def __init__(self, operation, recording_id, store=None, record=None, events=None, version=None):
        self.operation, self.recording_id = operation, recording_id
        self.store, self.record, self.events, self.version = store, record, events, version
        self.iterator, self.closed = None, False
        self.owns_lock = record is not None

    @property
    def headers(self):
        return self.operation.headers

    def _persist(self, summary):
        if len(json.dumps(summary, ensure_ascii=False).encode("utf-8")) > self.store.quotas.summary_output_bytes:
            raise HTTPException(413, "Summary exceeded the response budget")
        self.store._check_size(summary)
        payload = {"recording_id": self.recording_id, **summary, "frozen_version": self.version}
        self.operation.complete(payload, metadata={"snapshot_sha256": self.version})
        self.record.summary = summary
        self.record.summary_operation = {"operation_id": self.operation.operation["operation_id"]}
        return payload

    async def json_result(self):
        if self.operation.replay is not None:
            if "error" in self.operation.replay:
                raise HTTPException(502, self.operation.replay["error"], headers=self.headers)
            return self.operation.replay["result"]
        try:
            with self.operation.scope():
                self.operation.begin()
                if self.record.summary is not None:
                    mark_assistant_result("cache_hit", validated=True, local_result=True)
                    return self._persist(self.record.summary)
                with admission("ai"):
                    if not authorize_memory_start(current_owner_id()):
                        raise HTTPException(403, "Account disabled before execution")
                    summary = await generate_knowledge_summary(self.events, self.store.settings)
                if not self.events:
                    mark_assistant_result("no_events", validated=True, local_result=True)
                return self._persist(summary)
        finally:
            await self.aclose()

    async def _produce(self):
        if self.operation.replay is not None:
            if "error" in self.operation.replay:
                yield "error", self.operation.replay["error"]
            else:
                yield "done", self.operation.replay["result"]
            return
        self.operation.begin()
        yield "status", {"phase": "summarizing"}
        if self.record.summary is not None:
            mark_assistant_result("cache_hit", validated=True, local_result=True)
            yield "done", self._persist(self.record.summary)
            return
        parts, size = [], 0
        with admission("ai"):
            if not authorize_memory_start(current_owner_id()):
                raise HTTPException(403, "Account disabled before execution")
            async with aclosing(stream_knowledge_summary(self.events, self.store.settings)) as stream:
                async for delta in stream:
                    size += len(delta.encode("utf-8"))
                    if size > self.store.quotas.summary_output_bytes:
                        raise HTTPException(413, "Summary exceeded the response budget")
                    parts.append(delta)
                    yield "delta", {"text": delta}
        if not self.events:
            mark_assistant_result("no_events", validated=True, local_result=True)
        yield "done", self._persist({"summary": "".join(parts).strip(), "generated": bool(self.events)})

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        try:
            with self.operation.scope():
                if self.iterator is None:
                    self.iterator = self._produce()
                return await self.iterator.__anext__()
        except StopAsyncIteration:
            await self.aclose()
            raise
        except BaseException as exc:
            mark_current_usage_outcome(_status_for_exception(exc))
            await self.aclose()
            raise

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        try:
            with self.operation.scope():
                if self.iterator is not None:
                    await self.iterator.aclose()
                self.operation.abort()
        finally:
            if self.owns_lock:
                self.owns_lock = False
                self.record.summary_lock.release()


class LegacySummaryExecution:
    """Read-only pre-enablement cache; deliberately no durable receipt promise."""
    headers = {"X-Usage-Legacy-Cache": "true"}

    def __init__(self, result):
        self.result, self.closed = result, False

    async def json_result(self):
        return self.result

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration
        self.closed = True
        return "done", self.result

    async def aclose(self):
        self.closed = True
