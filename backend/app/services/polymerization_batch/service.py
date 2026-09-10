from __future__ import annotations

import io
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .execution import run_isolated
from .models import BatchError, BatchJobCreate, BatchPreviewRequest, BatchSettings, TableMapping
from .repository import BatchRepository
from .storage import check_id, digest, file_manifest, json_hash, read_json, resolve_file, write_json


BASE_PATH = "/api/v1/monomer-polymerization/batch"


class BatchService:
    def __init__(self, dsn: str, config: BatchSettings):
        self.config = config
        self.repository = BatchRepository(dsn, config)
        self.root = config.storage_root

    def status(self) -> dict:
        result = {"enabled": self.config.enabled, "available": False, "formats": ["csv", "xlsx"], "limits": self.config.public_limits(), "message": "批量聚合当前未启用。"}
        if not self.config.enabled:
            return result
        try:
            if not self.repository.ready():
                return {**result, "message": "批量任务数据库尚未准备好。"}
            worker = self.repository.worker_status()
            result.update(available=bool(worker and worker["fresh"] and worker["available"]),
                          message=worker["message"] if worker and worker["fresh"] else "批量计算 worker 尚未就绪。")
        except Exception:
            result["message"] = "批量任务服务暂不可用。"
        return result

    def require_enabled(self, *, worker=False):
        if not self.config.enabled:
            raise BatchError("批量聚合当前未启用。", "disabled", 503)
        if worker and not self.status()["available"]:
            raise BatchError("批量计算 worker 尚未就绪。", "worker_unavailable", 503)

    def inspect_import(self, import_id: str, mappings: dict | None = None) -> dict:
        item = self.repository.get_import(import_id)
        tables = run_isolated({"action": "inspect", "files": item["files"],
                               "mappings": mappings or {role: TableMapping().model_dump() for role in ("a", "b")}},
                              self.config, self.root / "imports" / import_id / "scratch")
        return {"import_id": import_id, "expires_at": item["expires_at"], "files": self.public_files(item["files"]), "tables": tables}

    @staticmethod
    def public_files(files: dict) -> dict:
        return {role: {key: value for key, value in meta.items() if key != "path"} for role, meta in files.items()}

    def preview(self, import_id: str, request: BatchPreviewRequest) -> dict:
        self.require_enabled()
        item = self.repository.get_import(import_id)
        revision = uuid4().hex
        self.repository.begin_preview(import_id, revision)
        inspected = self.inspect_import(import_id, request.model_dump())
        mappings = {role: inspected["tables"][role]["mapping"] for role in ("a", "b")}
        if not all(mapping["smiles_column"] for mapping in mappings.values()) or any(table.get("error") for table in inspected["tables"].values()):
            return {**inspected, "preview_revision": None, "can_submit": False, "statistics": None, "input_errors": []}
        snapshot = run_isolated({"action": "preview", "files": item["files"], "mappings": mappings},
                                self.config, self.root / "imports" / import_id / "scratch")
        path = self.root / "imports" / import_id / "previews" / f"{revision}.json"
        snapshot["files"] = self.public_files(item["files"])
        write_json(path, snapshot)
        statistics = {key: snapshot[key] for key in ("raw_pairs", "valid_pairs", "unique_pairs")}
        statistics["tables"] = {role: {key: table[key] for key in ("row_count", "valid_rows", "unique_count", "duplicate_rows", "invalid_rows", "blank_rows")}
                                for role, table in snapshot["tables"].items()}
        metadata = {"snapshot": file_manifest(self.root, path, "application/json"), "statistics": statistics, "can_submit": snapshot["can_submit"]}
        self.repository.save_preview(import_id, revision, metadata)
        errors = [{**{key: row[key] for key in ("source_key", "row_number", "id", "input_smiles", "error_code", "message")}, "role": role}
                  for role, table in snapshot["tables"].items() for row in table["rows"] if row["error_code"]]
        return {**inspected, "preview_revision": revision, "can_submit": snapshot["can_submit"],
                "statistics": statistics, "input_errors": errors[:100], "input_error_count": len(errors)}

    def create_job(self, request: BatchJobCreate, key: str) -> dict:
        self.require_enabled()
        if not key or len(key) > 128:
            raise BatchError("提交标识须为 1–128 个字符。", "invalid_idempotency_key")
        options = request.model_dump()
        request_hash = json_hash(options)
        job_id = uuid4().hex
        directory = self.root / "jobs" / job_id
        commit_pending = False
        try:
            with self.repository.connection() as conn:
                # Serialize admission and idempotency, including concurrent retries.
                previous = self.repository.admit(conn, key, request_hash)
                if previous:
                    return self.public_job(previous)
                worker = self.repository.worker_status()
                if not worker or not worker["fresh"] or not worker["available"]:
                    raise BatchError("批量计算 worker 尚未就绪。", "worker_unavailable", 503)
                if shutil.disk_usage(self.root).free < self.config.result_bytes * 2:
                    raise BatchError("批量文件存储空间不足。", "storage_full", 503)
                imported = self.repository.get_import(request.import_id, conn, lock=True)
                if imported["preview_revision"] != request.preview_revision:
                    raise BatchError("预检已更新，请使用最新预检结果提交。", "stale_preview", 409)
                preview = imported["preview"]
                if not preview or not preview["can_submit"]:
                    raise BatchError("任一表没有有效单体时无法提交。", "no_valid_rows")
                source = resolve_file(self.root, preview["snapshot"]["path"])
                if digest(source) != preview["snapshot"]["sha256"]:
                    raise BatchError("预检文件校验失败，请重新上传。", "artifact_corrupt", 503)
                snapshot = read_json(source)
                if snapshot.get("engine", {}).get("fingerprint") != worker["engine"].get("fingerprint"):
                    raise BatchError("预检与 worker 计算版本不一致，请等待服务就绪后重新预检。", "stale_engine", 409)
                directory.mkdir(parents=True)
                shutil.copyfile(source, directory / "inputs.json")
                for role, meta in imported["files"].items():
                    original = resolve_file(self.root, meta["path"])
                    if digest(original) != meta["sha256"]:
                        raise BatchError("上传文件校验失败，请重新上传。", "artifact_corrupt", 503)
                    shutil.copyfile(original, directory / f"source_{role}{original.suffix}")
                options["snapshot_path"] = str((directory / "inputs.json").relative_to(self.root))
                options["snapshot_sha256"] = digest(directory / "inputs.json")
                a, b = (snapshot["tables"][role]["unique_smiles"] for role in ("a", "b"))
                unique = sorted(set(a) | set(b))
                chunks = []
                for index in range(0, len(unique), self.config.classification_size):
                    chunks.append({"chunk_id": f"c{index}", "phase": 0, "kind": "classify", "payload": {"smiles": unique[index:index + self.config.classification_size]}})
                for i in range(0, len(a), self.config.chunk_size):
                    for j in range(0, len(b), self.config.chunk_size):
                        chunks.append({"chunk_id": f"g{i}-{j}", "phase": 1, "kind": "generate", "payload": {"a": a[i:i + self.config.chunk_size], "b": b[j:j + self.config.chunk_size]}})
                chunks.append({"chunk_id": "export", "phase": 2, "kind": "export", "payload": {}})
                options["limits"] = {name: getattr(self.config, name) for name in self.config.__dataclass_fields__ if name not in {"enabled", "storage_root"}}
                summary = {**preview["statistics"], "processed_pairs": 0, "computed_unique_pairs": 0, "candidate_count": 0, "pair_errors": 0}
                row = self.repository.insert_job(conn, job_id, options, request_hash, key, worker["engine"], summary, chunks)
                # A connection can fail after PostgreSQL commits but before its
                # acknowledgement arrives. Preserve inputs once commit begins;
                # idempotent retries recover the job, and cleanup reaps orphans.
                commit_pending = True
            return self.public_job(row)
        except BaseException:
            if not commit_pending:
                shutil.rmtree(directory, ignore_errors=True)
            raise

    def public_job(self, job: dict) -> dict:
        artifacts = {name: {key: value for key, value in item.items() if key != "path"} | {"name": name, "url": f"{BASE_PATH}/jobs/{job['id']}/artifacts/{name}"}
                     for name, item in job["artifacts"].items() if not name.startswith("preview")}
        return {"job_id": job["id"], "status": job["status"], "stage": job["stage"],
                "target_class": job["options"]["target_class"], "summary": job["summary"], "artifacts": artifacts,
                "created_at": job["created_at"], "updated_at": job["updated_at"], "finished_at": job["finished_at"],
                "expires_at": job["expires_at"], "error_code": job["error_code"], "message": job["message"]}

    def artifact(self, job_id: str, name: str):
        job = self.repository.get_job(job_id)
        if job["status"] == "expired" or (job["expires_at"] and job["expires_at"] <= datetime.now(timezone.utc)):
            raise BatchError("文件已过期。", "expired", 410)
        item = job["artifacts"].get(name)
        if not item:
            raise BatchError("结果文件尚未生成或不存在。", "artifact_not_found", 404)
        try:
            # Open before returning to the route: unlink by the retention worker
            # cannot interrupt an already-open download on the shared Linux volume.
            handle = resolve_file(self.root, item["path"]).open("rb")
        except FileNotFoundError as exc:
            raise BatchError("结果文件缺失。", "artifact_missing", 410) from exc
        return handle, item

    def results(self, job_id: str, offset: int, limit: int) -> dict:
        job = self.repository.get_job(job_id)
        if job["status"] == "expired" or (job["expires_at"] and job["expires_at"] <= datetime.now(timezone.utc)):
            raise BatchError("文件已过期。", "expired", 410)
        if not job["artifacts"]:
            return {"items": [], "total": 0, "next_offset": None}
        handle, _ = self.artifact(job_id, "preview-index.json")
        with handle:
            index = json.load(handle)
        if offset >= index["count"]:
            return {"items": [], "total": index["count"], "next_offset": None}
        handle, _ = self.artifact(job_id, "preview.jsonl")
        items = []
        with handle:
            handle.seek(index["offsets"][offset // 50])
            for _ in range(offset % 50):
                handle.readline()
            for _ in range(limit):
                line = handle.readline()
                if not line:
                    break
                items.append(json.loads(line))
        end = offset + len(items)
        return {"items": items, "total": index["count"], "next_offset": end if end < index["count"] else None}
