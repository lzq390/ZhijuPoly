from __future__ import annotations

from time import perf_counter

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from app.models import (
    PolymerResult,
    SmilesQueryRequest,
    SmilesQueryResponse,
    SmilesStandardizeRequest,
    SmilesStandardizeResponse,
    Structure2DRequest,
    Structure2DResponse,
    Structure3DRequest,
    Structure3DResponse,
    StructureImageRecognitionResponse,
)
from app.postgres_database import PostgresUnavailableError
from app.services.aggregator import load_polymer_result_postgres, load_polymer_results_postgres
from app.services.gpu_runtime_registry import (
    GpuQueueFullError,
    GpuQueueStoppedError,
    GpuQueueTimeoutError,
)
from app.services.image_recognition import recognize_structure_image_from_bytes, validate_structure_image
from app.services.property_similarity import property_similarity_search_postgres
from app.services.similarity import similarity_search_postgres
from app.services.smiles_utils import standardize_smiles
from app.services.structure_similarity_index import (
    StructureSimilarityIndexUnavailableError,
)
from app.services.structure_2d import generate_2d_svg
from app.services.structure_3d import generate_3d_molblock
from app.utils.exceptions import (
    InvalidImageError,
    InvalidSmilesError,
    ModelArtifactError,
    StructureRecognitionError,
    UnsupportedPredictionPropertyError,
)


router = APIRouter(prefix="/api/v1", tags=["query"])
POSTGRES_ONLY_DETAIL = "Postgres runtime is required; set STRUCTURED_DATA_BACKEND=postgres."


def _run_ocsr_inference(app, image_bytes: bytes, content_type: str | None):
    settings = app.state.settings
    validate_structure_image(
        image_bytes,
        content_type=content_type,
        max_bytes=settings.ocsr_max_image_bytes,
    )
    registry = getattr(app.state, "gpu_runtime_registry", None)
    if registry is None:
        raise ModelArtifactError("GPU runtime registry is unavailable")

    with registry.inference_session(
        "ocsr",
        timeout_seconds=settings.gpu_sync_queue_timeout_seconds,
    ) as runtime:
        try:
            result = recognize_structure_image_from_bytes(
                image_bytes,
                content_type=content_type,
                model_path=settings.ocsr_model_dir_path,
                device=settings.ocsr_device,
                max_bytes=settings.ocsr_max_image_bytes,
                runtime=runtime,
            )
        except (InvalidImageError, StructureRecognitionError):
            raise
        except Exception as exc:
            failure_kind = registry.record_inference_failure("ocsr", exc)
            if failure_kind == "oom":
                raise ModelArtifactError(
                    "OCSR GPU memory is exhausted; retry after current GPU work finishes"
                ) from exc
            raise
        # Publish success while this request still owns the scheduler permit so
        # status observers cannot see the next inference active first.
        registry.record_inference_success("ocsr")
    return result


@router.post("/query/smiles", response_model=SmilesQueryResponse)
async def query_smiles(request_body: SmilesQueryRequest, request: Request) -> SmilesQueryResponse:
    return await run_in_threadpool(_query_smiles_sync, request_body, request.app)


def _query_smiles_sync(request_body: SmilesQueryRequest, app) -> SmilesQueryResponse:
    started_at = perf_counter()
    settings = app.state.settings
    predicted_property_name: str | None = None
    predicted_property_value: float | None = None
    predicted_property_unit: str | None = None

    if settings.structured_data_backend != "postgres":
        raise HTTPException(status_code=503, detail=POSTGRES_ONLY_DETAIL)

    try:
        with app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            if request_body.match_mode == "structure":
                rows_with_scores = similarity_search_postgres(
                    connection,
                    request_body.smiles,
                    similarity_threshold=request_body.similarity_threshold,
                    top_k=request_body.top_k,
                    index=app.state.structure_similarity_index,
                )
            elif request_body.property_name is not None:
                if not settings.model_enabled:
                    raise HTTPException(status_code=503, detail="prediction service is disabled")
                predicted_property_value, predicted_property_unit, rows_with_scores = property_similarity_search_postgres(
                    connection,
                    request_body.smiles,
                    request_body.property_name,
                    model_dir=settings.model_dir_path,
                    similarity_threshold=request_body.similarity_threshold,
                    top_k=request_body.top_k,
                )
                predicted_property_name = request_body.property_name
            else:
                raise HTTPException(status_code=422, detail="property_name is required for property match mode")

            polymer_rows = [row for row, _ in rows_with_scores]
            scores = {int(row["polymer_id"]): score for row, score in rows_with_scores}
            results = load_polymer_results_postgres(connection, polymer_rows, similarity_scores=scores)
    except InvalidSmilesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UnsupportedPredictionPropertyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc
    except StructureSimilarityIndexUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail="structure similarity index is unavailable",
            headers={"Retry-After": "2"},
        ) from exc
    except ModelArtifactError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    elapsed_ms = (perf_counter() - started_at) * 1000
    return SmilesQueryResponse(
        match_type=request_body.match_mode,
        query_time_ms=elapsed_ms,
        total=len(results),
        predicted_property_name=predicted_property_name,
        predicted_property_value=predicted_property_value,
        predicted_property_unit=predicted_property_unit,
        results=results,
    )

