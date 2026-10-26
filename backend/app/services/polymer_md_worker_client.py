"""Private local UDS transport. An uncertain response never proves execution stopped."""
from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import quote
import httpx

from polymer_md.contracts import SubmitRequest, WorkerSnapshot
from .polymer_md_repository import PolymerMdError


class PolymerMdWorkerClient:
    def __init__(self, uds_path: str, *, client: httpx.AsyncClient | None = None):
        if not uds_path.startswith("/"):
            raise ValueError("polymer MD worker requires an absolute Unix socket")
        self.client = client or httpx.AsyncClient(base_url="http://polymer-md-worker", transport=httpx.AsyncHTTPTransport(uds=uds_path), timeout=30, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def json(self, method, path, *, payload=None, params=None, analysis_not_found=None):
        try:
            response = await self.client.request(method, path, json=payload, params=params)
            if analysis_not_found and response.status_code==404:
                raise PolymerMdError(analysis_not_found,"该任务的阶段轨迹或日志不存在。",404)
            if analysis_not_found and response.status_code==429:
                raise PolymerMdError("analysis_busy","轨迹分析并发已满，请稍后重试。",429)
            if response.status_code >= 300:
                raise PolymerMdError("worker_response_unknown", "计算服务响应尚未确认，任务名额继续保留。", 503)
            if len(response.content) > 16 * 1024**2:
                raise PolymerMdError("worker_response_invalid", "计算服务响应超过限制。", 502)
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PolymerMdError("worker_unavailable", "计算服务暂不可用，任务状态等待对账。", 503) from exc

    async def health(self):
        value = await self.json("GET", "/health")
        if not isinstance(value, dict) or value.get("protocol_version") != 1 or value.get("start_authorization_version") != 1:
            raise PolymerMdError("worker_protocol_invalid", "计算服务授权协议不匹配。", 503)
        return value

    async def snapshot(self, request: SubmitRequest, *, cancel=False):
        await self.health()
        path = "/jobs/" + request.job_id
        if cancel:
            raw = await self.json("POST", path + "/cancel", payload=request.model_dump(mode="json"))
        else:
            # Reposting the identical durable request repairs an uncertain
            # initial dispatch; worker journal guarantees exactly one attempt.
            raw = await self.json("POST", "/jobs", payload=request.model_dump(mode="json"))
        try:
            snapshot = WorkerSnapshot.model_validate(raw)
        except ValueError as exc:
            raise PolymerMdError("worker_snapshot_invalid", "计算服务快照校验失败。", 502) from exc
        if any(str(getattr(snapshot, key)) != str(getattr(request, key)) for key in ("job_id", "attempt_token", "request_sha256", "enqueue_sequence")):
            raise PolymerMdError("worker_attempt_mismatch", "计算服务返回了其他执行尝试。", 502)
        return snapshot

    async def delete(self, request: SubmitRequest, *, entire_job=False):
        path = "/jobs/" + request.job_id + ("" if entire_job else "/artifacts")
        result = await self.json("DELETE", path, payload=request.model_dump(mode="json"))
        if not isinstance(result, dict) or not (result.get("deleted") is True or result.get("artifacts_deleted") is True) or result.get("cleanup_confirmed") is not True:
            raise PolymerMdError("artifact_cleanup_unconfirmed", "产物清理尚未得到确认。", 503)
        # Require the response to acknowledge the same attempt when supplied;
        # absent identity is rejected rather than treating generic 200 as proof.
        if any(str(result.get(key))!=str(getattr(request,key)) for key in ("job_id","attempt_token","request_sha256","enqueue_sequence")):
            raise PolymerMdError("cleanup_attempt_mismatch", "产物清理执行身份不匹配。", 502)
        return result

    async def stage_json(self, request: SubmitRequest, stage_run_id, *, distance=None, frame_index=0, limit=10000):
        path = f"/jobs/{request.job_id}/stages/{quote(stage_run_id,safe='')}/"
        params = {"attempt_token": request.attempt_token}
        if distance is not None:
            value = await self.json("POST", path+"distance", payload=distance.model_dump(), params=params,analysis_not_found="trajectory_not_found")
        else:
            params.update({"offset": frame_index, "frame_index": frame_index, "limit": limit})
            value = await self.json("GET", path+"trajectory", params=params,analysis_not_found="trajectory_not_found")
        if not isinstance(value, dict):
            raise PolymerMdError("invalid_stage_response", "轨迹分析响应无效。", 502)
        return {**value, "job_id": request.job_id, "stage_run_id": stage_run_id}

    async def stage_logs(self, request: SubmitRequest, stage_run_id, *, tail_lines=100):
        value=await self.json("GET",f"/jobs/{request.job_id}/stages/{quote(stage_run_id,safe='')}/logs",
            params={"attempt_token":request.attempt_token,"tail_lines":tail_lines},analysis_not_found="stage_not_found")
        if not isinstance(value,dict) or not isinstance(value.get("lines"),list) or any(not isinstance(line,str) for line in value["lines"]):
            raise PolymerMdError("invalid_stage_logs", "阶段日志响应无效。", 502)
        return {**value,"job_id":request.job_id,"stage_run_id":stage_run_id}

    @asynccontextmanager
    async def artifact_stream(self, request: SubmitRequest, artifact_id):
        try:
            # A budget-sized local artifact can take longer than the JSON
            # control timeout to hash before headers are sent. Keep this
            # bounded and independent of task execution/cleanup decisions.
            async with self.client.stream("GET", f"/jobs/{request.job_id}/artifacts/{quote(artifact_id,safe='')}", params={"attempt_token":request.attempt_token}, headers={"Accept-Encoding":"identity"}, timeout=httpx.Timeout(30, read=600)) as response:
                if response.status_code != 200 or response.headers.get("content-encoding", "identity") not in {"", "identity"}:
                    raise PolymerMdError("artifact_response_invalid", "产物下载响应尚未确认。", 502)
                yield response
        except httpx.HTTPError as exc:
            raise PolymerMdError("artifact_worker_unavailable", "产物下载服务暂不可用。", 503) from exc
