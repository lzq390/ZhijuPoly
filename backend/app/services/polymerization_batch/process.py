"""Bounded subprocess entry point; never imports FastAPI or GPU models."""
from __future__ import annotations

import json
import os
import resource
import sys
from pathlib import Path


def main() -> int:
    import ctypes
    import signal
    expected_parent = int(os.environ["NEXPOLY_BATCH_PARENT_PID"])
    if ctypes.CDLL(None).prctl(1, signal.SIGKILL) != 0 or os.getppid() != expected_parent:
        return 127
    request_path, response_path = map(Path, sys.argv[1:3])
    request = json.loads(request_path.read_text())
    limits = request["config"]
    resource.setrlimit(resource.RLIMIT_AS, (limits["memory_bytes"], limits["memory_bytes"]))
    resource.setrlimit(resource.RLIMIT_FSIZE, (limits["result_bytes"], limits["result_bytes"]))
    from .models import BatchError, BatchSettings, TableMapping
    from .storage import digest, file_manifest, read_json, resolve_file, write_json
    config = BatchSettings(**{**limits, "storage_root": Path(limits["storage_root"])})
    root = config.storage_root
    try:
        action = request["action"]
        if action == "inspect":
            from .tables import inspect_table
            data = {}
            for role, meta in request["files"].items():
                mapping = TableMapping(**request["mappings"][role])
                path = resolve_file(root, meta["path"])
                try:
                    data[role] = inspect_table(path, mapping, config)
                except BatchError as exc:
                    sheets = []
                    if path.suffix == ".xlsx":
                        from .tables import checked_workbook
                        try:
                            workbook = checked_workbook(path, config)
                            sheets = [sheet.title for sheet in workbook.worksheets if sheet.sheet_state == "visible"]
                            workbook.close()
                        except BatchError:
                            pass
                    data[role] = {"error": str(exc), "headers": [], "sample": [], "row_count": 0,
                                  "blank_rows": 0, "sheets": sheets, "sheet": mapping.sheet,
                                  "mapping": mapping.model_dump()}
        elif action == "preview":
            from .tables import preview
            data = preview(request["files"], request["mappings"], root, config)
            from .chemistry import engine_fingerprint
            data["engine"] = engine_fingerprint()
        else:
            from .chemistry import classify, engine_fingerprint, generate
            from app.utils.exceptions import ModelArtifactError
            try:
                current_engine = engine_fingerprint()
            except ModelArtifactError as exc:
                raise BatchError("SMiPoly 运行时不可用。", "engine_unavailable", 503) from exc
            if current_engine["fingerprint"] != request["engine"]["fingerprint"]:
                raise BatchError("计算版本不一致。", "engine_version_mismatch", 503)
            if action == "classify":
                data = classify(request["smiles"])
            elif action == "generate":
                cache = {"rows": [], "errors": {}}
                keys = set(request["a"]) | set(request["b"])
                for artifact in request["classification"]:
                    path = resolve_file(root, artifact["path"])
                    if digest(path) != artifact["sha256"]:
                        raise BatchError("分类缓存校验失败。", "artifact_corrupt", 503)
                    item = read_json(path)
                    cache["rows"].extend(row for row in item["rows"] if row["source_key"] in keys)
                    cache["errors"].update(item["errors"])
                data = generate(request["a"], request["b"], cache, request["target"])
            elif action == "export":
                from .exporting import export_job
                data = export_job(root, request["job"], read_json(resolve_file(root, request["snapshot"])),
                                  request["chunks"], resolve_file(root, request["directory"]), config)
            else:
                raise RuntimeError("unknown batch subprocess action")
        write_json(response_path, {"ok": True, "data": data})
        return 0
    except Exception as exc:
        write_json(response_path, {"ok": False, "code": getattr(exc, "code", "calculation_error"),
                                   "status": getattr(exc, "status", 422), "message": str(exc)[:500]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
