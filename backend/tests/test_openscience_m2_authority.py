"""Integration checks against a freshly seeded, independent M2 authority database."""

import asyncio
import os
from pathlib import Path

import pytest


def test_candidates_cannot_use_each_others_browser_origins(monkeypatch, tmp_path):
    for key in ("APP_POSTGRES_DSN", "AUTH_POSTGRES_DSN", "APP_SERVICE_POSTGRES_DSN"):
        monkeypatch.setenv(key, "dbname=nexpoly_m2_dev")
    credential = tmp_path / "router"
    credential.write_text("x" * 40)
    monkeypatch.setenv("OPENSCIENCE_M2_ROUTER_TOKEN_FILE", str(credential))
    for key in ("OPENSCIENCE_M2_PARENT_ORIGIN", "OPENSCIENCE_M2_AGENT_ORIGIN", "OPENSCIENCE_M3_GATEWAY_TOKEN_FILE",
                "OPENSCIENCE_M4_ENABLED", "OPENSCIENCE_M2_ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)
    from app.auth.openscience_candidate import PROFILES
    from app.auth.openscience_dev import create_app

    for env, other in (("dev-m1", "dev-m6"), ("dev-m6", "dev-m1")):
        monkeypatch.setenv("OPENSCIENCE_M2_ENVIRONMENT", env)
        create_app()
        for key, origin in (("OPENSCIENCE_M2_PARENT_ORIGIN", "parent"), ("OPENSCIENCE_M2_AGENT_ORIGIN", "agent")):
            monkeypatch.setenv(key, PROFILES[other][origin])
            with pytest.raises(ValueError, match="different authority"):
                create_app()
            monkeypatch.delenv(key)


@pytest.mark.skipif(
    os.getenv("OPENSCIENCE_M2_INTEGRATION") != "1",
    reason="requires the provisioned local M2 development database",
)
def test_one_time_ticket_and_device_specific_revocation():
    import httpx
    from app.auth.openscience_dev import create_app

    app = create_app()

    parent = os.getenv("OPENSCIENCE_M2_PARENT_ORIGIN", "https://app.m2.test:18760")
    agent = os.getenv("OPENSCIENCE_M2_AGENT_ORIGIN", "https://agent.m2.test:18761")
    password = Path(os.environ["OPENSCIENCE_M2_PASSWORD_A_FILE"]).read_text().strip()
    service = {
        "Authorization": "Bearer " + Path(os.environ["OPENSCIENCE_M2_ROUTER_TOKEN_FILE"]).read_text().strip()
    }

    async def run():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with (
            httpx.AsyncClient(transport=transport, base_url=parent) as first,
            httpx.AsyncClient(transport=transport, base_url=parent) as second,
            httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as internal,
        ):

            async def sign(client):
                login = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "test-user-a", "password": password},
                    headers={"Origin": parent},
                )
                assert login.status_code == 200
                state = login.json()
                assert state["account_type"] == "member" and state["capabilities"]["openscience.use"]
                private = {
                    "Origin": parent,
                    "X-CSRF-Token": state["csrf_token"],
                    "X-Session-Context": state["session_id"],
                }
                signed = await client.post("/api/v1/openscience/workspace-ticket", headers=private)
                assert signed.status_code == 200
                result = signed.json()
                return private, {
                    "ticket": result["ticket"],
                    "handoff_id": result["handoff_id"],
                    "parent_origin": parent,
                    "agent_origin": agent,
                }

            first_headers, ticket = await sign(first)
            second_headers, other_ticket = await sign(second)
            path = "/internal/openscience/tickets/redeem"
            assert (await internal.post(path, json=ticket)).status_code == 403
            assert (
                await internal.post(path, headers=service, json={**ticket, "agent_origin": parent})
            ).status_code == 403
            copies = await asyncio.gather(
                *[internal.post(path, headers=service, json=ticket) for _ in range(2)]
            )
            assert sorted(r.status_code for r in copies) == [200, 401]
            grant = next(r.json() for r in copies if r.status_code == 200)
            grant2 = (await internal.post(path, headers=service, json=other_ticket)).json()
            assert grant["session_id"] != grant2["session_id"]
            assert grant["workspace_user_id"] == grant2["workspace_user_id"] == "test-user-a"

            async def validate(handle):
                return await internal.post(
                    "/internal/openscience/sessions/validate",
                    headers=service,
                    json={"agent_session": handle, "agent_origin": agent},
                )

            from time import time

            live = await validate(grant["agent_session"])
            assert live.status_code == 200
            assert time() < live.json()["valid_until"] <= time() + 30
            pending = await first.post("/api/v1/openscience/workspace-ticket", headers=first_headers)
            assert pending.status_code == 200
            unspent = {
                **ticket,
                "ticket": pending.json()["ticket"],
                "handoff_id": pending.json()["handoff_id"],
            }
            assert (await first.post("/api/v1/auth/logout", headers=first_headers)).status_code == 204
            assert (await validate(grant["agent_session"])).status_code == 401
            assert (await internal.post(path, headers=service, json=unspent)).status_code == 401
            assert (await validate(grant2["agent_session"])).status_code == 200
            assert (await second.post("/api/v1/auth/logout", headers=second_headers)).status_code == 204

    asyncio.run(run())


def test_private_unix_socket_peer_keeps_service_and_forwarded_header_checks(tmp_path, monkeypatch):
    import httpx

    for key in ("APP_POSTGRES_DSN", "AUTH_POSTGRES_DSN", "APP_SERVICE_POSTGRES_DSN"):
        monkeypatch.setenv(key, "dbname=nexpoly_m2_dev")
    credential = "local-test-credential-00000000000000000000"
    keyfile = tmp_path / "router-token"
    keyfile.write_text(credential)
    monkeypatch.setenv("OPENSCIENCE_M2_ROUTER_TOKEN_FILE", str(keyfile))
    from app.auth.openscience_dev import create_app

    app = create_app()

    async def request(path, headers, server=None, peer=None):
        async def listener(scope, receive, send):
            await app({**scope, "client": peer, "server": server}, receive, send)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=listener), base_url="http://localhost"
        ) as client:
            return await client.post(path, headers=headers, json={})

    async def run():
        service = {"Authorization": "Bearer " + credential}
        for path in ("/internal/openscience/tickets/redeem", "/internal/openscience/sessions/validate"):
            assert (
                await request(path, service)
            ).status_code == 422  # valid transport/secret, invalid payload
            assert (await request(path, {})).status_code == 403
            assert (await request(path, {**service, "X-Forwarded-For": "127.0.0.1"})).status_code == 403
            assert (await request(path, {**service, "Cookie": "unexpected=true"})).status_code == 403
            assert (await request(path, service, server=("127.0.0.1", 80))).status_code == 403
            assert (await request(path, service, peer=("192.0.2.1", 80))).status_code == 403
        assert (await request("/internal/another", service)).status_code == 403

    asyncio.run(run())
