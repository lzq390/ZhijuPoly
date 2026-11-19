"""Independent PolyTAO HTTP MCP bridge; no scientific runtime or database access."""
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

from app.models import (POLYTAO_DESCRIPTOR_NAMES, PolytaoDescriptorResponse,
                        PolytaoJobCreateResponse, PolytaoJobStatusResponse, PolytaoStatusResponse)
from app.mcp_security import CallLimiter, MCPFailure, RequestGuard
from app.polytao_mcp_contract import (INPUT_MODELS, OUTPUT_MODELS, MAX_BACKEND_BYTES, MAX_RESPONSE_BYTES,
    TOOL_NAMES, TOOL_ORDER, DESCRIPTOR_DESCRIPTIONS, DESCRIPTOR_LABELS, DESCRIPTOR_HELP, CandidateOutput, CapabilitiesOutput, DescriptorOutput,
    DraftOutput, ErrorDetail, ErrorOutput, GenerationParameters, JobInput, JobOutput, SubmitInput, SubmitOutput)
from app.polytao_mcp_journal import DraftError, JournalError, SubmissionJournal
from app.polytao_mcp_security import BackendSession, PolytaoClientRegistry, PolytaoMCPConfig

LOGGER = logging.getLogger("nexpoly.polytao_mcp.audit")
API = "/api/v1/conditional-generation/polytao"
TERMINAL = frozenset({"completed", "failed", "cancelled"})
DESCRIPTIONS = {
    "get_polytao_capabilities": "Read PolyTAO availability, required descriptor names, sampling bounds and scientific limitations before generating.",
    "calculate_polytao_descriptors": "Extract all 15 RDKit descriptors from a reference SMILES. The reference provides descriptor conditions; its scaffold is not preserved by generation.",
    "prepare_polytao_generation": "Open a persistent polymer generation parameter form for this conversation. Supply only descriptors explicitly given by the user, or an optional reference_smiles to calculate them. Missing descriptors remain blank, never guessed or zero-filled. This tool never submits a generation job. Ask the user to review and confirm the form.",
    "get_polytao_draft": "Read the current parameter form, including user edits and any confirmed generation receipt. Only after human confirmation returns a job_id should you continue by querying that job.",
    "update_polytao_draft": "Update a draft from the trusted parameter form with optimistic revision checking. This UI-only tool never submits a job. Null descriptor values clear those fields. An optional reference SMILES fills descriptors before applying explicit edits.",
    "confirm_polytao_generation": "Trusted UI confirmation of the exact complete draft revision. Freeze its parameters and submit at most one generation job. Repeat the same revision to recover its existing receipt; never replace an uncertain submission.",
    "cancel_polytao_draft": "Cancel an unconfirmed parameter draft without creating a generation job. This does not cancel already confirmed GPU jobs.",
    "get_polytao_job": "Read a job submitted by this MCP client; optionally wait up to 20 seconds. Return a snapshot and paged candidates. Waiting or disconnecting never cancels GPU work. Rank is acceptance order, not quality.",
}
LIMITATIONS = [
    "Reference structures provide descriptors; generated structures need not retain their scaffold.",
    "Descriptor values condition stochastic generation; descriptor matching error is not calculated.",
    "RDKit parseability and at least two connection points do not establish synthetic feasibility.",
    "Rank is acceptance order. SA score is not synthesis success probability.",
    "Completed jobs may contain zero or fewer candidates than requested. attempts is a legacy value, usually 1.",
    "Jobs and results use a bounded in-process backend store and may disappear after restart or eviction.",
    "Progress is stage-based. query_time_ms excludes queueing, initial model loading and SVG rendering.",
    "Development uses one dedicated account. Backend usage settlement is not implied by this adapter.",
    "Idempotency provides at most one HTTP submission attempt per client/account/key, not exactly-once execution.",
]


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def error_result(failure: MCPFailure, request_id: str) -> types.CallToolResult:
    output = ErrorOutput(error=ErrorDetail(code=failure.code, message=failure.message, retryable=failure.retryable), request_id=request_id)
    return types.CallToolResult(content=[types.TextContent(type="text", text=f"{failure.code}: {failure.message}")],
                                structuredContent=output.model_dump(mode="json"), isError=True)


