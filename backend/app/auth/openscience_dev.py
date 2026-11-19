"""NexPoly browser identity and OpenScience ticket authority for the isolated M2 deployment."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth.context import Identity, user_context
from app.auth import openscience_delegation_dev as m3
from app.auth.middleware import AuthenticationMiddleware
from app.auth.policy import capabilities
from app.auth.router import router as login_router
from app.auth.service import AuthService
from app.auth.settings import AuthSettings
from app.postgres_database import postgres_connection
from app.services.entitlement_repository import EntitlementRepository
from task_usage.entitlement_access import resolve_access_tier

ENVIRONMENT = "dev-m1"
TOKEN = Field(min_length=43, max_length=43, pattern=r"^[a-zA-Z0-9_-]{43}$")
INTERNAL_ROUTES = {"/internal/openscience/tickets/redeem", "/internal/openscience/sessions/validate"}


class WorkspaceAuthenticationMiddleware(AuthenticationMiddleware):
    async def __call__(self, scope, receive, send):
        # Uvicorn reports no Internet socket addresses for the private Unix
        # listener. Preserve the standard forwarded-header and service checks.
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and (
                scope["path"] in INTERNAL_ROUTES
                or (
                    hasattr(scope["app"].state, "openscience_m3") and scope["path"] in {m3.ISSUE, m3.EXCHANGE}
                )
            )
            and scope.get("client") is None
            and scope.get("server") is None
        ):
            scope = {**scope, "client": ("127.0.0.1", 0)}
        if scope["type"] == "http" and hasattr(scope["app"].state, "openscience_m3"):
            if any(k.lower() == b"x-openscience-delegation" for k, _ in scope["headers"]):
                return await scope["app"].state.openscience_m3.dispatch(self.app, scope, receive, send)
        return await super().__call__(scope, receive, send)


class Redeem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket: str = TOKEN
    handoff_id: UUID
    parent_origin: str = Field(max_length=256)
    agent_origin: str = Field(max_length=256)


class Validate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_session: str = TOKEN
    agent_origin: str = Field(max_length=256)


def digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("ascii")).digest()


def checked_origin(name, default):
    value = os.environ.get(name, default)
    parsed = urlsplit(value)
    if not (
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and value == f"https://{parsed.netloc}"
        and "*" not in value
    ):
        raise ValueError("Expected one exact HTTPS origin for independent M2")
    return value


def create_app():
    parent = checked_origin("OPENSCIENCE_M2_PARENT_ORIGIN", "https://app.m2.test:18760")
    agent = checked_origin("OPENSCIENCE_M2_AGENT_ORIGIN", "https://agent.m2.test:18761")
    if urlsplit(parent).hostname == urlsplit(agent).hostname:
        raise ValueError("M2 parent and workspace need separate cookie hosts")
    auth = AuthService(
        AuthSettings(
            application_dsn=os.environ["APP_POSTGRES_DSN"],
            auth_dsn=os.environ["AUTH_POSTGRES_DSN"],
            service_dsn=os.environ["APP_SERVICE_POSTGRES_DSN"],
            cookie_name="__Host-nexpoly_m2_session",
            cookie_secure=True,
            allowed_origins=(parent,),
            runtime_environment="dev",
        )
    )
    from psycopg.conninfo import conninfo_to_dict

    if any(
        conninfo_to_dict(dsn).get("dbname") != "nexpoly_m2_dev"
        for dsn in (auth.settings.application_dsn, auth.settings.auth_dsn, auth.settings.service_dsn)
    ):
        raise ValueError("M2 authority requires its dedicated development database")
    service_secret = Path(os.environ["OPENSCIENCE_M2_ROUTER_TOKEN_FILE"]).read_bytes().strip()
    if not 32 <= len(service_secret) <= 256:
        raise ValueError("Invalid M2 service credential")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.auth = auth
    app.state.entitlement_repository = EntitlementRepository(auth.settings.application_dsn)
    app.include_router(login_router)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _error):
        return JSONResponse(
            {"detail": "Invalid request"}, status_code=422, headers={"Cache-Control": "no-store"}
        )

    def router_identity(request: Request):
        headers = request.headers.getlist("authorization")
        if (
            len(headers) != 1
            or not hmac.compare_digest(headers[0].encode(), b"Bearer " + service_secret)
            or request.headers.get("cookie")
            or request.headers.get("origin")
        ):
            raise HTTPException(
                403, "Workspace router authentication required", headers={"Cache-Control": "no-store"}
            )

    def current_grant(conn, owner, login_id):
        row = conn.execute(
            """
            SELECT s.created_at AS login_issued_at,s.expires_at, clock_timestamp() AS checked_at
              FROM auth.sessions s JOIN auth.users u USING(user_id)
             WHERE s.session_id=%s AND s.user_id=%s AND s.revoked_at IS NULL
               AND s.expires_at>clock_timestamp() AND u.status='active'
               AND NOT u.is_system AND NOT u.must_change_password
               FOR SHARE OF u,s
        """,
            (login_id, owner),
        ).fetchone()
        if row is None:
            raise HTTPException(401, "Login is no longer active")
        workspace = conn.execute(
            "SELECT openscience_m2.lock_workspace(%s,%s) AS binding", (ENVIRONMENT, owner)
        ).fetchone()["binding"]
        if workspace is None:
            raise HTTPException(403, "Workspace pilot access is disabled")
        # Use the real billing/entitlement resolver and the API role's owner RLS.
        # Internal calls start in service_context, so explicitly restore the real owner.
        with user_context(Identity(str(owner), str(login_id)), auth.settings):
            with postgres_connection(auth.settings.application_dsn) as app_conn:
                member = resolve_access_tier(app_conn, owner_user_id=owner) == "member"
                # Cap the current check at the paid period's true expiry for
                # accounts relying exclusively on a subscription.
                cutoff = (
                    app_conn.execute(
                        """
                    SELECT CASE WHEN a.override_tier='member' OR (a.override_tier IS NULL AND a.legacy_member)
                                THEN NULL ELSE s.paid_through END AS paid_until
                      FROM billing.accounts a LEFT JOIN billing.subscriptions s USING(owner_user_id)
                     WHERE a.owner_user_id=%s
                """,
                        (owner,),
                    ).fetchone()
                    if member
                    else None
                )
        if not cutoff or not capabilities(member)["openscience.use"]:
            raise HTTPException(403, "Workspace membership required")
        deadline = min(row["expires_at"].timestamp(), row["checked_at"].timestamp() + 30)
        if cutoff["paid_until"] is not None:
            deadline = min(deadline, cutoff["paid_until"].timestamp())
        return {
            "user_id": str(owner),
            "session_id": str(login_id),
            "environment": ENVIRONMENT,
            "workspace_user_id": workspace,
            "login_issued_at": row["login_issued_at"].timestamp(),
            "valid_until": deadline,
            "expires_at": row["expires_at"].timestamp(),
        }

    @app.post("/api/v1/openscience/workspace-ticket")
    def issue(request: Request):
        if request.headers.getlist("origin") != [parent]:
            raise HTTPException(403, "Unexpected workspace parent")
        login = request.state.auth_session
        ticket = secrets.token_urlsafe(32)
        handoff = uuid4()
        with auth.connection() as conn:
            grant = current_grant(conn, login["user_id"], login["session_id"])
            conn.execute("""
                DELETE FROM openscience_m2.handoffs h
                 WHERE (h.redeemed_at IS NULL AND h.ticket_expires_at<clock_timestamp())
                    OR h.revoked_at<clock_timestamp()-interval '1 day'
                    OR EXISTS (SELECT FROM auth.sessions s WHERE s.session_id=h.login_id
                                AND s.expires_at<clock_timestamp())
            """)
            conn.execute(
                """
                INSERT INTO openscience_m2.handoffs
                       (handoff_id,ticket_hash,environment,owner_id,workspace_user_id,login_id,
                        parent_origin,agent_origin,ticket_expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+interval '60 seconds')
            """,
                (
                    handoff,
                    digest(ticket),
                    ENVIRONMENT,
                    login["user_id"],
                    grant["workspace_user_id"],
                    login["session_id"],
                    parent,
                    agent,
                ),
            )
        return {
            "ticket": ticket,
            "handoff_id": str(handoff),
            "expires_in": 60,
            "exchange_url": agent + "/_nexpoly/auth/exchange",
        }

    def inspect_handoff(conn, row):
        if not row or row["revoked_at"] is not None:
            raise HTTPException(401, "Workspace handoff is no longer active")
        grant = current_grant(conn, row["owner_id"], row["login_id"])
        if grant["workspace_user_id"] != row["workspace_user_id"]:
            raise HTTPException(403, "Workspace binding changed")
        return {**grant, "issued_at": row["issued_at"].timestamp(), "handoff_id": str(row["handoff_id"])}

    @app.post("/internal/openscience/tickets/redeem", dependencies=[Depends(router_identity)])
    def redeem(body: Redeem):
        if body.parent_origin != parent or body.agent_origin != agent:
            raise HTTPException(403, "Workspace audience mismatch")
        credential = secrets.token_urlsafe(32)
        with auth.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM openscience_m2.handoffs WHERE handoff_id=%s AND ticket_hash=%s
                   AND environment=%s AND parent_origin=%s AND agent_origin=%s AND redeemed_at IS NULL
                   AND ticket_expires_at>clock_timestamp()
            """,
                (body.handoff_id, digest(body.ticket), ENVIRONMENT, parent, agent),
            ).fetchone()
            grant = inspect_handoff(conn, row)
            redeemed = conn.execute(
                """
                UPDATE openscience_m2.handoffs SET redeemed_at=clock_timestamp(),agent_hash=%s
                 WHERE handoff_id=%s AND redeemed_at IS NULL AND revoked_at IS NULL
                   AND ticket_expires_at>clock_timestamp() RETURNING handoff_id
            """,
                (digest(credential), body.handoff_id),
            ).fetchone()
            if not redeemed:
                raise HTTPException(401, "Workspace ticket already consumed or expired")
        return JSONResponse({**grant, "agent_session": credential}, headers={"Cache-Control": "no-store"})

    @app.post("/internal/openscience/sessions/validate", dependencies=[Depends(router_identity)])
    def validate(body: Validate):
        if body.agent_origin != agent:
            raise HTTPException(403, "Workspace audience mismatch")
        with auth.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM openscience_m2.handoffs WHERE agent_hash=%s AND environment=%s
                   AND agent_origin=%s AND parent_origin=%s AND redeemed_at IS NOT NULL
            """,
                (digest(body.agent_session), ENVIRONMENT, agent, parent),
            ).fetchone()
            grant = inspect_handoff(conn, row)
            # The pilot/user/login can have been revoked and re-enabled between
            # the initial lookup and taking the canonical authorization locks.
            active = conn.execute(
                "SELECT 1 FROM openscience_m2.handoffs WHERE handoff_id=%s AND revoked_at IS NULL",
                (row["handoff_id"],),
            ).fetchone()
            if not active:
                raise HTTPException(401, "Workspace session revoked")
        return JSONResponse(grant, headers={"Cache-Control": "no-store"})

    m3_key_file = os.environ.get("OPENSCIENCE_M3_GATEWAY_TOKEN_FILE")
    if m3_key_file:
        from app.auth.openscience_m3_native import install_native

        gateway_key = Path(m3_key_file).read_bytes().strip()
        if not 32 <= len(gateway_key) <= 256 or hmac.compare_digest(service_secret, gateway_key):
            raise ValueError("Use a distinct M3 gateway service credential")
        m3.install(app, auth, agent, router_identity, gateway_key, inspect_handoff)
        install_native(app)
    # Auth must establish the original actor before native entitlement and usage admission.
    app.add_middleware(WorkspaceAuthenticationMiddleware, auth_service=auth)
    return app


app = create_app()
