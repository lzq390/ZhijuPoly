"""Durable hand-off for backend-owned process-local model jobs.

Both dispatch and execution fences must succeed. Unknown outcomes retain their
reservation. Losing a process-local record never authorizes another execution.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import logging
import json
from threading import Event, Lock
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
from app.auth.context import (
    current_owner_id, current_identity, database_settings, Identity, user_context, service_context,
)
from task_usage.entitlements import enabled, EntitlementError
from app.services.entitlement_service import (
    request_idempotency_key, request_fingerprint, public_entitlement_error,
)

logger = logging.getLogger(__name__)
MODULES = frozenset({"conditional_generation", "polytao"})
TERMINAL = frozenset({"completed", "found_enough", "exhausted", "failed", "cancelled"})
_EXECUTOR_ID = "memory:" + uuid4().hex


class ExecutionFenced(RuntimeError):
    """The durable operation no longer authorizes this invocation."""


def _operation_kwargs(operation):
    return dict(owner_user_id=operation["owner_user_id"],
                operation_id=operation["operation_id"], generation=operation["generation"])


def _status_model(module):
    from app.models import (ConditionalGenerationJobStatusResponse,
                            PolytaoJobStatusResponse, ReverseDesignTgJobStatusResponse)
    return {"conditional_generation": ConditionalGenerationJobStatusResponse,
            "polytao": PolytaoJobStatusResponse,
            "reverse_design": ReverseDesignTgJobStatusResponse}[module]


def _unknown(operation):
    return HTTPException(409, detail={"code": "usage_outcome_unknown",
        "operation_id": operation["operation_id"], "state": operation["state"],
        "reservation_held": operation["state"] not in {"released", "succeeded"}})


def _restore(module, operation):
    if operation["state"] not in {"succeeded", "released"}:
        return None
    payload = operation.get("result_json")
    if payload is None:
        return None
    snapshot = _status_model(module).model_validate(payload)
    if snapshot.job_id != operation["native_task_id"]:
        raise RuntimeError("Persisted entitlement result identity mismatch")
    return snapshot


def _spec(module, payload):
    keys = {
        "conditional_generation": ("candidate_count", "top_k", "temperature", "delta_tg"),
        "polytao": ("candidate_count", "temperature", "top_k", "top_p", "max_length"),
        "reverse_design": ("candidate_size",),
    }[module]
    value = {key: payload[key] for key in keys}
    if module == "polytao":
        value["operation"] = "generate"
    return value


def _facts(module, snapshot, completion_proven):
    """Small typed policy facts, never prompts, model output text, or SVGs."""
    result = snapshot.result
    if module == "reverse_design":
        return {"validated": result is not None, "completion_proven": completion_proven}
    valid = 0
    validated = result is not None
    returned = 0 if result is None else result.returned_count
    if result is not None:
        from rdkit import Chem
        seen = set()
        validated = returned == len(result.results)
        for candidate in result.results:
            molecule = Chem.MolFromSmiles(candidate.generated_smiles)
            if molecule is None:
                validated = False
                continue
            if module == "polytao" and not candidate.valid_smiles:
                validated = False
                continue
            if module == "conditional_generation" and sum(
                    atom.GetAtomicNum() == 0 for atom in molecule.GetAtoms()) < 2:
                validated = False
                continue
            canonical = Chem.MolToSmiles(molecule)
            if canonical not in seen:
                seen.add(canonical)
                valid += 1
    return {"validated": validated, "completion_proven": completion_proven,
            "valid_count": valid, "returned_count": returned}


class MemorySubmission:
    def __init__(self, repository, policy, module, owner_user_id, idempotency_key,
                 fingerprint, *, spec=None, operation=None, normalize=None, qualify=None):
        self.repository, self.policy = repository, deepcopy(policy)
        self.module, self.owner_user_id = module, owner_user_id
        self.idempotency_key, self.fingerprint = idempotency_key, fingerprint
        self.spec, self.operation = spec, operation
        self.normalize, self.qualify = normalize, qualify
        self._lock, self._pending = Lock(), None
        self.settled = False
        self.execution_fenced = False
        self.executor_id = _EXECUTOR_ID
        self._identity = current_identity(required=False) or Identity(owner_user_id)
        if self._identity.user_id != owner_user_id:
            raise ValueError("Entitlement owner differs from submitting identity")
        self._settings = database_settings()

    @contextmanager
    def owner_scope(self):
        # Future callbacks run after the executor's user_context has unwound.
        # Restore the captured actor; do not weaken the store's owner checks.
        with user_context(self._identity, self._settings), service_context():
            yield

    def replay(self):
        if self.operation is None:
            return None
        native = self.operation.get("native_task_id")
        if not native:
            raise _unknown(self.operation)
        restored = _restore(self.module, self.operation)
        if restored is not None:
            return restored
        if self.operation["state"] in {"released", "unknown"}:
            raise _unknown(self.operation)
        return SimpleNamespace(job_id=native, status="pending" if
            self.operation["state"] == "reserved" else "running")

    def bind(self, job_id):
        """Atomically reserve the logical submission and bind its native id."""
        normalized = self.normalize(self.policy, self.module, self.spec)
        self.spec = normalized["canonical_spec"]
        try:
            result = self.repository.reserve(owner_user_id=self.owner_user_id,
                module=self.module, idempotency_key=self.idempotency_key,
                request_fingerprint=self.fingerprint,
                policy_version=normalized["policy_version"], spec=self.spec,
                native_task_id=job_id)
        except EntitlementError as exc:
            raise public_entitlement_error(exc) from exc
        self.operation = result["operation"]
        return result["created"] is True

    def submit(self, submitter, executor, run, *args, **kwargs):
        dispatch = self.repository.begin_dispatch(**_operation_kwargs(self.operation),
                                                   executor_id=self.executor_id)
        self.operation = dispatch["operation"]
        if not dispatch["transitioned"]:
            raise ExecutionFenced("Entitlement dispatch was not acquired")
        # submit may enqueue then raise while starting a thread. The callable
        # cannot claim execution until the submitter confirms its Future.
        resolved = Event()
        accepted = False

        def guarded(*run_args):
            resolved.wait()
            if not accepted:
                raise ExecutionFenced("Native dispatch was rejected before execution")
            try:
                claim = self.repository.claim_running(**_operation_kwargs(self.operation),
                                                        executor_id=self.executor_id)
            except Exception as exc:
                self.execution_fenced = True
                raise ExecutionFenced("Entitlement execution confirmation unavailable") from exc
            self.operation = claim["operation"]
            if not claim["transitioned"]:
                self.execution_fenced = True
                raise ExecutionFenced("Entitlement execution was not acquired")
            return run(*run_args)

        try:
            future = submitter(executor, guarded, *args, **kwargs)
        except BaseException:
            resolved.set()
            try:
                self.repository.release(**_operation_kwargs(self.operation),
                    reason="dispatch_rejected", proof={
                        "kind": "dispatch_rejected_before_execution",
                        "executor_id": self.executor_id})
            except Exception:
                logger.error("entitlement_dispatch_compensation_unconfirmed module=%s operation=%s",
                             self.module, self.operation["operation_id"])
            raise
        accepted = True
        resolved.set()
        return future

    def finish(self, snapshot, future):
        """Persist after computation stops; retry never invokes the runner."""
        with self._lock:
            if self.settled:
                return True
            if self.execution_fenced:
                return False
            if not future.done() or snapshot.status not in TERMINAL:
                return False
            self._pending = (snapshot, future)
            try:
                completion = not future.cancelled() and future.exception() is None
                facts = _facts(self.module, snapshot, completion)
                decision = self.qualify(self.policy, self.module, self.spec,
                                        facts, snapshot.status)
                if not decision["terminal"]:
                    return False
                payload = snapshot.model_dump(mode="json")
                # A bounded result must be durably deliverable before it can
                # consume a success. Preserve a small honest failure instead of
                # truncating output and calling it successful.
                try:
                    size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                    oversized = size + 16 > self.policy["result_max_bytes"]
                    failure = "result_retention_limit"
                except (ValueError, TypeError):
                    oversized, failure = True, "invalid_result"
                if oversized:
                    fields = {"status": "failed", "result": None}
                    if self.module == "polytao":
                        fields.update(progress_stage="failed", error_message=failure,
                                      progress_message=failure)
                    else:
                        fields.update(error=failure, message=None)
                    payload = snapshot.model_copy(update=fields).model_dump(mode="json")
                    decision = {"outcome": failure, "qualified": False}
                result = self.repository.settle(**_operation_kwargs(self.operation),
                    outcome=decision["outcome"], qualified=decision["qualified"],
                    result_json=payload)
                operation = result["operation"]
                if _restore(self.module, operation) is None:
                    return False
                self.operation = operation
                self.settled, self._pending = True, None
                return True
            except Exception as exc:
                logger.error("entitlement_settlement_unconfirmed module=%s operation=%s error=%s code=%s",
                             self.module, self.operation["operation_id"], type(exc).__name__,
                             getattr(exc, "code", None))
                return False

    def visible(self, snapshot):
        pending = self._pending
        if pending is not None:
            self.finish(*pending)
        if self.settled:
            return _restore(self.module, self.operation)
        if snapshot.status not in TERMINAL:
            return snapshot
        fields = {"status": "running", "result": None, "finished_at": None}
        if self.module == "polytao":
            fields.update(progress_percent=99, progress_stage="settling",
                          progress_message="Result persistence is not yet confirmed.",
                          error_message=None)
        else:
            fields.update(message="Result persistence is not yet confirmed.", error=None)
        return snapshot.model_copy(update=fields)


def submit_memory_job(entitlement, submitter, executor, run, *args, **kwargs):
    if entitlement is None:
        return submitter(executor, run, *args, **kwargs)
    return entitlement.submit(submitter, executor, run, *args, **kwargs)


def submission_for_request(request, module, body):
    # This model-assisted query is explicitly rate-only in the eight-pool plan.
    # It keeps the native account/channel limits; no success reservation exists.
    if module == "reverse_design":
        return None
    if not enabled():
        return None
    if module not in MODULES:
        raise ValueError("Unsupported memory entitlement module")
    repository = getattr(request.app.state, "entitlement_repository", None)
    policy = getattr(request.app.state, "entitlement_policy", None)
    if repository is None:
        raise HTTPException(503, detail="Entitlement repository unavailable")
    key = request_idempotency_key(request)
    payload = body.model_dump(mode="json")
    fingerprint = request_fingerprint(payload)
    owner = current_owner_id()
    try:
        operation = repository.lookup(owner_user_id=owner, module=module,
            idempotency_key=key, request_fingerprint=fingerprint)
    except EntitlementError as exc:
        raise public_entitlement_error(exc) from exc
    # Existing results remain retrievable after a policy change.
    if operation is not None:
        request.state.usage_operation_id = str(operation["operation_id"])
        return MemorySubmission(repository, {}, module, owner, key, fingerprint,
                                operation=operation)
    if policy is None:
        raise HTTPException(503, detail="Entitlement policy unavailable")
    from task_usage.entitlement_policy import (
        normalize_spec, qualify, InvalidSpecification, EntitlementPolicyError,
    )
    spec = _spec(module, payload)
    try:
        normalize_spec(policy, module, spec)
    except InvalidSpecification as exc:
        raise HTTPException(422, detail={"code": "usage_specification_unsupported"}) from exc
    except EntitlementPolicyError as exc:
        raise HTTPException(503, detail={"code": "usage_policy_unavailable"}) from exc
    return MemorySubmission(repository, policy, module, owner, key, fingerprint,
        spec=spec, normalize=normalize_spec, qualify=qualify)


def get_memory_job(request, module, job_id, native_lookup):
    if module == "reverse_design" or not enabled():
        return native_lookup(job_id)
    repository = getattr(request.app.state, "entitlement_repository", None)
    if repository is None:
        raise HTTPException(503, detail="Entitlement repository unavailable")
    operation = repository.get_by_native(owner_user_id=current_owner_id(),
                                         module=module, native_task_id=job_id)
    if operation is not None:
        restored = _restore(module, operation)
        if restored is not None:
            return restored
    try:
        snapshot = native_lookup(job_id)
    except KeyError:
        if operation is not None:
            raise _unknown(operation) from None
        raise
    if operation is not None and snapshot.status in TERMINAL:
        # Settlement may race the initial read and remove its local handle.
        # A small durable failure (e.g. byte limit) must override the original
        # successful in-memory result, even in that interleaving.
        confirmed = repository.get_by_native(owner_user_id=current_owner_id(),
                                              module=module, native_task_id=job_id)
        restored = _restore(module, confirmed) if confirmed is not None else None
        if restored is None:
            raise _unknown(confirmed or operation)
        return restored
    return snapshot


def revoke_not_dispatched(repository, operation):
    """CAS revocation prevents later dispatch, without inferring process death."""
    if operation["state"] != "reserved":
        return False
    result = repository.release(**_operation_kwargs(operation),
        reason="not_dispatched_recovered", proof={"kind": "not_dispatched"})
    return result["transitioned"] is True


def associate_request(request, entitlement):
    if entitlement is not None and entitlement.operation is not None:
        request.state.usage_operation_id = str(entitlement.operation["operation_id"])
        request.state.usage_operation_state = entitlement.operation["state"]
