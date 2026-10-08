"""Default-off, owner-scoped durable delivery for the three assistant entrypoints.

A reservation belongs to a logical request, not to provider attempts. Final
results are saved atomically with settlement before their done frame is sent.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from threading import RLock
from uuid import uuid4

from fastapi import HTTPException
from task_usage import entitlements as core
from task_usage.entitlement_policy import normalize_spec, qualify
from app.auth.context import current_identity, database_settings, user_context
from app.services.assistant_completion import CompletionEvidence, completion_scope
from app.services.entitlement_service import (request_idempotency_key, request_fingerprint,
    service_from_request, public_entitlement_error, operation_header)

MAX_RESULT_BYTES = 256 * 1024
_REPLAY_EVENTS = {"skill_result", "navigation", "action_proposal"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _pending(operation):
    return HTTPException(409, {"code": "usage_operation_pending", "operation_id": operation["operation_id"],
        "message": "This logical request is already reserved; retrieve its existing operation."},
        headers=operation_header(operation))


def _stored(operation):
    result = operation.get("result_json")
    if operation.get("state") in {"succeeded", "released"} and isinstance(result, dict):
        return result
    raise _pending(operation)


def lookup_request(request, module, payload):
    if not core.enabled():
        return None
    repository = getattr(request.app.state, "entitlement_repository", None)
    if repository is None:
        raise HTTPException(503, {"code": "usage_entitlements_unavailable"})
    identity = current_identity()
    key, fingerprint = request_idempotency_key(request), request_fingerprint(payload)
    try:
        operation = repository.lookup(owner_user_id=identity.user_id, module=module,
            idempotency_key=key, request_fingerprint=fingerprint)
    except core.EntitlementError as exc:
        raise public_entitlement_error(exc) from exc
    if operation is not None:
        return AssistantOperation(repository, None, module, None, operation, replay=_stored(operation))
    return None


def prepare_operation(request, module, payload, spec):
    existing = lookup_request(request, module, payload)
    if existing is not None or not core.enabled():
        return existing
    repository, policy = service_from_request(request)
    try:
        selection = normalize_spec(policy, module, spec)
    except ValueError as exc:
        raise HTTPException(422, {"code": "usage_specification_rejected"}) from exc
    try:
        result = repository.reserve(owner_user_id=current_identity().user_id, module=module,
            idempotency_key=request_idempotency_key(request), request_fingerprint=request_fingerprint(payload),
            policy_version=selection["policy_version"], spec=selection["canonical_spec"])
    except core.EntitlementError as exc:
        raise public_entitlement_error(exc) from exc
    operation = result.get("operation")
    if not operation:
        raise HTTPException(503, {"code": "usage_reservation_unavailable"})
    if not result.get("created"):
        return AssistantOperation(repository, None, module, None, operation, replay=_stored(operation))
    return AssistantOperation(repository, policy, module, selection["canonical_spec"], operation)


def prepare_chat(request, request_body, module="assistant", images=(), *, image_sources=None, lookup_only=False):
    if not core.enabled():
        return None
    body = request_body.model_dump(mode="json")
    sources = image_sources or ["user"] * len(images)
    payload = {"request": body, "images": [{"sha256": hashlib.sha256(data).hexdigest(),
        "mime": mime, "bytes": len(data), "source": source} for (data, mime), source in zip(images, sources)]}
    if lookup_only:
        return lookup_request(request, module, payload)
    spec = {"message_count": len(request_body.messages), "input_characters": len(_json(body)),
        "image_count": len(images), "image_bytes": sum(len(data) for data, _ in images),
        "max_output_tokens": 2400 if module == "tg_assistant" else 1400, "provider_attempt_limit": 4}
    return prepare_operation(request, module, payload, spec)


class AssistantOperation:
    def __init__(self, repository, policy, module, spec, operation, *, replay=None):
        self.repository, self.policy, self.module, self.spec = repository, policy, module, spec
        self.operation, self.replay = operation, replay
        self.identity, self.database = current_identity(), database_settings()
        self.executor_id = "assistant-" + uuid4().hex
        self.evidence = CompletionEvidence(spec["provider_attempt_limit"], spec["max_output_tokens"]) if spec else None
        self.events = []
        self.lock = RLock()
        self.finished = replay is not None

    @property
    def headers(self):
        return operation_header(self.operation)

    @contextmanager
    def scope(self):
        with user_context(self.identity, self.database), completion_scope(self.evidence):
            yield

    def _call(self, method, **kwargs):
        with user_context(self.identity, self.database):
            value = getattr(self.repository, method)(owner_user_id=self.identity.user_id,
                operation_id=self.operation["operation_id"], generation=self.operation["generation"], **kwargs)
        if not value.get("transitioned"):
            raise _pending(value.get("operation") or self.operation)
        self.operation = value["operation"]
        return value

    def dispatch(self):
        with self.lock:
            if self.finished or self.operation["state"] != "reserved":
                raise _pending(self.operation)
            self._call("begin_dispatch", executor_id=self.executor_id)

    def begin(self):
        with self.lock:
            if self.finished:
                raise _pending(self.operation)
            if self.operation["state"] == "reserved":
                self.dispatch()
            if self.operation["state"] != "dispatching":
                raise _pending(self.operation)
            self._call("claim_running", executor_id=self.executor_id)

    def dispatch_rejected_before_execution(self):
        # Caller must keep its worker gate closed until this transition completes.
        with self.lock:
            self._call("release", reason="dispatch_rejected_before_execution",
                proof={"kind": "dispatch_rejected_before_execution", "executor_id": self.executor_id})
            self.finished = True

    def _bounded(self, result):
        if len(_json(result).encode("utf-8")) > min(MAX_RESULT_BYTES, self.policy.get("result_max_bytes", MAX_RESULT_BYTES)):
            raise HTTPException(413, {"code": "usage_result_too_large"})
        return result

    def complete(self, payload, *, metadata=None):
        with self.lock:
            if self.finished:
                return
            output = bool(str(payload.get("message", payload.get("summary", ""))).strip()) or bool(self.events)
            facts = self.evidence.facts(output)
            decision = qualify(self.policy, self.module, self.spec, facts, "completed")
            if not decision["terminal"]:
                self._unknown("completion_unproven")
                raise HTTPException(502, {"code": "usage_completion_unproven"})
            result = self._bounded({"version": 1, "module": self.module, "result": payload,
                "events": self.events, "metadata": metadata or {}, "outcome": decision["outcome"]})
            self._call("settle", qualified=decision["qualified"], outcome=decision["outcome"], result_json=result)
            self.replay, self.finished = result, True

    def event(self, kind, payload):
        if kind in _REPLAY_EVENTS:
            self._bounded({"events": [*self.events, {"event": kind, "payload": payload}]})
            self.events.append({"event": kind, "payload": payload})
        elif kind == "done":
            self.complete(payload)
        elif kind in {"error", "skill_error"}:
            self.abort()

    def _unknown(self, reason):
        if not self.finished:
            self._call("mark_unknown", reason=reason)
            self.finished = True

    def abort(self):
        with self.lock:
            if self.finished:
                return
            if self.operation["state"] == "reserved":
                self._call("release", reason="not_dispatched", proof={"kind": "not_dispatched"})
                self.finished = True
            elif self.operation["state"] == "running" and self.events and self.evidence.local_result_proven:
                self.complete({"message": ""})
            elif self.operation["state"] == "running" and self.evidence.all_providers_terminated():
                # Only explicit provider terminals (or no invocation), never EOF,
                # justify releasing a failed local delivery. Do not save partial text.
                decision = qualify(self.policy, self.module, self.spec, None, "failed")
                result = {"version": 1, "module": self.module, "error": {"code": "assistant_failed"},
                    "events": [], "outcome": decision["outcome"]}
                self._call("settle", qualified=False, outcome=decision["outcome"], result_json=result)
                self.replay, self.finished = result, True
            else:
                self._unknown("producer_outcome_unknown")

    def replay_frames(self):
        if "error" in self.replay:
            yield sse("error", self.replay["error"])
            return
        for item in self.replay.get("events", []):
            yield sse(item["event"], item["payload"])
        yield sse("done", self.replay["result"])


def sse(kind, payload):
    return f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class EntitledIterator:
    """Per-pull context binding; close also handles a never-started producer."""
    def __init__(self, content, operation, *, producer=True):
        self.content, self.operation, self.producer = content, operation, producer
        self.iterator, self.started, self.closed = None if callable(content) else iter(content), False, False
        self.lock = RLock()

    def __iter__(self):
        return self

    def __next__(self):
        with self.lock:
            if self.closed:
                raise StopIteration
            try:
                with self.operation.scope():
                    if not self.started:
                        self.started = True
                        if self.producer:
                            self.operation.begin()
                    if self.iterator is None:
                        self.content = self.content()
                        self.iterator = iter(self.content)
                    item = next(self.iterator)
                    if self.producer:
                        if isinstance(item, str):
                            lines = item.splitlines()
                            kind = next((line[7:] for line in lines if line.startswith("event: ")), None)
                            data = next((line[6:] for line in lines if line.startswith("data: ")), None)
                            if kind and data:
                                self.operation.event(kind, json.loads(data))
                        else:
                            self.operation.event(item.event, item.payload)
                    return item
            except BaseException:
                self.close()
                raise

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                with self.operation.scope():
                    close = getattr(self.content, "close", None)
                    if close:
                        close()
            finally:
                # The Tg HTTP consumer does not own its running producer. Its
                # wrapper can release only a still-reserved never-started task.
                if self.producer or self.operation.operation["state"] == "reserved":
                    self.operation.abort()


def wrap_chat(content, operation, *, producer=True):
    if operation is None:
        return content() if callable(content) else content
    if operation.replay is not None:
        return operation.replay_frames()
    return EntitledIterator(content, operation, producer=producer)
