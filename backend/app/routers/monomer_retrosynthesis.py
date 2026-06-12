from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.models import MonomerRetrosynthesisRequest, MonomerRetrosynthesisResponse
from app.services.monomer_retrosynthesis import predict_monomer_precursors
from app.utils.exceptions import InvalidSmilesError, ModelArtifactError


router = APIRouter(prefix="/api/v1", tags=["monomer-retrosynthesis"])


@router.post("/monomer-retrosynthesis", response_model=MonomerRetrosynthesisResponse)
async def monomer_retrosynthesis(
    request_body: MonomerRetrosynthesisRequest,
    request: Request,
) -> MonomerRetrosynthesisResponse:
    settings = request.app.state.settings
    if not settings.retro_model_enabled:
        raise HTTPException(status_code=503, detail="retrosynthesis service is disabled")

    try:
        return predict_monomer_precursors(
            request_body.smiles,
            target_role=request_body.target_role,
            num_beams=request_body.num_beams,
            num_return_sequences=request_body.num_return_sequences,
            max_new_tokens=request_body.max_new_tokens,
            model_id=settings.retro_model_id,
            device=settings.retro_device,
        )
    except InvalidSmilesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ModelArtifactError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