@router.get("/polymer/{polymer_id}", response_model=PolymerResult)
async def get_polymer_detail(polymer_id: int, request: Request) -> PolymerResult:
    settings = request.app.state.settings

    if settings.structured_data_backend != "postgres":
        raise HTTPException(status_code=503, detail=POSTGRES_ONLY_DETAIL)

    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            polymer_row = connection.execute(
                """
                SELECT polymer_id, polymer_name, smiles, canonical_smiles, rdkit_parse_ok
                FROM core.polymers
                WHERE polymer_id = %s
                """,
                (polymer_id,),
            ).fetchone()

            if polymer_row is None:
                raise HTTPException(status_code=404, detail="polymer not found")

            return load_polymer_result_postgres(connection, polymer_row)
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc

@router.post("/structure/3d", response_model=Structure3DResponse)
async def generate_structure_3d(request_body: Structure3DRequest, request: Request) -> Structure3DResponse:
    try:
        settings = request.app.state.settings
        molblock, capped_smiles = await run_in_threadpool(
            generate_3d_molblock,
            request_body.smiles,
            timeout_seconds=settings.structure_3d_timeout_seconds,
        )
    except InvalidSmilesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return Structure3DResponse(
        molblock=molblock,
        capped_smiles=capped_smiles,
        format="mol",
    )


@router.post("/structure/2d", response_model=Structure2DResponse)
async def generate_structure_2d(request_body: Structure2DRequest) -> Structure2DResponse:
    structure_svg = await run_in_threadpool(generate_2d_svg, request_body.smiles)
    if structure_svg is None:
        raise HTTPException(status_code=422, detail=f"invalid smiles: {request_body.smiles}")
    return Structure2DResponse(structure_svg=structure_svg)


@router.post("/structure/standardize-smiles", response_model=SmilesStandardizeResponse)
async def standardize_structure_smiles(request_body: SmilesStandardizeRequest) -> SmilesStandardizeResponse:
    started_at = perf_counter()
    try:
        standardized_smiles = standardize_smiles(request_body.smiles)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    elapsed_ms = (perf_counter() - started_at) * 1000
    return SmilesStandardizeResponse(
        input_smiles=request_body.smiles,
        standardized_smiles=standardized_smiles,
        changed=standardized_smiles != request_body.smiles,
        query_time_ms=elapsed_ms,
    )


@router.post("/structure/recognize-image", response_model=StructureImageRecognitionResponse)
async def recognize_structure_image(
    request: Request,
    image: UploadFile = File(...),
) -> StructureImageRecognitionResponse:
    started_at = perf_counter()
    settings = request.app.state.settings
    if not settings.ocsr_enabled:
        await image.close()
        raise HTTPException(status_code=503, detail="image recognition service is disabled")

    try:
        image_bytes = await image.read(settings.ocsr_max_image_bytes + 1)
        result = await run_in_threadpool(
            _run_ocsr_inference,
            request.app,
            image_bytes,
            image.content_type,
        )
    except InvalidImageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ModelArtifactError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GpuQueueFullError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except (GpuQueueTimeoutError, GpuQueueStoppedError) as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StructureRecognitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await image.close()

    elapsed_ms = (perf_counter() - started_at) * 1000
    return StructureImageRecognitionResponse(
        smiles=result.smiles,
        molfile=result.molfile,
        confidence=result.confidence,
        warnings=result.warnings or [],
        query_time_ms=elapsed_ms,
    )