def tool_result(output: Any, request_id: str, *, protocol_request_id: Any = None) -> types.CallToolResult:
    output.request_id = request_id
    if isinstance(output, JobOutput):
        summary = f"Job {output.job_id}: {output.status}; {output.returned_count}/{output.requested_count} candidates, {len(output.results)} on this page. Snapshot: {output.snapshot_at}."
    elif isinstance(output, SubmitOutput):
        summary = f"Job {output.job_id} accepted; {'replayed existing submission' if output.replayed else 'query this job for results'}."
    elif isinstance(output, DescriptorOutput):
        summary = "Calculated all 15 descriptor conditions. The reference scaffold is not guaranteed in generated candidates."
    elif isinstance(output, DraftOutput):
        summary = (f"Generation form {output.draft_id}, revision {output.revision}: {output.status}; "
                   + (f"confirmed job {output.job_id}. Query this job for results." if output.job_id else
                      f"{len(output.missing_fields)} descriptor fields missing. Human confirmation in the form is required before generation."))
    else:
        summary = "PolyTAO capabilities and generation limitations."
    result = types.CallToolResult(content=[types.TextContent(type="text", text=summary)], structuredContent=output.model_dump(mode="json"))
    envelope_bytes = len(json.dumps({"jsonrpc": "2.0", "id": protocol_request_id}, ensure_ascii=False).encode()) + 1024
    if len(result.model_dump_json(by_alias=True, exclude_none=True).encode()) + envelope_bytes > MAX_RESPONSE_BYTES:
        raise MCPFailure("RESPONSE_TOO_LARGE", "Result exceeds 256 KiB; request a smaller candidate page. SMILES were not truncated.")
    return result


