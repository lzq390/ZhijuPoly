"""Account-owned, immutable drawing snapshots. No scientific jobs are started here."""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import json
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from psycopg.types.json import Jsonb

from app.services.private_quotas import PrivateQuotaSettings
from app.services.smiles_utils import standardize_smiles
from app.services.structure_2d import generate_2d_svg


class CreateStructureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    smiles: str = Field(min_length=1, max_length=8000)
    ket: dict[str, Any]
    snapshot_schema_version: Literal[1] = 1
    editor_version: str = Field(min_length=1, max_length=64)
    client_request_id: UUID

    @field_validator("name", "smiles", "editor_version", mode="before")
    @classmethod
    def nonblank(cls, value):
        if not isinstance(value, str):
            raise ValueError("must be text")
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class RenameStructureRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    metadata_revision: int = Field(ge=1)

    @field_validator("name", mode="before")
    @classmethod
    def nonblank(cls, value):
        return CreateStructureRecord.nonblank(value)


PUBLIC_COLUMNS = "id,name,smiles,canonical_smiles,snapshot_schema_version,editor_version,preview_svg,metadata_revision,created_at,updated_at"


def failure(status: int, code: str, message: str):
    raise HTTPException(status, {"code": code, "message": message})


def clean_document(ket: dict) -> dict:
    quotas = PrivateQuotaSettings.from_environment()
    try:
        encoded = json.dumps(ket, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, RecursionError):
        failure(422, "invalid_ket", "画板文档格式无效。")
    if len(encoded) > quotas.structure_library_ket_bytes:
        failure(413, "ket_too_large", "画板文档超过大小限制。")
    root = ket.get("root")
    if not isinstance(root, dict) or not isinstance(root.get("nodes"), list) or not root["nodes"]:
        failure(422, "invalid_ket", "请保存包含结构的完整画板。")
    if any(not isinstance(node, dict) for node in root["nodes"]):
        failure(422, "invalid_ket", "画板根节点格式无效。")

    def walk(value, depth=0):
        if depth > 64:
            failure(422, "invalid_ket", "画板文档嵌套过深。")
        if isinstance(value, list):
            return [walk(item, depth + 1) for item in value]
        if isinstance(value, dict):
            if "$ref" in value and (not isinstance(value["$ref"], str) or not isinstance(ket.get(value["$ref"]), dict)):
                failure(422, "invalid_ket", "画板包含无效结构引用。")
            return {key: walk(item, depth + 1) for key, item in value.items() if key != "selected"}
        return value

    cleaned = walk(ket)
    atoms = False
    visited: set[str] = set()

    def reachable(value, depth=0):
        nonlocal atoms
        if depth > 64:
            failure(422, "invalid_ket", "画板结构引用嵌套过深。")
        if isinstance(value, list):
            for item in value:
                reachable(item, depth + 1)
        elif isinstance(value, dict):
            if value.get("type") == "molecule" and isinstance(value.get("atoms"), list) and value["atoms"]:
                if any(not isinstance(atom, dict) for atom in value["atoms"]):
                    failure(422, "invalid_ket", "画板原子格式无效。")
                atoms = True
            ref = value.get("$ref")
            if ref is not None and ref not in visited:
                visited.add(ref)
                reachable(cleaned[ref], depth + 1)
            for key, item in value.items():
                if key != "$ref":
                    reachable(item, depth + 1)

    # Orphaned molecules cannot make an empty/malformed root a valid drawing.
    reachable(cleaned["root"])
    if not atoms:
        failure(422, "invalid_ket", "画板中没有可保存的原子。")
    return cleaned


def serialize(row):
    if row is None:
        failure(404, "structure_not_found", "结构记录不存在或已删除。")
    return {key: str(value) if isinstance(value, UUID) else value for key, value in dict(row).items()}


def get_record(connection, owner: str, record_id: UUID):
    return serialize(connection.execute(
        f"SELECT {PUBLIC_COLUMNS},ket FROM structure_library.records WHERE owner_user_id=%s AND id=%s",
        (owner, record_id)).fetchone())


