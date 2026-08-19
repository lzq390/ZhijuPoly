#!/usr/bin/env python3
"""Reject blocked identities in commits newly introduced to protected main."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


BLOCKED_EMAILS = frozenset({"x@y"})
FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
CO_AUTHOR_TRAILER = re.compile(
    r"^[ \t]*co-authored-by[ \t]*:[ \t]*(?P<value>[^\r\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
EMAIL_ATOM_CHARS = r"A-Za-z0-9.!#$%&'*+/=?^_`{|}~-"


class CommitIdentityError(RuntimeError):
    """Raised when the requested Git range cannot be checked safely."""


@dataclass(frozen=True)
class Finding:
    commit_sha: str
    field: str
    email: str


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        command = "git " + " ".join(arguments)
        raise CommitIdentityError(f"{command} failed: {stderr}")
    return result.stdout.decode("utf-8", errors="replace")


def _validated_sha(value: str, label: str) -> str:
    if not FULL_SHA.fullmatch(value):
        raise CommitIdentityError(f"{label} must be a full 40-character Git SHA")
    return value.lower()


def commits_in_range(repository: Path, base_sha: str, head_sha: str) -> tuple[str, ...]:
    """Return commits reachable from head but not from base, oldest first."""

    base = _validated_sha(base_sha, "base SHA")
    head = _validated_sha(head_sha, "head SHA")
    for label, commit_sha in (("base", base), ("head", head)):
        object_type = _git(repository, "cat-file", "-t", commit_sha).strip()
        if object_type != "commit":
            raise CommitIdentityError(
                f"{label} SHA {commit_sha} is {object_type!r}, not a commit"
            )

    output = _git(repository, "rev-list", "--reverse", f"{base}..{head}", "--")
    commits = tuple(line for line in output.splitlines() if line)
    if any(not FULL_SHA.fullmatch(commit_sha) for commit_sha in commits):
        raise CommitIdentityError("git rev-list returned an invalid commit SHA")
    return commits


def _raw_email(repository: Path, commit_sha: str, placeholder: str) -> str:
    # Lower-case %ae/%ce deliberately bypass .mailmap canonicalization.
    return _git(
        repository,
        "show",
        "--no-patch",
        f"--format={placeholder}",
        commit_sha,
        "--",
    ).strip()


def _message(repository: Path, commit_sha: str) -> str:
    return _git(
        repository,
        "show",
        "--no-patch",
        "--format=%B",
        commit_sha,
        "--",
    )


def _trailer_mentions_email(value: str, email: str) -> bool:
    pattern = re.compile(
        rf"(?<![{EMAIL_ATOM_CHARS}]){re.escape(email)}"
        rf"(?![{EMAIL_ATOM_CHARS}])",
        re.IGNORECASE,
    )
    return pattern.search(value) is not None


def validate_range(
    repository: Path,
    base_sha: str,
    head_sha: str,
) -> tuple[tuple[str, ...], tuple[Finding, ...]]:
    commits = commits_in_range(repository, base_sha, head_sha)
    findings: list[Finding] = []
    blocked_by_casefold = {email.casefold(): email for email in BLOCKED_EMAILS}

    for commit_sha in commits:
        for field, placeholder in (("author", "%ae"), ("committer", "%ce")):
            actual_email = _raw_email(repository, commit_sha, placeholder)
            blocked_email = blocked_by_casefold.get(actual_email.casefold())
            if blocked_email is not None:
                findings.append(Finding(commit_sha, field, blocked_email))

        message = _message(repository, commit_sha)
        for trailer in CO_AUTHOR_TRAILER.finditer(message):
            value = trailer.group("value")
            for blocked_email in BLOCKED_EMAILS:
                if _trailer_mentions_email(value, blocked_email):
                    findings.append(
                        Finding(commit_sha, "Co-authored-by", blocked_email)
                    )

    return commits, tuple(findings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path.cwd(),
        help="Git worktree to inspect (default: current directory)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repository = args.repository.resolve()
    try:
        commits, findings = validate_range(
            repository,
            args.base_sha,
            args.head_sha,
        )
    except CommitIdentityError as exc:
        print(f"commit identity validation failed closed: {exc}")
        return 2

    if findings:
        print("blocked commit identities detected:")
        for finding in findings:
            print(
                f"- {finding.commit_sha}: {finding.field} contains "
                f"{finding.email}"
            )
        return 1

    print(f"validated {len(commits)} new commit(s); no blocked identities found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
