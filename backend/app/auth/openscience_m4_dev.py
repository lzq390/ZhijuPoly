"""Bind private M4 service requests to the original dev-m1 browser handoff and workspace generation."""

import hashlib
import hmac
import os
from pathlib import Path
import re
from uuid import UUID

from fastapi import HTTPException, Request

PREFIX = "/internal/openscience/m4/"


def install(app, auth, agent_origin, parent_origin, inspect_handoff):
    from app.auth.openscience_m4_knowledge import install_knowledge
    from app.auth.openscience_m4_model import install_model

    keys = {
        service: Path(os.environ["M4_" + service.upper() + "_KEY_FILE"]).read_bytes().strip()
        for service in ("model", "knowledge")
    }
    if min(map(len, keys.values())) < 32 or keys["model"] == keys["knowledge"]:
        raise ValueError("Distinct M4 service identities required")
    generations = Path("/m3generations")

    def principal(request: Request):
        path = request.url.path.removeprefix(PREFIX)
        service, _, operation = path.partition("/")
        headers = request.headers

        def single(key):
            values = headers.getlist(key)
            return values[0] if len(values) == 1 else ""

        if (
            service not in keys
            or not hmac.compare_digest(single("authorization").encode(), b"Bearer " + keys[service])
            or "cookie" in headers
            or "origin" in headers
        ):
            raise HTTPException(403, "Private service identity required")
        token = single("x-m4-handoff")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{43}", token):
            raise HTTPException(401, "Active workspace login required")
        with auth.connection() as conn:
            row = conn.execute(
                """SELECT * FROM openscience_m2.handoffs WHERE agent_hash=%s AND environment='dev-m1'
                   AND agent_origin=%s AND parent_origin=%s AND redeemed_at IS NOT NULL""",
                (hashlib.sha256(token.encode()).digest(), agent_origin, parent_origin),
            ).fetchone()
            grant = inspect_handoff(conn, row)
            active = conn.execute(
                "SELECT 1 FROM openscience_m2.handoffs WHERE handoff_id=%s AND revoked_at IS NULL",
                (row["handoff_id"],),
            ).fetchone()
        owner = grant["workspace_user_id"]
        if not active or owner not in ("test-user-a", "test-user-b"):
            raise HTTPException(401, "Workspace login expired")
        try:
            current = (generations / owner).read_text().strip()
        except OSError:
            current = ""
        if not re.fullmatch(r"[0-9a-f]{64}", current) or not hmac.compare_digest(
            current.encode(), single("x-m4-generation").encode()
        ):
            raise HTTPException(401, "Workspace generation expired")
        if service == "model" and operation != "usage":
            session, turn = single("x-m4-session"), single("x-m4-turn")
            try:
                request_id = str(UUID(single("x-m4-request")))
            except ValueError:
                raise HTTPException(403, "Model request ID required") from None
            if not re.fullmatch(r"ses_[a-zA-Z0-9_-]{1,120}", session) or not re.fullmatch(
                r"msg_[a-zA-Z0-9_-]{1,120}", turn
            ):
                raise HTTPException(403, "Model turn required")
            return {
                **grant,
                "scope": "model.invoke",
                "workspace_session": session,
                "turn_id": turn,
                "request_id": request_id,
            }
        scope = (
            "model.usage"
            if service == "model"
            else ("knowledge.mcp" if operation == "mcp" else "knowledge.web")
        )
        return {**grant, "scope": scope}

    app.state.openscience_m4 = True
    install_knowledge(app, principal)
    install_model(app, principal)
