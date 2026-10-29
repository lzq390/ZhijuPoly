"""Bounded transport for the independent, read-only trajectory service."""
from __future__ import annotations

import asyncio

import httpx

from .polymer_md_repository import PolymerMdError


ANALYSIS_TIMEOUT_SECONDS = 90
ERRORS = {
    "analysis_busy": (429, "轨迹分析繁忙，请稍后重试。"),
    "analysis_timeout": (504, "本次轨迹分析超时，模拟任务继续运行。"),
    "analysis_service_unavailable": (503, "轨迹分析服务暂不可用，已有模拟任务不受影响。"),
    "analysis_storage_busy": (503, "轨迹分析临时存储空间不足，请稍后重试。"),
    "artifact_integrity_mismatch": (409, "轨迹输入校验失败，请刷新任务后重试。"),
    "trajectory_not_found": (404, "该阶段尚无可分析的轨迹。"),
    "stage_not_found": (404, "该任务的阶段轨迹不存在。"),
    "invalid_analysis_request": (422, "原子编号、采样范围或轨迹数据无效。"),
    "invalid_analysis_data": (422, "阶段轨迹元数据或采样数据无效。"),
    "artifact_identity_mismatch": (409, "轨迹输入与当前阶段的产物清单不匹配。"),
    "artifact_reference_invalid": (409, "轨迹输入的存储引用无效。"),
    "artifact_unavailable": (409, "轨迹输入已移除或暂不可读，请刷新任务。"),
    "analysis_storage_unavailable": (503, "轨迹分析存储暂不可用，请稍后重试。"),
    "analysis_response_invalid": (502, "轨迹分析响应无效。"),
    "analysis_failed": (503, "本次轨迹分析失败，模拟任务继续运行。"),
}


class PolymerMdAnalysisClient:
    def __init__(self, uds_path: str, *, client: httpx.AsyncClient | None = None,
                 health_client: httpx.AsyncClient | None = None):
        if not uds_path.startswith("/"):
            raise ValueError("analysis service requires an absolute Unix socket")
        self.client = client or httpx.AsyncClient(
            base_url="http://polymer-md-analysis", transport=httpx.AsyncHTTPTransport(uds=uds_path),
            timeout=httpx.Timeout(95, connect=2, pool=2), follow_redirects=False)
        # Health cannot wait for a calculation connection or its timeout.
        self.health_client = health_client or httpx.AsyncClient(
            base_url="http://polymer-md-analysis", transport=httpx.AsyncHTTPTransport(uds=uds_path),
            timeout=1, follow_redirects=False)

    async def close(self):
        await self.client.aclose()
        if self.health_client is not self.client:
            await self.health_client.aclose()

    async def health(self):
        try:
            response = await self.health_client.get("/health", timeout=1)
            response.raise_for_status()
            if len(response.content) > 65536:
                raise ValueError("health response too large")
            value = response.json()
            if (not isinstance(value, dict) or value.get("protocol_version") != 1
                    or value.get("service") != "polymer-md-analysis" or type(value.get("ready")) is not bool
                    or value.get("request_timeout_seconds") != ANALYSIS_TIMEOUT_SECONDS
                    or any(type(value.get(k)) is not int or not 0 <= value[k] <= 1 for k in ("active", "queued"))):
                raise ValueError("invalid analysis health")
            return value
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            status, message = ERRORS["analysis_service_unavailable"]
            raise PolymerMdError("analysis_service_unavailable", message, status) from exc

    async def analyze(self, operation: str, payload: dict, *, wait_disconnected=None):
        if operation not in {"trajectory", "distance"}:
            raise ValueError("unknown analysis operation")

        task = asyncio.create_task(self.client.post("/" + operation, json=payload))
        watcher = asyncio.create_task(wait_disconnected()) if wait_disconnected else None
        try:
            if watcher:
                done, _ = await asyncio.wait((task, watcher), return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    watcher.result()
                    raise asyncio.CancelledError("analysis client disconnected")
            response = await task
            if len(response.content) > 16 * 1024**2:
                raise PolymerMdError("invalid_stage_response", "轨迹分析响应超过限制。", 502)
            try:
                value = response.json()
            except ValueError as exc:
                raise PolymerMdError("invalid_stage_response", "轨迹分析响应无效。", 502) from exc
            if response.status_code != 200:
                code = value.get("code") if isinstance(value, dict) else None
                code = code if code in ERRORS else "analysis_service_unavailable"
                status, message = ERRORS[code]
                raise PolymerMdError(code, message, status)
            if not isinstance(value, dict):
                raise PolymerMdError("invalid_stage_response", "轨迹分析响应无效。", 502)
            return value
        except httpx.TimeoutException as exc:
            status, message = ERRORS["analysis_timeout"]
            raise PolymerMdError("analysis_timeout", message, status) from exc
        except httpx.HTTPError as exc:
            status, message = ERRORS["analysis_service_unavailable"]
            raise PolymerMdError("analysis_service_unavailable", message, status) from exc
        finally:
            pending = [future for future in (task, watcher) if future is not None]
            for future in pending:
                future.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
