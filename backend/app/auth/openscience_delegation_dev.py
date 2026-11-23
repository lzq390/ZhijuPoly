"""Two-hop tool authorization, confined to the independent OpenScience developer deployment."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
import re
import secrets
import time
from typing import Annotated, Literal
from uuid import UUID

import anyio
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app.auth import openscience_adapter_scopes as adapters
from app.auth.context import Identity, user_context

ISSUE = "/internal/openscience/delegations"
EXCHANGE = ISSUE + "/exchange"
ADAPTER_CONTEXT = "/internal/openscience/adapter-context"
ADAPTER_EXCHANGE = "/internal/openscience/adapter-exchange"
DFT = "/api/v1/monomer-dft/jobs"
TOKEN_PATTERN = r"^[A-Za-z0-9_-]{43}$"
Token = Annotated[str, Field(pattern=TOKEN_PATTERN, min_length=43, max_length=43)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]
Reference = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,160}$")]
Tool = Literal[
    "predict_polymer_properties",
    "polymerize_monomers",
    "dft_job_submit",
    "dft_job_list",
    "dft_job_status",
    "dft_job_result",
]
SECRET_HEADERS = {
    "cookie",
    "origin",
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
}
PRIVATE = {"Cache-Control": "private, no-store"}
GENERATIONS = Path("/m3generations")


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_session: Token
    agent_origin: str = Field(max_length=256)
    operation_id: UUID
    tool: Tool | adapters.AdapterTool
    arguments_sha256: Digest
    job_id: (
        Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*$", max_length=128)] | None
    ) = None
    channel: Literal["model", "ui"] = "model"
    draft_id: Annotated[str, Field(pattern=r"^(ptd|rtd)_[0-9a-f]{32}$")] | None = None
    revision: int | None = Field(None, strict=True, ge=1, le=2147483647)
    confirmed: StrictBool = False
    workspace_session: str = Field(pattern=r"^ses_[A-Za-z0-9_-]{1,120}$")
    workspace_generation: Digest
    turn_expires_ms: int = Field(strict=True, gt=0, le=2**53 - 1)
    turn_id: str = Field(pattern=r"^msg_[A-Za-z0-9_-]{1,120}$")
    call_id: Reference


class AdapterContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_token: Token
    tool: Tool | adapters.AdapterTool
    arguments_sha256: Digest


class Exchange(AdapterContext):
    method: Literal["GET", "POST"]
    path: str = Field(min_length=1, max_length=300)
    query: str = Field(default="", max_length=256)
    request_sha256: Digest
    idempotency_key: Digest | None = None


def token_hash(value: str) -> bytes:
    return hashlib.sha256(value.encode("ascii")).digest()


def allowed_target(tool, job_id, method, path, query):
    fixed = {
        "predict_polymer_properties": ("POST", "/api/v1/predict"),
        "polymerize_monomers": ("POST", "/api/v1/monomer-polymerization"),
        "dft_job_submit": ("POST", DFT),
        "dft_job_list": ("GET", DFT),
    }
    if tool in fixed:
        return (method, path) == fixed[tool] and (
            not query
            or (tool == "dft_job_list" and re.fullmatch(r"page_size=[0-9]{1,3}", query) is not None)
        )
    return (
        tool in {"dft_job_status", "dft_job_result"}
        and job_id is not None
        and method == "GET"
        and path.startswith(DFT + "/")
        and path[len(DFT) + 1 :].lower() == str(job_id).lower()
        and not query
    )


class Delegations:
    def __init__(self, auth, agent, gateway_secret, inspect_handoff):
        self.auth, self.agent = auth, agent
        self.gateway_secret = b"Bearer " + gateway_secret
        self.inspect_handoff = inspect_handoff
        self.keys = {"gateway": self.gateway_secret}
        if os.environ.get("OPENSCIENCE_M3_ADAPTERS_ENABLED") == "1":
            for name in adapters.BASES:
                key = Path(os.environ["M3_" + name.upper() + "_AUTHORITY_FILE"]).read_bytes().strip()
                credential = b"Bearer " + key
                if len(key) < 32 or credential in self.keys.values():
                    raise ValueError("Development adapters require distinct service credentials")
                self.keys[name] = credential

    def service_identity(self, headers):
        values = headers.getlist("authorization")
        if len(values) != 1 or any(key in headers for key in SECRET_HEADERS):
            raise HTTPException(403, "Private service credential required")
        for name, key in self.keys.items():
            if hmac.compare_digest(key, values[0].encode()):
                return name
        raise HTTPException(403, "Private service credential required")

    def current_generation(self, workspace, expected):
        if workspace not in {"test-user-a", "test-user-b"} or not isinstance(expected, str):
            return False
        try:
            marker = (GENERATIONS / workspace).read_bytes()
        except OSError:
            return False
        return hmac.compare_digest(marker, expected.encode("ascii"))

    def live_handoff(self, conn, predicate, value):
        # The predicate is chosen by our handlers, never taken from a request.
        row = conn.execute(
            "SELECT * FROM openscience_m2.handoffs WHERE redeemed_at IS NOT NULL AND environment='dev-m1' "
            f"AND {predicate}=%s AND agent_origin=%s",
            (value, self.agent),
        ).fetchone()
        grant = self.inspect_handoff(conn, row)
        # inspect_handoff locked user/session/binding across this transaction.
        # A revoke/re-enable before the lock must permanently retire this handoff.
        active = conn.execute(
            "SELECT 1 FROM openscience_m2.handoffs WHERE handoff_id=%s AND revoked_at IS NULL",
            (row["handoff_id"],),
        ).fetchone()
        if not active:
            raise HTTPException(401, "Workspace authorization revoked")
        return grant

    def issue(self, body):
        if body.agent_origin != self.agent or (body.tool == "dft_job_submit" and not body.confirmed):
            raise HTTPException(403, "Tool operation was not authorized")
        family = adapters.service(body.tool)
        if family not in self.keys:
            raise HTTPException(403, "Tool adapter is unavailable")
        if (
            body.channel == "ui"
            and body.tool not in adapters.UI
            or body.channel == "model"
            and body.tool in adapters.FORM_ONLY
        ):
            raise HTTPException(403, "Tool audience mismatch")
        if family != "gateway" and body.confirmed != (body.tool in adapters.CONFIRM):
            raise HTTPException(403, "Trusted form confirmation required")
        if body.tool in adapters.CONFIRM and (not body.draft_id or not body.revision):
            raise HTTPException(422, "Expected an exact draft revision")
        if body.tool in {"dft_job_status", "dft_job_result"}:
            try:
                UUID(body.job_id or "")
            except ValueError:
                raise HTTPException(422, "Invalid DFT job") from None
        if (body.tool in {"dft_job_status", "dft_job_result"} | adapters.JOB_READS) != (
            body.job_id is not None
        ):
            raise HTTPException(422, "Invalid tool job scope")
        token = secrets.token_urlsafe(32)
        with self.auth.connection() as conn:
            grant = self.live_handoff(conn, "agent_hash", token_hash(body.agent_session))
            deadline = min(grant["valid_until"], body.turn_expires_ms / 1000)
            if deadline <= time.time() or not self.current_generation(
                grant["workspace_user_id"], body.workspace_generation
            ):
                raise HTTPException(401, "Tool turn or workspace generation expired")
            binding = (
                UUID(grant["handoff_id"]),
                body.tool,
                body.arguments_sha256,
                str(body.job_id) if body.job_id else None,
                body.confirmed,
                body.workspace_session,
                body.turn_id,
                body.call_id,
                body.workspace_generation,
                body.turn_expires_ms,
                body.channel,
                body.draft_id,
                body.revision,
            )
            conn.execute(
                """
                INSERT INTO openscience_m3.operations
                  (operation_id,environment,owner_id,login_id,workspace_user_id,handoff_id,tool,
                   arguments_sha256,job_id,confirmed,workspace_session,turn_id,call_id,workspace_generation,turn_expires_ms,channel,draft_id,revision)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (operation_id) DO NOTHING
            """,
                (
                    body.operation_id,
                    grant["environment"],
                    grant["user_id"],
                    grant["session_id"],
                    grant["workspace_user_id"],
                    *binding,
                ),
            )
            recorded = conn.execute(
                """
                SELECT handoff_id,tool,arguments_sha256,job_id,confirmed,workspace_session,turn_id,call_id,workspace_generation,turn_expires_ms,channel,draft_id,revision
                  FROM openscience_m3.operations WHERE operation_id=%s
            """,
                (body.operation_id,),
            ).fetchone()
            if tuple(recorded.values()) != binding:
                raise HTTPException(409, "Operation already belongs to a different call")
            conn.execute(
                """
                INSERT INTO openscience_m3.delegations(token_hash,operation_id,audience,expires_at)
                VALUES (%s,%s,'capability-gateway',to_timestamp(%s))
            """,
                (token_hash(token), body.operation_id, deadline),
            )
        return {
            "operation_token": token,
            "operation_id": str(body.operation_id),
            "valid_until": deadline,
        }

    def lookup(self, conn, digest, audience):
        row = conn.execute(
            """
            SELECT d.*,o.handoff_id,o.tool,o.arguments_sha256,o.job_id,o.owner_id,o.login_id,o.workspace_user_id,o.workspace_generation,
                   o.workspace_session,o.channel,o.confirmed,o.draft_id,o.revision,o.turn_id,o.call_id
              FROM openscience_m3.delegations d JOIN openscience_m3.operations o USING(operation_id)
             WHERE d.token_hash=%s AND d.audience=%s AND d.expires_at>clock_timestamp()
               AND o.turn_expires_ms>extract(epoch FROM clock_timestamp())*1000
        """,
            (digest, audience),
        ).fetchone()
        if not row or not row["handoff_id"]:
            raise HTTPException(401, "Tool delegation expired or invalid")
        if not self.current_generation(row["workspace_user_id"], row["workspace_generation"]):
            raise HTTPException(401, "Workspace generation expired")
        active = self.live_handoff(conn, "handoff_id", row["handoff_id"])
        if (str(row["owner_id"]), str(row["login_id"]), row["workspace_user_id"]) != (
            active["user_id"],
            active["session_id"],
            active["workspace_user_id"],
        ):
            raise HTTPException(403, "Workspace binding changed")
        deadline = min(row["expires_at"].timestamp(), active["valid_until"])
        if deadline <= time.time():
            raise HTTPException(401, "Delegation expired")
        return row, deadline

    def checked_operation(self, conn, body, service):
        row, deadline = self.lookup(conn, token_hash(body.operation_token), "capability-gateway")
        if (
            row["tool"] != body.tool
            or row["arguments_sha256"] != body.arguments_sha256
            or adapters.service(row["tool"]) != service
        ):
            raise HTTPException(403, "Tool operation scope mismatch")
        return row, deadline

    def context(self, body, service):
        with self.auth.connection() as conn:
            row, deadline = self.checked_operation(conn, body, service)
            return {
                **{name: str(row[name]) for name in ("owner_id", "login_id", "operation_id")},
                **{name: row[name] for name in ("tool", "workspace_session", "confirmed", "channel")},
                "valid_until": deadline,
            }

    def exchange(self, body, service="gateway"):
        access = secrets.token_urlsafe(32)
        with self.auth.connection() as conn:
            row, deadline = self.checked_operation(conn, body, service)
            if service == "gateway":
                allowed = not body.idempotency_key and allowed_target(
                    row["tool"], row["job_id"], body.method, body.path, body.query
                )
            else:
                allowed = adapters.native_target(row, body, conn)
            if not allowed:
                raise HTTPException(403, "Native operation scope mismatch")
            conn.execute(
                """
                INSERT INTO openscience_m3.delegations
                    (token_hash,operation_id,audience,expires_at,method,path,query,request_sha256,native_key)
                VALUES (%s,%s,'nexpoly-native',to_timestamp(%s),%s,%s,%s,%s,%s)
            """,
                (
                    token_hash(access),
                    row["operation_id"],
                    deadline,
                    body.method,
                    body.path,
                    body.query,
                    body.request_sha256,
                    body.idempotency_key,
                ),
            )
        return {"access_token": access, "operation_id": str(row["operation_id"]), "valid_until": deadline}

    def claim(self, digest, method, path, query, payload_digest, service):
        with self.auth.connection() as conn:
            row, deadline = self.lookup(conn, digest, "nexpoly-native")
            if adapters.service(row["tool"]) != service:
                raise HTTPException(403, "Wrong native calling service")
            if (row["method"], row["path"], row["query"], row["request_sha256"]) != (
                method,
                path,
                query,
                payload_digest,
            ):
                raise HTTPException(403, "Native method, resource or payload differs from delegation")
            accepted = conn.execute(
                """
                UPDATE openscience_m3.delegations SET used_at=clock_timestamp()
                 WHERE token_hash=%s AND used_at IS NULL AND expires_at>clock_timestamp() RETURNING used_at
            """,
                (digest,),
            ).fetchone()
            if not accepted:
                raise HTTPException(401, "Native authorization already consumed or expired")
        identity = Identity(str(row["owner_id"]), str(row["login_id"]), str(row["operation_id"]))
        return (
            identity,
            deadline,
            (row["workspace_user_id"], row["workspace_generation"]),
            row["native_key"],
        )

    def receipt(self, digest, status, request_id, usage_id):
        with self.auth.connection() as conn:
            conn.execute(
                """
                UPDATE openscience_m3.delegations SET response_status=%s,usage_request_id=%s,usage_operation_id=%s
                 WHERE token_hash=%s AND used_at IS NOT NULL
            """,
                (status, request_id, usage_id, digest),
            )

    async def dispatch(self, app, scope, receive, send):
        headers = Headers(scope=scope)
        try:
            service = self.service_identity(headers)
            # The gateway enters on the private Unix listener. Direct loopback
            # peers also support the isolated ASGI/integration test transport.
            if scope.get("client") is not None or scope.get("server") is not None:
                try:
                    local = ipaddress.ip_address((scope.get("client") or ("",))[0]).is_loopback
                except ValueError:
                    local = False
                if not local:
                    raise HTTPException(403, "Gateway needs a local service transport")
            values = headers.getlist("x-openscience-delegation")
            if len(values) != 1 or not re.fullmatch(TOKEN_PATTERN, values[0]):
                raise HTTPException(401, "Native delegation required")
            digest = token_hash(values[0])
            request_body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                request_body.extend(message.get("body", b""))
                if len(request_body) > 1024 * 1024:
                    raise HTTPException(413, "Native payload too large")
                if not message.get("more_body"):
                    break
            try:
                query = scope["query_string"].decode("ascii")
            except UnicodeDecodeError:
                raise HTTPException(400, "Invalid native query") from None
            identity, deadline, generation, native_key = await anyio.to_thread.run_sync(
                self.claim,
                digest,
                scope["method"],
                scope["path"],
                query,
                hashlib.sha256(request_body).hexdigest(),
                service,
            )
        except HTTPException as e:
            return await JSONResponse({"detail": e.detail}, status_code=e.status_code, headers=PRIVATE)(
                scope, receive, send
            )
        body = bytes(request_body)
        offered = False

        async def replay():
            nonlocal offered
            if offered:
                return await receive()
            offered = True
            return {"type": "http.request", "body": body, "more_body": False}

        # Construct the inner request from verified data. The login owner, action ID,
        # request body and method have been bound by the authority, independently of MCP arguments.
        kept = [(k, v) for k, v in scope["headers"] if k.lower() in {b"host", b"accept"}]
        if scope["method"] == "POST":
            kept.extend(
                [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
            )
        kept.append((b"idempotency-key", (native_key or "openscience-" + identity.request_id).encode()))
        scope = {
            **scope,
            "headers": kept,
            "extensions": {
                k: v for k, v in scope.get("extensions", {}).items() if k != "http.response.pathsend"
            },
        }
        scope.setdefault("state", {}).update(
            auth_session={
                "user_id": identity.user_id,
                "session_id": identity.session_id,
                "must_change_password": False,
            },
            auth_token=None,
        )
        denied = False

        async def delivery(message):
            nonlocal denied
            if denied:
                return
            if message["type"] == "http.response.start":
                native_headers = Headers(raw=message.get("headers", []))
                await anyio.to_thread.run_sync(
                    self.receipt,
                    digest,
                    message["status"],
                    native_headers.get("x-usage-request-id"),
                    native_headers.get("x-usage-operation-id"),
                )
                if time.time() >= deadline or not self.current_generation(*generation):
                    denied = True
                    return await JSONResponse(
                        {"detail": "Native authorization expired", "code": "native_response_withheld"},
                        status_code=401,
                        headers=PRIVATE,
                    )(scope, replay, send)
                message = {
                    **message,
                    "headers": [
                        (k, v)
                        for k, v in message.get("headers", [])
                        if k.lower() not in {b"cache-control", b"set-cookie"}
                    ]
                    + [(b"cache-control", b"private, no-store")],
                }
            elif message["type"] == "http.response.body" and (
                time.time() >= deadline or not self.current_generation(*generation)
            ):
                denied = True
                return await send({"type": "http.response.body", "body": b"", "more_body": False})
            await send(message)

        with user_context(identity, self.auth.settings):
            await app(scope, replay, delivery)


def install(app, auth, agent, router_identity, gateway_secret, inspect_handoff):
    authority = Delegations(auth, agent, gateway_secret, inspect_handoff)
    app.state.openscience_m3 = authority

    @app.post(ISSUE, dependencies=[Depends(router_identity)])
    def issue(body: Operation):
        return JSONResponse(authority.issue(body), headers=PRIVATE)

    def gateway_identity(request: Request):
        if authority.service_identity(request.headers) != "gateway":
            raise HTTPException(403, "Gateway credential required")

    @app.post(EXCHANGE, dependencies=[Depends(gateway_identity)])
    def exchange(body: Exchange):
        return JSONResponse(authority.exchange(body), headers=PRIVATE)

    if len(authority.keys) > 1:

        def adapter_service(request: Request):
            service = authority.service_identity(request.headers)
            if service == "gateway":
                raise HTTPException(403, "Adapter credential required")
            return service

        @app.post(ADAPTER_CONTEXT)
        def context(body: AdapterContext, service=Depends(adapter_service)):
            return JSONResponse(authority.context(body, service), headers=PRIVATE)

        @app.post(ADAPTER_EXCHANGE)
        def adapter_exchange(body: Exchange, service=Depends(adapter_service)):
            return JSONResponse(authority.exchange(body, service), headers=PRIVATE)
