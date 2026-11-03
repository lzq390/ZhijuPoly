from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest

from scripts.ci import validate_workflows as policy


ROOT = Path(__file__).resolve().parents[2]
CI_TEXT = (ROOT / ".github/workflows/ci.yml").read_text()
SCRIPT = ROOT / "scripts/ci/prepare_image_build_disk.sh"


class ImageInfrastructurePolicyTests(unittest.TestCase):
    def test_explicit_git_credentials_are_required_for_every_build(self) -> None:
        failures: list[str] = []
        policy.validate_remote_git_context_auth(CI_TEXT, failures)
        self.assertEqual(failures, [])
        secret = "\n".join(policy.REVIEWED_GIT_SECRET_LINES) + "\n"
        for job in ("image-build", "release", "openscience-overlay", "openscience-release"):
            body = policy.workflow_job_body(CI_TEXT, job, [])
            assert body is not None
            builds = [step for step in policy.workflow_step_blocks(body) if secret in step]
            for step in builds:
                changed_body = body.replace(step, step.replace(secret, "", 1), 1)
                failures = []
                policy.validate_remote_git_context_auth(CI_TEXT.replace(body, changed_body), failures)
                self.assertTrue(any(job in failure for failure in failures), failures)

    def test_build_context_must_stay_immutable(self) -> None:
        failures: list[str] = []
        changed = CI_TEXT.replace(policy.REVIEWED_GIT_CONTEXT_LINE, "          context: .", 1)
        policy.validate_remote_git_context_auth(changed, failures)
        self.assertTrue(failures)

    def test_disk_preparation_is_required_and_cannot_be_ignored(self) -> None:
        failures: list[str] = []
        policy.validate_image_disk_preparation(CI_TEXT, failures)
        self.assertEqual(failures, [])
        for job in ("image-build", "exact-b-bridge", "release"):
            body = policy.workflow_job_body(CI_TEXT, job, [])
            assert body is not None
            selected = next(step for step in policy.workflow_step_blocks(body) if str(SCRIPT.name) in step)
            for replacement in ("", selected.replace("        timeout-minutes:", "        continue-on-error: true\n        timeout-minutes:")):
                with self.subTest(job=job, replacement=replacement):
                    failures = []
                    changed = CI_TEXT.replace(body, body.replace(selected, replacement, 1), 1)
                    policy.validate_image_disk_preparation(changed, failures)
                    self.assertTrue(any(job in failure for failure in failures), failures)


class HostedRunnerDiskPreparationTests(unittest.TestCase):
    def run_script(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.update({
            "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
            "RUNNER_OS": "Linux", "ImageOS": "ubuntu24", "GITHUB_JOB": "exact-b-bridge",
            "FAKE_DOCKER_ROOT": "/var/lib/docker", "FAKE_FREE_KIB": str(32 * 1024 * 1024),
        })
        environment.update(overrides)
        # Every command that could mutate machine state is a shell stub. The
        # real script still exercises all guards, ordering, and capacity checks.
        harness = r'''
docker() { printf '%s\n' "$FAKE_DOCKER_ROOT"; }
sudo() { printf 'SDK_DELETE'; printf ' <%s>' "$@"; printf '\n'; }
df() {
  if [[ "$1" == -Pk ]]; then
    printf 'Filesystem 1024-blocks Used Available Capacity Mounted\n'
    printf '/dev/fake 999999999 1 %s 1%% /\n' "$FAKE_FREE_KIB"
  else
    printf 'DISK_REPORT\n'
  fi
}
export -f docker sudo df
exec bash "$1"
'''
        return subprocess.run(
            ["bash", "-c", harness, "disk-test", str(SCRIPT)],
            env=environment, capture_output=True, text=True, timeout=10, check=False,
        )

    def test_supported_jobs_delete_only_unused_sdks(self) -> None:
        for job in ("exact-b-bridge", "image-build", "release"):
            with self.subTest(job=job):
                result = self.run_script(GITHUB_JOB=job)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(
                    "SDK_DELETE <--non-interactive> <rm> <-rf> <--> "
                    "</usr/local/lib/android> </usr/share/dotnet> "
                    "</usr/local/.ghcup> </usr/share/swift>", result.stdout,
                )
                self.assertEqual(result.stdout.count("DISK_REPORT"), 2)
                self.assertEqual(result.stdout.count("SDK_DELETE"), 1)

    def test_unapproved_environments_fail_before_cleanup(self) -> None:
        for variable, value in (
            ("GITHUB_ACTIONS", ""), ("RUNNER_ENVIRONMENT", "self-hosted"),
            ("RUNNER_OS", "Windows"), ("ImageOS", "ubuntu22"),
            ("GITHUB_JOB", "production"), ("FAKE_DOCKER_ROOT", "/custom/docker"),
        ):
            with self.subTest(variable=variable):
                result = self.run_script(**{variable: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("SDK_DELETE", result.stdout)

    def test_low_or_unreadable_space_is_a_hard_failure(self) -> None:
        for free in (str(32 * 1024 * 1024 - 1), "", "unknown"):
            with self.subTest(free=free):
                result = self.run_script(FAKE_FREE_KIB=free)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("at least 32 GiB", result.stderr)


if __name__ == "__main__":
    unittest.main()
