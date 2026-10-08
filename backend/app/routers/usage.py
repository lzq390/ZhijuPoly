"""Authenticated personal usage dashboard. There is no caller-supplied owner."""
from datetime import datetime
import logging

from fastapi import APIRouter, HTTPException, Query, Request

from app.auth.context import current_owner_id
from app.services.usage_statistics import UsageStatistics, period
from task_usage.ledger import METRICS, metering_enabled

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])
logger = logging.getLogger("nexpoly.usage")


def catalog_modules():
    from app.usage_catalog import catalog_modules as implementation
    return implementation()


def _coverage(request):
    modules = catalog_modules()
    warnings = ["统计按任务提交时间归属，不是账单；历史任务不自动补记。",
                "未观测到的指标保留为空；执行耗时不是 GPU 占用时间。",
                "进程异常退出后，无法证明已结束的内存任务保留最后观测状态，等待核实对账。"]
    collector = getattr(request.app.state, "usage_collector", None)
    if collector and collector.snapshot().get("degraded"):
        warnings.append("计量写入出现异常，部分终态尚待补记；当前数据可能不完整。")
    return {"uncovered": [item["id"] for item in modules if item["coverage"] == "external"], "warnings": warnings}


def _ready(request):
    current_owner_id()
    if not metering_enabled():
        raise HTTPException(503, detail={"code": "usage_metering_disabled", "message": "用量统计尚未启用"})
    return UsageStatistics(request.app.state.settings.app_postgres_dsn)


def _range(start, end):
    try:
        return period(start, end)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None


@router.get("/catalog")
def catalog():
    current_owner_id()
    labels = {"queue_ms": ("排队时间", "ms"), "execution_ms": ("已观测执行时间", "ms"),
              "observed_wall_ms": ("任务历时", "ms"), "provider_input_tokens": ("供应商输入 Tokens", "tokens"),
              "provider_output_tokens": ("供应商输出 Tokens", "tokens"), "provider_total_tokens": ("供应商总 Tokens", "tokens"),
              "cached_input_tokens": ("供应商缓存输入 Tokens", "tokens"), "reasoning_tokens": ("供应商推理 Tokens", "tokens"),
              "artifact_bytes": ("累计已观测产物体积", "bytes")}
    return {"enabled": metering_enabled(), "mode": "statistics_only", "modules": catalog_modules(),
            "metrics": [{"id": name, "label": labels[name][0], "unit": labels[name][1]} for name in METRICS],
            "retention_days": 365}


@router.get("/summary")
def summary(request: Request, start: datetime | None = None, end: datetime | None = None,
            module: str | None = Query(None, max_length=64)):
    service = _ready(request)
    start, end = _range(start, end)
    try:
        result = service.summary(start=start, end=end, module=module,
                                 labels={item["id"]: item["label"] for item in catalog_modules()})
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except Exception:
        logger.error("usage_summary_unavailable")
        raise HTTPException(503, detail={"code": "usage_metering_unavailable", "message": "用量统计暂时不可用"}) from None
    result["coverage"] = _coverage(request)
    return result


@router.get("/records")
def records(request: Request, start: datetime | None = None, end: datetime | None = None,
            module: str | None = Query(None, max_length=64), status: str | None = Query(None, max_length=32),
            page: int = Query(1, ge=1, le=10000), page_size: int = Query(25, ge=1, le=100),
            include_children: bool = False):
    service = _ready(request)
    start, end = _range(start, end)
    try:
        return service.records(start=start, end=end, module=module, status=status,
                               page=page, page_size=page_size, include_children=include_children)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except Exception:
        logger.error("usage_records_unavailable")
        raise HTTPException(503, detail={"code": "usage_metering_unavailable", "message": "用量统计暂时不可用"}) from None
