"""The template CLI exposes safe transfer evidence without remote text."""
from pathlib import Path
import unittest

from ops.self_hosted_runner import template


class TemplateDownloadDiagnosticTests(unittest.TestCase):
    def test_failure_reports_the_artifact_and_observed_short_read(self):
        event = {"event": "failed", "stage": "docker_packages", "error_kind": "short_read",
                 "artifact": "docker-ce.deb", "attempt": 3, "http_status": 200,
                 "received_bytes": 9539441, "expected_bytes": 24586336,
                 "exception": "credential-secret", "response": "credential-secret"}
        output = str(template.guest_build_failure(event, Path("/protected/build.log")))
        for value in ("docker_packages", "short_read", "artifact=docker-ce.deb", "attempt=3",
                      "http_status=200", "received_bytes=9539441", "expected_bytes=24586336"):
            self.assertIn(value, output)
        self.assertNotIn("credential-secret", output)

    def test_failure_does_not_turn_unknown_length_into_zero(self):
        output = str(template.guest_build_failure({"stage": "runner_download",
                     "error_kind": "checksum_mismatch", "artifact": "runner.tar.gz",
                     "attempt": 3, "received_bytes": 123}, Path("/protected/build.log")))
        self.assertIn("checksum_mismatch", output)
        self.assertNotIn("expected_bytes", output)

    def test_failure_rejects_unbounded_numbers_and_arbitrary_artifact_text(self):
        output = str(template.guest_build_failure({"stage": "docker_packages",
                     "error_kind": "validation_error", "artifact": "credential-secret",
                     "attempt": True, "received_bytes": -1, "expected_bytes": 2 ** 63,
                     "http_status": 999}, Path("/protected/build.log")))
        self.assertNotIn("credential-secret", output)
        for field in ("artifact=", "attempt=", "received_bytes=", "expected_bytes=", "http_status="):
            self.assertNotIn(field, output)

    def test_retries_and_success_are_visible_with_fixed_fields(self):
        event = {"event": "template_download", "state": "retry", "artifact": "docker-ce.deb",
                 "attempt": 1, "error_kind": "short_read", "received_bytes": 1, "expected_bytes": 2}
        output = template.guest_download_progress(event)
        self.assertIn("state=retry", output)
        self.assertIn("short_read", output)
        self.assertIn("received_bytes=1", output)
        event.update(state="verified", attempt=2)
        event.pop("error_kind")
        self.assertIn("state=verified", template.guest_download_progress(event))

    def test_progress_cannot_print_remote_text_or_arbitrary_identities(self):
        valid = {"event": "template_download", "state": "started",
                 "artifact": "docker-ce.deb", "attempt": 1}
        cases = [None, [], {**valid, "artifact": ["docker-ce.deb"]},
                 {**valid, "artifact": "credential-secret"}, {**valid, "state": "credential-secret"},
                 {**valid, "attempt": True}, {**valid, "attempt": 4},
                 {**valid, "url": "https://user:credential-secret@example.com"}]
        for event in cases:
            with self.subTest(event=event):
                self.assertIsNone(template.guest_download_progress(event))


if __name__ == "__main__":
    unittest.main()
