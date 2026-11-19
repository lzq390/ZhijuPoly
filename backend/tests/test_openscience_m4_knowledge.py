"""Private text index behaviour and its independent PostgreSQL access boundary."""

import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg.conninfo import make_conninfo
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.auth.openscience_m4_knowledge import PREFIX, document_chunks, install_knowledge


@pytest.fixture(scope="module")
def database():
    cfg = shutil.which("pg_config")
    if not cfg or os.getuid() == 0:
        pytest.skip("requires unprivileged local PostgreSQL server binaries")
    bindir = Path(subprocess.check_output([cfg, "--bindir"], text=True).strip())
    if not (bindir / "initdb").is_file():
        pytest.skip("PostgreSQL server binaries unavailable")
    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations/openscience-dev/0007_m4_knowledge.sql"
    )
    with TemporaryDirectory(prefix="kb-m4-") as ipc:
        data = str(Path(ipc) / "pg")
        subprocess.run(
            [
                bindir / "initdb",
                "-D",
                data,
                "-A",
                "trust",
                "-U",
                "m4_admin",
                "-E",
                "UTF8",
                "--no-locale",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        server = [bindir / "pg_ctl", "-w", "-D", data]
        subprocess.run(
            [
                *server,
                "-o",
                f"-k {ipc} -c listen_addresses='' -c unix_socket_permissions=0700",
                "-l",
                str(Path(ipc) / "pg.log"),
                "start",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        try:
            admin = make_conninfo(dbname="postgres", user="m4_admin", host=ipc)
            a, b = str(uuid4()), str(uuid4())
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute("CREATE ROLE nexpoly_api LOGIN")
                conn.execute("CREATE SCHEMA auth")
                conn.execute("CREATE TABLE auth.users (user_id uuid PRIMARY KEY)")
                conn.execute("INSERT INTO auth.users VALUES (%s),(%s)", (a, b))
                conn.execute(migration.read_text())
            yield make_conninfo(dbname="postgres", user="nexpoly_api", host=ipc), a, b
        finally:
            subprocess.run(
                [*server, "stop", "-m", "immediate"],
                check=True,
                stdout=subprocess.DEVNULL,
            )


def test_owner_search_and_citations(database):
    dsn, a, b = database
    app = FastAPI()
    app.state.auth = SimpleNamespace(settings=SimpleNamespace(application_dsn=dsn))
    users = {"a": a, "b": b}

    def principal(request: Request):
        return {
            "user_id": users[request.headers["test-actor"]],
            "session_id": str(uuid4()),
            "environment": "dev-m1",
            "scope": request.headers["test-scope"],
        }

    install_knowledge(app, principal)
    clients = {
        k: TestClient(
            app,
            base_url="http://test",
            headers={"test-actor": k, "test-scope": "knowledge.web"},
        )
        for k in users
    }
    probes = {}
    for k, client in clients.items():
        kb = client.post(PREFIX + "/kbs", json={"name": "Identical"}).json()
        assert kb["owner_id"] == users[k]
        kb_id = kb["kb_id"]
        body = f"Shared query term. 私有文献。 Unique{k}-MARKER".encode()
        path = PREFIX + f"/kbs/{kb_id}/documents"
        doc_response = client.post(
            path, files={"file": ("same.txt", body, "text/plain")}
        )
        assert doc_response.status_code == 200, doc_response.text
        doc_id = doc_response.json()["id"]
        assert client.get(path + "/" + doc_id + "/download").content == body
        assert client.get(path).json()[0]["doc_id"] == doc_id
        search = client.post(PREFIX + "/search", json={"query": "文献"}).json()
        assert len(search["results"]) == 1
        hit = search["results"][0]
        probes[k] = kb_id, doc_id, hit, body

    for k, client in clients.items():
        other = "b" if k == "a" else "a"
        kb_id, doc_id, hit, body = probes[k]
        foreign_kb, foreign_doc, foreign_hit, _ = probes[other]
        assert client.get(PREFIX + "/kbs").json()[0]["doc_count"] == 1
        assert (
            client.get(
                PREFIX + hit["citation_url"].removeprefix("/knowledge-api")
            ).json()["text"]
            == body.decode()
        )
        assert (
            client.get(
                PREFIX + foreign_hit["citation_url"].removeprefix("/knowledge-api")
            ).status_code
            == 404
        )
        result = client.post(
            PREFIX + "/search", json={"query": f"Unique{other}-MARKER"}
        )
        assert result.json()["results"] == []
        assert (
            client.post(
                PREFIX + "/search",
                json={"query": "query", "kb_ids": [kb_id, foreign_kb]},
            ).status_code
            == 403
        )
        assert (
            client.post(
                PREFIX + f"/kbs/{foreign_kb}/documents",
                files={"file": ("foreign.txt", b"text")},
            ).status_code
            == 404
        )
        rpc = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "kb_read_citation",
                "arguments": {
                    "kb_id": kb_id,
                    "document_id": foreign_doc,
                    "chunk_id": hit["chunk_id"],
                },
            },
        }
        assert client.post(PREFIX + "/mcp", json=rpc).status_code == 403
        mcp = {"test-scope": "knowledge.mcp"}
        assert client.get(PREFIX + "/kbs", headers=mcp).status_code == 403
        result = client.post(PREFIX + "/mcp", json=rpc, headers=mcp).json()["result"]
        assert (
            result["isError"]
            and json.loads(result["content"][0]["text"])["error"]["code"] == "not_found"
        )
        rpc["params"]["arguments"]["document_id"] = doc_id
        result = client.post(PREFIX + "/mcp", json=rpc, headers=mcp).json()["result"]
        assert json.loads(result["content"][0]["text"])["text"] == body.decode()

    with psycopg.connect(dsn) as conn:
        for table in ("collections", "documents", "chunks"):
            assert (
                conn.execute(
                    f"SELECT count(*) FROM openscience_m4_knowledge.{table}"
                ).fetchone()[0]
                == 0
            )
        conn.execute("SELECT set_config('app.user_id',%s,true)", (a,))
        for table in ("collections", "documents", "chunks"):
            assert conn.execute(
                f"SELECT owner_id::text FROM openscience_m4_knowledge.{table}"
            ).fetchall() == [(a,)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO openscience_m4_knowledge.collections(owner_id,name) VALUES (%s,'forbidden')",
                (b,),
            )


def test_document_chunks_keep_quotable_text_and_bounded_size():
    original = "水溶性聚合物 " * 160 + "\nSecond paragraph " + "a" * 2450
    chunks = list(document_chunks(original))
    assert len(chunks) >= 3
    assert all(chunk in original and len(chunk) <= 1200 for chunk in chunks)
