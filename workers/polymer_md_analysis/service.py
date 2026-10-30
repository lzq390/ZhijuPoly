from __future__ import annotations

import asyncio
from collections import deque
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from .child import RESPONSE_LIMIT
from .config import AnalysisSettings
from .contracts import AnalysisError, SourceRequest
from .files import open_root, strict_json


class AnalysisService:
    def __init__(self, settings: AnalysisSettings):
        self.settings = settings
        self.ready = False
        self.stopping = False
        self.active = 0
        self.waiting = deque()
        self._guard = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._processes: set[asyncio.subprocess.Process] = set()
        self._spool_lock = None

    def _prepare_spool(self):
        root = open_root(self.settings.job_root)
        os.close(root)
        spool = self.settings.spool_root
        spool.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = open_root(spool)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("analysis spool must be private and owned")
            self._spool_lock = os.open(".analysis.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            fcntl.flock(self._spool_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Only this service writes this separately locked private spool.
            for child in spool.iterdir():
                if re.fullmatch(r"request-[A-Za-z0-9_-]{8}", child.name):
                    info = child.lstat()
                    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                        raise ValueError("unexpected analysis spool entry")
                    shutil.rmtree(child)
        finally:
            os.close(fd)

    async def start(self):
        try:
            await asyncio.to_thread(self._prepare_spool)
            if self.settings.probe_on_start:
                data = await asyncio.wait_for(self._child_probe(), timeout=min(30, self.settings.timeout_seconds))
                self.ready = data.get("ready") is True
            else:
                self.ready = True
        except Exception:
            self.ready = False

    def health(self):
        return {"protocol_version": 1, "service": "polymer-md-analysis", "ready": self.ready and not self.stopping,
                "active": self.active, "queued": len(self.waiting), "max_running_analyses": 1,
                "max_pending_analyses": 1, "request_timeout_seconds": self.settings.timeout_seconds}

    async def stop(self):
        self.stopping, self.ready = True, False
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
        for process in list(self._processes):
            await self._terminate(process)
        if self._spool_lock is not None:
            os.close(self._spool_lock)
            self._spool_lock = None

    def _environment(self):
        environment = {key: value for key, value in os.environ.items() if key in {"HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "PATH"}}
        environment.update(PYTHONPATH=str(Path(__file__).resolve().parents[2]), PYTHONDONTWRITEBYTECODE="1",
            PYTHONNOUSERSITE="1", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1",
            CUDA_VISIBLE_DEVICES="", NVIDIA_VISIBLE_DEVICES="none")
        return environment

    async def _spawn(self, *args):
        process = await asyncio.create_subprocess_exec(self.settings.child_python, "-m", "workers.polymer_md_analysis.child", *args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=self._environment(), cwd=str(Path(__file__).resolve().parents[2]), start_new_session=True)
        self._processes.add(process)
        return process

    async def _terminate(self, process):
        async def discard(stream):
            while await stream.read(65536):
                pass
        # A cancelled/bounded collector may leave a pipe paused at its buffer
        # limit. Drain without retaining bytes, so wait() cannot deadlock even
        # after the native process has been killed.
        drains = [asyncio.create_task(discard(stream)) for stream in (process.stdout, process.stderr)]
        try:
            if process.stdin is not None:
                process.stdin.close()
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), self.settings.terminate_grace_seconds)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    # Never release a slot, lock or spool until our child is reaped.
                    try:
                        await asyncio.wait_for(process.wait(), 2)
                    except asyncio.TimeoutError:
                        self.ready = False
                        await process.wait()
            await asyncio.gather(*drains)
            self._processes.discard(process)
        finally:
            for task in drains:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*drains, return_exceptions=True)

    @staticmethod
    async def _read(stream, limit):
        data = bytearray()
        while chunk := await stream.read(65536):
            if len(data) + len(chunk) > limit:
                raise AnalysisError("analysis_response_invalid", "分析进程输出超过允许大小。", 502)
            data.extend(chunk)
        return bytes(data)

    async def _collect(self, process, payload):
        async def send():
            if payload:
                process.stdin.write(payload)
                await process.stdin.drain()
            process.stdin.close()
        tasks = [asyncio.create_task(send()), asyncio.create_task(self._read(process.stdout, RESPONSE_LIMIT)),
                 asyncio.create_task(self._read(process.stderr, 65536))]
        try:
            _, stdout, _stderr = await asyncio.gather(*tasks)
            await process.wait()
            value = strict_json(stdout)
            if process.returncode != 0 or not isinstance(value, dict):
                raise AnalysisError("analysis_failed", "只读分析进程异常退出；后台模拟继续运行。")
            if value.get("ok") is not True:
                error = value.get("error", {})
                if not isinstance(error, dict) or not isinstance(error.get("status"), int) or not 400 <= error["status"] < 600:
                    raise AnalysisError("analysis_response_invalid", "分析进程响应无效。", 502)
                raise AnalysisError(error.get("code", "analysis_failed"), error.get("message", "只读分析失败。"), error["status"])
            result = value.get("result")
            if not isinstance(result, dict):
                raise AnalysisError("analysis_response_invalid", "分析进程响应无效。", 502)
            return result
        except (ValueError, BrokenPipeError, ConnectionResetError) as exc:
            raise AnalysisError("analysis_failed", "只读分析进程异常退出；后台模拟继续运行。") from exc
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _child_probe(self):
        process = await self._spawn("--probe")
        try:
            return await self._collect(process, b"")
        finally:
            await self._terminate(process)

    async def _run_child(self, operation, source):
        creating = asyncio.create_task(asyncio.to_thread(tempfile.mkdtemp, prefix="request-", dir=self.settings.spool_root))
        private = process = spawning = None
        try:
            private = Path(await asyncio.shield(creating))
            envelope = {"operation": operation, "request": source.model_dump(mode="json"), "private": str(private),
                "settings": {"job_root": str(self.settings.job_root), "spool_root": str(self.settings.spool_root),
                    "spool_budget_bytes": self.settings.spool_budget_bytes, "min_free_bytes": self.settings.min_free_bytes,
                    "free_fraction": self.settings.free_fraction}}
            spawning = asyncio.create_task(self._spawn())
            process = await asyncio.shield(spawning)
            return await self._collect(process, json.dumps(envelope, allow_nan=False).encode())
        finally:
            async def cleanup():
                nonlocal private, process
                if private is None:
                    private = Path(await creating)
                if process is None and spawning is not None:
                    try:
                        process = await spawning
                    except Exception:
                        pass
                if process is not None:
                    await self._terminate(process)
                await asyncio.to_thread(shutil.rmtree, private)
            # Repeated HTTP/task cancellation must never let cleanup outlive
            # its slot or delete files while the native process can still read.
            finishing = asyncio.create_task(cleanup())
            interrupted = False
            while not finishing.done():
                try:
                    await asyncio.shield(finishing)
                except asyncio.CancelledError:
                    interrupted = True
            await finishing
            if interrupted:
                raise asyncio.CancelledError

    async def _admit(self):
        ticket = None
        async with self._guard:
            if not self.ready or self.stopping:
                raise AnalysisError("analysis_service_unavailable", "只读分析服务暂不可用；后台模拟继续运行。")
            if self.active:
                if self.waiting:
                    raise AnalysisError("analysis_busy", "已有分析运行且已有一项等待，请稍后重试。", 429)
                ticket = asyncio.get_running_loop().create_future()
                self.waiting.append(ticket)
            else:
                self.active = 1
        if ticket is not None:
            try:
                await ticket
            except BaseException:
                async with self._guard:
                    if ticket in self.waiting:
                        self.waiting.remove(ticket)
                    elif ticket.done() and not ticket.cancelled():
                        self._advance()
                raise

    def _advance(self):
        while self.waiting:
            ticket = self.waiting.popleft()
            if not ticket.done():
                ticket.set_result(None)
                return
        self.active = 0

    async def analyze(self, operation: str, source: SourceRequest):
        task = asyncio.current_task()
        self._tasks.add(task)
        admitted = False
        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                await self._admit()
                admitted = True
                return await self._run_child(operation, source)
        except TimeoutError as exc:
            raise AnalysisError("analysis_timeout", "距离或轨迹分析超时，请稍后重试；后台模拟继续运行。", 504) from exc
        finally:
            if admitted:
                async with self._guard:
                    self._advance()
            self._tasks.discard(task)
