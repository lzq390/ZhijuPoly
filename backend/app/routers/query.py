from __future__ import annotations

from time import perf_counter

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from app.models import (
    PolymerResult,
    SmilesQueryRequest,
    SmilesQueryResponse,
    Structure3DRequest,
    Structure3DResponse,
    StructureImageRecognitionResponse,
)
from app.services.aggregator import load_polymer_result, load_polymer_results
from app.services.image_recognition import recognize_structure_image_from_bytes
from app.services.property_similarity import property_similarity_search
from app.services.similarity import similarity_search
from app.services.structure_3d import generate_3d_molblock
from app.utils.exceptions import (
    InvalidImageError,
    InvalidSmilesError,
    ModelArtifactError,
    StructureRecognitionError,
    UnsupportedPredictionPropertyError,
)


router = APIRouter(prefix="/api/v1", tags=["query"])


@router.post("/query/smiles", response_model=SmilesQueryResponse)
async def query_smiles(request_body: SmilesQueryRequest, request: Request) -> SmilesQueryResponse:
    started_at = perf_counter()
    settings = request.app.state.settings
    predicted_property_name: str | None = None
    predicted_property_value: float | None = None
    predicted_property_unit: str | None = None

    try:
        with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
            if request_body.match_mode == "structure":
                rows_with_scores = similarity_search(
                    connection,
                    request_body.smiles,
                    similarity_threshold=0.0,
                    top_k=10,
                )
            elif request_body.property_name is not None:
                if not settings.model_enabled:
                    raise HTTPException(status_code=503, detail="prediction service is disabled")
                predicted_property_value, predicted_property_unit, rows_with_scores = property_similarity_search(
                    connection,
                    request_body.smiles,
                    request_body.property_name,
                    model_dir=settings.model_dir_path,
                    top_k=10,
                )
                predicted_property_name = request_body.property_name
            else:
                raise HTTPException(status_code=422, detail="property_name is required for property match mode")

            polymer_rows = [row for row, _ in rows_with_scores]
            scores = {int(row["polymer_id"]): score for row, score in rows_with_scores}

            results = load_polymer_results(connection, polymer_rows, similarity_scores=scores)
    except InvalidSmilesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UnsupportedPredictionPropertyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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

    with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
        polymer_row = connection.execute(
            """
            SELECT polymer_id, '' AS polymer_name, smiles, canonical_smiles, rdkit_parse_ok
            FROM polymers
            WHERE polymer_id = ?
            """,
            (polymer_id,),
        ).fetchone()

        if polymer_row is None:
            raise HTTPException(status_code=404, detail="polymer not found")

        return load_polymer_result(connection, polymer_row)


@router.post("/structure/3d", response_model=Structure3DResponse)
async def generate_structure_3d(request_body: Structure3DRequest, request: Request) -> Structure3DResponse:
    try:
        settings = request.app.state.settings
        molblock, capped_smiles = generate_3d_molblock(
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
        result = recognize_structure_image_from_bytes(
            image_bytes,
            content_type=image.content_type,
            model_path=settings.ocsr_model_dir_path,
            device=settings.ocsr_device,
            max_bytes=settings.ocsr_max_image_bytes,
        )
    except InvalidImageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ModelArtifactError as exc:
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
