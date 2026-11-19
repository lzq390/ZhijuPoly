"""Delegation boundary regressions; the integration case needs the isolated seeded M3 PG."""

import asyncio
import hashlib
import os
from pathlib import Path
import sys
import time
from types import ModuleType
from uuid import uuid4

import pytest

from app.auth.openscience_delegation_dev import DFT, EXCHANGE, ISSUE, allowed_target


def test_native_targets_are_bound_to_the_tool_and_job():
    job, other = str(uuid4()), str(uuid4())
    assert allowed_target("dft_job_list", None, "GET", DFT, "page_size=10")
    assert not allowed_target("dft_job_list", None, "GET", DFT, "owner=other")
    assert not allowed_target("dft_job_list", None, "POST", DFT, "")
    for tool in ("dft_job_status", "dft_job_result"):
        assert allowed_target(tool, job, "GET", DFT + "/" + job, "")
        assert not allowed_target(tool, job, "GET", DFT + "/" + other, "")
        assert not allowed_target(tool, job, "GET", DFT + "/" + job + "/artifacts/x", "")
    assert allowed_target("dft_job_submit", None, "POST", DFT, "")
    assert not allowed_target("predict_polymer_properties", None, "POST", DFT, "")


@pytest.mark.skipif(
    os.getenv("OPENSCIENCE_M3_AUTH_TEST") != "1",
    reason="requires dedicated dev-m1 credentials and a seeded isolated PostgreSQL test cluster",
)
def test_real_login_delegation_scope_replay_expiration_and_logout(monkeypatch, tmp_path):
    import httpx
    from fastapi import Request
    from app.auth.context import current_identity, is_service_context
    import app.auth.openscience_delegation_dev as delegation

    monkeypatch.setattr(delegation, "GENERATIONS", tmp_path)
    for user in ("test-user-a", "test-user-b"):
        (tmp_path / user).write_text("a" * 64)

    monkeypatch.setenv("OPENSCIENCE_M3_ADAPTERS_ENABLED", "1")
    service_headers = {}
    for name in ("polytao", "retrosynthesis"):
        secret = uuid4().hex
        credential = tmp_path / (name + "-service")
        credential.write_text(secret)
        monkeypatch.setenv("M3_" + name.upper() + "_AUTHORITY_FILE", str(credential))
        service_headers[name] = {"Authorization": "Bearer " + secret}

    # The probe exposes the native middleware's canonical owner and idempotency key.
    # Separate E2E exercises the six real routes, workers and entitlement accounting.
    def install_probe(app):
        @app.post("/api/v1/conditional-generation/polytao/jobs")
        @app.post("/api/v1/predict")
        async def predict(request: Request):
            identity = current_identity()
            assert not is_service_context() and "authorization" not in request.headers
            assert "x-openscience-delegation" not in request.headers
            return {
                "owner": identity.user_id,
                "login": identity.session_id,
                "key": request.headers["idempotency-key"],
            }

    module = ModuleType("app.auth.openscience_m3_native")
    module.install_native = install_probe
    monkeypatch.setitem(sys.modules, module.__name__, module)
    from app.auth.openscience_dev import create_app

    app = create_app()
    parent = "https://app.m2.test:18760"
    agent = "https://agent.m2.test:18761"

    def key(name):
        return Path(os.environ[name]).read_text().strip()

    router = {"Authorization": "Bearer " + key("OPENSCIENCE_M2_ROUTER_TOKEN_FILE")}
    gateway = {"Authorization": "Bearer " + key("OPENSCIENCE_M3_GATEWAY_TOKEN_FILE")}
    payload = b'{"smiles":"C"}'
    sha = hashlib.sha256(payload).hexdigest()
    operation = str(uuid4())
    tool = "predict_polymer_properties"
    path = "/api/v1/predict"

    async def check():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1250))
        async with (
            httpx.AsyncClient(transport=transport, base_url=parent) as a,
            httpx.AsyncClient(transport=transport, base_url=parent) as b,
            httpx.AsyncClient(transport=transport, base_url="http://localhost") as trusted,
        ):

            async def sign(user, client):
                response = await client.post(
                    "/api/v1/auth/login",
                    headers={"Origin": parent},
                    json={
                        "username": "test-user-" + user,
                        "password": key("OPENSCIENCE_M2_PASSWORD_" + user.upper() + "_FILE"),
                    },
                )
                assert response.status_code == 200
                session = response.json()
                private = {
                    "Origin": parent,
                    "X-CSRF-Token": session["csrf_token"],
                    "X-Session-Context": session["session_id"],
                }
                ticket = (
                    await client.post("/api/v1/openscience/workspace-ticket", headers=private)
                ).json()
                handoff = await trusted.post(
                    "/internal/openscience/tickets/redeem",
                    headers=router,
                    json={
                        "agent_origin": agent,
                        "parent_origin": parent,
                        "ticket": ticket["ticket"],
                        "handoff_id": ticket["handoff_id"],
                    },
                )
                assert handoff.status_code == 200
                return handoff.json()["agent_session"], private, session

            token_a, private_a, user_a = await sign("a", a)
            token_b, private_b, user_b = await sign("b", b)
            user_b_id = user_b["user"]["id"]
            first = {
                "agent_session": token_a,
                "agent_origin": agent,
                "operation_id": operation,
                "tool": tool,
                "arguments_sha256": sha,
                "workspace_session": "ses_test",
                "workspace_generation": "a" * 64,
                "turn_expires_ms": int(time.time() * 1000) + 60000,
                "turn_id": "msg_test",
                "call_id": "call_test",
            }

            def issue(args):
                return trusted.post(ISSUE, headers=router, json=args)

            # Every hop independently authenticates the calling adapter. The last hop
            # additionally checks the login on the prepare call and the exact draft revision.
            prepare = {**first, "tool": "prepare_polytao_generation", "operation_id": str(uuid4())}
            assert (await issue(prepare)).status_code == 200
            draft_id = "ptd_" + uuid4().hex
            form = {
                **first,
                "channel": "ui",
                "confirmed": True,
                "operation_id": str(uuid4()),
                "tool": "confirm_polytao_generation",
                "draft_id": draft_id,
                "revision": 2,
            }
            assert (await issue({**form, "channel": "model"})).status_code == 403
            op_token = (await issue(form)).json()["operation_token"]
            context = {"tool": form["tool"], "arguments_sha256": sha, "operation_token": op_token}
            poly, retro = service_headers["polytao"], service_headers["retrosynthesis"]
            assert (
                await trusted.post(delegation.ADAPTER_CONTEXT, headers=retro, json=context)
            ).status_code == 403
            assert (await trusted.post(delegation.ADAPTER_CONTEXT, headers=poly, json=context)).json()[
                "login_id"
            ] == user_a["session_id"]
            native_route = "/api/v1/conditional-generation/polytao/jobs"
            stable = delegation.adapters.draft_key(user_a["user"]["id"], draft_id, 2)
            exchange_form = {
                **context,
                "method": "POST",
                "path": native_route,
                "request_sha256": sha,
                "idempotency_key": stable,
            }

            async def exchange_adapter(data, headers=poly):
                return await trusted.post(delegation.ADAPTER_EXCHANGE, headers=headers, json=data)

            assert (await exchange_adapter(exchange_form, retro)).status_code == 403
            assert (
                await exchange_adapter({**exchange_form, "idempotency_key": "a" * 64})
            ).status_code == 403
            wrong_login = (
                await issue({**form, "agent_session": token_b, "operation_id": str(uuid4())})
            ).json()["operation_token"]
            assert (
                await exchange_adapter(
                    {
                        **exchange_form,
                        "operation_token": wrong_login,
                        "idempotency_key": delegation.adapters.draft_key(user_b_id, draft_id, 2),
                    }
                )
            ).status_code == 403
            native_token = (await exchange_adapter(exchange_form)).json()["access_token"]

            async def use_adapter(headers):
                return await trusted.post(
                    native_route,
                    headers={**headers, "X-OpenScience-Delegation": native_token},
                    content=payload,
                )

            assert (await use_adapter(retro)).status_code == 403
            assert (await use_adapter(gateway)).status_code == 403
            assert (await use_adapter(poly)).json()["key"] == stable
            assert (await use_adapter(poly)).status_code == 401
            assert (await trusted.post(ISSUE, headers=gateway, json=first)).status_code == 403
            assert (await issue({k: v for k, v in first.items() if k != "turn_id"})).status_code == 422
            assert (await issue({**first, "tool": "dft_job_submit"})).status_code == 403
            assert (await issue({**first, "turn_expires_ms": 1})).status_code == 401
            issued = await issue(first)
            assert issued.status_code == 200
            assert (await issue({**first, "agent_session": token_b})).status_code == 409
            assert (await issue({**first, "turn_id": "msg_other"})).status_code == 409
            req = {
                "operation_token": issued.json()["operation_token"],
                "tool": tool,
                "arguments_sha256": sha,
                "method": "POST",
                "path": path,
                "request_sha256": sha,
                "query": "",
            }

            def exchange(args):
                return trusted.post(EXCHANGE, headers=gateway, json=args)

            assert (await trusted.post(EXCHANGE, headers=router, json=req)).status_code == 403
            assert (await exchange({**req, "tool": "dft_job_submit"})).status_code == 403
            assert (await exchange({**req, "path": DFT})).status_code == 403
            assert (await exchange({**req, "arguments_sha256": "0" * 64})).status_code == 403
            granted = await exchange(req)
            assert granted.status_code == 200
            native = granted.json()["access_token"]
            assert granted.json()["valid_until"] <= issued.json()["valid_until"]
            headers = {
                **gateway,
                "X-OpenScience-Delegation": native,
                "Idempotency-Key": "client-must-not-win",
            }
            assert (
                await trusted.post(path, headers={**headers, "Cookie": "dummy=yes"}, content=payload)
            ).status_code == 403
            assert (await trusted.post(path, headers=headers, content=b"{}")).status_code == 403
            assert (
                await trusted.post(path + "?other=yes", headers=headers, content=payload)
            ).status_code == 403
            assert (await trusted.post(DFT, headers=headers, content=payload)).status_code == 403
            # Only one request can claim this access token, including on concurrent retry.
            responses = await asyncio.gather(
                *[trusted.post(path, headers=headers, content=payload) for _ in range(2)]
            )
            assert sorted(r.status_code for r in responses) == [200, 401]
            assert next(r.json() for r in responses if r.status_code == 200) == {
                "owner": user_a["user"]["id"],
                "login": user_a["session_id"],
                "key": "openscience-" + operation,
            }
            # Replacing a workspace retires already issued credentials at both hops.
            old_native = (await exchange(req)).json()["access_token"]
            (tmp_path / "test-user-a").write_text("b" * 64)
            assert (await issue(first)).status_code == 401
            assert (await exchange(req)).status_code == 401
            assert (
                await trusted.post(
                    path, headers={**gateway, "X-OpenScience-Delegation": old_native}, content=payload
                )
            ).status_code == 401
            first.update(workspace_generation="b" * 64, operation_id=str(uuid4()))
            req["operation_token"] = (await issue(first)).json()["operation_token"]
            stale = await exchange(req)
            assert stale.status_code == 200
            expires_ms = int(time.time() * 1000) + 2400
            exp = await issue(
                {
                    **first,
                    "workspace_generation": "a" * 64,
                    "agent_session": token_b,
                    "operation_id": str(uuid4()),
                    "turn_expires_ms": expires_ms,
                }
            )
            assert exp.status_code == 200
            assert exp.json()["valid_until"] == expires_ms / 1000
            full = await issue(
                {
                    **first,
                    "workspace_generation": "a" * 64,
                    "agent_session": token_b,
                    "operation_id": str(uuid4()),
                }
            )
            assert 0 < full.json()["valid_until"] - time.time() <= 30
            assert (await a.post("/api/v1/auth/logout", headers=private_a)).status_code == 204
            assert (await exchange(req)).status_code == 401
            assert (
                await trusted.post(
                    path,
                    headers={**gateway, "X-OpenScience-Delegation": stale.json()["access_token"]},
                    content=payload,
                )
            ).status_code == 401
            b_req = {**req, "operation_token": exp.json()["operation_token"]}
            assert (await exchange(b_req)).status_code == 200
            await asyncio.sleep(max(0, exp.json()["valid_until"] - time.time()) + 0.1)
            assert (await exchange(b_req)).status_code == 401
            full_req = {**req, "operation_token": full.json()["operation_token"]}
            assert (await exchange(full_req)).status_code == 200
            await asyncio.sleep(max(0, full.json()["valid_until"] - time.time()) + 0.1)
            assert (await exchange(full_req)).status_code == 401
            assert (await b.post("/api/v1/auth/logout", headers=private_b)).status_code == 204

    asyncio.run(check())
