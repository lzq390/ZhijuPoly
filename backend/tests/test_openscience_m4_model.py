"""Owner accounting and provider failure cases in an isolated, ephemeral PostgreSQL database."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
import httpx
import psycopg
from psycopg.rows import dict_row
import pytest

from app.auth.openscience_m4_model import BASE, ModelLedger, install_model, provider_input


@pytest.fixture(scope="module")
def ledger():
    dsn = os.environ.get("OPENSCIENCE_M4_MODEL_TEST_DSN")
    if not dsn:
        pytest.skip("requires the isolated ephemeral model-accounting test database")
    # Deliberately distinct from the persistent dev-m1 deployment and normal test/prod DBs.
    if psycopg.conninfo.conninfo_to_dict(dsn).get("dbname") != "m4_model_test":
        pytest.fail("Expected a dedicated M4 model test database")
    with psycopg.connect(dsn) as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname='nexpoly_auth'").fetchone():
            conn.execute("CREATE ROLE nexpoly_auth NOLOGIN")
        if not conn.execute("SELECT to_regclass('openscience_m4.model_requests')").fetchone()[0]:
            migration = Path(__file__).resolve().parents[1] / "migrations/openscience-dev/0006_m4_model.sql"
            conn.execute(migration.read_text())
        for column, filename in (
            ("request_tag", "0008_m4_model_retry.sql"),
            ("workspace_generation_fingerprint", "0009_m4_model_generation.sql"),
        ):
            if not conn.execute(
                "SELECT 1 FROM pg_attribute WHERE attrelid='openscience_m4.model_requests'::regclass "
                "AND attname=%s AND NOT attisdropped", (column,),
            ).fetchone():
                migration = Path(__file__).resolve().parents[1] / "migrations/openscience-dev" / filename
                conn.execute(migration.read_text())

    @contextmanager
    def connect():
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            conn.execute("SET LOCAL ROLE nexpoly_auth")
            yield conn

    return ModelLedger(connect, budget=30)


def actor(owner=None):
    return {
        "user_id": str(owner or uuid4()),
        "session_id": str(uuid4()),
        "workspace_user_id": "test-user-a",
        "workspace_generation_fingerprint": "a" * 64,
        "scope": "model.invoke",
        "environment": "dev-m1",
        "workspace_session": "ses_test",
        "turn_id": "msg_test",
        "turn_expires_ms": time.time_ns() // 1_000_000 + 900000,
        "request_id": str(uuid4()),
    }


def test_atomic_admission_retry_and_owner_budgets(ledger):
    a, b = actor(), actor()
    rows = [{**a, "request_id": str(uuid4())} for _ in range(3)]

    def admit(row):
        try:
            ledger.reserve(row, "chat.completions", 10, 5)
            return 200
        except HTTPException as e:
            return e.status_code

    with ThreadPoolExecutor(max_workers=3) as pool:
        codes = list(pool.map(admit, rows))
    assert sorted(codes) == [200, 200, 429]
    assert admit(b) == 200  # third global slot; A exhausted both its budget and its own stream limit.
    first, second = [r for r, c in zip(rows, codes) if c == 200]
    assert ledger.summary(a["user_id"])["reserved_tokens"] == 30
    ledger.settle(first["request_id"], None, "stream_interrupted")
    ledger.settle(second["request_id"], None, "missing_usage")
    assert ledger.summary(a["user_id"])["estimated_tokens"] == 30
    assert admit({**a, "request_id": str(uuid4())}) == 429  # no active A streams, still at the daily limit.
    receipt = {"input_tokens": 10, "output_tokens": 2}
    for _ in range(2):
        ledger.settle(first["request_id"], receipt, None)
    ledger.settle(first["request_id"], None, "stream_interrupted")
    assert admit(first) == 409
    summary = ledger.summary(a["user_id"])
    assert len(summary["records"]) == 2 and summary["used_tokens"] == 12
    assert summary["estimated_tokens"] == 15 and summary["reserved_tokens"] == 0
    assert b["request_id"] not in {str(r["request_id"]) for r in summary["records"]}
    ledger.release(b["request_id"])


def test_pre_generation_request_keeps_its_usage_and_unknown_generation(ledger):
    historical = actor()
    with ledger.connect() as conn:
        # Match receipts written by the earlier gateway (including during a rollback).
        conn.execute(
            """INSERT INTO openscience_m4.model_requests
                (request_id,environment,owner_id,login_id,workspace_user_id,workspace_session,turn_id,
                 model,protocol,reserved_input,max_output,request_tag)
               VALUES (%s,'dev-m1',%s,%s,'test-user-a','ses_test','msg_test','qwen2.5-7b','responses',8,4,%s)""",
            (historical["request_id"], historical["user_id"], historical["session_id"], b"t" * 32),
        )
    ledger.settle(historical["request_id"], {"input_tokens": 8, "output_tokens": 1}, None)
    usage = ledger.summary(historical["user_id"])
    assert usage["used_tokens"] == 9 and usage["records"][0]["workspace_generation_fingerprint"] is None


def test_model_protocols_missing_usage_and_interrupted_provider(ledger):
    actors = {}
    calls = []

    def principal(request: Request):
        return actors[request.headers["test-id"]]

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"text":"hello"}\n'
            raise httpx.ReadError("provider stream interrupted")

    def provider(request):
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"input_tokens": 8})
        prompt = json.loads(request.content)["prompt"]
        calls.append(prompt)
        if "interrupt" in prompt:
            return httpx.Response(200, stream=BrokenStream())
        result = '{"text":"hello"}\n'
        if "omit" not in prompt:
            result += '{"usage":{"input_tokens":8,"output_tokens":2}}\n'
        return httpx.Response(200, text=result)

    app = FastAPI()
    install_model(
        app, principal, request_key=b"dev-test-key", transport=httpx.MockTransport(provider), ledger=ledger
    )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for kind in ("chat/completions", "responses", "omit", "interrupt"):
                ident = actor()
                actors[kind] = ident
                path = "/v1/" + (kind if "/" in kind or kind == "responses" else "chat/completions")
                body = {"model": "qwen2.5-7b", "stream": True, "max_tokens": 4}
                if kind == "responses":
                    body["input"] = "normal response"
                else:
                    body["messages"] = [{"role": "user", "content": kind}]

                async def run():
                    return await client.post(BASE + path, headers={"test-id": kind}, json=body)

                if kind == "interrupt":
                    with pytest.raises((ExceptionGroup, httpx.ReadError)):
                        await run()
                else:
                    response = await run()
                    assert response.status_code == 200 and "hello" in response.text
                    if kind == "responses":
                        assert "event: response.completed" in response.text
                assert (await run()).status_code == 409
                summary = ledger.summary(ident["user_id"])
                assert len(summary["records"]) == 1 and summary["reserved_tokens"] == 0
                if kind in {"interrupt", "omit"}:
                    assert summary["estimated_tokens"] == 12 and summary["used_tokens"] == 0
                    assert summary["records"][0]["reason"] == (
                        "stream_interrupted" if kind == "interrupt" else "missing_usage"
                    )
                else:
                    assert summary["used_tokens"] == 10 and summary["estimated_tokens"] == 0

    asyncio.run(exercise())
    assert len(calls) == 4


def test_model_does_not_accept_user_selected_provider():
    with pytest.raises(HTTPException):
        provider_input(
            {"model": "unrecognized", "messages": [{"role": "user", "content": "x"}]}, "chat.completions"
        )
    work = provider_input({"model": "qwen2.5-7b", "input": "hello", "max_output_tokens": 999}, "responses")
    assert work["max_output"] == 12 and "hello" in work["prompt"]


def test_downstream_disconnect_and_late_worker_receipt(ledger):
    current = actor()
    receipts = {}
    delivered = asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"text":"first-fragment"}\n'
            await asyncio.Event().wait()

    def provider(request):
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"input_tokens": 8})
        if request.url.path == "/receipts":
            ids = json.loads(request.content)["requests"]
            return httpx.Response(200, json={i: receipts[i] for i in ids if i in receipts})
        assert json.loads(request.content)["request_id"] == current["request_id"]
        return httpx.Response(200, stream=SlowStream())

    def principal(request: Request):
        return {**current, "scope": "model.usage" if request.method == "GET" else "model.invoke"}

    app = FastAPI()
    install_model(
        app, principal, request_key=b"dev-test-key", transport=httpx.MockTransport(provider), ledger=ledger
    )
    body = json.dumps(
        {"model": "qwen2.5-7b", "input": "hello", "stream": True, "max_output_tokens": 4}
    ).encode()

    async def check():
        body_sent = False

        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": body}
            await delivered.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if b"first-fragment" in message.get("body", b""):
                delivered.set()

        path = BASE + "/v1/responses"
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "server": ("local", 80),
            "client": ("127.0.0.1", 1024),
        }
        await asyncio.wait_for(app(scope, receive, send), 3)
        assert delivered.is_set()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            before = (await client.get(BASE + "/usage")).json()
            assert before["estimated_tokens"] == 12 and before["reserved_tokens"] == 0
            receipts[current["request_id"]] = {"input_tokens": 8, "output_tokens": 1}
            for _ in range(2):
                after = (await client.get(BASE + "/usage")).json()
                assert after["used_tokens"] == 9 and after["estimated_tokens"] == 0
                assert len(after["records"]) == 1 and after["records"][0]["state"] == "actual"

    asyncio.run(check())


def test_concurrent_gateway_streams_reserve_per_owner(ledger):
    a, b = actor(), actor()
    requests = {
        row["request_id"]: row
        for row in [a, b, {**a, "request_id": str(uuid4())}, {**a, "request_id": str(uuid4())}]
    }
    first, other, second, extra = requests
    running = {rid: asyncio.Event() for rid in (first, second)}
    release = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        def __init__(self, rid):
            self.rid = rid

        async def __aiter__(self):
            yield b'{"text":"running"}\n'
            if self.rid in running:
                running[self.rid].set()
                await release.wait()
            yield b'{"usage":{"input_tokens":10,"output_tokens":1}}\n'

    def provider(request):
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"input_tokens": 10})
        return httpx.Response(200, stream=Stream(json.loads(request.content)["request_id"]))

    def principal(request: Request):
        return requests[request.headers["test-id"]]

    app = FastAPI()
    install_model(
        app, principal, request_key=b"dev-test-key", transport=httpx.MockTransport(provider), ledger=ledger
    )
    body = {"model": "qwen2.5-7b", "input": "hi", "stream": True, "max_output_tokens": 5}

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:

            async def call(rid):
                return await client.post(BASE + "/v1/responses", headers={"test-id": rid}, json=body)

            tasks = [asyncio.create_task(call(rid)) for rid in (first, second)]
            try:
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in running.values())), 5)
                assert ledger.summary(a["user_id"])["reserved_tokens"] == 30
                assert (await call(extra)).status_code == 429
                assert (await call(other)).status_code == 200
                assert (await call(extra)).status_code == 429
            finally:
                release.set()
                done = await asyncio.gather(*tasks)
            assert all(response.status_code == 200 for response in done)
            assert ledger.summary(a["user_id"])["used_tokens"] == 22
            assert ledger.summary(b["user_id"])["used_tokens"] == 11

    asyncio.run(check())


@pytest.mark.parametrize("change", ["logout", "expiry", "generation"])
def test_recheck_after_token_count_refuses_new_model_work(ledger, monkeypatch, change):
    owner = actor()
    called = []
    active = True
    now_ms = owner["turn_expires_ms"] - 100
    generation = owner["workspace_generation_fingerprint"]
    monkeypatch.setattr(time, "time_ns", lambda: now_ms * 1_000_000)

    def principal(request: Request):
        if not active:
            raise HTTPException(401, "Login expired")
        return {**owner, "workspace_generation_fingerprint": generation}

    def provider(req):
        nonlocal active, now_ms, generation
        called.append(req.url.path)
        if req.url.path == "/tokenize":
            if change == "logout":
                active = False
            elif change == "expiry":
                now_ms = owner["turn_expires_ms"]
            else:
                generation = "b" * 64
            return httpx.Response(200, json={"input_tokens": 8})
        return httpx.Response(200, text='{"usage":{"input_tokens":8,"output_tokens":1}}\n')

    app = FastAPI()
    install_model(
        app, principal, request_key=b"dev-test-key", transport=httpx.MockTransport(provider), ledger=ledger
    )

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            reply = await client.post(BASE + "/v1/responses", json={"model": "qwen2.5-7b", "input": "hi"})
        assert reply.status_code == 401
        assert called == ["/tokenize"]
        usage = ledger.summary(owner["user_id"])
        assert usage["reserved_tokens"] == usage["estimated_tokens"] == usage["used_tokens"] == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("failure,code,reason", [
    ("busy", 429, "worker_busy"),
    ("connect", 503, "worker_not_connected"),
    ("connect_timeout", 503, "worker_not_connected"),
])
def test_unstarted_worker_retries_only_the_same_request(ledger, failure, code, reason):
    original = actor()
    alternate = {**original, "session_id": str(uuid4())}
    foreign = {**actor(), "request_id": original["request_id"]}
    logins = {
        "original": original, "second": alternate, "foreign": foreign,
        "restarted": {**original, "workspace_generation_fingerprint": "b" * 64},
        "renewed": {**original, "turn_expires_ms": original["turn_expires_ms"] + 1000},
    }
    attempts = []

    def principal(request: Request):
        return logins[request.headers["test-login"]]

    def provider(req):
        if req.url.path == "/tokenize":
            return httpx.Response(200, json={"input_tokens": 8})
        attempts.append(json.loads(req.content)["request_id"])
        if len(attempts) == 1:
            if failure == "connect":
                raise httpx.ConnectError("Unix socket not listening", request=req)
            if failure == "connect_timeout":
                raise httpx.ConnectTimeout("worker connection timed out", request=req)
            return httpx.Response(429)
        assert json.loads(req.content)["turn_expires_ms"] == original["turn_expires_ms"]
        return httpx.Response(200, text='{"usage":{"input_tokens":8,"output_tokens":1}}\n')

    app = FastAPI()
    install_model(
        app, principal, request_key=b"dev-test-key", transport=httpx.MockTransport(provider), ledger=ledger
    )

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:

            async def call(login="original", prompt="first"):
                return await client.post(
                    BASE + "/v1/responses",
                    headers={"test-login": login},
                    json={"model": "qwen2.5-7b", "input": prompt},
                )

            assert (await call()).status_code == code
            held = ledger.summary(original["user_id"])
            assert held["reserved_tokens"] == held["used_tokens"] == held["estimated_tokens"] == 0
            assert held["records"][0]["state"] == "released" and held["records"][0]["reason"] == reason
            for login in ("second", "foreign", "restarted", "renewed"):
                assert (await call(login)).status_code == 409
            assert (await call(prompt="other")).status_code == 409
            assert (await call()).status_code == 200
            assert (await call()).status_code == 409
            assert attempts == [original["request_id"]] * 2
            usage = ledger.summary(original["user_id"])
            assert usage["used_tokens"] == 9 and len(usage["records"]) == 1
            assert usage["records"][0]["workspace_generation_fingerprint"] == original["workspace_generation_fingerprint"]

    asyncio.run(check())


@pytest.mark.parametrize("failure,code,state", [
    ("read_error", 503, "estimated"), ("expired_in_worker", 401, "released"),
])
def test_provider_refusal_or_unknown_delivery(ledger, failure, code, state):
    owner = actor()
    attempts = []

    def principal(request: Request):
        return owner

    def provider(req):
        if req.url.path == "/tokenize":
            return httpx.Response(200, json={"input_tokens": 8})
        attempts.append(req.url.path)
        assert json.loads(req.content)["turn_expires_ms"] == owner["turn_expires_ms"]
        if failure == "read_error":
            raise httpx.ReadError("connection lost after request was sent", request=req)
        return httpx.Response(412)

    app = FastAPI()
    install_model(app, principal, request_key=b"test", ledger=ledger, transport=httpx.MockTransport(provider))

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            body = {"model": "qwen2.5-7b", "input": "hi", "max_tokens": 4}
            for expected in (code, 409):
                assert (await client.post(BASE + "/v1/responses", json=body)).status_code == expected
        assert attempts == ["/generate"]
        usage = ledger.summary(owner["user_id"])
        assert usage["reserved_tokens"] == usage["used_tokens"] == 0
        assert usage["records"][0]["state"] == state
        assert usage["estimated_tokens"] == (12 if state == "estimated" else 0)

    asyncio.run(check())
