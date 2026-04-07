from __future__ import annotations

from fastapi import APIRouter, HTTPException


router = APIRouter(prefix="/api/v1", tags=["predict"])


@router.post("/predict")
async def predict() -> None:
    raise HTTPException(status_code=501, detail="预测功能暂未启用,接口已预留")
