"""One backend executor, durable native receipts, and no automatic GPU replay."""
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, RLock
from time import sleep
from types import SimpleNamespace
from uuid import uuid4
import logging
from fastapi import HTTPException
from app.auth.context import service_context
from app.models import MonomerRetrosynthesisRequest
from app.services.private_execution import submit_private_job, capture_job_owner_scope
from app.services.usage_background import finish_background_usage
from app.services.gpu_runtime_registry import GpuQueueTimeoutError, GpuQueueFullError, GpuQueueStoppedError
from app.services.monomer_retrosynthesis import predict_monomer_precursors, _infer_target_role
from app.services.retrosynthesis_repository import public_job
from app.services.retrosynthesis_targets import canonical_target
from app.utils.exceptions import ModelArtifactError
from app.services.entitlement_sync import _facts

logger = logging.getLogger(__name__)


class RetrosynthesisJobManager:
    def __init__(self, app, repository):
        self.app, self.repository = app, repository
        self.instance_id = "retrosynthesis:" + uuid4().hex
        self._mutex = RLock()
        self._executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix="retrosynthesis")
        self._futures = {}
        self._accepting = False
        self._stopping = False
        self._lock_context = None
        self._lock_connection = None
        self._blocked_reason = "not_started"

    @property
    def accepting(self):
        return self._accepting and not self._stopping

    @property
    def active_jobs(self):
        with self._mutex:
            return len(self._futures)

    @property
    def active_executions(self):
        return self.active_jobs

    def start(self):
        with self._mutex:
            if self._stopping or self._lock_connection is not None:
                return self.accepting
            if not self.app.state.settings.retro_jobs_enabled:
                self._blocked_reason = "disabled"
                return False
            try:
                with service_context():
                    context = self.repository.connection_factory(self.repository.dsn)
                    connection = context.__enter__()
                    locked = connection.execute("SELECT pg_try_advisory_lock(1479045713,26) AS acquired").fetchone()["acquired"]
                    connection.commit()
                if not locked:
                    context.__exit__(None,None,None)
                    self._blocked_reason = "another_executor_active"
                    return False
                self._lock_context, self._lock_connection = context, connection
                self._accepting = self.repository.recover()
                self._blocked_reason = "" if self._accepting else "execution_verification_required"
                return self.accepting
            except Exception:
                logger.warning("retrosynthesis_manager_unavailable",exc_info=False)
                self._blocked_reason = "database_unavailable"
                self._close_lock()
                return False

    def _healthy(self):
        if not self.accepting or self._lock_connection is None:
            return False
        try:
            self._lock_connection.execute("SELECT 1")
            self._lock_connection.commit()
            return True
        except Exception:
            self._accepting = False
            self._blocked_reason = "executor_lock_lost"
            return False

    def create(self, owner, body, key):
        with self._mutex:
            if not self._healthy():
                raise HTTPException(503,detail={"code":"retrosynthesis_executor_unavailable"})
            row, created = self.repository.create(owner,body,key)
            if not created:
                return public_job(row)
            barrier, dispatch = Event(), {"committed":False}
            owner_scope = capture_job_owner_scope()
            try:
                if not self.repository.queued(row,self.instance_id):
                    raise RuntimeError("native dispatch rejected")
                future = submit_private_job(self._executor,self._run,row,barrier,dispatch,
                    channel="backend_gpu",task_type="retrosynthesis",task_id=str(row["job_id"]),
                    submitted_at=row["created_at"],on_disabled=lambda: None)
                self._futures[str(row["job_id"])] = future
                dispatch["committed"] = True
                future.add_done_callback(lambda done: self._done_owned(row,done,owner_scope))
            except Exception:
                # A Future may have been enqueued before submit raised; the
                # barrier prevents it from invoking the model in that case.
                self.repository.finish(row,self.instance_id,status="interrupted",error_code="submission_rejected")
                raise
            finally:
                barrier.set()
            return public_job(self.repository.get(owner,job_id=row["job_id"]))

    def _run(self,row,barrier,dispatch):
        barrier.wait()
        if not dispatch["committed"]:
            return
        status,result,error,facts = "failed",None,"inference_failed",None
        try:
            settings = self.app.state.settings
            # Include time spent waiting in the executor in the queue budget.
            elapsed = (datetime.now(timezone.utc)-row["created_at"]).total_seconds()
            remaining = max(0.0,float(settings.gpu_async_queue_timeout_seconds)-elapsed)
            if remaining <= 0:
                raise GpuQueueTimeoutError("Retrosynthesis queue deadline expired",model_name="retrosynthesis")
            with self.app.state.gpu_runtime_registry.inference_session("retrosynthesis",timeout_seconds=remaining) as runtime:
                with self._mutex:
                    healthy = self._healthy()
                if not healthy:
                    # We have not started compute, so a terminal no-execution
                    # proof is safe even after losing the manager connection.
                    status,error = "interrupted","executor_lock_lost_before_start"
                elif not self.repository.running(row,self.instance_id):
                    status,error = "interrupted","start_not_authorized"
                else:
                    body = MonomerRetrosynthesisRequest.model_validate({k:v for k,v in row["request_json"].items() if k != "source"})
                    try:
                        raw = predict_monomer_precursors(**body.model_dump(),model_id=settings.retro_model_id,device=settings.retro_device,runtime=runtime)
                        self.app.state.gpu_runtime_registry.record_inference_success("retrosynthesis")
                    except Exception as exc:
                        kind = self.app.state.gpu_runtime_registry.record_inference_failure("retrosynthesis",exc)
                        error = "gpu_out_of_memory" if kind == "oom" else "inference_failed"
                        raise
                    result = raw.model_dump(mode="json")
                    facts = _facts("retrosynthesis",row["request_json"],result)
                    result.pop("device",None)
                    # Preserve the old synchronous contract while returning true
                    # structure detection on the new async interface.
                    _, mol = canonical_target(body.smiles)
                    result["inferred_target_role"] = _infer_target_role(mol)
                    result["model_id"] = Path(settings.retro_model_id).name
                    for candidate in result["candidates"]:
                        candidate.pop("raw_output",None)
                    status,error = "completed",None
        except GpuQueueTimeoutError:
            status,error = "timed_out","gpu_queue_timeout"
        except GpuQueueFullError:
            status,error = "failed","gpu_queue_full"
        except GpuQueueStoppedError:
            status,error = "interrupted","gpu_queue_stopped"
        except HTTPException as exc:
            status,error = "interrupted","start_not_authorized" if exc.status_code == 403 else "execution_rejected"
        except ModelArtifactError:
            pass
        except Exception:
            logger.warning("retrosynthesis_execution_failed",exc_info=False)
        # Retry persistence, never inference. Keep the Future/admission live until
        # the terminal transaction is confirmed; process death becomes unknown.
        delay = 0.1
        while True:
            try:
                self.repository.finish(row,self.instance_id,status=status,result=result,error_code=error,facts=facts)
                return
            except Exception:
                logger.warning("retrosynthesis_result_commit_retry",exc_info=False)
                sleep(delay)
                delay = min(5.0,delay*2)

    def _done_owned(self,row,future,owner_scope):
        with owner_scope():
            self._done(row,future)

    def _done(self,row,future):
        try:
            if future.cancelled():
                self.repository.finish(row,self.instance_id,status="interrupted",error_code="shutdown_before_start")
            snapshot = self.repository.get(str(row["owner_user_id"]),job_id=row["job_id"])
            if not future.cancelled() and future.exception() is not None and snapshot and snapshot["status"] not in {"completed","failed","timed_out","interrupted"}:
                if snapshot["started_at"] is None:
                    self.repository.finish(row,self.instance_id,status="interrupted",error_code="executor_rejected_before_start")
                else:
                    self.repository.mark_unknown(row,self.instance_id)
                    self._accepting = False
                    self._blocked_reason = "execution_verification_required"
                snapshot = self.repository.get(str(row["owner_user_id"]),job_id=row["job_id"])
            if snapshot:
                finish_background_usage(future,SimpleNamespace(**public_job(snapshot)))
        except Exception:
            logger.warning("retrosynthesis_completion_observation_failed",exc_info=False)
        finally:
            with self._mutex:
                self._futures.pop(str(row["job_id"]),None)
                if self._stopping and not self._futures:
                    self._close_lock()

    def stop_accepting(self):
        with self._mutex:
            self._stopping = True
            self._accepting = False

    def cancel_pending(self):
        with self._mutex:
            futures = tuple(self._futures.values())
        for future in futures:
            future.cancel()
        return futures

    @staticmethod
    def wait_for_futures(futures,*,timeout_seconds):
        return not wait_futures(futures,timeout=timeout_seconds)[1] if futures else True

    def close_executor(self,*,wait=False):
        self._executor.shutdown(wait=wait,cancel_futures=True)
        with self._mutex:
            if not self._futures:
                self._close_lock()

    def _close_lock(self):
        context, self._lock_context = self._lock_context, None
        self._lock_connection = None
        if context is not None:
            try:
                context.__exit__(None,None,None)
            except Exception:
                pass
