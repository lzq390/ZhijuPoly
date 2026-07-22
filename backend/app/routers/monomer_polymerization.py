from __future__ import annotations

import anyio
from fastapi import APIRouter, HTTPException, Request

from app.models import (
    MonomerPolymerizationRequest,
    MonomerPolymerizationResponse,
    MonomerPolymerizationStatusResponse,
)
from app.services.monomer_polymerization import (
    get_monomer_polymerization_status,
    run_monomer_polymerization,
)
from app.utils.exceptions import InvalidSmilesError, ModelArtifactError


router = APIRouter(prefix="/api/v1", tags=["monomer-polymerization"])


@router.get("/monomer-polymerization/status", response_model=MonomerPolymerizationStatusResponse)
async def monomer_polymerization_status(request: Request) -> MonomerPolymerizationStatusResponse:
    settings = request.app.state.settings
    return await anyio.to_thread.run_sync(
        get_monomer_polymerization_status,
        settings.smipoly_enabled,
        limiter=request.app.state.smipoly_limiter,
    )


@router.post("/monomer-polymerization", response_model=MonomerPolymerizationResponse)
async def monomer_polymerization(
    request_body: MonomerPolymerizationRequest,
    request: Request,
) -> MonomerPolymerizationResponse:
    settings = request.app.state.settings
    if not settings.smipoly_enabled:
        raise HTTPException(status_code=503, detail="monomer polymerization service is disabled")

    try:
        return await anyio.to_thread.run_sync(
            run_monomer_polymerization,
            request_body,
            limiter=request.app.state.smipoly_limiter,
        )
    except InvalidSmilesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ModelArtifactError as exc:
        raise HTTPException(status_code=503, detail="monomer polymerization service is unavailable") from exc
