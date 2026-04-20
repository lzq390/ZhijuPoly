from __future__ import annotations

from time import perf_counter

from fastapi import APIRouter, HTTPException, Request

from app.models import PredictRequest, PredictResponse
from app.services.predictor import get_available_properties, predict as predict_properties
from app.utils.exceptions import InvalidSmilesError, ModelArtifactError, UnsupportedPredictionPropertyError


router = APIRouter(prefix="/api/v1", tags=["predict"])


@router.post("/predict", response_model=PredictResponse)
async def predict(request_body: PredictRequest, request: Request) -> PredictResponse:
    started_at = perf_counter()
    settings = request.app.state.settings

    if not settings.model_enabled:
        raise HTTPException(status_code=503, detail="prediction service is disabled")

    try:
        available_properties = set(get_available_properties(settings.model_dir_path))
        invalid = [value for value in request_body.properties if value not in available_properties]
        if invalid:
            raise UnsupportedPredictionPropertyError(
                "unsupported prediction properties: " + ", ".join(invalid)
            )

        predictions = predict_properties(
            request_body.smiles,
            request_body.properties,
            model_dir=settings.model_dir_path,
        )
    except (InvalidSmilesError, UnsupportedPredictionPropertyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ModelArtifactError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return PredictResponse(
        predictions=predictions,
        query_time_ms=(perf_counter() - started_at) * 1000,
    )
