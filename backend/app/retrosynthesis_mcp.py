"""Independent retrosynthesis MCP: HTTP-only native adapter and durable UI drafts."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx2 as httpx
from mcp import types
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import TypeAdapter, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.mcp_security import CallLimiter, MCPFailure, RequestGuard
from app.retrosynthesis_mcp_contract import (
    INPUT_MODELS, OUTPUT_MODELS, MAX_BACKEND_BYTES, MAX_RESPONSE_BYTES, TOOL_NAMES, TOOL_ORDER,
    CapabilitiesOutput, DraftOutput, ErrorDetail, ErrorOutput, JobInput, JobOutput, Parameters,
    PredictionResult, ResolveOutput, SubmissionOutput,
)
from app.retrosynthesis_mcp_journal import DraftError, JournalError, SubmissionJournal
from app.retrosynthesis_mcp_security import BackendSession, RetrosynthesisClientRegistry, RetrosynthesisMCPConfig

LOGGER = logging.getLogger("nexpoly.retrosynthesis_mcp.audit")
API = "/api/v1/monomer-retrosynthesis"
DESCRIPTIONS = {
    "get_retrosynthesis_capabilities": "Read single-step monomer precursor prediction availability, supported target inputs, parameter bounds and scientific limitations.",
    "resolve_retrosynthesis_target": "Resolve a concrete target from SMILES, a local cached name, or the authorized account's saved structure library. Ambiguous names return choices. This read-only tool never loads the prediction model or starts a job; never invent a molecular structure from a vague description.",
    "prepare_monomer_retrosynthesis": "Open a persistent monomer retrosynthesis parameter form in this conversation. Target and parameters may be incomplete. Use only the user's explicit target and parameters. Show the form and ask the user to review and confirm; this tool never submits a prediction job.",
    "get_retrosynthesis_draft": "Read the current form and its confirmed job receipt, including user edits. Only query a job after the user confirms it in the form. Recover uncertain submission by lookup without submitting another job.",
    "update_retrosynthesis_draft": "Trusted form-only draft edit with optimistic revision checking. Resolve the selected target and validate parameters without submitting a job. Null target clears the selection; null parameter resets its default.",
    "confirm_monomer_retrosynthesis": "Trusted UI-only confirmation of an exact complete draft revision. Freeze target and parameters and submit one idempotent native job. An explicit human retry first looks up the original receipt; if absent, resend exactly the frozen request and same key. Never create a replacement key or retry POST automatically.",
    "cancel_retrosynthesis_draft": "Cancel an unsubmitted parameter draft. This never cancels an already submitted prediction or GPU execution.",
    "get_retrosynthesis_job": "Read the persisted job confirmed in this conversation, optionally waiting up to 20 seconds. Disconnection never cancels computation. Results are single-step precursor candidates, not complete routes or experimental protocols.",
}
LIMITATIONS = [
    "Predicts single-step precursor candidates for one concrete monomer, not a complete multi-step synthesis route.",
    "Only SMILES, local cached names and the authorized account's saved structures are supported; vague descriptions require a concrete target.",
    "Local name matching does not guarantee coverage of Chinese common names, commercial names or CAS numbers.",
    "target_role is a purpose label and does not constrain model generation; detected structure role is reported separately.",
    "Candidate ordering is model output order, not a calibrated probability or experimental success rate.",
    "SMILES validity and smaller precursor size do not establish synthetic feasibility, conditions, yields or reagent availability.",
    "A completed job may contain zero candidates or invalid candidates; invalid candidates are labeled explicitly.",
    "Queue timeout does not impose a hard inference deadline; a disconnected client does not stop GPU work.",
    "Only unsubmitted drafts can be cancelled. Uncertain executions are not automatically rerun or refunded.",
]


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def error_result(failure: MCPFailure, request_id: str) -> types.CallToolResult:
    output = ErrorOutput(error=ErrorDetail(code=failure.code, message=failure.message, retryable=failure.retryable), request_id=request_id)
    return types.CallToolResult(content=[types.TextContent(type="text", text=f"{failure.code}: {failure.message}")],
                                structuredContent=output.model_dump(mode="json"), isError=True)


def tool_result(output: Any, request_id: str, *, protocol_request_id: Any = None) -> types.CallToolResult:
    output.request_id = request_id
    if isinstance(output, DraftOutput):
        summary = (f"Retrosynthesis form {output.draft_id}, revision {output.revision}: {output.status}. "
                   + (f"Confirmed job {output.job_id}; query this job." if output.job_id else
                      "Human review and confirmation in the form are required before prediction."))
    elif isinstance(output, JobOutput):
        summary = f"Job {output.job_id}: {output.status}; {output.result.total if output.result else 0} candidates. Snapshot: {output.snapshot_at}."
    elif isinstance(output, ResolveOutput):
        summary = "Target resolved; review its structure before submission." if output.target else "Choose or provide a concrete molecular structure."
    else:
        summary = "Retrosynthesis capabilities, defaults and scientific limitations."
    result = types.CallToolResult(content=[types.TextContent(type="text", text=summary)], structuredContent=output.model_dump(mode="json"))
    envelope = len(json.dumps({"jsonrpc": "2.0", "id": protocol_request_id}, ensure_ascii=False).encode()) + 1024
    if len(result.model_dump_json(by_alias=True, exclude_none=True).encode()) + envelope > MAX_RESPONSE_BYTES:
        raise MCPFailure("RESPONSE_TOO_LARGE", "Result exceeds 256 KiB. Reduce the target search limit; molecular strings were not truncated.")
    return result


class MCPRuntime:
    def __init__(self, config: RetrosynthesisMCPConfig, *, transport: Any = None):
        self.config = config
        self.client = httpx.AsyncClient(base_url=config.backend_url, follow_redirects=False, trust_env=False,
                                       timeout=httpx.Timeout(22, connect=5), transport=transport)
        self.limiter = CallLimiter(config)
        self.journal: SubmissionJournal | None = None
        self.ready = False

    def startup_check(self) -> None:
        RetrosynthesisClientRegistry.read(self.config.clients_file, tool_names=TOOL_NAMES)
        try:
            BackendSession.read(self.config)
        except MCPFailure as failure:
            if failure.code != "SESSION_EXPIRED":
                raise
        try:
            self.journal = SubmissionJournal(self.config.journal_file, backend_url=self.config.backend_url,
                                             account_uuid=self.config.account_uuid)
        except JournalError:
            self.journal = None
        self.ready = True

    async def close(self) -> None:
        self.ready = False
        await self.client.aclose()
        if self.journal:
            self.journal.close()

    def get_journal(self) -> SubmissionJournal:
        if self.journal is None or self.journal.failed:
            raise MCPFailure("JOURNAL_UNAVAILABLE", "Draft and submission history is unavailable; preserve it for operator recovery.")
        return self.journal

    async def request(self, method: str, path: str, session: BackendSession, *, body: dict | None = None,
                      idempotency_key: str | None = None) -> tuple[int, Any]:
        headers = session.headers(post=method == "POST")
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        async with self.client.stream(method, path, headers=headers, json=body) as response:
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_BACKEND_BYTES:
                    raise MCPFailure("BACKEND_RESPONSE_TOO_LARGE", "Backend response exceeds the adapter limit.")
            try:
                payload = json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeDecodeError):
                raise MCPFailure("BACKEND_INVALID_RESPONSE", "Backend returned invalid JSON.") from None
            return response.status_code, payload

    @staticmethod
    def check_status(status: int, payload: Any) -> None:
        if 200 <= status < 300:
            return
        code = payload.get("code") if isinstance(payload, dict) else None
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if code is None and isinstance(detail, dict):
            code = detail.get("code")
        if status == 401 or code in {"session_context_mismatch", "csrf_mismatch"}:
            raise MCPFailure("SESSION_EXPIRED", "Native session expired, changed or was revoked; renew the same authorized account.")
        if code == "structure_library_unavailable":
            raise MCPFailure("STRUCTURE_LIBRARY_UNAVAILABLE", "The authorized account's structure library is unavailable. Provide a concrete SMILES target instead.")
        if code in {"name_resolution_unavailable", "retrosynthesis_target_source_unavailable"}:
            raise MCPFailure("TARGET_SOURCE_UNAVAILABLE", "Local target lookup is unavailable. Provide a concrete SMILES target instead.")
        mapping = {
            403: ("PERMISSION_DENIED", "The authorized account lacks permission for this operation."),
            404: ("JOB_NOT_FOUND", "The requested owned record was not found."),
            409: ("BACKEND_CONFLICT", "Backend rejected an idempotency or state conflict."),
            422: ("INVALID_ARGUMENT", "Backend rejected the target or prediction parameters."),
            429: ("CAPACITY_LIMITED", "Backend capacity or rate limit reached."),
            503: ("MODEL_UNAVAILABLE", "Backend model, schema or scheduler is unavailable."),
        }
        raise MCPFailure(*mapping.get(status, ("BACKEND_UNAVAILABLE", "Backend request failed or redirected.")), retryable=status in {429, 503})

    async def session(self) -> BackendSession:
        session = BackendSession.read(self.config)
        status, payload = await self.request("GET", "/api/v1/auth/session", session)
        self.check_status(status, payload)
        if (not isinstance(payload, dict) or payload.get("authenticated") is not True
                or not isinstance(payload.get("user"), dict) or payload["user"].get("id") != self.config.account_uuid
                or payload.get("session_id") != session.session_id or payload.get("csrf_token") != session.csrf_token):
            raise MCPFailure("SESSION_IDENTITY_MISMATCH", "Native session does not match the pinned account and session.")
        if (payload.get("account_type") != "member" or payload["user"].get("must_change_password") is not False
                or not isinstance(payload.get("capabilities"), dict) or payload["capabilities"].get("retrosynthesis.use") is not True):
            raise MCPFailure("PERMISSION_DENIED", "The pinned account requires active membership, retrosynthesis permission and a completed password change.")
        return session

    async def resolve(self, target: Any, session: BackendSession) -> ResolveOutput:
        status, payload = await self.request("POST", API + "/targets/resolve", session, body=target.model_dump(exclude_none=True))
        self.check_status(status, payload)
        return ResolveOutput.model_validate(payload)

    @staticmethod
    def draft_output(item: dict, submission: SubmissionOutput | None = None) -> DraftOutput:
        payload = item["payload"]
        missing = [] if payload.get("resolved_target") else ["target"]
        return DraftOutput(draft_id=item["draft_id"], conversation_id=item["conversation_id"], revision=item["revision"],
            status=item["state"], ready=not missing and item["state"] != "cancelled", editable=item["state"] == "editing",
            target=payload.get("target"), resolved_target=payload.get("resolved_target"),
            target_candidates=payload.get("target_candidates", []), next_cursor=payload.get("next_cursor"),
            parameters=payload["parameters"], explicit_parameters=payload.get("explicit_parameters", []),
            parameter_fields=Parameters.model_json_schema()["properties"],
            missing_fields=missing, errors=[{"field": "target", "message": payload.get("target_message") or "请选择或输入一个明确的目标分子。"}] if missing else [],
            job_id=item["job_id"], submission_error=item["error_code"], submission=submission,
            created_at=item["created_at"], updated_at=item["updated_at"])

    async def draft_values(self, args: Any, session: BackendSession, previous: dict | None = None) -> dict:
        payload = json.loads(json.dumps(previous)) if previous else {
            "target": None, "resolved_target": None, "target_candidates": [], "next_cursor": None,
            "target_message": None, "parameters": Parameters().model_dump(), "explicit_parameters": []}
        if previous is None or "target" in args.model_fields_set:
            payload.update(target=None, resolved_target=None, target_candidates=[], next_cursor=None, target_message=None)
            if args.target is not None:
                resolution = await self.resolve(args.target, session)
                payload.update(target=args.target.model_dump(mode="json"),
                    resolved_target=resolution.target.model_dump(mode="json") if resolution.target else None,
                    target_candidates=[item.model_dump(mode="json") for item in resolution.candidates],
                    next_cursor=resolution.next_cursor, target_message=resolution.message)
        defaults = Parameters().model_dump()
        explicit = set(payload.get("explicit_parameters", []))
        edits = args.parameters.model_dump(exclude_unset=True)
        for name, value in edits.items():
            payload["parameters"][name] = defaults[name] if value is None else value
            explicit.discard(name) if value is None else explicit.add(name)
        if "num_beams" not in explicit:
            payload["parameters"]["num_beams"] = max(5, payload["parameters"]["num_return_sequences"])
        try:
            payload["parameters"] = Parameters.model_validate(payload["parameters"]).model_dump()
        except ValidationError:
            raise MCPFailure("INVALID_ARGUMENT", "Prediction parameters conflict; candidate count must not exceed beam count.") from None
        payload["explicit_parameters"] = sorted(explicit)
        return payload

    @staticmethod
    def submission_body(item: dict) -> dict:
        payload = item["payload"]
        target = payload["resolved_target"]
        return {"smiles": target["canonical_smiles"], **payload["parameters"], "source": target["source"]}

    def backend_key(self, owner_id: str, key: str) -> str:
        return hashlib.sha256(json.dumps([owner_id, self.config.account_uuid, key], separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def accepted_job(payload: Any) -> tuple[str, str]:
        if not isinstance(payload, dict):
            raise ValueError("Invalid job response")
        job = JobInput(conversation_id="validation", job_id=payload.get("job_id"))
        status = payload.get("status")
        if status not in {"pending", "queued", "running", "completed", "failed", "timed_out", "interrupted", "unknown"}:
            raise ValueError("Invalid job state")
        return job.job_id, status

    async def recover(self, owner_id: str, key: str, body: dict, session: BackendSession) -> SubmissionOutput:
        status, payload = await self.request("GET", API + "/jobs/lookup?idempotency_key=" + self.backend_key(owner_id, key), session)
        if status == 404:
            raise MCPFailure("SUBMISSION_NOT_RECEIVED", "No native receipt is currently visible. The user may explicitly retry this frozen draft using exactly the same request and key; do not create a replacement task.")
        self.check_status(status, payload)
        job_id, job_status = self.accepted_job(payload)
        self.get_journal().recover_submission(owner_id, self.config.account_uuid, key, job_id=job_id, job_status=job_status)
        return SubmissionOutput(job_id=job_id, status=job_status, replayed=True, effective_parameters=body)

    async def submit(self, item: dict, owner_id: str, session: BackendSession) -> SubmissionOutput:
        journal, account = self.get_journal(), self.config.account_uuid
        key = "draft:" + item["draft_id"] + ":" + str(item["revision"])
        body = self.submission_body(item)
        record, fresh = journal.begin(owner_id, account, key, body)
        if record.get("conflict"):
            raise MCPFailure("IDEMPOTENCY_CONFLICT", "This frozen draft key was already used with different parameters.")
        if not fresh:
            if record["state"] == "accepted":
                return SubmissionOutput(job_id=record["job_id"], status=record["job_status"], replayed=True, effective_parameters=body)
            if record["state"] == "capacity":
                raise MCPFailure("JOURNAL_CAPACITY_LIMITED", "Submission history is full; preserve it for operator migration.")
            if record["state"] == "rejected":
                raise MCPFailure(record.get("error_code") or "SUBMISSION_REJECTED", "This frozen submission was rejected; its original rejection is replayed.")
            try:
                return await self.recover(owner_id, key, body, session)
            except MCPFailure as failure:
                if failure.code != "SUBMISSION_NOT_RECEIVED":
                    raise
                # This path is entered only by the UI confirmation action. A
                # read or automatic recovery never reaches a second POST.
                # PostgreSQL owns durable deduplication even if an older POST
                # reaches the backend after this explicit retry.
        try:
            status, payload = await self.request("POST", API + "/jobs", session, body=body, idempotency_key=self.backend_key(owner_id, key))
            if status in {401, 403, 422, 429}:
                try:
                    self.check_status(status, payload)
                except MCPFailure as failure:
                    latest = journal.submission_record(owner_id, account, key)
                    if latest and latest["state"] == "accepted":
                        return SubmissionOutput(job_id=latest["job_id"], status=latest["job_status"], replayed=True, effective_parameters=body)
                    if fresh and latest and latest["state"] == "dispatching":
                        journal.finish(owner_id, account, key, state="rejected", error_code=failure.code)
                    raise
            if status not in {200, 202}:
                raise ValueError("No definite acceptance")
            job_id, job_status = self.accepted_job(payload)
            journal.recover_submission(owner_id, account, key, job_id=job_id, job_status=job_status)
            return SubmissionOutput(job_id=job_id, status=job_status, replayed=False, effective_parameters=body)
        except BaseException as failure:
            record = journal.submission_record(owner_id, account, key)
            if record and record["state"] == "rejected":
                raise
            if record and record["state"] == "dispatching":
                journal.finish(owner_id, account, key, state="outcome_unknown", error_code="SUBMISSION_OUTCOME_UNKNOWN")
            if isinstance(failure, asyncio.CancelledError):
                raise
            if record and record["state"] == "accepted":
                return SubmissionOutput(job_id=record["job_id"], status=record["job_status"], replayed=True, effective_parameters=body)
            try:
                return await self.recover(owner_id, key, body, session)
            except MCPFailure as recovery:
                if recovery.code == "SUBMISSION_NOT_RECEIVED":
                    raise
                raise MCPFailure("SUBMISSION_OUTCOME_UNKNOWN", "Submission receipt is uncertain. Keep this frozen draft and key; use lookup and never create a replacement task.") from None
            except (httpx.HTTPError, ValidationError, ValueError):
                raise MCPFailure("SUBMISSION_OUTCOME_UNKNOWN", "Submission receipt is uncertain. Keep this frozen draft and key; use lookup and never create a replacement task.") from None

    async def execute_draft(self, name: str, args: Any, owner_id: str, session: BackendSession) -> DraftOutput:
        journal, account = self.get_journal(), self.config.account_uuid
        if name == "prepare_monomer_retrosynthesis":
            return self.draft_output(journal.create_draft(owner_id, account, args.conversation_id, await self.draft_values(args, session)))
        item = journal.read_draft(owner_id, account, args.conversation_id, args.draft_id)
        if name == "get_retrosynthesis_draft":
            if item["state"] not in {"editing", "cancelled", "rejected"}:
                key = "draft:" + args.draft_id + ":" + str(item["revision"])
                record = journal.submission_record(owner_id, account, key)
                if record is not None:
                    submission = None
                    if record["state"] == "accepted":
                        submission = SubmissionOutput(job_id=record["job_id"], status=record["job_status"], replayed=True, effective_parameters=self.submission_body(item))
                    elif record["state"] in {"dispatching", "outcome_unknown"}:
                        try:
                            submission = await self.recover(owner_id, key, self.submission_body(item), session)
                        except MCPFailure as failure:
                            if failure.code not in {"SUBMISSION_OUTCOME_UNKNOWN", "SUBMISSION_NOT_RECEIVED"}:
                                raise
                            item = journal.finish_draft(owner_id, account, args.conversation_id, args.draft_id,
                                state="submission_unknown", error_code=failure.code)
                    if submission:
                        item = journal.finish_draft(owner_id, account, args.conversation_id, args.draft_id, state="submitted", job_id=submission.job_id)
                        return self.draft_output(item, submission)
                    if record["state"] == "rejected":
                        item = journal.finish_draft(owner_id, account, args.conversation_id, args.draft_id, state="rejected", error_code=record["error_code"])
            return self.draft_output(item)
        journal._check_revision(item, args.revision)
        if name == "update_retrosynthesis_draft":
            if item["state"] != "editing":
                raise MCPFailure("DRAFT_LOCKED", "Only an unconfirmed draft can be edited.")
            payload = await self.draft_values(args, session, item["payload"])
            return self.draft_output(journal.update_draft(owner_id, account, args.conversation_id, args.draft_id, args.revision, payload))
        if name == "cancel_retrosynthesis_draft":
            return self.draft_output(journal.cancel_draft(owner_id, account, args.conversation_id, args.draft_id, args.revision))
        if not self.draft_output(item).ready:
            raise MCPFailure("DRAFT_INCOMPLETE", "Select one validated target structure before confirming.")
        item = journal.confirm_draft(owner_id, account, args.conversation_id, args.draft_id, args.revision)
        try:
            submission = await self.submit(item, owner_id, session)
        except MCPFailure as failure:
            journal.finish_draft(owner_id, account, args.conversation_id, args.draft_id,
                state="submission_unknown" if failure.code in {"SUBMISSION_OUTCOME_UNKNOWN", "SUBMISSION_NOT_RECEIVED"} else "rejected", error_code=failure.code)
            raise
        return self.draft_output(journal.finish_draft(owner_id, account, args.conversation_id, args.draft_id,
                                state="submitted", job_id=submission.job_id), submission)

    async def execute(self, name: str, args: Any, client_id: str) -> Any:
        registry = RetrosynthesisClientRegistry.read(self.config.clients_file, tool_names=TOOL_NAMES)
        credential = next((client for client in registry.clients if client.client_id == client_id), None)
        if credential is None or name not in credential.tools:
            raise MCPFailure("PERMISSION_DENIED", "This MCP client lacks permission for this tool.")
        owner_id = registry.owner_for(client_id)
        session = await self.session()
        if name in {"prepare_monomer_retrosynthesis", "get_retrosynthesis_draft", "update_retrosynthesis_draft", "confirm_monomer_retrosynthesis", "cancel_retrosynthesis_draft"}:
            return await self.execute_draft(name, args, owner_id, session)
        if name == "resolve_retrosynthesis_target":
            return await self.resolve(args.target, session)
        if name == "get_retrosynthesis_capabilities":
            status, payload = await self.request("GET", API + "/status", session)
            self.check_status(status, payload)
            if not isinstance(payload, dict):
                raise ValueError("Invalid capabilities")
            fields = {name: payload[name] for name in ("enabled", "available", "schema_ready", "accepting", "model_available", "name_resolution_available", "structure_library_available")}
            return CapabilitiesOutput(**fields, submission_ready=bool(payload["available"] and payload["accepting"] and self.journal and not self.journal.failed),
                parameters=Parameters.model_json_schema()["properties"], limitations=LIMITATIONS)
        assert isinstance(args, JobInput)
        if not self.get_journal().owns_conversation_job(owner_id, self.config.account_uuid, args.conversation_id, args.job_id):
            raise MCPFailure("JOB_NOT_FOUND", "No confirmed task belongs to this client and conversation.")
        status, payload = await self.request("GET", API + "/jobs/" + args.job_id + "?wait_seconds=" + str(args.wait_seconds), session)
        self.check_status(status, payload)
        job_id, job_status = self.accepted_job(payload)
        if job_id != args.job_id:
            raise MCPFailure("BACKEND_INVALID_RESPONSE", "Backend returned a different job identifier.")
        result = payload.get("result")
        if result is not None:
            result = dict(result)
            result.pop("device", None)
            result.pop("detected_target_role", None)
            model_id = result.get("model_id", "")
            if isinstance(model_id, str) and (model_id.startswith(("/", ".")) or "\\" in model_id or ":" in model_id):
                result["model_id"] = "ReactionT5v2-retrosynthesis-USPTO_50k"
            result["candidates"] = [{key: value for key, value in candidate.items() if key != "raw_output"} for candidate in result["candidates"]]
            result = PredictionResult.model_validate(result)
        error_code = payload.get("error_code")
        # Only stable public codes are exposed; never forward backend exception text.
        public_error = error_code if error_code in {"gpu_queue_timeout", "gpu_queue_full", "gpu_queue_stopped",
            "gpu_out_of_memory", "inference_failed", "usage_outcome_unknown", "result_too_large", "submission_rejected",
            "shutdown_before_start", "executor_rejected_before_start", "executor_lock_lost_before_start", "start_not_authorized",
            "execution_rejected", "executor_outcome_unknown", "executor_interrupted", "not_started_before_restart"} else ("PREDICTION_FAILED" if error_code else None)
        warnings = []
        if job_status == "completed" and result and not result.total:
            warnings.append("Prediction completed without a precursor candidate.")
        if job_status == "unknown":
            warnings.append("Execution outcome requires operator verification; do not submit a replacement task.")
        return JobOutput(job_id=job_id, status=job_status, stage=payload["stage"], created_at=payload["created_at"],
            updated_at=payload["updated_at"], started_at=payload.get("started_at"), finished_at=payload.get("finished_at"),
            snapshot_at=timestamp(), error_code=public_error, result=result, warnings=warnings)

    async def call(self, context: Any, parameters: Any) -> types.CallToolResult:
        started, acquired = time.perf_counter(), False
        client = context.request.state.mcp_client
        request_id = context.request.state.mcp_request_id
        name, code = parameters.name, "ok"
        try:
            if name not in TOOL_NAMES:
                raise MCPFailure("UNKNOWN_TOOL", "Unknown tool; discover available tools first.")
            if name not in client.tools:
                raise MCPFailure("PERMISSION_DENIED", "This MCP client lacks permission for this tool.")
            try:
                args = INPUT_MODELS[name].model_validate(parameters.arguments or {})
            except ValidationError:
                raise MCPFailure("INVALID_ARGUMENT", "Arguments must match the published strict schema and parameter bounds.") from None
            self.limiter.acquire(client.client_id)
            acquired = True
            async with asyncio.timeout(self.config.tool_timeout_seconds):
                output = await self.execute(name, args, client.client_id)
            return tool_result(output, request_id, protocol_request_id=getattr(context, "request_id", None))
        except TimeoutError:
            code = "QUERY_TIMEOUT"
            return error_result(MCPFailure(code, "Tool wait budget expired. Query the same draft or job; do not create a replacement submission."), request_id)
        except asyncio.CancelledError:
            code = "CALL_CANCELLED"
            raise
        except DraftError as failure:
            code = failure.code
            return error_result(MCPFailure(code, failure.message), request_id)
        except JournalError:
            code = "JOURNAL_UNAVAILABLE"
            return error_result(MCPFailure(code, "Draft history is unavailable; submissions are disabled pending operator recovery."), request_id)
        except MCPFailure as failure:
            code = failure.code
            return error_result(failure, request_id)
        except (httpx.HTTPError, ValidationError, ValueError, KeyError, TypeError):
            code = "BACKEND_UNAVAILABLE"
            return error_result(MCPFailure(code, "Backend communication or response validation failed; existing jobs were not cancelled."), request_id)
        except Exception:
            code = "INTERNAL_ERROR"
            return error_result(MCPFailure(code, "Tool could not complete; contact the operator with the request identifier."), request_id)
        finally:
            if acquired:
                self.limiter.release(client.client_id)
            LOGGER.info(json.dumps({"client_id": client.client_id, "request_id": request_id,
                "tool": name if name in TOOL_NAMES else "unknown", "status": code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2)}, separators=(",", ":")))


def create_app(config: RetrosynthesisMCPConfig | None = None, *, transport: Any = None) -> Any:
    config = config or RetrosynthesisMCPConfig.from_env()
    runtime = MCPRuntime(config, transport=transport)

    async def list_tools(context: Any, parameters: Any) -> types.ListToolsResult:
        result = []
        for name in TOOL_ORDER:
            if name not in context.request.state.mcp_client.tools:
                continue
            schema = TypeAdapter(OUTPUT_MODELS[name] | ErrorOutput).json_schema()
            schema["type"] = "object"
            result.append(types.Tool(name=name, description=DESCRIPTIONS[name], inputSchema=INPUT_MODELS[name].model_json_schema(),
                outputSchema=schema, annotations=types.ToolAnnotations(readOnlyHint=name in {
                    "get_retrosynthesis_capabilities", "resolve_retrosynthesis_target", "get_retrosynthesis_draft", "get_retrosynthesis_job"},
                destructiveHint=False, idempotentHint=name != "prepare_monomer_retrosynthesis", openWorldHint=False)))
        return types.ListToolsResult(tools=result)

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok" if runtime.ready else "unavailable",
            "submission_ready": runtime.journal is not None and not runtime.journal.failed}, status_code=200 if runtime.ready else 503)

    server = Server("nexpoly-retrosynthesis", version="1.0.0", on_list_tools=list_tools, on_call_tool=runtime.call,
        instructions="Single-step monomer precursor prediction. Read capabilities, resolve a concrete target, then prepare a parameter form. The human must review and confirm in the form. No model-accessible tool can submit or confirm a job. Read the draft receipt and query only its confirmed job in the same conversation. Never invent SMILES from vague descriptions, replace uncertain submissions, or claim experimental feasibility.")
    server.middleware.clear()
    app = server.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True,
        max_request_body_size=32 * 1024, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=list(config.allowed_hosts), allowed_origins=list(config.allowed_origins)),
        custom_starlette_routes=[Route("/healthz", health, methods=["GET"])])
    sdk_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Any):
        try:
            runtime.startup_check()
        except Exception:
            await runtime.close()
            raise RuntimeError("Retrosynthesis MCP startup checks failed; verify private files and pinned account configuration.") from None
        try:
            async with sdk_lifespan(application):
                yield
        finally:
            await runtime.close()

    app.router.lifespan_context = lifespan
    app.add_middleware(RequestGuard, config=config, tool_names=TOOL_NAMES, registry_type=RetrosynthesisClientRegistry)
    app.state.mcp_runtime = runtime
    return app


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    LOGGER.setLevel(logging.INFO)
    try:
        config = RetrosynthesisMCPConfig.from_env()
    except (OSError, ValueError):
        raise SystemExit("Invalid retrosynthesis MCP configuration; verify explicit backend and private files.") from None
    uvicorn.run(create_app(config), host=config.host, port=config.port, access_log=False,
                log_level="warning", proxy_headers=False, timeout_keep_alive=5)


if __name__ == "__main__":
    main()
