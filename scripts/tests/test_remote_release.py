from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "ci" / "remote_release.sh"
RELEASE_SHA = "a" * 40


class RemoteReleaseTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.transport_log = self.root / "transport.log"
        self._write_fake_transport("ssh")
        self._write_fake_transport("scp")

        self.bundle = self.root / f"nexpoly-release-{RELEASE_SHA}.tar.gz"
        controller = b"#!/usr/bin/env python3\n"
        info = tarfile.TarInfo("./scripts/release_controller.py")
        info.mode = 0o700
        info.size = len(controller)
        with tarfile.open(self.bundle, "w:gz") as archive:
            archive.addfile(info, io.BytesIO(controller))

        bundle_bytes = self.bundle.read_bytes()
        self.manifest = self.root / "release-manifest.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "source_sha": RELEASE_SHA,
                    "release_bundle": {
                        "name": self.bundle.name,
                        "size": len(bundle_bytes),
                        "sha256": "sha256:" + hashlib.sha256(bundle_bytes).hexdigest(),
                    },
                }
            ),
            encoding="utf-8",
        )
        self.environment = {
            **os.environ,
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "TRANSPORT_LOG": str(self.transport_log),
            "NEXPOLY_RELEASE_SHA": RELEASE_SHA,
            "NEXPOLY_SSH_HOST": "production.example.invalid",
            "NEXPOLY_SSH_USER": "deploy",
            "NEXPOLY_SSH_PRIVATE_KEY": "test-private-key",
            "NEXPOLY_SSH_KNOWN_HOSTS": "production.example.invalid ssh-ed25519 AAAATEST",
            "NEXPOLY_PRODUCTION_ROOT": "/data/lzq/gith/nexpoly",
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "1",
        }

    def _write_fake_transport(self, name: str) -> None:
        path = self.bin_dir / name
        path.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '{name}\\n' >>\"$TRANSPORT_LOG\"\n"
            "printf '%s\\n' \"$@\" >>\"$TRANSPORT_LOG\"\n"
            "cat >/dev/null || true\n",
            encoding="utf-8",
        )
        path.chmod(0o700)

    def run_script(
        self,
        operation: str = "auto",
        *,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SCRIPT), operation, str(self.manifest), str(self.bundle)],
            cwd=REPOSITORY_ROOT,
            env=environment or self.environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_no_transport(self) -> None:
        self.assertFalse(self.transport_log.exists(), self.transport_log.read_text() if self.transport_log.exists() else "")

    def test_valid_pair_uses_two_ssh_calls_and_one_scp_call(self) -> None:
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.transport_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(calls.count("ssh"), 2)
        self.assertEqual(calls.count("scp"), 1)
        self.assertNotIn("test-private-key", "\n".join(calls))

    def test_tampered_bundle_is_rejected_before_transport(self) -> None:
        with self.bundle.open("ab") as destination:
            destination.write(b"tampered")

        result = self.run_script()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wrong size", result.stderr)
        self.assert_no_transport()

    def test_invalid_operation_is_rejected_before_transport(self) -> None:
        result = self.run_script("redeploy")

        self.assertEqual(result.returncode, 2)
        self.assertIn("operation must be auto or bootstrap", result.stderr)
        self.assert_no_transport()

    def test_missing_known_hosts_is_rejected_before_transport(self) -> None:
        environment = dict(self.environment)
        del environment["NEXPOLY_SSH_KNOWN_HOSTS"]

        result = self.run_script(environment=environment)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NEXPOLY_SSH_KNOWN_HOSTS", result.stderr)
        self.assert_no_transport()

    def test_symlink_bundle_is_rejected_before_transport(self) -> None:
        real_bundle = self.bundle.with_suffix(".real")
        self.bundle.rename(real_bundle)
        self.bundle.symlink_to(real_bundle)

        result = self.run_script()

        self.assertEqual(result.returncode, 2)
        self.assertIn("missing or is a symlink", result.stderr)
        self.assert_no_transport()


if __name__ == "__main__":
    unittest.main()
