"""Authenticated personal drawing library; uses the existing private transport."""
from contextlib import contextmanager
import logging
from time import perf_counter
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.auth.context import current_owner_id
from app.postgres_database import PostgresUnavailableError
from app.services.private_quotas import PrivateQuotaSettings
from app.services import structure_library as library

router = APIRouter(prefix="/api/v1/structure-library", tags=["structure-library"])
logger = logging.getLogger(__name__)


def ready(connection):
    return connection.execute("SELECT to_regclass('structure_library.records') IS NOT NULL AS ready").fetchone()["ready"]


@contextmanager
def database(request: Request, *, require_enabled=True):
    settings = request.app.state.settings
    if require_enabled and not settings.structure_library_enabled:
        raise HTTPException(503, "个人结构库尚未启用。")
    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            if require_enabled and not ready(connection):
                raise HTTPException(503, "个人结构库尚未完成数据库升级。")
            yield connection
    except PostgresUnavailableError:
        raise HTTPException(503, "结构库暂时无法连接，请稍后重试。") from None


@router.get("/status")
def status(request: Request):
    quotas = PrivateQuotaSettings.from_environment()
    with database(request, require_enabled=False) as connection:
        schema_ready = ready(connection)
    enabled = request.app.state.settings.structure_library_enabled
    return {"enabled": enabled, "schema_ready": schema_ready, "available": enabled and schema_ready,
            "max_records": quotas.structure_library_max_rows, "max_bytes": quotas.structure_library_max_bytes,
            "max_ket_bytes": quotas.structure_library_ket_bytes,
            "message": "" if enabled and schema_ready else "个人结构库尚未启用或尚未完成升级。"}


@router.get("/records")
def records(request: Request, q: str = Query("", max_length=8000), limit: int = Query(20, ge=1, le=100), cursor: str | None = Query(None, max_length=512)):
    with database(request) as connection:
        return library.list_records(connection, current_owner_id(), q.strip(), limit, cursor)


@router.post("/records", status_code=201)
def save(body: library.CreateStructureRecord, request: Request):
    start = perf_counter()
    try:
        with database(request) as connection:
            result = library.save_record(connection, current_owner_id(), body)
        logger.info("structure_library.save request=%s status=saved elapsed_ms=%.1f", body.client_request_id, (perf_counter()-start)*1000)
        return result
    except HTTPException as exc:
        logger.info("structure_library.save request=%s status=%s elapsed_ms=%.1f", body.client_request_id, exc.status_code, (perf_counter()-start)*1000)
        raise


@router.get("/records/{record_id}")
def detail(record_id: UUID, request: Request):
    with database(request) as connection:
        return library.get_record(connection, current_owner_id(), record_id)


@router.patch("/records/{record_id}")
def rename(record_id: UUID, body: library.RenameStructureRecord, request: Request):
    with database(request) as connection:
        return library.rename_record(connection, current_owner_id(), record_id, body)


@router.delete("/records/{record_id}", status_code=204)
def delete(record_id: UUID, request: Request):
    with database(request) as connection:
        library.delete_record(connection, current_owner_id(), record_id)
    return Response(status_code=204)