def save_record(connection, owner: str, body: CreateStructureRecord):
    ket = clean_document(body.ket)
    payload = {**body.model_dump(mode="json"), "ket": ket}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    # The same transaction covers idempotency, quota checks and insertion.
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("structure-library:" + owner,))
    existing = connection.execute("SELECT id,create_payload_hash FROM structure_library.records WHERE owner_user_id=%s AND client_request_id=%s", (owner, body.client_request_id)).fetchone()
    if existing:
        if existing["create_payload_hash"] != digest:
            failure(409, "save_request_conflict", "此保存请求已用于其他内容，请重新保存。")
        return get_record(connection, owner, existing["id"])
    try:
        canonical = standardize_smiles(body.smiles)
    except ValueError:
        failure(422, "invalid_smiles", "结构无法转换为有效 SMILES，画板已保留。")
    try:
        preview = generate_2d_svg(body.smiles)
    except Exception:
        preview = None  # A thumbnail failure must not lose a valid drawing.
    # Reserve the maximum UTF-8 name size so renames cannot bypass the byte quota.
    size = len(json.dumps({**payload, "canonical_smiles": canonical, "preview_svg": preview}, ensure_ascii=False).encode()) + 400 - len(body.name.encode())
    quotas = PrivateQuotaSettings.from_environment()
    total = connection.execute("SELECT count(*) AS rows,COALESCE(sum(storage_bytes),0) AS bytes FROM structure_library.records WHERE owner_user_id=%s", (owner,)).fetchone()
    if total["rows"] >= quotas.structure_library_max_rows or total["bytes"] + size > quotas.structure_library_max_bytes:
        failure(429, "structure_library_quota", "结构库已达容量限制，请删除不再需要的记录后重试。")
    record_id = uuid4()
    connection.execute("""INSERT INTO structure_library.records
        (id,owner_user_id,name,smiles,canonical_smiles,ket,snapshot_schema_version,editor_version,
         preview_svg,client_request_id,create_payload_hash,storage_bytes)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (record_id, owner, body.name, body.smiles, canonical, Jsonb(ket), body.snapshot_schema_version,
         body.editor_version, preview, body.client_request_id, digest, size))
    return get_record(connection, owner, record_id)


def list_records(connection, owner: str, query: str, limit: int, cursor: str | None):
    clauses, params = ["owner_user_id=%s"], [owner]
    if query:
        # A literal substring search, not user-supplied LIKE wildcards.
        clauses.append("(strpos(lower(name),lower(%s))>0 OR strpos(smiles,%s)>0 OR strpos(canonical_smiles,%s)>0)")
        params.extend([query, query, query])
    if cursor:
        try:
            timestamp, record_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            time = datetime.fromisoformat(timestamp)
            if time.tzinfo is None:
                raise ValueError()
            record_id = UUID(record_id)
        except (ValueError, TypeError, UnicodeError):
            failure(422, "invalid_cursor", "分页位置无效，请重新加载。")
        clauses.append("(created_at,id)<(%s,%s)")
        params.extend([time, record_id])
    params.append(limit + 1)
    rows = connection.execute(f"SELECT {PUBLIC_COLUMNS} FROM structure_library.records WHERE {' AND '.join(clauses)} ORDER BY created_at DESC,id DESC LIMIT %s", params).fetchall()
    items = [serialize(row) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([last["created_at"].isoformat(), last["id"]]).encode()).decode()
    return {"items": items, "next_cursor": next_cursor}


def rename_record(connection, owner: str, record_id: UUID, body: RenameStructureRecord):
    row = connection.execute(f"""UPDATE structure_library.records SET name=%s,
        metadata_revision=metadata_revision+1,updated_at=now()
        WHERE owner_user_id=%s AND id=%s AND metadata_revision=%s RETURNING {PUBLIC_COLUMNS}""",
        (body.name, owner, record_id, body.metadata_revision)).fetchone()
    if row is None:
        get_record(connection, owner, record_id)  # Cross-account and missing IDs remain 404.
        failure(409, "structure_revision_conflict", "记录名称已在其他窗口修改，请刷新后重试。")
    return serialize(row)


def delete_record(connection, owner: str, record_id: UUID):
    if connection.execute("DELETE FROM structure_library.records WHERE owner_user_id=%s AND id=%s RETURNING id", (owner, record_id)).fetchone() is None:
        failure(404, "structure_not_found", "结构记录不存在或已删除。")
