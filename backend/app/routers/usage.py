"""Authenticated personal usage dashboard. There is no caller-supplied owner."""
from datetime import datetime
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from app.auth.context import current_owner_id
from app.services.usage_statistics import UsageQueryError, UsageStatistics, period
from app.usage_groups import UsageFilterError, module_groups, resolve_group_modules
from task_usage.ledger import METRICS, _MODULE, metering_enabled

router = APIRouter(prefix="/api/v1/usage", tags=["usage"])
logger = logging.getLogger("nexpoly.usage")


def catalog_modules():
    from app.usage_catalog import catalog_modules as implementation
    return implementation()


def _observation_warnings(request):
    warnings = ["末次观测状态不保证进程已终止；无法确认的任务等待核实。"]
    collector = getattr(request.app.state, "usage_collector", None)
    if collector and collector.snapshot().get("degraded"):
        warnings.append("计量写入出现异常，部分终态尚待补记；当前数据可能不完整。")
    return warnings


def _coverage(request):
    modules = catalog_modules()
    warnings = ["统计按任务提交时间归属，不是账单；历史任务不自动补记。",
                "未观测到的指标保留为空；执行耗时不是 GPU 占用时间。",
                *_observation_warnings(request)]
    return {"uncovered": [item["id"] for item in modules if item["coverage"] == "external"], "warnings": warnings}


def _ready(request):
    current_owner_id()
    if not metering_enabled():
        raise HTTPException(503, detail={"code": "usage_metering_disabled", "message": "用量统计尚未启用"})
    return UsageStatistics(request.app.state.settings.app_postgres_dsn)


def _range(start, end):
    try:
        return period(start, end)
    except UsageQueryError as exc:
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
            "module_groups": module_groups(),
            "metrics": [{"id": name, "label": labels[name][0], "unit": labels[name][1]} for name in METRICS],
            "retention_days": 365}


@router.get("/summary")
def summary(request: Request, start: datetime | None = None, end: datetime | None = None,
            module: str | None = Query(None, max_length=64),
            module_group: str | None = Query(None, max_length=64),
            timezone: Literal["UTC", "Asia/Shanghai"] = "UTC"):
    service = _ready(request)
    start, end = _range(start, end)
    try:
        resolve_group_modules(module, module_group)
        result = service.summary(start=start, end=end, module=module, module_group=module_group, timezone=timezone,
                                 labels={item["id"]: item["label"] for item in catalog_modules()})
    except (UsageQueryError, UsageFilterError) as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except Exception:
        logger.error("usage_summary_unavailable")
        raise HTTPException(503, detail={"code": "usage_metering_unavailable", "message": "用量统计暂时不可用"}) from None
    result["coverage"] = _coverage(request)
    return result


@router.get("/records")
def records(request: Request, start: datetime | None = None, end: datetime | None = None,
            module: str | None = Query(None, max_length=64), status: str | None = Query(None, max_length=32),
            module_group: str | None = Query(None, max_length=64),
            page: int = Query(1, ge=1, le=10000), page_size: int = Query(25, ge=1, le=100),
            include_children: bool = False):
    service = _ready(request)
    start, end = _range(start, end)
    try:
        resolve_group_modules(module, module_group)
        return service.records(start=start, end=end, module=module, module_group=module_group, status=status,
                               page=page, page_size=page_size, include_children=include_children)
    except (UsageQueryError, UsageFilterError) as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except Exception:
        logger.error("usage_records_unavailable")
        raise HTTPException(503, detail={"code": "usage_metering_unavailable", "message": "用量统计暂时不可用"}) from None


@router.get("/activity")
def activity(request: Request, module: str | None = Query(None, max_length=64, pattern=_MODULE.pattern),
             module_group: str | None = Query(None, max_length=64),
             offset: int = Query(0, ge=0, le=100000), limit: int = Query(25, ge=1, le=100)):
    service = _ready(request)
    try:
        resolve_group_modules(module, module_group)
        result = service.activity(module=module, module_group=module_group, offset=offset, limit=limit)
    except (UsageQueryError, UsageFilterError) as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except Exception:
        logger.error("usage_activity_unavailable")
        raise HTTPException(503, detail={"code": "usage_metering_unavailable", "message": "操作记录暂时不可用"}) from None
    result["modules"] = [{"id": item["id"], "label": item["label"]}
                         for item in catalog_modules() if item["coverage"] != "external"]
    result["module_groups"] = module_groups(include_external=False)
    result["warnings"] = [
        "仅展示已观测并保留的操作，不作为计次，也不改变功能权限或额度。",
        "同一任务合并展示最新尝试；历史未采集任务不会补记，OpenScience 独立服务尚未接入。",
        "操作记录不保存完整结果；请在原功能页面查看仍可用的结果。",
        *_observation_warnings(request),
    ]
    return result
