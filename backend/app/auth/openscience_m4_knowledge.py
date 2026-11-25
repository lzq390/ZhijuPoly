"""Owner-scoped text document storage, search and MCP citations for independent dev-m1."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import PurePath
import re
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from psycopg.errors import UniqueViolation
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)
from typing import Annotated

from app.auth.context import Identity, user_context
from app.postgres_database import postgres_connection


PREFIX = "/internal/openscience/m4/knowledge"
MAX_UPLOAD_BYTES = 256 * 1024
MAX_CHUNK_LENGTH = 1200
DOC_COLUMNS = """
    doc_id AS id,doc_id,kb_id,filename,mime_type,octet_length(original) AS size_bytes,
    chunk_count,'就绪' AS status,created_at,created_at AS updated_at,
    created_at AS status_checked_at,NULL::text AS error_message
"""
KB_COLUMNS = """
    kb_id,owner_id,name,description,created_at,created_at AS updated_at,
    'user' AS owner_type,'private' AS visibility
"""
EVIDENCE = """
    SELECT c.kb_id,k.name AS kb_name,c.doc_id AS document_id,d.filename AS document_name,
           c.chunk_id,c.body AS text,'body' AS source
"""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def no_nul(cls, value):
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("NUL characters cannot be stored")
        return value


class NewCollection(StrictModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    description: str | None = Field(default=None, max_length=1024)


class SearchInput(StrictModel):
    query: str = Field(
        min_length=1, max_length=1000, description="Query text, up to 1000 UTF-8 bytes"
    )
    kb_ids: list[UUID] | None = Field(
        default=None, max_length=20, description="Authorized collection IDs"
    )
    top_k: int = Field(default=5, strict=True, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def query_bytes(cls, query):
        if len(query.encode("utf-8")) > 1000:
            raise ValueError("Search query exceeds 1000 UTF-8 bytes")
        return query


class CitationInput(StrictModel):
    kb_id: UUID
    document_id: UUID
    chunk_id: UUID


def refused(status, code):
    return HTTPException(status, {"code": code})


def document_chunks(text):
    if "\x00" in text or not text.strip():
        raise refused(422, "empty_or_invalid_text")
    start = 0
    while start < len(text):
        end = min(start + MAX_CHUNK_LENGTH, len(text))
        if end < len(text):
            # Prefer a paragraph or word boundary near the end of each quoted passage.
            for separator in ("\n", " "):
                boundary = text.rfind(separator, start + MAX_CHUNK_LENGTH // 2, end)
                if boundary != -1:
                    end = boundary + 1
                    break
        chunk = text[start:end].strip()
        if chunk:
            yield chunk
        start = end


def require_kb(conn, kb_id):
    if not conn.execute(
        "SELECT 1 FROM openscience_m4_knowledge.collections WHERE kb_id=%s", (kb_id,)
    ).fetchone():
        raise refused(404, "not_found")


def with_citation(row):
    kb, doc, chunk = row["kb_id"], row["document_id"], row["chunk_id"]
    return {
        **row,
        "citation_url": f"/knowledge-api/kbs/{kb}/documents/{doc}/chunks/{chunk}",
    }


def search_documents(conn, request):
    ids = request.kb_ids
    if ids is None:
        ids = [
            row["kb_id"]
            for row in conn.execute(
                "SELECT kb_id FROM openscience_m4_knowledge.collections ORDER BY kb_id"
            )
        ]
    else:
        ids = list(dict.fromkeys(ids))
        count = conn.execute(
            "SELECT count(*) AS visible FROM openscience_m4_knowledge.collections WHERE kb_id=ANY(%s::uuid[])",
            (ids,),
        ).fetchone()["visible"]
        if count != len(ids):
            raise refused(403, "forbidden_kb")
    rows = conn.execute(
        EVIDENCE
        + """, ts_rank_cd(c.terms,q.terms) AS score
        FROM openscience_m4_knowledge.chunks c
        JOIN openscience_m4_knowledge.documents d USING (owner_id,kb_id,doc_id)
        JOIN openscience_m4_knowledge.collections k USING (owner_id,kb_id)
        CROSS JOIN (SELECT plainto_tsquery('simple',%s) AS terms) q
        WHERE c.kb_id=ANY(%s::uuid[]) AND (c.terms @@ q.terms OR strpos(lower(c.body),lower(%s))>0)
        ORDER BY score DESC,c.kb_id,c.doc_id,c.ordinal LIMIT %s
        """,
        (request.query, ids, request.query, request.top_k),
    )
    return {
        "query": request.query,
        "kb_ids": ids,
        "degraded": False,
        "dropped_empty": 0,
        "results": [
            dict(with_citation(row), ref=f"E{i}") for i, row in enumerate(rows, 1)
        ],
    }


def read_citation(conn, kb_id, doc_id, chunk_id):
    row = conn.execute(
        EVIDENCE
        + """
        FROM openscience_m4_knowledge.chunks c
        JOIN openscience_m4_knowledge.documents d USING (owner_id,kb_id,doc_id)
        JOIN openscience_m4_knowledge.collections k USING (owner_id,kb_id)
        WHERE c.kb_id=%s AND c.doc_id=%s AND c.chunk_id=%s
        """,
        (kb_id, doc_id, chunk_id),
    ).fetchone()
    if not row:
        raise refused(404, "not_found")
    return with_citation(row)


MCP_TOOLS = [
    {
        "name": "kb_search",
        "description": "Search available knowledge bases. Cite the returned document and chunk.",
        "inputSchema": SearchInput.model_json_schema(),
    },
    {
        "name": "kb_read_citation",
        "description": "Read the original text of an authorized search citation.",
        "inputSchema": CitationInput.model_json_schema(),
    },
]


def install_knowledge(app, principal, *, environment="dev-m1"):
    settings = app.state.auth.settings
    router = APIRouter(prefix=PREFIX)

    @contextmanager
    def connection(grant):
        with user_context(Identity(grant["user_id"], grant["session_id"]), settings):
            with postgres_connection(settings.application_dsn) as conn:
                yield conn

    def web(grant=Depends(principal)):
        if grant["scope"] != "knowledge.web" or grant["environment"] != environment:
            raise refused(403, "wrong_knowledge_scope")
        return grant

    @router.get("/kbs")
    def collections(grant=Depends(web)):
        with connection(grant) as conn:
            return conn.execute(
                "SELECT "
                + KB_COLUMNS
                + """,
                (SELECT count(*) FROM openscience_m4_knowledge.documents d
                  WHERE d.kb_id=k.kb_id AND d.owner_id=k.owner_id) AS doc_count
                FROM openscience_m4_knowledge.collections k ORDER BY created_at,kb_id"""
            ).fetchall()

    @router.post("/kbs")
    def create_kb(data: NewCollection, grant=Depends(web)):
        try:
            with connection(grant) as conn:
                return conn.execute(
                    "INSERT INTO openscience_m4_knowledge.collections (owner_id,name,description) "
                    "VALUES (%s,%s,%s) RETURNING " + KB_COLUMNS + ",0 AS doc_count",
                    (grant["user_id"], data.name, data.description),
                ).fetchone()
        except UniqueViolation as error:
            raise refused(409, "duplicate_kb_name") from error

    @router.get("/kbs/{kb_id}/documents")
    def documents(kb_id: UUID, grant=Depends(web)):
        with connection(grant) as conn:
            require_kb(conn, kb_id)
            return conn.execute(
                "SELECT "
                + DOC_COLUMNS
                + " FROM openscience_m4_knowledge.documents WHERE kb_id=%s ORDER BY created_at,doc_id",
                (kb_id,),
            ).fetchall()

    @router.post("/kbs/{kb_id}/documents")
    def upload(kb_id: UUID, file: UploadFile = File(), grant=Depends(web)):
        name = file.filename or ""
        if not 1 <= len(name) <= 240 or re.search(r"[/\\\x00-\x1f\x7f]", name):
            raise refused(422, "invalid_filename")
        extension = PurePath(name).suffix.lower()
        if extension not in {".txt", ".md"}:
            raise refused(415, "unsupported_type")
        original = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(original) > MAX_UPLOAD_BYTES:
            raise refused(413, "payload_too_large")
        try:
            chunks = list(document_chunks(original.decode("utf-8-sig")))
        except UnicodeDecodeError as error:
            raise refused(422, "invalid_utf8") from error
        with connection(grant) as conn:
            require_kb(conn, kb_id)
            doc = conn.execute(
                "INSERT INTO openscience_m4_knowledge.documents (owner_id,kb_id,filename,mime_type,original,chunk_count) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING " + DOC_COLUMNS,
                (
                    grant["user_id"],
                    kb_id,
                    name,
                    "text/plain" if extension == ".txt" else "text/markdown",
                    original,
                    len(chunks),
                ),
            ).fetchone()
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO openscience_m4_knowledge.chunks (owner_id,kb_id,doc_id,ordinal,body) VALUES (%s,%s,%s,%s,%s)",
                    [
                        (grant["user_id"], kb_id, doc["doc_id"], i, text)
                        for i, text in enumerate(chunks)
                    ],
                )
            return doc

    @router.get("/kbs/{kb_id}/documents/{doc_id}")
    def detail(kb_id: UUID, doc_id: UUID, grant=Depends(web)):
        with connection(grant) as conn:
            doc = conn.execute(
                "SELECT "
                + DOC_COLUMNS
                + " FROM openscience_m4_knowledge.documents WHERE kb_id=%s AND doc_id=%s",
                (kb_id, doc_id),
            ).fetchone()
            if not doc:
                raise refused(404, "not_found")
            return doc

    @router.get("/kbs/{kb_id}/documents/{doc_id}/download")
    def download(kb_id: UUID, doc_id: UUID, grant=Depends(web)):
        with connection(grant) as conn:
            doc = conn.execute(
                "SELECT filename,original FROM openscience_m4_knowledge.documents WHERE kb_id=%s AND doc_id=%s",
                (kb_id, doc_id),
            ).fetchone()
            if not doc:
                raise refused(404, "not_found")
            return Response(
                bytes(doc["original"]),
                media_type="application/octet-stream",
                headers={
                    "Content-Disposition": "attachment; filename*=UTF-8''"
                    + quote(doc["filename"], safe=""),
                    "Cache-Control": "private,no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )

    @router.get("/kbs/{kb_id}/documents/{doc_id}/chunks/{chunk_id}")
    def citation(kb_id: UUID, doc_id: UUID, chunk_id: UUID, grant=Depends(web)):
        with connection(grant) as conn:
            return read_citation(conn, kb_id, doc_id, chunk_id)

    @router.post("/search")
    def search(data: SearchInput, grant=Depends(web)):
        with connection(grant) as conn:
            return search_documents(conn, data)

    @router.post("/mcp")
    def mcp(call: dict, grant=Depends(principal)):
        if grant["scope"] != "knowledge.mcp" or grant["environment"] != environment:
            raise refused(403, "wrong_knowledge_scope")
        method, call_id = call.get("method"), call.get("id")
        envelope = {"jsonrpc": "2.0", "id": call_id}
        if call.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return {
                **envelope,
                "error": {"code": -32600, "message": "Invalid JSON-RPC request"},
            }
        if method == "notifications/initialized" and call_id is None:
            return Response(status_code=202)
        if type(call_id) not in (str, int):
            return {
                **envelope,
                "error": {"code": -32600, "message": "Request id required"},
            }
        params = call.get("params", {})
        if not isinstance(params, dict):
            return {**envelope, "error": {"code": -32602, "message": "Invalid params"}}
        if method == "initialize":
            versions = ("2025-03-26", "2025-06-18", "2025-11-25")
            version = params.get("protocolVersion")
            result = {
                "protocolVersion": version if version in versions else versions[-1],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": f"{environment}-private-knowledge", "version": "1"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": MCP_TOOLS}
        elif method == "tools/call":
            try:
                with connection(grant) as conn:
                    if params.get("name") == "kb_search":
                        value = search_documents(
                            conn, SearchInput.model_validate(params.get("arguments"))
                        )
                    elif params.get("name") == "kb_read_citation":
                        data = CitationInput.model_validate(params.get("arguments"))
                        value = read_citation(
                            conn, data.kb_id, data.document_id, data.chunk_id
                        )
                    else:
                        return {
                            **envelope,
                            "error": {
                                "code": -32602,
                                "message": "Unknown knowledge tool",
                            },
                        }
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                jsonable_encoder(value), ensure_ascii=False
                            ),
                        }
                    ]
                }
            except (HTTPException, ValidationError) as error:
                detail = (
                    error.detail
                    if isinstance(error, HTTPException)
                    else {"code": "invalid_input"}
                )
                result = {
                    "isError": True,
                    "content": [
                        {"type": "text", "text": json.dumps({"error": detail})}
                    ],
                }
        else:
            return {**envelope, "error": {"code": -32601, "message": "Unknown method"}}
        return JSONResponse(
            {**envelope, "result": result},
            headers={"Cache-Control": "private,no-store"},
        )

    app.include_router(router)
