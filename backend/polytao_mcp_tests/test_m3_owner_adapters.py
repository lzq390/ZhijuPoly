"""Exercise both real MCP runtimes and persistent journals with isolated native HTTP fixtures."""

import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
import json
import secrets
import sqlite3
from uuid import uuid4

import httpx2 as httpx
import pytest

from app.auth import openscience_m3_adapters as adapters
from app.polytao_mcp_contract import DESCRIPTOR_DESCRIPTIONS


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["polytao", "retrosynthesis"])
@pytest.mark.parametrize("receipt_fault", ["response_lost", "lease_expired", "legacy_expiry"])
async def test_scoped_confirm_and_recover_after_restart(tmp_path, monkeypatch, kind, receipt_fault):
    owner_a, owner_b = sorted(adapters.OWNERS)
    alice, other_alice, bob = (str(uuid4()) for _ in range(3))
    ingress, authority = (secrets.token_urlsafe(32) for _ in range(2))
    for label, key in (("ingress", ingress), ("authority", authority)):
        (tmp_path / label).write_text(key)
    for key, value in {
        "M3_ADAPTER": kind,
        "M3_ADAPTER_STATE_DIR": tmp_path,
        "M3_ADAPTER_INGRESS_FILE": tmp_path / "ingress",
        "M3_ADAPTER_AUTHORITY_FILE": tmp_path / "authority",
        "M3_AUTHORITY_SOCKET": "/unused",
        "M3_ADAPTER_VERIFY_DROP_RECEIPT": "1" if receipt_fault == "response_lost" else "0",
        "M3_ADAPTER_VERIFY_EVIDENCE": tmp_path / "faults.json",
        "M3_ADAPTER_SUBMITS_PER_MINUTE": "1",
    }.items():
        monkeypatch.setenv(key, str(value))

    operations, grants, accepted = {}, {}, {}
    expired_operations = set()
    receipts_available = True
    poly = kind == "polytao"
    prepare = "prepare_polytao_generation" if poly else "prepare_monomer_retrosynthesis"
    confirm = "confirm_polytao_generation" if poly else "confirm_monomer_retrosynthesis"
    conversation = "ses_fixture"
    seed = {"conversation_id": conversation}
    if poly:
        seed["descriptors"] = dict.fromkeys(DESCRIPTOR_DESCRIPTIONS, 1.0)
    else:
        seed["target"] = {"source": "smiles", "smiles": "CCO"}

    def fixture(request):
        assert request.headers["authorization"] == "Bearer " + authority
        data = json.loads(request.content or b"{}")
        path = request.url.path
        if path in (adapters.CONTEXT, adapters.EXCHANGE):
            if data["operation_token"] in expired_operations:
                return httpx.Response(401, json={})
            ctx = operations[data["operation_token"]]
            if (data["arguments_sha256"], data["tool"]) != (ctx["digest"], ctx["tool"]):
                return httpx.Response(403, json={})
            if path == adapters.CONTEXT:
                return httpx.Response(200, json=ctx)
            grant = secrets.token_urlsafe(32)
            grants[grant] = (ctx["owner_id"], data)
            return httpx.Response(200, json={"access_token": grant})
        owner, exchange = grants.pop(request.headers["x-openscience-delegation"])
        assert sha256(request.content).hexdigest() == exchange["request_sha256"]
        if path.endswith("/targets/resolve"):
            return httpx.Response(
                200,
                json={
                    "status": "resolved",
                    "target": {
                        "canonical_smiles": "CCO",
                        "input_smiles": "CCO",
                        "inferred_target_role": "other",
                        "source": {"type": "smiles"},
                    },
                    "candidates": [],
                    "message": "resolved",
                },
            )
        key = (owner, exchange["idempotency_key"])
        if path.endswith("/jobs/lookup"):
            if not receipts_available:
                return httpx.Response(404, json={})
            return httpx.Response(200, json=accepted[key])
        assert path.endswith("/jobs") and key not in accepted, "a native submit was repeated"
        accepted[key] = {
            "job_id": "polytao.instance." + uuid4().hex if poly else str(uuid4()),
            "status": "pending",
        }
        if receipt_fault == "lease_expired":
            expired_operations.add(exchange["operation_token"])
            return httpx.Response(401, json={"code": "native_response_withheld"})
        if receipt_fault == "legacy_expiry":
            return httpx.Response(401, json={"detail": "Native authorization expired"})
        return httpx.Response(202, json=accepted[key])

    def transport(*, uds):
        assert uds == "/unused"
        return httpx.MockTransport(fixture)

    monkeypatch.setattr(adapters.httpx, "AsyncHTTPTransport", transport)

    @asynccontextmanager
    async def running():
        app = adapters.create_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://localhost",
                headers={"accept": "application/json", "authorization": "Bearer " + ingress},
            ) as client:
                yield client

    async def invoke(client, tool, args, login=alice, *, raw=None, signed=None, channel=None):
        operation = secrets.token_urlsafe(32)
        canonical = json.dumps(args, separators=(",", ":"))
        operations[operation] = {
            "owner_id": owner_b if login == bob else owner_a,
            "login_id": login,
            "channel": channel or ("ui" if tool == confirm else "model"),
            "confirmed": tool == confirm,
            "tool": tool,
            "workspace_session": conversation,
            "digest": sha256((signed or canonical).encode()).hexdigest(),
        }
        r = await client.post(
            "/mcp",
            headers={"x-openscience-operation": operation},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": tool,
                    "arguments": args,
                    "_meta": {"nexpoly/arguments-json": raw or canonical},
                },
            },
        )
        assert r.status_code == 200 and "result" in r.json(), r.text
        return r.json()["result"]["structuredContent"]

    def ref(draft):
        return {key: draft[key] for key in ("conversation_id", "draft_id", "revision")}

    async with running() as http:
        assert (await http.post("/mcp", headers={"authorization": "Bearer " + authority})).status_code == 403
        assert (await invoke(http, prepare, seed, signed="{}"))["error"]["code"] == "PERMISSION_DENIED"
        assert (await invoke(http, prepare, seed, raw="{}"))["error"]["code"] == "INVALID_ARGUMENT"
        for actor, login in ((owner_a, alice), (owner_b, bob)):
            draft = ref(await invoke(http, prepare, seed, login))
            with sqlite3.connect(tmp_path / actor / "journal.db") as db:
                table = "generation_drafts" if poly else "retrosynthesis_drafts"
                row = db.execute(
                    f"SELECT account_uuid,payload FROM {table} WHERE draft_id=?", (draft["draft_id"],)
                ).fetchone()
                assert row[0] == actor and json.loads(row[1])["_m3_initiating_login"] == login
            if login == alice:
                a_draft = draft
            else:
                b_draft = draft
        a_extra = ref(await invoke(http, prepare, seed))
        assert (await invoke(http, confirm, a_draft, channel="model"))["error"]["http_status"] == 403
        assert (await invoke(http, confirm, a_draft, other_alice))["error"]["http_status"] == 403
        assert (await invoke(http, confirm, a_draft, bob))["error"]["http_status"] == 404
        assert not accepted
        failure = "SESSION_EXPIRED" if receipt_fault == "legacy_expiry" else "SUBMISSION_OUTCOME_UNKNOWN"
        assert (await invoke(http, confirm, a_draft))["error"]["code"] == failure
        assert len(accepted) == 1

    # New service process, same state directory and key. Race two explicit owner confirmations.
    async with running() as http:
        assert (await invoke(http, confirm, a_draft, other_alice))["error"]["http_status"] == 403
        if receipt_fault == "legacy_expiry":
            read = "get_polytao_draft" if poly else "get_retrosynthesis_draft"
            identity = {k: a_draft[k] for k in ("draft_id", "conversation_id")}
            receipts_available = False
            assert (await invoke(http, read, identity))["status"] == "rejected"
            receipts_available = True
            assert (await invoke(http, read, identity))["status"] == "submitted"
        first, second = await asyncio.gather(invoke(http, confirm, a_draft), invoke(http, confirm, a_draft))
        assert first["job_id"] == second["job_id"]
        assert first["submission"]["replayed"] and second["submission"]["replayed"]
        assert (await invoke(http, confirm, a_extra))["error"]["http_status"] == 429
        assert (await invoke(http, confirm, b_draft, bob))["error"]["code"] == failure
        b_receipt = await invoke(http, confirm, b_draft, bob)
        assert b_receipt["submission"]["replayed"] and b_receipt["job_id"] != first["job_id"]
    assert len(accepted) == 2
    if receipt_fault == "response_lost":
        probe = json.loads((tmp_path / "faults.json").read_bytes())
        assert probe["dropped"] == probe["recovered"] and len(probe["recovered"]) == 2
    elif receipt_fault == "lease_expired":
        assert len(expired_operations) == 2


