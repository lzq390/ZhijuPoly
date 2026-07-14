from __future__ import annotations

import contextlib
import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_PATH = REPO_ROOT / "scripts" / "monomer_backend_status_probe.py"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

SPEC = importlib.util.spec_from_file_location("monomer_backend_status_probe", PROBE_PATH)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def transport_health(**overrides: object) -> dict[str, object]:
    health: dict[str, object] = {
        "supported": True,
        "runtime_ready": True,
        "runtime_error": None,
    }
    health.update(overrides)
    return health


def status_payload(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "enabled": True,
        "available": True,
        "worker_status": "ok",
        "worker_mode": "real",
        "db_configured": True,
        "byteff2_root_exists": True,
        "runtime_ready": True,
        "active_jobs": 0,
        "protocols": {"Transport": transport_health()},
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


class ProbeStatusTests(unittest.TestCase):
    def run_probe(
        self,
        fetch,
        *,
        retries: int = 3,
        require_transport_ready: bool = False,
    ):
        stdout = io.StringIO()
        stderr = io.StringIO()
        sleeps: list[float] = []
        result = probe.probe_status(
            "http://127.0.0.1:9000/api/v1/monomer-md/status",
            timeout_seconds=40,
            retries=retries,
            retry_delay_seconds=2.0,
            fetch=fetch,
            sleep=sleeps.append,
            stdout=stdout,
            stderr=stderr,
            require_transport_ready=require_transport_ready,
        )
        return result, stdout.getvalue(), stderr.getvalue(), sleeps

    def test_defaults_match_deployment_contract(self):
        args = probe.build_parser().parse_args(
            ["--url", "http://127.0.0.1:9000/api/v1/monomer-md/status"]
        )
        self.assertEqual(args.timeout_seconds, 40)
        self.assertEqual(args.retries, 3)
        self.assertFalse(args.require_transport_ready)


    def test_first_attempt_succeeds_and_uses_configured_timeout(self):
        calls: list[tuple[str, int]] = []

        def fetch(url: str, timeout: int) -> bytes:
            calls.append((url, timeout))
            return status_payload()

        result, stdout, stderr, sleeps = self.run_probe(fetch)

        self.assertTrue(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 40)
        self.assertEqual(sleeps, [])
        self.assertEqual(stderr, "")
        self.assertTrue(json.loads(stdout)["available"])

    def test_timeout_then_success_retries_without_real_sleep(self):
        calls = 0

        def fetch(_url: str, timeout: int) -> bytes:
            nonlocal calls
            calls += 1
            self.assertEqual(timeout, 40)
            if calls == 1:
                raise probe.ProbeFailure("timeout")
            return status_payload()

        result, _stdout, stderr, sleeps = self.run_probe(fetch)

        self.assertTrue(result)
        self.assertEqual(calls, 2)
        self.assertEqual(sleeps, [2.0])
        self.assertIn("request timed out", stderr)

    def test_invalid_json_then_success_retries(self):
        payloads = iter([b"not-json", status_payload()])

        result, _stdout, stderr, sleeps = self.run_probe(
            lambda _url, _timeout: next(payloads)
        )

        self.assertTrue(result)
        self.assertEqual(sleeps, [2.0])
        self.assertIn("invalid JSON", stderr)

    def test_unavailable_then_success_retries_without_leaking_body(self):
        sentinel = "DO_NOT_LOG_SECRET_SENTINEL"
        payloads = iter(
            [
                status_payload(
                    available=False,
                    message=sentinel,
                    runtime_error=sentinel,
                    api_key=sentinel,
                ),
                status_payload(),
            ]
        )

        result, stdout, stderr, sleeps = self.run_probe(
            lambda _url, _timeout: next(payloads)
        )

        self.assertTrue(result)
        self.assertEqual(sleeps, [2.0])
        self.assertNotIn(sentinel, stdout)
        self.assertNotIn(sentinel, stderr)
        self.assertIn("status reported unavailable", stderr)

    def test_continuous_failure_uses_four_attempts_and_three_retries(self):
        calls = 0

        def fetch(_url: str, _timeout: int) -> bytes:
            nonlocal calls
            calls += 1
            raise probe.ProbeFailure("network_error")

        result, stdout, stderr, sleeps = self.run_probe(fetch, retries=3)

        self.assertFalse(result)
        self.assertEqual(calls, 4)
        self.assertEqual(sleeps, [2.0, 2.0, 2.0])
        self.assertEqual(stdout, "")
        self.assertIn("after 4 attempts", stderr)

    def test_non_object_json_is_rejected(self):
        result, _stdout, stderr, sleeps = self.run_probe(
            lambda _url, _timeout: b"[]",
            retries=0,
        )

        self.assertFalse(result)
        self.assertEqual(sleeps, [])
        self.assertIn("invalid JSON shape", stderr)

    def test_safe_summary_excludes_free_text_and_unapproved_status_values(self):
        sentinel = "DO_NOT_LOG_SECRET_SENTINEL"
        result, stdout, stderr, _sleeps = self.run_probe(
            lambda _url, _timeout: status_payload(
                worker_status=sentinel,
                message=sentinel,
                runtime_error=sentinel,
                api_key=sentinel,
            ),
            retries=0,
        )

        self.assertTrue(result)
        self.assertNotIn(sentinel, stdout)
        self.assertNotIn(sentinel, stderr)
        summary = json.loads(stdout)
        self.assertNotIn("worker_status", summary)
        self.assertNotIn("message", summary)
        self.assertTrue(summary["available"])

    def test_response_size_limit_and_loopback_only_url(self):
        with self.assertRaises(probe.ProbeFailure) as oversized:
            probe.decode_status(b"x" * (probe.MAX_RESPONSE_BYTES + 1))
        self.assertEqual(oversized.exception.kind, "response_too_large")

        with self.assertRaises(probe.ProbeFailure) as external:
            probe.fetch_status("https://example.com/status", 1)
        self.assertEqual(external.exception.kind, "invalid_url")

    def test_curl_status_zero_is_network_error_not_http_zero(self):
        completed = subprocess.CompletedProcess(
            args=["curl"],
            returncode=7,
            stdout=b"\n000",
            stderr=b"connection refused",
        )
        with mock.patch.object(probe.subprocess, "run", return_value=completed):
            with self.assertRaises(probe.ProbeFailure) as raised:
                probe.fetch_status("http://127.0.0.1:1/status", 1)
        self.assertEqual(raised.exception.kind, "network_error")

    def test_curl_http_failure_keeps_numeric_status(self):
        completed = subprocess.CompletedProcess(
            args=["curl"],
            returncode=22,
            stdout=b"\n503",
            stderr=b"request failed",
        )
        with mock.patch.object(probe.subprocess, "run", return_value=completed):
            with self.assertRaises(probe.ProbeFailure) as raised:
                probe.fetch_status("http://127.0.0.1:9000/status", 1)
        self.assertEqual(raised.exception.kind, "http_error")
        self.assertEqual(raised.exception.detail, 503)

    def test_truncated_http_200_is_network_error_not_http_success(self):
        completed = subprocess.CompletedProcess(
            args=["curl"],
            returncode=18,
            stdout=b'{"available":true}\n200',
            stderr=b"transfer closed with bytes remaining",
        )
        with mock.patch.object(probe.subprocess, "run", return_value=completed):
            with self.assertRaises(probe.ProbeFailure) as raised:
                probe.fetch_status("http://127.0.0.1:9000/status", 1)
        self.assertEqual(raised.exception.kind, "network_error")

    def test_pathological_json_is_classified_without_traceback(self):
        huge_integer = b'{"available":true,"value":' + (b"9" * 5_000) + b"}"
        deeply_nested = (
            b'{"available":true,"value":'
            + (b"[" * 20_000)
            + b"0"
            + (b"]" * 20_000)
            + b"}"
        )
        for payload in (huge_integer, deeply_nested):
            with self.subTest(length=len(payload)):
                with self.assertRaises(probe.ProbeFailure) as raised:
                    probe.decode_status(payload)
                self.assertEqual(raised.exception.kind, "invalid_json")

    @unittest.skipUnless(shutil.which("curl"), "curl is required for deployment")
    def test_redirect_is_rejected_instead_of_followed(self):
        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://example.com/")
                self.end_headers()

            def log_message(self, _format, *_args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(probe.ProbeFailure) as raised:
                probe.fetch_status(
                    f"http://127.0.0.1:{server.server_port}/status",
                    2,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(raised.exception.kind, "http_error")
        self.assertEqual(raised.exception.detail, 302)

    @unittest.skipUnless(shutil.which("curl"), "curl is required for deployment")
    def test_slow_drip_response_obeys_absolute_deadline(self):
        payload = status_payload()

        class SlowHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    for byte in payload:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.2)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, _format, *_args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with self.assertRaises(probe.ProbeFailure) as raised:
                probe.fetch_status(
                    f"http://127.0.0.1:{server.server_port}/status",
                    1,
                )
        finally:
            elapsed = time.monotonic() - started
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(raised.exception.kind, "timeout")
        self.assertLess(elapsed, 2.5)


class ConfigurationValidationTests(unittest.TestCase):
    def test_probe_cli_rejects_invalid_timeout_and_retry_values(self):
        invalid_args = [
            ["--timeout-seconds", "0"],
            ["--timeout-seconds", "301"],
            ["--timeout-seconds", "abc"],
            ["--retries", "-1"],
            ["--retries", "4"],
            ["--retries", "abc"],
        ]
        for extra in invalid_args:
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    probe.main(
                        [
                            "--url",
                            "http://127.0.0.1:9000/api/v1/monomer-md/status",
                            *extra,
                        ]
                    )
            self.assertEqual(raised.exception.code, 2)

    def test_workflow_runs_the_probe_tests(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("bash -n scripts/deploy_server.sh", workflow)
        self.assertIn('python3 -m unittest -v "${unittest_files[@]}"', workflow)
        self.assertIn("scripts/tests/test_monomer_md_worker_launcher.py", workflow)
        self.assertIn("scripts/tests/test_worker_slot_runtime.py", workflow)


if __name__ == "__main__":
    unittest.main()
