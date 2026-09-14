from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from .models import BatchError


ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def check_id(value: str) -> str:
    if not ID_PATTERN.fullmatch(value):
        raise BatchError("任务或导入记录不存在。", "not_found", 404)
    return value


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def json_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.part")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def resolve_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise BatchError("文件路径无效。", "invalid_artifact", 404)
    return path


def file_manifest(root: Path, path: Path, media_type: str) -> dict:
    return {"path": str(path.relative_to(root)), "size_bytes": path.stat().st_size,
            "sha256": digest(path), "media_type": media_type}