class MCPRuntime:
    def __init__(self, config: PolytaoMCPConfig, *, transport: Any = None):
        self.config = config
        self.client = httpx.AsyncClient(base_url=config.backend_url, follow_redirects=False, trust_env=False,
                                       timeout=httpx.Timeout(20, connect=5), transport=transport)
        self.limiter = CallLimiter(config)
        self.journal: SubmissionJournal | None = None
        self.ready = False

    def startup_check(self) -> None:
        PolytaoClientRegistry.read(self.config.clients_file, tool_names=TOOL_NAMES)
        # Expired sessions must be renewable without restarting the service.
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

    async def request(self, method: str, path: str, session: BackendSession, *, body: dict | None = None,
                      idempotency_key: str | None = None) -> tuple[int, Any]:
        headers = session.headers(post=method == "POST")
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        async with self.client.stream(method, path, headers=headers, json=body) as response:
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_BACKEND_BYTES:
                    raise MCPFailure("BACKEND_RESPONSE_TOO_LARGE", "Backend response exceeds the adapter's 8 MiB limit.")
            try:
                payload = json.loads(content, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeDecodeError):
                raise MCPFailure("BACKEND_INVALID_RESPONSE", "Backend returned an invalid JSON response.") from None
            return response.status_code, payload

    @staticmethod
    def check_status(status: int, payload: Any) -> None:
        if 200 <= status < 300:
            return
        code = None
        if isinstance(payload, dict):
            detail = payload.get("error", payload.get("detail"))
            if isinstance(detail, dict):
                code = detail.get("code")
        # Native authentication middleware returns {detail,code}.
        if isinstance(payload, dict):
            code = payload.get("code", code)
        if code == "usage_outcome_unknown":
            raise MCPFailure("USAGE_OUTCOME_UNKNOWN", "Backend reports an uncertain usage outcome; do not automatically submit again.")
        if status == 401 or code in {"session_context_mismatch", "csrf_mismatch"}:
            raise MCPFailure("SESSION_EXPIRED", "Development session is invalid, revoked or changed; renew the same account.")
        mapping = {403: ("PERMISSION_DENIED", "Development account lacks permission for this operation."),
                   404: ("JOB_NOT_FOUND", "Job is absent; it may have been lost after backend restart or eviction."),
                   410: ("RESULT_EXPIRED", "Backend job result expired."),
                   409: ("BACKEND_CONFLICT", "Backend rejected the request because of a conflict."),
                   422: ("INVALID_ARGUMENT", "Backend rejected the SMILES or generation arguments."),
                   429: ("CAPACITY_LIMITED", "Backend capacity or request rate limit reached."),
                   503: ("MODEL_UNAVAILABLE", "Backend model or scheduler is unavailable.")}
        mapped = mapping.get(status, ("BACKEND_UNAVAILABLE", "Backend request failed or redirected; contact the operator."))
        raise MCPFailure(*mapped, retryable=status in {429, 503})

    async def session(self) -> BackendSession:
        session = BackendSession.read(self.config)
        status, payload = await self.request("GET", "/api/v1/auth/session", session)
        self.check_status(status, payload)
        if (not isinstance(payload, dict) or payload.get("authenticated") is not True
                or not isinstance(payload.get("user"), dict)
                or payload["user"].get("id") != self.config.account_uuid
                or payload.get("session_id") != session.session_id
                or payload.get("csrf_token") != session.csrf_token):
            raise MCPFailure("SESSION_IDENTITY_MISMATCH", "Backend session does not match the pinned development account and session.")
        if (payload.get("account_type") != "member" or payload["user"].get("must_change_password") is not False
                or not isinstance(payload.get("capabilities"), dict)
                or payload["capabilities"].get("polytao.use") is not True):
            raise MCPFailure("PERMISSION_DENIED", "The pinned development account requires active member access, PolyTAO permission and a completed password change.")
        return session

    def get_journal(self) -> SubmissionJournal:
        if self.journal is None or self.journal.failed:
            raise MCPFailure("JOURNAL_UNAVAILABLE", "Submission history is unavailable; submissions are disabled until the operator recovers it.")
        return self.journal

    async def submit(self, args: SubmitInput, client_id: str, session: BackendSession) -> SubmitOutput:
        journal = self.get_journal()
        body = args.backend_body()
        record, fresh = journal.begin(client_id, self.config.account_uuid, args.idempotency_key, body)
        if record.get("conflict"):
            raise MCPFailure("IDEMPOTENCY_CONFLICT", "This key was already used with different generation parameters.")
        if not fresh:
            if record["state"] == "accepted":
                return SubmitOutput(job_id=record["job_id"], status=record["job_status"], replayed=True, effective_parameters=body)
            errors = {"dispatching": ("SUBMISSION_PENDING", "The original submission is being confirmed; query again using the same key."),
                      "capacity": ("JOURNAL_CAPACITY_LIMITED", "Submission history is full; an operator must preserve and migrate it."),
                      "rejected": (record.get("error_code") or "SUBMISSION_REJECTED", "This submission was definitively rejected before execution; the original rejection is replayed."),
                      "outcome_unknown": ("SUBMISSION_OUTCOME_UNKNOWN", "The original submission outcome is unknown. Do not automatically submit again or change the key.")}
            raise MCPFailure(*errors.get(record["state"], errors["outcome_unknown"]))
        rejected = False
        try:
            # Namespace backend keys too: future backend deduplication must not
            # conflate two MCP clients choosing the same user-facing key.
            backend_key = hashlib.sha256(json.dumps([client_id, self.config.account_uuid, args.idempotency_key], separators=(",", ":")).encode()).hexdigest()
            status, payload = await self.request("POST", API + "/jobs", session, body=body, idempotency_key=backend_key)
            # Only known execution-before-admission refusals can be replayed as rejected.
            if status in {401, 403, 404, 422, 429}:
                try:
                    self.check_status(status, payload)
                except MCPFailure as failure:
                    journal.finish(client_id, self.config.account_uuid, args.idempotency_key,
                                   state="rejected", error_code=failure.code)
                    rejected = True
                    raise
            if status != 202:
                raise MCPFailure("SUBMISSION_OUTCOME_UNKNOWN", "Backend did not confirm acceptance. Do not automatically submit again or change the key.")
            accepted = PolytaoJobCreateResponse.model_validate(payload)
            JobInput(job_id=accepted.job_id)  # Prevent a backend ID from becoming a path injection later.
            journal.finish(client_id, self.config.account_uuid, args.idempotency_key,
                           state="accepted", job_id=accepted.job_id, job_status=accepted.status)
            return SubmitOutput(job_id=accepted.job_id, status=accepted.status, replayed=False, effective_parameters=body)
        except BaseException as failure:
            # Includes cancellation and process shutdown. A killed process is recovered on startup.
            if rejected:
                raise
            try:
                journal.finish(client_id, self.config.account_uuid, args.idempotency_key,
                               state="outcome_unknown", error_code="SUBMISSION_OUTCOME_UNKNOWN")
            except JournalError:
                pass
            raise MCPFailure("SUBMISSION_OUTCOME_UNKNOWN", "Submission outcome is unknown. Do not automatically resubmit or change the key; contact the operator.") from None

    async def descriptors(self, smiles: str, session: BackendSession) -> DescriptorOutput:
        status, payload = await self.request("POST", API + "/descriptors", session, body={"smiles": smiles})
        self.check_status(status, payload)
        result = PolytaoDescriptorResponse.model_validate(payload)
        if len(result.descriptors) != len(POLYTAO_DESCRIPTOR_NAMES) or {item.name for item in result.descriptors} != set(POLYTAO_DESCRIPTOR_NAMES):
            raise MCPFailure("BACKEND_INVALID_RESPONSE", "Backend returned an incomplete or duplicate descriptor vector.")
        return DescriptorOutput(input_smiles=result.input_smiles, canonical_smiles=result.canonical_smiles,
                                descriptors={item.name: item.value for item in result.descriptors}, query_time_ms=result.query_time_ms)

    @staticmethod
    def draft_output(item: dict, submission: SubmitOutput | None = None) -> DraftOutput:
        payload = item["payload"]
        missing = [name for name in POLYTAO_DESCRIPTOR_NAMES if name not in payload["descriptors"]]
        return DraftOutput(draft_id=item["draft_id"], conversation_id=item["conversation_id"], revision=item["revision"],
            status=item["state"], ready=not missing and item["state"] != "cancelled", editable=item["state"] == "editing",
            descriptors=payload["descriptors"], descriptor_sources=payload["descriptor_sources"], parameters=payload["parameters"],
            missing_fields=missing, errors=[{"field": name, "message": f"请填写{DESCRIPTOR_LABELS[name]}，未知值不会自动补零。"} for name in missing],
            descriptor_fields=[{"name": name, "label": DESCRIPTOR_LABELS[name], "description": DESCRIPTOR_HELP[name],
                "unit": "g/mol" if name == "MolWt" else None, "required": True} for name in POLYTAO_DESCRIPTOR_NAMES],
            parameter_fields=GenerationParameters.model_json_schema()["properties"], reference_smiles=payload.get("reference_smiles"),
            canonical_smiles=payload.get("canonical_smiles"), job_id=item["job_id"], submission_error=item["error_code"],
            submission=submission, created_at=item["created_at"], updated_at=item["updated_at"])

    async def draft_values(self, args: Any, session: BackendSession, previous: dict | None = None) -> dict:
        payload = json.loads(json.dumps(previous)) if previous is not None else {
            "descriptors": {}, "descriptor_sources": {}, "parameters": GenerationParameters().model_dump(),
            "reference_smiles": None, "canonical_smiles": None}
        if previous is None or "reference_smiles" in args.model_fields_set:
            if args.reference_smiles is None:
                for name in list(payload["descriptors"]):
                    if payload["descriptor_sources"].get(name) == "reference":
                        payload["descriptors"].pop(name, None)
                        payload["descriptor_sources"].pop(name, None)
                payload["reference_smiles"], payload["canonical_smiles"] = None, None
            else:
                extracted = await self.descriptors(args.reference_smiles, session)
                payload["descriptors"] = extracted.descriptors.model_dump()
                payload["descriptor_sources"] = {name: "reference" for name in POLYTAO_DESCRIPTOR_NAMES}
                payload["reference_smiles"], payload["canonical_smiles"] = args.reference_smiles, extracted.canonical_smiles
        for name, value in args.descriptors.model_dump(exclude_unset=True).items():
            if value is None:
                payload["descriptors"].pop(name, None)
                payload["descriptor_sources"].pop(name, None)
            else:
                payload["descriptors"][name] = value
                payload["descriptor_sources"][name] = "user"
        if previous is None:
            payload["parameters"] = args.model_dump(include=set(GenerationParameters.model_fields))
        else:
            defaults = GenerationParameters().model_dump()
            for name, value in args.parameters.model_dump(exclude_unset=True).items():
                payload["parameters"][name] = defaults[name] if value is None else value
        payload["parameters"] = GenerationParameters.model_validate(payload["parameters"]).model_dump()
        return payload

    async def execute_draft(self, name: str, args: Any, owner_id: str, session: BackendSession) -> DraftOutput:
        journal, account = self.get_journal(), self.config.account_uuid
        if name == "prepare_polytao_generation":
            payload = await self.draft_values(args, session)
            return self.draft_output(journal.create_draft(owner_id, account, args.conversation_id, payload))
        item = journal.read_draft(owner_id, account, args.conversation_id, args.draft_id)
        if name == "get_polytao_draft":
            # The native acceptance may have been durably journaled just before
            # process death prevented the draft receipt write. Recover from that
            # authoritative record without ever sending a submission request.
            if item["state"] not in {"editing", "cancelled"}:
                record = journal.submission_record(owner_id, account,
                    "draft:" + args.draft_id + ":" + str(item["revision"]))
                if record is not None and record["state"] == "accepted":
                    if item["state"] != "submitted" or item["job_id"] != record["job_id"]:
                        item = journal.finish_draft(owner_id, account, args.conversation_id, args.draft_id,
                                                    state="submitted", job_id=record["job_id"])
                    values = item["payload"]
                    return self.draft_output(item, SubmitOutput(job_id=record["job_id"], status=record["job_status"],
                        replayed=True, effective_parameters={"descriptors": values["descriptors"], **values["parameters"]}))
                if record is not None and record["state"] in {"outcome_unknown", "rejected"}:
                    state = "submission_unknown" if record["state"] == "outcome_unknown" else "rejected"
                    if item["state"] != state or item["error_code"] != record["error_code"]:
                        item = journal.finish_draft(owner_id, account, args.conversation_id, args.draft_id,
                            state=state, error_code=record["error_code"])
            return self.draft_output(item)
        if name == "update_polytao_draft":
            journal._check_revision(item, args.revision)
            if item["state"] != "editing":
                raise MCPFailure("DRAFT_LOCKED", "Only an unconfirmed draft can be edited.")
            payload = await self.draft_values(args, session, item["payload"])
            return self.draft_output(journal.update_draft(owner_id, account, args.conversation_id, args.draft_id, args.revision, payload))
        if name == "cancel_polytao_draft":
            return self.draft_output(journal.cancel_draft(owner_id, account, args.conversation_id, args.draft_id, args.revision))
        assert name == "confirm_polytao_generation"
        journal._check_revision(item, args.revision)
        if item["state"] == "cancelled":
            raise MCPFailure("DRAFT_CANCELLED", "This draft was cancelled without creating a generation task.")
        if self.draft_output(item).missing_fields:
            raise MCPFailure("DRAFT_INCOMPLETE", "Fill all 15 descriptor values before confirming. Missing values are never inferred or zero-filled.")
        item = journal.confirm_draft(owner_id, account, args.conversation_id, args.draft_id, args.revision)
        values = item["payload"]
        dispatched = SubmitInput(descriptors=values["descriptors"], **values["parameters"],
                                 idempotency_key="draft:" + args.draft_id + ":" + str(args.revision))
        try:
            submission = await self.submit(dispatched, owner_id, session)
        except MCPFailure as failure:
            if failure.code not in {"SUBMISSION_PENDING", "JOURNAL_UNAVAILABLE"}:
                journal.finish_draft(owner_id, account, args.conversation_id, args.draft_id,
                    state="submission_unknown" if failure.code == "SUBMISSION_OUTCOME_UNKNOWN" else "rejected", error_code=failure.code)
            raise
        item = journal.finish_draft(owner_id, account, args.conversation_id, args.draft_id,
                                    state="submitted", job_id=submission.job_id)
        return self.draft_output(item, submission)

    async def execute(self, name: str, args: Any, client_id: str) -> Any:
        registry = PolytaoClientRegistry.read(self.config.clients_file, tool_names=TOOL_NAMES)
        credential = next((item for item in registry.clients if item.client_id == client_id), None)
        if credential is None or name not in credential.tools:
            raise MCPFailure("PERMISSION_DENIED", "This MCP client lacks permission for this tool.")
        owner_id = registry.owner_for(client_id)
        session = await self.session()
        return await self.execute_for_owner(name, args, owner_id, session, client_id=client_id)

    async def execute_for_owner(self, name: str, args: Any, owner_id: str, session: BackendSession, *, client_id: str) -> Any:
        """Execute after the request transport has established its native owner."""
        if name in {"prepare_polytao_generation", "get_polytao_draft", "update_polytao_draft", "confirm_polytao_generation", "cancel_polytao_draft"}:
            return await self.execute_draft(name, args, owner_id, session)
        if name == "get_polytao_capabilities":
            status, payload = await self.request("GET", API + "/status", session)
            self.check_status(status, payload)
            state = PolytaoStatusResponse.model_validate(payload)
            return CapabilitiesOutput(enabled=state.enabled, available=state.available,
                submission_ready=self.journal is not None and not self.journal.failed,
                runtime_ready=state.runtime_ready, runtime_status=state.worker_status, active_jobs=state.active_jobs,
                descriptors=[{"name": key, "description": DESCRIPTOR_DESCRIPTIONS[key], "unit": "g/mol" if key == "MolWt" else None} for key in POLYTAO_DESCRIPTOR_NAMES],
                parameters=GenerationParameters.model_json_schema()["properties"], limitations=LIMITATIONS)
        if name == "calculate_polytao_descriptors":
            return await self.descriptors(args.smiles, session)
        assert isinstance(args, JobInput)
        if not self.get_journal().owns_job(client_id, self.config.account_uuid, args.job_id):
            raise MCPFailure("JOB_NOT_FOUND", "This MCP client has no confirmed submission for the requested job.")
        deadline = time.monotonic() + args.wait_seconds
        job = None
        while True:
            if job is not None and time.monotonic() >= deadline:
                break
            try:
                if job is None:
                    status, payload = await self.request("GET", API + "/jobs/" + args.job_id, session)
                else:
                    # An optional follow-up read cannot outlive the caller's
                    # wait budget; preserve the last known snapshot instead.
                    async with asyncio.timeout(max(0, deadline - time.monotonic())):
                        status, payload = await self.request("GET", API + "/jobs/" + args.job_id, session)
            except TimeoutError:
                if job is not None:
                    break
                raise
            self.check_status(status, payload)
            job = PolytaoJobStatusResponse.model_validate(payload)
            snapshot_at = timestamp()
            if job.job_id != args.job_id:
                raise MCPFailure("BACKEND_INVALID_RESPONSE", "Backend returned a different task identifier.")
            remaining = deadline - time.monotonic()
            if job.status in TERMINAL or remaining <= 0:
                break
            await asyncio.sleep(min(2, remaining))
        assert job is not None
        result = job.result
        candidates = result.results if result else []
        if ((result is not None and (result.requested_count != job.requested_count
                or result.returned_count != job.returned_count))
                or job.returned_count != len(candidates) or job.returned_count > job.requested_count
                or [item.rank for item in candidates] != list(range(1, len(candidates) + 1))):
            raise MCPFailure("BACKEND_INVALID_RESPONSE", "Backend task counts and candidate ordering are inconsistent.")
        page = candidates[args.offset:args.offset + args.limit]
        warnings = []
        if job.status == "completed" and job.returned_count < job.requested_count:
            warnings.append("Generation completed with fewer candidates than requested, including possibly zero candidates.")
        if job.status == "failed":
            warnings.append("Generation failed; no replacement job was submitted.")
        error_code = "USAGE_OUTCOME_UNKNOWN" if job.error_message == "usage_outcome_unknown" else ("GENERATION_FAILED" if job.status == "failed" else None)
        return JobOutput(job_id=job.job_id, status=job.status, progress_stage=job.progress_stage,
            progress_percent=job.progress_percent, created_at=job.created_at, updated_at=job.updated_at,
            started_at=job.started_at, finished_at=job.finished_at, snapshot_at=snapshot_at,
            requested_count=job.requested_count, returned_count=job.returned_count, attempts=job.attempts,
            filter_counter=result.filter_counter if result else {},
            results=[CandidateOutput(**item.model_dump(include={"rank", "generated_smiles", "valid_smiles", "sa_score", "warnings"})) for item in page],
            offset=args.offset, limit=args.limit, next_offset=args.offset + args.limit if args.offset + args.limit < len(candidates) else None,
            error_code=error_code, warnings=warnings)

    async def call(self, context: Any, parameters: Any) -> types.CallToolResult:
        started, acquired = time.perf_counter(), False
        client = context.request.state.mcp_client
        request_id = context.request.state.mcp_request_id
        name, code, count = parameters.name, "ok", 0
        try:
            if name not in TOOL_NAMES:
                raise MCPFailure("UNKNOWN_TOOL", "Unknown tool; discover available tools first.")
            if name not in client.tools:
                raise MCPFailure("PERMISSION_DENIED", "This MCP client lacks permission for this tool.")
            try:
                args = INPUT_MODELS[name].model_validate(parameters.arguments or {})
            except ValidationError:
                raise MCPFailure("INVALID_ARGUMENT", "Arguments must match the published strict schema and numeric bounds; descriptor values must be finite numbers or explicitly unset in a draft.") from None
            self.limiter.acquire(client.client_id)
            acquired = True
            async with asyncio.timeout(self.config.tool_timeout_seconds):
                output = await self.execute(name, args, client.client_id)
            count = len(getattr(output, "results", []))
            return tool_result(output, request_id, protocol_request_id=getattr(context, "request_id", None))
        except TimeoutError:
            code = "QUERY_TIMEOUT"
            return error_result(MCPFailure(code, "Tool wait budget expired. Query an existing job or retry the same submission key; do not create a replacement task."), request_id)
        except asyncio.CancelledError:
            code = "CALL_CANCELLED"
            raise
        except JournalError:
            code = "JOURNAL_UNAVAILABLE"
            return error_result(MCPFailure(code, "Submission history is unavailable; submissions are disabled pending operator recovery."), request_id)
        except DraftError as failure:
            code = failure.code
            return error_result(MCPFailure(code, failure.message), request_id)
        except MCPFailure as failure:
            code = failure.code
            return error_result(failure, request_id)
        except (httpx.HTTPError, ValidationError, ValueError):
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
                "duration_ms": round((time.perf_counter() - started) * 1000, 2), "result_count": count}, separators=(",", ":")))