def test_owner_journal_publication(tmp_path, monkeypatch):
    import multiprocessing
    import os
    from types import SimpleNamespace

    cfg = SimpleNamespace(
        name="polytao",
        evidence=None,
        state=tmp_path,
        authority_socket="/unused",
        authority_key=secrets.token_urlsafe(32),
    )
    service = adapters.Adapter(cfg)
    owner, damaged_owner = sorted(adapters.OWNERS)
    initialize = service.journal_type.initialize

    children = multiprocessing.get_context("fork")

    def interrupted(path):
        path.touch()
        os._exit(23)  # Kill only this test's private initializer, before the directory rename.

    monkeypatch.setattr(service.journal_type, "initialize", staticmethod(interrupted))
    interrupted_child = children.Process(target=service.published_journal, args=(owner,))
    interrupted_child.start()
    interrupted_child.join(5)
    assert interrupted_child.exitcode == 23
    assert not (tmp_path / owner).exists()

    started, release = children.Event(), children.Event()
    initializations = children.Value("i", 0)
    results = children.Queue()

    def hold(path):
        with initializations.get_lock():
            initializations.value += 1
        started.set()
        assert release.wait(5)
        initialize(path)

    def publish():
        path = service.published_journal(owner)
        results.put(path.with_suffix(".db.identity").read_text())

    monkeypatch.setattr(service.journal_type, "initialize", staticmethod(hold))
    first, second = children.Process(target=publish), children.Process(target=publish)
    first.start()
    assert started.wait(5)
    second.start()
    assert not (tmp_path / owner).exists()
    release.set()
    for child in (first, second):
        child.join(5)
        assert child.exitcode == 0
    assert initializations.value == 1
    assert results.get(timeout=2) == results.get(timeout=2)
    results.close()
    results.join_thread()
    check = service.journal(owner)
    assert check.identity == (tmp_path / owner / "journal.db.identity").read_text()
    with sqlite3.connect(check.path) as db:
        assert db.execute("SELECT account_uuid FROM metadata").fetchone() == (owner,)
        assert db.execute("SELECT count(*) FROM m3_admissions").fetchone() == (0,)

    # Existing damaged directories must remain available for diagnosis.
    damaged = tmp_path / damaged_owner
    damaged.mkdir(mode=0o700)
    (damaged / "journal.db").write_bytes(b"original damaged content")
    with pytest.raises(service.journal_errors[1]):
        service.journal(damaged_owner)
    assert (damaged / "journal.db").read_bytes() == b"original damaged content"
    assert not (damaged / "journal.db.identity").exists()
    asyncio.run(service.close())
