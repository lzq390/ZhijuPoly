from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "local_runner_guest_download", ROOT / "ops/self_hosted_runner/guest/build.py")
assert spec and spec.loader
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class GuestDownloadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.destination = self.root / "docker-ce.deb"
        self.destination.write_bytes(b"previous verified artifact")
        self.good = b"complete official release fixture"
        self.artifact = {
            "url": "https://example.invalid/release?secret=do-not-log",
            "sha256": hashlib.sha256(self.good).hexdigest(),
        }
        self.clock = 0.0
        self.calls = []
        self.events = []

    def download(self, responses):
        pending = iter(responses)

        def curl(command, **options):
            response = next(pending)
            self.calls.append((command, options))
            output = Path(command[command.index("--output") + 1])
            self.assertNotEqual(output, self.destination)
            self.assertEqual(output.parent, self.destination.parent)
            self.assertEqual(self.destination.read_bytes(), b"previous verified artifact")
            output.write_bytes(response.get("data", self.good))
            self.clock += response.get("elapsed", 0)
            if response.get("timeout"):
                raise subprocess.TimeoutExpired(command, options["timeout"], output="do-not-log")
            length = response.get("length", str(len(self.good)))
            return subprocess.CompletedProcess(
                command, response.get("code", 0),
                stdout=f"{response.get('status', 200)}\n{length}\n")

        with mock.patch.object(build.subprocess, "run", side_effect=curl), \
                mock.patch.object(build.time, "monotonic", side_effect=lambda: self.clock), \
                mock.patch.object(build, "emit", side_effect=lambda event, **fields: self.events.append((event, fields))):
            build.download(self.artifact, self.destination)

    def assert_clean(self):
        self.assertEqual(list(self.root.iterdir()), [self.destination])

    def test_verified_file_replaces_destination_only_after_full_validation(self):
        self.download([{}])
        self.assertEqual(self.destination.read_bytes(), self.good)
        self.assert_clean()
        command, options = self.calls[0]
        self.assertEqual(command[:2], ["curl", "--disable"])
        for flag, expected in (("--proxy", "http://10.0.2.2:27892"), ("--noproxy", ""),
                               ("--proto", "=https"), ("--proto-redir", "=https"),
                               ("--retry", "0"), ("--max-filesize", str(1024 ** 3)),
                               ("--speed-time", "30"), ("--speed-limit", "1")):
            self.assertEqual(command[command.index(flag) + 1], expected)
        self.assertEqual(float(command[command.index("--max-time") + 1]), 600)
        self.assertEqual(options["timeout"], 600)
        self.assertIs(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(command[command.index("--write-out") + 1], "%{http_code}\n%header{content-length}\n")
        self.assertEqual([fields["state"] for _, fields in self.events], ["started", "verified"])

    def test_silent_short_eof_is_retried_before_atomic_publication(self):
        self.download([{"data": b"partial", "elapsed": 4}, {}])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.destination.read_bytes(), self.good)
        retry = self.events[1][1]
        self.assertEqual(retry["state"], "retry")
        self.assertEqual(retry["error_kind"], "short_read")
        self.assertEqual(retry["received_bytes"], 7)
        self.assertEqual(retry["expected_bytes"], len(self.good))
        self.assertEqual(self.calls[1][1]["timeout"], 596)
        self.assert_clean()

    def test_retryable_transport_and_http_failures_are_bounded(self):
        cases = [(18, 200, "short_read"), (28, 200, "timeout"),
                 (56, 200, "network_error"), (22, 408, "http_error"),
                 (22, 429, "http_error"), (22, 502, "http_error")]
        for code, status, kind in cases:
            with self.subTest(code=code, status=status):
                self.calls.clear()
                self.events.clear()
                self.download([{"code": code, "status": status, "data": b"partial"}, {}])
                self.assertEqual(len(self.calls), 2)
                self.assertEqual(self.events[1][1]["error_kind"], kind)
                self.assert_clean()
                self.destination.write_bytes(b"previous verified artifact")

    def test_permissions_tls_and_filesystem_errors_do_not_retry(self):
        for code, status, kind in [(22, 403, "http_error"), (22, 404, "http_error"),
                                   (60, 200, "validation_error"), (23, 200, "filesystem_error"),
                                   (63, 200, "validation_error")]:
            with self.subTest(code=code, status=status):
                self.calls.clear()
                with self.assertRaises(build.ArtifactDownloadError) as caught:
                    self.download([{"code": code, "status": status}])
                self.assertEqual(caught.exception.details["error_kind"], kind)
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.destination.read_bytes(), b"previous verified artifact")
                self.assert_clean()

    def test_bad_sha_never_replaces_existing_file_and_stops_after_three_attempts(self):
        with self.assertRaises(build.ArtifactDownloadError) as caught:
            self.download([{"data": b"x" * len(self.good)}] * 3)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(caught.exception.details["error_kind"], "checksum_mismatch")
        self.assertEqual(caught.exception.details["attempt"], 3)
        self.assertEqual(self.destination.read_bytes(), b"previous verified artifact")
        self.assert_clean()

    def test_missing_content_length_still_requires_locked_sha(self):
        self.download([{"length": ""}])
        self.assertEqual(self.destination.read_bytes(), self.good)
        self.assertNotIn("expected_bytes", self.events[-1][1])
        self.assert_clean()

    def test_timeout_has_one_shared_hard_deadline_and_no_publication(self):
        with self.assertRaises(build.ArtifactDownloadError) as caught:
            self.download([{"code": 56, "elapsed": 590},
                           {"timeout": True, "elapsed": 10, "data": b"partial"}])
        self.assertEqual([options["timeout"] for _, options in self.calls], [600, 10])
        self.assertEqual(caught.exception.details["error_kind"], "timeout")
        self.assertEqual(caught.exception.details["received_bytes"], 7)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.destination.read_bytes(), b"previous verified artifact")
        self.assert_clean()

    def test_digest_validation_cannot_publish_after_deadline(self):
        with self.assertRaises(build.ArtifactDownloadError) as caught:
            self.download([{"elapsed": 600}])
        self.assertEqual(caught.exception.details["error_kind"], "timeout")
        self.assertEqual(self.destination.read_bytes(), b"previous verified artifact")
        self.assert_clean()

    def test_cleanup_failure_is_classified_without_retry_or_overwriting_target(self):
        original = Path.unlink

        def denied(path, **kwargs):
            if path.name.startswith(".docker-ce.deb."):
                raise PermissionError("sensitive filesystem detail")
            return original(path, **kwargs)

        with mock.patch.object(Path, "unlink", new=denied):
            with self.assertRaises(build.ArtifactDownloadError) as caught:
                self.download([{"code": 18, "data": b"partial"}])
        self.assertEqual(caught.exception.details["error_kind"], "filesystem_error")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertEqual(self.destination.read_bytes(), b"previous verified artifact")

    def test_failure_and_progress_events_never_expose_remote_text_or_unknown_names(self):
        self.destination = self.root / "secret-artifact-name"
        self.destination.write_bytes(b"previous verified artifact")
        with self.assertRaises(build.ArtifactDownloadError) as caught:
            self.download([{"code": 22, "status": 404}])
        with mock.patch.object(build, "emit") as emit:
            build.report_failure(caught.exception)
        self.assertEqual(emit.call_args.kwargs["artifact"], "unknown")
        self.assertEqual(emit.call_args.kwargs["http_status"], 404)
        self.assertEqual(emit.call_args.kwargs["received_bytes"], len(self.good))
        for value in (str(caught.exception), repr(emit.call_args), repr(self.events)):
            self.assertNotIn("do-not-log", value)
            self.assertNotIn("https://", value)
            self.assertNotIn("secret-artifact-name", value)


if __name__ == "__main__":
    unittest.main()