def create_app(config: PolytaoMCPConfig | None = None, *, transport: Any = None) -> Any:
    config = config or PolytaoMCPConfig.from_env()
    runtime = MCPRuntime(config, transport=transport)

    async def list_tools(context: Any, parameters: Any) -> types.ListToolsResult:
        tools = []
        for name in TOOL_ORDER:
            if name not in context.request.state.mcp_client.tools:
                continue
            schema = TypeAdapter(OUTPUT_MODELS[name] | ErrorOutput).json_schema()
            schema["type"] = "object"
            tools.append(types.Tool(name=name, description=DESCRIPTIONS[name], inputSchema=INPUT_MODELS[name].model_json_schema(),
                outputSchema=schema, annotations=types.ToolAnnotations(readOnlyHint=name in {
                    "get_polytao_capabilities", "calculate_polytao_descriptors", "get_polytao_draft", "get_polytao_job"},
                destructiveHint=False, idempotentHint=name != "prepare_polytao_generation", openWorldHint=False)))
        return types.ListToolsResult(tools=tools)

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok" if runtime.ready else "unavailable",
                             "submission_ready": runtime.journal is not None and not runtime.journal.failed},
                            status_code=200 if runtime.ready else 503)

    server = Server("nexpoly-polytao", version="1.0.0", on_list_tools=list_tools, on_call_tool=runtime.call,
        instructions="Development polymer generation. Read capabilities, then prepare a parameter form using only known values. The user must review and confirm the form before any generation. No model-accessible tool can submit or confirm. Read the draft receipt and query only its confirmed job. Never automatically replace an uncertain submission. Results are stochastic candidates, not verified syntheses.")
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
            raise RuntimeError("PolyTAO MCP startup checks failed; verify private credentials and pinned account configuration.") from None
        try:
            async with sdk_lifespan(application):
                yield
        finally:
            await runtime.close()

    app.router.lifespan_context = lifespan
    app.add_middleware(RequestGuard, config=config, tool_names=TOOL_NAMES, registry_type=PolytaoClientRegistry)
    app.state.mcp_runtime = runtime
    return app


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    LOGGER.setLevel(logging.INFO)
    try:
        config = PolytaoMCPConfig.from_env()
    except (OSError, ValueError):
        raise SystemExit("Invalid PolyTAO MCP configuration; verify explicit backend and private files.") from None
    uvicorn.run(create_app(config), host=config.host, port=config.port, access_log=False,
                log_level="warning", proxy_headers=False, timeout_keep_alive=5)


if __name__ == "__main__":
    main()
