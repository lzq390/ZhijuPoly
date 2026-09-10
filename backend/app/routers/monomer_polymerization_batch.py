from __future__ import annotations

import csv
import hashlib
import io
import os
import shutil
from pathlib import Path
from uuid import uuid4

import anyio
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.datastructures import UploadFile

from app.services.polymerization_batch.models import BatchError, BatchJobCreate, BatchPreviewRequest, BatchJob, BatchResults, BatchImport, BatchImportPreview
from app.services.polymerization_batch.service import BatchService


router = APIRouter(prefix="/api/v1/monomer-polymerization/batch", tags=["monomer-polymerization-batch"])


async def batch_error_handler(request: Request, exc: BatchError):
    return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": str(exc)})


class BatchUploadLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/api/v1/monomer-polymerization/batch/imports":
            return await self.app(scope, receive, send)
        total = 0
        async def bounded_receive():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                raise BatchError("上传请求超过总大小限额。", "request_limit", 413)
            return message
        await self.app(scope, bounded_receive, send)


def service(request: Request) -> BatchService:
    return request.app.state.polymerization_batch


@router.post("/imports", status_code=201, response_model=BatchImport)
async def create_import(request: Request):
    batch = service(request)
    batch.require_enabled()
    import_id = uuid4().hex
    directory = batch.root / "imports" / import_id
    directory.mkdir(parents=True)
    files = {}
    try:
        async with request.form(max_files=2, max_fields=0) as form:
            if set(form.keys()) != {"file_a", "file_b"} or len(form.multi_items()) != 2:
                raise BatchError("请分别上传单体表 A 和 B。", "missing_files")
            for role in ("a", "b"):
                upload = form[f"file_{role}"]
                if not isinstance(upload, UploadFile):
                    raise BatchError("请上传两个表文件。", "missing_files")
                filename = Path((upload.filename or "").replace("\\", "/")).name[:255]
                suffix = Path(filename).suffix.lower()
                if suffix not in {".csv", ".xlsx"}:
                    raise BatchError("仅支持 CSV 和 XLSX 文件。", "unsupported_format")
                path = directory / f"source_{role}{suffix}"
                size, checksum = 0, hashlib.sha256()
                async with await anyio.open_file(path, "wb") as handle:
                    while data := await upload.read(64 * 1024):
                        size += len(data)
                        if size > batch.config.file_bytes:
                            raise BatchError("单文件超过大小限额。", "file_limit", 413)
                        checksum.update(data)
                        await handle.write(data)
                files[role] = {"filename": filename, "path": str(path.relative_to(batch.root)),
                               "size_bytes": size, "sha256": checksum.hexdigest(), "format": suffix[1:]}
        await anyio.to_thread.run_sync(batch.repository.create_import, import_id, files)
        return await anyio.to_thread.run_sync(batch.inspect_import, import_id, limiter=request.app.state.polymerization_batch_validation_limiter)
    except BaseException:
        # No job references this directory yet; failed multipart uploads leave no blobs.
        shutil.rmtree(directory, ignore_errors=True)
        raise


@router.post("/imports/{import_id}/preview", response_model=BatchImportPreview)
async def preview_import(import_id: str, body: BatchPreviewRequest, request: Request):
    return await anyio.to_thread.run_sync(service(request).preview, import_id, body, limiter=request.app.state.polymerization_batch_validation_limiter)


@router.post("/jobs", status_code=202, response_model=BatchJob)
async def create_job(body: BatchJobCreate, request: Request, idempotency_key: str = Header(..., min_length=1, max_length=128)):
    return await anyio.to_thread.run_sync(service(request).create_job, body, idempotency_key)


@router.get("/jobs/{job_id}", response_model=BatchJob)
async def get_job(job_id: str, request: Request):
    batch = service(request)
    job = await anyio.to_thread.run_sync(batch.repository.get_job, job_id)
    return batch.public_job(job)


@router.get("/jobs/{job_id}/results", response_model=BatchResults)
async def get_results(job_id: str, request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    return await anyio.to_thread.run_sync(service(request).results, job_id, offset, limit)


@router.post("/jobs/{job_id}/cancel", response_model=BatchJob)
async def cancel_job(job_id: str, request: Request):
    batch = service(request)
    job = await anyio.to_thread.run_sync(batch.repository.cancel, job_id)
    return batch.public_job(job)


@router.get("/jobs/{job_id}/artifacts/{name}")
async def download_artifact(job_id: str, name: str, request: Request):
    if name.startswith("preview"):
        raise BatchError("文件不存在。", "artifact_not_found", 404)
    handle, meta = await anyio.to_thread.run_sync(service(request).artifact, job_id, name)
    def chunks():
        try:
            while chunk := handle.read(64 * 1024):
                yield chunk
        finally:
            handle.close()
    return StreamingResponse(chunks(), media_type=meta["media_type"], headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Content-Length": str(meta["size_bytes"]),
        "ETag": f'"{meta["sha256"]}"', "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@router.get("/templates/{role}.{format}")
async def download_template(role: str, format: str):
    if role not in {"a", "b"} or format not in {"csv", "xlsx"}:
        raise BatchError("模板不存在。", "not_found", 404)
    smiles = "Nc1ccc(N)cc1" if role == "a" else "O=C1OC(=O)c2cc3c(cc21)C(=O)OC3=O"
    rows = [["id", "name", "SMILES"], ["001", "示例二胺" if role == "a" else "示例二酐", smiles]]
    if format == "csv":
        output = io.StringIO(newline="")
        csv.writer(output).writerows(rows)
        data, media = output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"
    else:
        def excel():
            from openpyxl import Workbook
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "monomers"
            for row in rows:
                sheet.append(row)
            for row in sheet:
                for cell in row:
                    cell.number_format = "@"
            output = io.BytesIO()
            workbook.save(output)
            workbook.close()
            return output.getvalue()
        data = await anyio.to_thread.run_sync(excel)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="monomers_{role}.{format}"'})
