from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.ci import validate_commit_identity as identity


ROOT = Path(__file__).resolve().parents[2]
CI_TEXT = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
SCRIPT = ROOT / "scripts" / "ci" / "validate_commit_identity.py"
GOOD_EMAIL = "48942548+lzq390@users.noreply.github.com"


def run_git(
    repository: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        env=environment,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


class CommitIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repository = Path(self.temporary_directory.name)
        run_git(self.repository, "init", "--quiet")
        run_git(self.repository, "config", "user.name", "Liu")
        run_git(self.repository, "config", "user.email", GOOD_EMAIL)
        self.base_sha = self.commit("base")

    def commit(
        self,
        message: str,
        *,
        author_email: str = GOOD_EMAIL,
        committer_email: str = GOOD_EMAIL,
    ) -> str:
        environment = os.environ.copy()
        environment.update(
            {
                "GIT_AUTHOR_NAME": "Liu",
                "GIT_AUTHOR_EMAIL": author_email,
                "GIT_COMMITTER_NAME": "Liu",
                "GIT_COMMITTER_EMAIL": committer_email,
            }
        )
        run_git(
            self.repository,
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            message,
            environment=environment,
        )
        return run_git(self.repository, "rev-parse", "HEAD")

    def findings(self, head_sha: str, *, base_sha: str | None = None):
        return identity.validate_range(
            self.repository,
            base_sha or self.base_sha,
            head_sha,
        )[1]

    def test_accepts_the_configured_noreply_identity(self) -> None:
        head_sha = self.commit("valid identity")
        self.assertEqual(self.findings(head_sha), ())

    def test_rejects_blocked_author_case_insensitively(self) -> None:
        head_sha = self.commit("bad author", author_email="X@Y")
        self.assertEqual(
            [finding.field for finding in self.findings(head_sha)],
            ["author"],
        )

    def test_rejects_blocked_committer(self) -> None:
        head_sha = self.commit("bad committer", committer_email="x@y")
        self.assertEqual(
            [finding.field for finding in self.findings(head_sha)],
            ["committer"],
        )

    def test_rejects_case_insensitive_coauthor_trailer(self) -> None:
        head_sha = self.commit(
            "bad trailer\n\ncO-aUtHoReD-bY : Liu <X@Y>"
        )
        self.assertEqual(
            [finding.field for finding in self.findings(head_sha)],
            ["Co-authored-by"],
        )

    def test_accepts_an_unrelated_coauthor(self) -> None:
        head_sha = self.commit(
            "valid trailer\n\nCo-authored-by: Example <example@example.com>"
        )
        self.assertEqual(self.findings(head_sha), ())

    def test_ignores_blocked_identity_at_the_base_of_the_range(self) -> None:
        historical_bad_sha = self.commit(
            "historical trailer\n\nCo-authored-by: Liu <x@y>"
        )
        head_sha = self.commit("new valid commit")
        self.assertEqual(
            self.findings(head_sha, base_sha=historical_bad_sha),
            (),
        )

    def test_fails_closed_for_an_invalid_sha(self) -> None:
        head_sha = self.commit("valid identity")
        with self.assertRaises(identity.CommitIdentityError):
            identity.validate_range(self.repository, "not-a-sha", head_sha)

    def test_command_line_reports_the_blocked_field(self) -> None:
        head_sha = self.commit("bad author", author_email="x@y")
        result = subprocess.run(
            [
                "python3",
                str(SCRIPT),
                "--repository",
                str(self.repository),
                "--base-sha",
                self.base_sha,
                "--head-sha",
                head_sha,
            ],
            cwd=ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("author contains x@y", result.stdout)

    def test_workflow_runs_the_guard_over_new_commit_range(self) -> None:
        self.assertIn("Reject blocked commit identities", CI_TEXT)
        self.assertIn("scripts/ci/validate_commit_identity.py", CI_TEXT)
        self.assertIn(
            "github.event.pull_request.base.sha || github.event.before",
            CI_TEXT,
        )
        self.assertIn(
            "github.event.pull_request.head.sha || github.sha",
            CI_TEXT,
        )


if __name__ == "__main__":
    unittest.main()
