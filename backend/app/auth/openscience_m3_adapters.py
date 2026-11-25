"""Delegated MCP entry point for one independent dev-m1 adapter over Unix sockets."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import parse_qs
from uuid import UUID, uuid4

import httpx2 as httpx
from mcp import types
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.mcp_security import CallLimiter, MCPFailure
from app.auth.openscience_candidate import authority_environment, candidate_members


CONTEXT = "/internal/openscience/adapter-context"
EXCHANGE = "/internal/openscience/adapter-exchange"
ERROR_STATUS = {
    "INVALID_ARGUMENT": 422,
    "DRAFT_INCOMPLETE": 422,
    "DRAFT_NOT_FOUND": 404,
    "JOB_NOT_FOUND": 404,
    "CONFIRMATION_LOGIN_MISMATCH": 403,
    "PERMISSION_DENIED": 403,
    "SESSION_EXPIRED": 401,
    "RATE_LIMITED": 429,
    "CONCURRENCY_LIMITED": 429,
    "CAPACITY_LIMITED": 429,
    "DRAFT_REVISION_CONFLICT": 409,
    "DRAFT_LOCKED": 409,
    "DRAFT_CANCELLED": 409,
}


def key_hash(*parts):
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Config:
    name: str
    state: Path
    ingress_key: str
    authority_key: str
    authority_socket: str
    submit_limit: int
    evidence: Path | None
    environment: str
    owners: frozenset[str]

    @classmethod
    def load(cls):
        name = os.environ["M3_ADAPTER"]
        if name not in {"polytao", "retrosynthesis"}:
            raise ValueError("Expected an M3 development adapter")
        root = Path(os.environ["M3_ADAPTER_STATE_DIR"])
        if not root.is_absolute() or root.is_symlink() or root.stat().st_mode & 0o077:
            raise ValueError("Expected a private adapter data directory")
        ingress, authority = (
            Path(os.environ[k]).read_text().strip()
            for k in ("M3_ADAPTER_INGRESS_FILE", "M3_ADAPTER_AUTHORITY_FILE")
        )
        if min(len(ingress), len(authority)) < 32 or hmac.compare_digest(ingress, authority):
            raise ValueError("Expected distinct adapter service credentials")
        limit = int(os.environ.get("M3_ADAPTER_SUBMITS_PER_MINUTE", "20"))
        if not 1 <= limit <= 60:
            raise ValueError("Expected 1 to 60 new submissions per user and minute")
        evidence = (
            Path(os.environ["M3_ADAPTER_VERIFY_EVIDENCE"])
            if os.environ.get("M3_ADAPTER_VERIFY_DROP_RECEIPT") == "1"
            else None
        )
        environment = authority_environment()
        if environment == "dev-m6":
            raise ValueError("The AC-22 candidate does not provide tool adapters")
        owners = frozenset(m["user_id"] for m in candidate_members(environment))
        return cls(name, root, ingress, authority, os.environ["M3_AUTHORITY_SOCKET"], limit, evidence, environment, owners)


class ReceiptProbe:
    """Discard a response between native acceptance and journal update, only in acceptance mode."""

    def __init__(self, path):
        self.path = path
        self.data = {"dropped": [], "recovered": []}
        if path:
            if path.exists():
                self.data = json.loads(path.read_bytes())
            else:
                self.save()

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data))
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def observe(self, kind, owner, job_id, key):
        if not self.path:
            return False
        record = {"owner_id": owner, "job_id": job_id, "key": key}
        if kind == "dropped":
            if any(row["owner_id"] == owner for row in self.data[kind]):
                return False
        elif record not in self.data["dropped"] or record in self.data["recovered"]:
            return False
        self.data[kind].append(record)
        self.save()
        return True


class DelegatedCall:
    def __init__(self, bridge, operation, tool, raw):
        self.bridge = bridge
        self.claim = {
            "operation_token": operation,
            "tool": tool,
            "arguments_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        }
        self.lost_key = None

    async def authority(self, path, body):
        response = await self.bridge.client.post(path, json=body)
        if response.status_code in (401, 403):
            code = "SESSION_EXPIRED" if response.status_code == 401 else "PERMISSION_DENIED"
            raise MCPFailure(code, "The original tool authorization is no longer valid.")
        response.raise_for_status()
        return response.json()

    async def authorize(self):
        ctx = await self.authority(CONTEXT, self.claim)
        if ctx["owner_id"] not in self.bridge.config.owners or ctx["tool"] != self.claim["tool"]:
            raise MCPFailure("PERMISSION_DENIED", "Unexpected adapter operation or workspace owner.")
        self.owner = ctx["owner_id"]
        self.login = str(UUID(ctx["login_id"]))
        return ctx

    async def native(self, method, path, body, key):
        raw = json.dumps(body, allow_nan=False, separators=(",", ":")).encode() if body is not None else b""
        route, _, query = path.partition("?")
        lookup = route.endswith("/jobs/lookup")
        if lookup:
            (key,) = parse_qs(query)["idempotency_key"]
        if self.lost_key and key == self.lost_key:
            raise httpx.ReadError("Receipt unavailable to this development attempt")
        exchange = {
            **self.claim,
            "method": method,
            "path": route,
            "query": query,
            "request_sha256": hashlib.sha256(raw).hexdigest(),
        }
        if key is not None:
            exchange["idempotency_key"] = key
        grant = await self.authority(EXCHANGE, exchange)
        headers = {"x-openscience-delegation": grant["access_token"], "content-type": "application/json"}
        async with self.bridge.client.stream(method, path, headers=headers, content=raw) as response:
            received = bytearray()
            async for chunk in response.aiter_bytes():
                received.extend(chunk)
                if len(received) > self.bridge.contract.MAX_BACKEND_BYTES:
                    raise MCPFailure(
                        "BACKEND_RESPONSE_TOO_LARGE", "Native response exceeded the adapter limit."
                    )
            payload = json.loads(received)
            status = response.status_code
        if status == 401 and isinstance(payload, dict) and payload.get("code") == "native_response_withheld":
            # The native route already ran. Let the existing lost-receipt workflow
            # look up the frozen submission key on a fresh authorization.
            raise httpx.ReadError("The original native response could not be delivered.")
        if status in (200, 202) and key and isinstance(payload, dict) and payload.get("job_id"):
            job = payload["job_id"]
            if lookup:
                self.bridge.probe.observe("recovered", self.owner, job, key)
            elif method == "POST" and route.endswith("/jobs"):
                if self.bridge.probe.observe("dropped", self.owner, job, key):
                    self.lost_key = key
                    raise httpx.ReadError("Development acceptance discarded native receipt")
        return status, payload


class OwnerRuntime:
    """Mixin for the original MCP runtime. Owner and transport are immutable for each call."""

    def __init__(self, journal, owner):
        self.config = SimpleNamespace(account_uuid=owner)
        self.journal = journal

    async def request(self, method, path, session, *, body=None, idempotency_key=None):
        return await session.native(method, path, body, idempotency_key)

    async def draft_values(self, args, session, previous=None):
        payload = await super().draft_values(args, session, previous)
        payload["_m3_initiating_login"] = previous["_m3_initiating_login"] if previous else session.login
        return payload

    async def execute_draft(self, name, args, owner, session):
        if name in {
            "get_polytao_draft", "get_retrosynthesis_draft",
            "confirm_polytao_generation", "confirm_monomer_retrosynthesis",
        }:
            draft = self.journal.read_draft(owner, owner, args.conversation_id, args.draft_id)
            if draft["state"] == "rejected" and draft["error_code"] == "SESSION_EXPIRED":
                # Older adapters recorded an undelivered receipt as a rejection. Only
                # a native receipt for the original frozen key can change that state.
                key = f"draft:{args.draft_id}:{draft['revision']}"
                if "polytao" in name:
                    receipt = await self.recover(owner, key, session)
                else:
                    try:
                        receipt = await self.recover(owner, key, self.submission_body(draft), session)
                    except MCPFailure as error:
                        if error.code != "SUBMISSION_NOT_RECEIVED":
                            raise
                        receipt = None
                if receipt is not None:
                    self.journal.finish_draft(
                        owner, owner, args.conversation_id, args.draft_id, state="submitted", job_id=receipt.job_id
                    )
        return await super().execute_draft(name, args, owner, session)


class PolyRecovery:
    """The native development API offers owner-scoped lookup and stable-key replay for PolyTAO."""

    async def recover(self, owner, key, session):
        from app.polytao_mcp import API

        native_key = key_hash(owner, owner, key)
        status, receipt = await self.request(
            "GET", API + "/jobs/lookup?idempotency_key=" + native_key, session
        )
        if status == 404:
            return None
        self.check_status(status, receipt)
        return self.record_receipt(owner, key, receipt)

    def record_receipt(self, owner, key, payload):
        from app.polytao_mcp_contract import JobInput, SubmitOutput

        job_id = JobInput(job_id=payload["job_id"]).job_id
        receipt = SubmitOutput(
            job_id=job_id, status=payload["status"], replayed=True, effective_parameters={}
        )
        self.journal.recover_submission(owner, owner, key, job_id=job_id, job_status=receipt.status)
        return receipt

    async def submit(self, args, owner, session):
        from app.polytao_mcp import API

        key = args.idempotency_key
        record = self.journal.submission_record(owner, owner, key)
        if record is None or record["state"] not in {"dispatching", "outcome_unknown"}:
            return await super().submit(args, owner, session)
        body = args.backend_body()
        record, _ = self.journal.begin(owner, owner, key, body)
        if record.get("conflict"):
            raise MCPFailure("IDEMPOTENCY_CONFLICT", "The frozen submission parameters changed.")
        found = await self.recover(owner, key, session)
        if found is None:
            # Only an explicit repeat confirmation reaches this branch. The native endpoint
            # durably deduplicates the frozen key even if an earlier POST is still in flight.
            status, receipt = await self.request(
                "POST", API + "/jobs", session, body=body, idempotency_key=key_hash(owner, owner, key)
            )
            self.check_status(status, receipt)
            found = self.record_receipt(owner, key, receipt)
        found.effective_parameters = body
        return found

    async def execute_draft(self, name, args, owner, session):
        if name == "get_polytao_draft":
            draft = self.journal.read_draft(owner, owner, args.conversation_id, args.draft_id)
            key = f"draft:{args.draft_id}:{draft['revision']}"
            record = self.journal.submission_record(owner, owner, key)
            if record and record["state"] in {"dispatching", "outcome_unknown"}:
                await self.recover(owner, key, session)
        return await super().execute_draft(name, args, owner, session)


class OwnerJournal:
    def recover_submission(self, client_id, account_uuid, key, *, job_id, job_status):
        with self._transaction() as db:
            repaired = db.execute(
                """UPDATE submissions SET state='accepted',job_id=?,job_status=?,error_code=NULL,updated_at=?
                   WHERE client_id=? AND account_uuid=? AND idempotency_key=?
                     AND state='rejected' AND error_code='SESSION_EXPIRED'""",
                (job_id, job_status, datetime.now(timezone.utc).isoformat(), client_id, account_uuid, key),
            ).rowcount
        if not repaired:
            super().recover_submission(client_id, account_uuid, key, job_id=job_id, job_status=job_status)

    def create_admissions(self):
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS m3_admissions (draft_key TEXT PRIMARY KEY, admitted_at INTEGER NOT NULL)"
            )

    def admit(self, key, limit):
        now = int(time.time() * 1000)
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM m3_admissions WHERE draft_key=?", (key,)).fetchone():
                return True
            count = db.execute(
                "SELECT count(*) FROM m3_admissions WHERE admitted_at>?", (now - 60000,)
            ).fetchone()[0]
            if count >= limit:
                return False
            db.execute("INSERT INTO m3_admissions VALUES(?,?)", (key, now))
            return True


class Adapter:
    def __init__(self, cfg):
        self.config = cfg
        self.module = import_module(f"app.{cfg.name}_mcp")
        self.contract = import_module(f"app.{cfg.name}_mcp_contract")
        journal_module = import_module(f"app.{cfg.name}_mcp_journal")
        self.journal_errors = (journal_module.DraftError, journal_module.JournalError)
        self.runtime_type = type(
            "DelegatedRuntime",
            ((PolyRecovery,) if cfg.name == "polytao" else ()) + (OwnerRuntime, self.module.MCPRuntime),
            {},
        )
        self.journal_type = type("PrivateJournal", (OwnerJournal, journal_module.SubmissionJournal), {})
        self.journals = {}
        self.locks = {}
        self.limiter = CallLimiter(SimpleNamespace(rate_limit=60, max_concurrency=2))
        self.probe = ReceiptProbe(cfg.evidence)
        self.client = httpx.AsyncClient(
            base_url="http://localhost",
            trust_env=False,
            follow_redirects=False,
            transport=httpx.AsyncHTTPTransport(uds=cfg.authority_socket),
            headers={"Authorization": "Bearer " + cfg.authority_key},
            timeout=22,
        )

    def open_journal(self, path, owner):
        journal = self.journal_type(path, backend_url=f"http://{self.config.environment}-native", account_uuid=owner)
        try:
            journal.create_admissions()
        except BaseException:
            journal.close()
            raise
        return journal

    def published_journal(self, owner):
        root = self.config.state / str(UUID(owner))
        if not os.path.lexists(root):
            parent_fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                # All adapter processes use the same parent inode for publication. A killed
                # initializer releases its lock and leaves no incomplete owner directory.
                fcntl.flock(parent_fd, fcntl.LOCK_EX)
                if not os.path.lexists(root):
                    with TemporaryDirectory(dir=root.parent, prefix=".new-journal-") as stage:
                        pending = Path(stage)
                        path = pending / "journal.db"
                        self.journal_type.initialize(path)
                        self.open_journal(path, owner).close()
                        directory_fd = os.open(pending, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
                        pending.rename(root)
                        os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        if root.is_symlink() or not root.is_dir():
            raise self.journal_errors[1]("Unexpected private journal directory")
        return root / "journal.db"

    def journal(self, owner):
        if owner not in self.journals:
            self.journals[owner] = self.open_journal(self.published_journal(owner), owner)
            self.locks[owner] = asyncio.Lock()
        return self.journals[owner]

    async def execute(self, call, args, ctx):
        name = call.claim["tool"]
        owner = call.owner
        model = self.contract.MODEL_TOOL_NAMES
        ui = self.contract.UI_TOOL_NAMES
        if (
            ctx["channel"] == "model"
            and name not in model
            or ctx["channel"] == "ui"
            and name not in ui
            or ctx["channel"] not in {"model", "ui"}
        ):
            raise MCPFailure("PERMISSION_DENIED", "Tool is unavailable to the caller's channel.")
        if getattr(args, "conversation_id", ctx["workspace_session"]) != ctx["workspace_session"]:
            raise MCPFailure("PERMISSION_DENIED", "Conversation differs from the original operation.")
        journal = self.journal(owner)
        runtime = self.runtime_type(journal, owner)
        if name.startswith("confirm_"):
            if ctx["channel"] != "ui" or ctx["confirmed"] is not True:
                raise MCPFailure(
                    "PERMISSION_DENIED", "Human confirmation from the initiating login is required."
                )
            draft = journal.read_draft(owner, owner, args.conversation_id, args.draft_id)
            if draft["payload"].get("_m3_initiating_login") != call.login:
                raise MCPFailure(
                    "CONFIRMATION_LOGIN_MISMATCH", "Only the login that opened this form may confirm it."
                )
            journal._check_revision(draft, args.revision)
            if not runtime.draft_output(draft).ready:
                raise MCPFailure("DRAFT_INCOMPLETE", "Confirm a complete active draft.")
            if not journal.admit(f"{args.draft_id}:{args.revision}", self.config.submit_limit):
                raise MCPFailure(
                    "RATE_LIMITED", "This user reached the new submission limit for this minute.", True
                )
        if self.config.name == "polytao":
            output = await runtime.execute_for_owner(name, args, owner, call, client_id=owner)
            if name == "get_polytao_capabilities":
                output.limitations = [
                    s
                    for s in output.limitations
                    if not s.startswith(
                        ("Development uses one dedicated account", "Idempotency provides at most one HTTP")
                    )
                ]
        else:
            output = await runtime.execute_for_owner(name, args, owner, call)
        return output

    async def call(self, context, parameters):
        request_id = uuid4().hex
        try:
            name = parameters.name
            if name not in self.contract.TOOL_NAMES:
                raise MCPFailure("PERMISSION_DENIED", "Unknown adapter tool.")
            operation = context.request.headers.get("x-openscience-operation", "")
            raw = (parameters.meta or {}).get("nexpoly/arguments-json")
            if not isinstance(raw, str) or not operation or len(operation) != 43:
                raise MCPFailure("SESSION_EXPIRED", "A scoped tool operation is required.")
            values = json.loads(raw)
            if not isinstance(values, dict) or values != parameters.arguments:
                raise MCPFailure(
                    "INVALID_ARGUMENT", "The supplied arguments differ from the signed tool call."
                )
            call = DelegatedCall(self, operation, name, raw)
            ctx = await call.authorize()
            args = self.contract.INPUT_MODELS[name].model_validate(values)
            owner = call.owner
            self.limiter.acquire(owner)
            try:
                self.journal(owner)
                # Serialize writes/receipt recovery within one owner. Other owners have independent locks.
                async with self.locks[owner]:
                    ctx = await call.authorize()
                    output = await self.execute(call, args, ctx)
                    await call.authorize()
            finally:
                self.limiter.release(owner)
            return self.module.tool_result(output, request_id, protocol_request_id=context.request_id)
        except self.journal_errors[0] as e:
            failure = MCPFailure(e.code, e.message)
        except self.journal_errors[1]:
            failure = MCPFailure("JOURNAL_UNAVAILABLE", "The owner's submission history is unavailable.")
        except MCPFailure as e:
            failure = e
        except (ValidationError, ValueError, TypeError):
            failure = MCPFailure(
                "INVALID_ARGUMENT", "The operation or response did not match the adapter contract."
            )
        except (TimeoutError, httpx.HTTPError):
            failure = MCPFailure("BACKEND_UNAVAILABLE", "The development service is unavailable.", True)
        result = self.module.error_result(failure, request_id)
        result.structured_content["error"]["http_status"] = ERROR_STATUS.get(failure.code, 503)
        return result

    async def catalog(self, _context, _parameters):
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=name,
                    description=self.module.DESCRIPTIONS[name],
                    inputSchema=self.contract.INPUT_MODELS[name].model_json_schema(),
                )
                for name in self.contract.MODEL_TOOL_ORDER
            ]
        )

    async def close(self):
        await self.client.aclose()
        for journal in self.journals.values():
            journal.close()


def create_app():
    cfg = Config.load()
    adapter = Adapter(cfg)
    server = Server(
        f"{cfg.environment}-{cfg.name}", version="1.0.0", on_list_tools=adapter.catalog, on_call_tool=adapter.call
    )
    server.middleware.clear()
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=96 * 1024,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["localhost", "localhost:*"],
            allowed_origins=[],
        ),
    )
    sdk_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with sdk_lifespan(application):
                yield
        finally:
            await adapter.close()

    app.router.lifespan_context = lifespan

    async def service_identity(request, call_next):
        values = request.headers.getlist("authorization")
        if (
            len(values) != 1
            or not hmac.compare_digest(values[0].encode(), ("Bearer " + cfg.ingress_key).encode())
            or any(k in request.headers for k in ("origin", "cookie", "forwarded", "x-forwarded-for"))
        ):
            return JSONResponse({"error": "adapter_service_identity_required"}, status_code=403)
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=service_identity)
    return app
