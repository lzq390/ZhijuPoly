from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest

import httpx

from workers.polymer_md_analysis.contracts import AnalysisError
from workers.polymer_md_analysis.main import create_app
from workers.polymer_md_analysis.service import AnalysisService
from workers.polymer_md_analysis.tests.helpers import JOB, TOKEN, fixture, query, settings


async def until(condition, timeout=5):
    end = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() >= end:
            raise AssertionError("test condition did not become true")
        await asyncio.sleep(.01)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = settings(self.root, timeout_seconds=5, terminate_grace_seconds=.05)
        self.config.job_root.mkdir()
        self.service = AnalysisService(self.config)
        await self.service.start()
        self.assertTrue(self.service.ready)
        self.body = {"job_id": str(JOB), "attempt_token": TOKEN, "stage_run_id": "eq1-1", "stage": "eq1",
            "artifacts": [{"artifact_id": "a", "stage_run_id": "eq1-1", "kind": "analysis", "filename": "unused.json", "size_bytes": 0,
                "sha256": "0" * 64, "storage_ref": f"{JOB}/{TOKEN}/science/eq1/unused.json"}]}

    async def asyncTearDown(self):
        await self.service.stop()
        self.temporary.cleanup()

    def install_child(self, *, duration=60, ignore_term=False, cpu=False):
        processes = []
        script = "import sys,json,time,signal,pathlib; x=json.load(sys.stdin); "
        if ignore_term:
            script += "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        script += "pathlib.Path(x['private'],'child-ready').touch(); "
        if cpu:
            script += f"exec('end=time.monotonic()+{duration}\\nwhile time.monotonic()<end: pass'); "
        else:
            script += f"time.sleep({duration}); "
        script += "print(json.dumps({'ok':True,'result':{'test_only':True}}))"
        async def spawn(*_args):
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", script, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
            self.service._processes.add(process)
            processes.append(process)
            return process
        self.service._spawn = spawn
        return processes

    async def test_one_running_one_queued_overflow_and_queue_cancel_do_not_start_another_child(self):
        processes = self.install_child()
        first = asyncio.create_task(self.service.analyze("distance", query(self.body)))
        await until(lambda: len(list(self.config.spool_root.glob("request-*/child-ready"))) == 1)
        second = asyncio.create_task(self.service.analyze("distance", query(self.body)))
        await until(lambda: len(self.service.waiting) == 1)
        self.assertEqual(len(processes), 1)
        self.assertEqual(len(list(self.config.spool_root.glob("request-*"))), 1)
        with self.assertRaises(AnalysisError) as caught:
            await self.service.analyze("distance", query(self.body))
        self.assertEqual(caught.exception.status, 429)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        self.assertEqual(len(self.service.waiting), 0)
        self.assertIsNone(processes[0].returncode)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertIsNotNone(processes[0].returncode)
        self.assertFalse(self.service._processes)
        self.assertEqual(self.service.active, 0)
        self.assertFalse(list(self.config.spool_root.glob("request-*")))

    async def test_cpu_reader_is_a_process_and_cheap_health_remains_responsive(self):
        processes = self.install_child(cpu=True)
        first = asyncio.create_task(self.service.analyze("distance", query(self.body)))
        await until(lambda: len(list(self.config.spool_root.glob("request-*/child-ready"))) == 1)
        app = create_app(service=self.service)
        start = time.monotonic()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://reader") as client:
            for _ in range(5):
                response = await client.get("/health")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["ready"])
                self.assertEqual(response.json()["active"], 1)
        self.assertLess(time.monotonic() - start, 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertIsNotNone(processes[0].returncode)

    async def test_deadline_covers_queue_and_reaps_only_its_own_child_before_releasing(self):
        self.service.settings = replace(self.config, timeout_seconds=.25)
        processes = self.install_child(ignore_term=True)
        with self.assertRaises(AnalysisError) as caught:
            await self.service.analyze("distance", query(self.body))
        self.assertEqual(caught.exception.code, "analysis_timeout")
        self.assertEqual(caught.exception.status, 504)
        self.assertEqual(processes[0].returncode, -signal.SIGKILL)
        self.assertEqual(self.service.active, 0)
        self.assertFalse(list(self.config.spool_root.glob("request-*")))

    async def test_repeated_cancel_during_termination_cannot_release_or_clean_spool_early(self):
        processes = self.install_child(ignore_term=True)
        first = asyncio.create_task(self.service.analyze("distance", query(self.body)))
        await until(lambda: len(list(self.config.spool_root.glob("request-*/child-ready"))) == 1)
        first.cancel()
        await asyncio.sleep(.01)
        first.cancel()
        self.assertEqual(self.service.active, 1)
        self.assertTrue(list(self.config.spool_root.glob("request-*")))
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(processes[0].returncode, -signal.SIGKILL)
        self.assertEqual(self.service.active, 0)
        self.assertFalse(list(self.config.spool_root.glob("request-*")))

    async def test_pending_queue_uses_original_deadline_and_never_creates_spool(self):
        processes = self.install_child()
        first = asyncio.create_task(self.service.analyze("distance", query(self.body)))
        await until(lambda: len(processes) == 1)
        self.service.settings = replace(self.config, timeout_seconds=.05)
        with self.assertRaises(AnalysisError) as caught:
            await self.service.analyze("distance", query(self.body))
        self.assertEqual(caught.exception.code, "analysis_timeout")
        self.assertEqual(len(processes), 1)
        self.assertEqual(len(self.service.waiting), 0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first

    async def test_waiting_request_runs_only_after_previous_process_is_reaped(self):
        processes = self.install_child(duration=.05)
        first = asyncio.create_task(self.service.analyze("distance", query(self.body)))
        await until(lambda: len(processes) == 1)
        second = asyncio.create_task(self.service.analyze("distance", query(self.body)))
        await until(lambda: len(self.service.waiting) == 1)
        self.assertEqual(len(processes), 1)
        await first
        self.assertIsNotNone(processes[0].returncode)
        await second
        self.assertEqual(len(processes), 2)
        self.assertTrue(all(p.returncode == 0 for p in processes))
        self.assertEqual(self.service.active, 0)

    async def test_bounded_diagnostics_failure_drains_pipes_and_reaps_child(self):
        processes = []
        async def spawn(*_args):
            process = await asyncio.create_subprocess_exec(sys.executable, "-c",
                "import sys,time;sys.stdin.read();sys.stderr.write('x'*1048576);sys.stderr.flush();time.sleep(60)",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            processes.append(process); self.service._processes.add(process)
            return process
        self.service._spawn = spawn
        with self.assertRaises(AnalysisError) as caught:
            await asyncio.wait_for(self.service.analyze("distance", query(self.body)), 2)
        self.assertEqual(caught.exception.code, "analysis_response_invalid")
        self.assertIsNotNone(processes[0].returncode)
        self.assertEqual(self.service.active, 0)
        self.assertFalse(list(self.config.spool_root.glob("request-*")))

    async def test_spool_lock_prevents_second_controller_and_stale_owned_temp_is_cleaned_after_restart(self):
        leftover = self.config.spool_root / "request-abcd1234"
        leftover.mkdir(mode=0o700)
        (leftover / "partial.xtc").write_bytes(b"incomplete")
        other = AnalysisService(self.config)
        await other.start()
        self.assertFalse(other.ready)
        self.assertTrue(leftover.exists())
        await other.stop()
        await self.service.stop()
        self.service = AnalysisService(self.config)
        await self.service.start()
        self.assertTrue(self.service.ready)
        self.assertFalse(leftover.exists())

    async def test_asgi_disconnect_cancels_and_reaps_native_child(self):
        processes = self.install_child()
        app = create_app(service=self.service)
        messages = asyncio.Queue()
        sent = []
        body = query(self.body).model_dump_json().encode()
        await messages.put({"type": "http.request", "body": body, "more_body": False})
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/distance", "raw_path": b"/distance", "root_path": "", "query_string": b"", "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1000), "server": ("reader", 80)}
        async def send(message):
            sent.append(message)
        request = asyncio.create_task(app(scope, messages.get, send))
        await until(lambda: len(list(self.config.spool_root.glob("request-*/child-ready"))) == 1)
        await messages.put({"type": "http.disconnect"})
        await asyncio.wait_for(request, 2)
        self.assertIsNotNone(processes[0].returncode)
        self.assertEqual(self.service.active, 0)
        self.assertEqual(sent[0]["status"], 499)
        self.assertFalse(list(self.config.spool_root.glob("request-*")))

    async def test_request_validation_and_body_limit_do_not_spawn_and_do_not_echo_paths(self):
        processes = self.install_child()
        app = create_app(service=self.service)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://reader") as client:
            bad = query(self.body).model_dump(mode="json")
            bad["artifacts"][0]["storage_ref"] = "/private/test-secret"
            response = await client.post("/distance", json=bad)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("test-secret", response.text)
            response = await client.post("/distance", content=b" " * (2 * 1024**2 + 1))
            self.assertEqual(response.status_code, 413)
        self.assertFalse(processes)

    async def test_real_child_distance_and_frame_with_extra_manifest_and_no_job_mutation(self):
        body, directory, _ = fixture(self.config.job_root)
        before = {str(p.relative_to(self.config.job_root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in self.config.job_root.rglob("*") if p.is_file()}
        app = create_app(service=self.service)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://reader") as client:
            response = await client.post("/distance", json=query(body).model_dump(mode="json"))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["job_id"], str(JOB))
            self.assertEqual(len(response.json()["points"]), 29)
            response = await client.post("/trajectory", json={**body, "frame_index": 101, "limit": 1})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertAlmostEqual(response.json()["time_ps"], .11)
            self.assertEqual(response.json()["sampled_points"], 1)
        after = {str(p.relative_to(self.config.job_root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in self.config.job_root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(self.service._processes)
        self.assertFalse(list(self.config.spool_root.glob("request-*")))


if __name__ == "__main__":
    unittest.main()
