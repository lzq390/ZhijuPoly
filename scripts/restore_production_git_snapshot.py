#!/usr/bin/env python3
"""Restore production Git metadata from the sealed whole-directory snapshot.

The command is deliberately separate from deployment rollback. It may run
only after an operator has made a terminal decision for an interrupted first
deployment. It exchanges the complete production ``.git`` directory with an
independent golden copy and archives the displaced directory. Ref deletion,
reset-only repair, and partial object recovery are not accepted substitutes.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left
import ctypes
import errno
import fcntl
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Mapping


SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))
import production_git_snapshot as snapshot  # noqa: E402


PRODUCTION_ROOT = snapshot.PRODUCTION_ROOT
RUNTIME_ROOT = snapshot.RUNTIME_ROOT
RESTORE_JOURNAL_ROOT_RELATIVE = Path("state/production-git-restore-transactions")
DEPLOY_LOCK_RELATIVE = Path("state/deploy.lock")
CURRENT_STATE_RELATIVE = Path("state/current-deployment.json")
DEPLOY_MARKER_RELATIVE = Path("state/deploy-in-progress.json")
RESTORE_OPERATION_RE = re.compile(r"restore-git-[a-z0-9][a-z0-9._-]{7,95}\Z")
DEPLOY_OPERATION_RE = re.compile(r"[a-z0-9][a-z0-9._-]{7,127}\Z")
DECISIONS = {
    "abandon-operation-to-predecessor",
    "restore-before-new-operation",
}
RENAME_EXCHANGE = 2
POLICY = "nexpoly-production-git-whole-directory-restore-v1"
MAX_TRACKED_PATHS = 2_000_000


class RestoreError(RuntimeError):
    """The golden Git restore cannot be performed safely."""


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require_restore_operation(value: object) -> str:
    if not isinstance(value, str) or RESTORE_OPERATION_RE.fullmatch(value) is None:
        raise RestoreError("restore operation ID is invalid")
    return value


def _require_deploy_operation(value: object) -> str:
    if not isinstance(value, str) or DEPLOY_OPERATION_RE.fullmatch(value) is None:
        raise RestoreError("abandoned deployment operation ID is invalid")
    return value


def _require_decision(value: object) -> str:
    if not isinstance(value, str) or value not in DECISIONS:
        raise RestoreError("restore terminal decision is invalid")
    return value


def _require_absolute_path(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RestoreError(f"{label} is invalid")
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise RestoreError(f"{label} is invalid")
    return value


def _require_git_path(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RestoreError(f"{label} is invalid")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or path.as_posix() != value
        or "." in path.parts
        or ".." in path.parts
        or path.parts[0] == ".git"
    ):
        raise RestoreError(f"{label} is invalid")
    return value


def _validate_identity(value: object, label: str) -> dict[str, int]:
    if (
        not isinstance(value, dict)
        or set(value) != {"device", "inode"}
        or type(value.get("device")) is not int
        or type(value.get("inode")) is not int
        or value["device"] < 0
        or value["inode"] <= 0
    ):
        raise RestoreError(f"{label} is invalid")
    return {"device": value["device"], "inode": value["inode"]}


def _directory_identity(path: Path) -> dict[str, int]:
    descriptor = snapshot._open_directory(path)
    try:
        metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    return {"device": metadata.st_dev, "inode": metadata.st_ino}


def _open_lock(path: Path) -> int:
    snapshot._require_private_directory(path.parent)
    try:
        descriptor = os.open(
            path,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise RestoreError(f"restore lock identity is unsafe: {path}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except OSError as exc:
        raise RestoreError(f"restore lock is unavailable: {path}") from exc


def _rename_exchange(parent: Path, left: str, right: str) -> None:
    if not all(
        name and "/" not in name and name not in {".", ".."}
        for name in (left, right)
    ):
        raise RestoreError("restore exchange name is unsafe")
    parent_fd = snapshot._open_directory(parent)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise RestoreError("atomic rename exchange is unavailable")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            parent_fd,
            left.encode("utf-8"),
            parent_fd,
            right.encode("utf-8"),
            RENAME_EXCHANGE,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise RestoreError(
                f"atomic Git directory exchange failed with errno {error}"
            )
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _manifest_digest(git_dir: Path) -> tuple[dict[str, object], str]:
    manifest = snapshot.scan_git_directory(git_dir)
    return manifest, snapshot.canonical_digest(manifest)


def _worktree_state(
    production_root: Path,
    *,
    predecessor_sha: str,
    predecessor_tree: str,
) -> dict[str, object]:
    try:
        head = snapshot._run_git(production_root, "rev-parse", "HEAD").stdout.strip()
        tree = snapshot._run_git(
            production_root,
            "rev-parse",
            "HEAD^{tree}",
        ).stdout.strip()
        status_output = snapshot._run_git(
            production_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout
    except snapshot.SnapshotError:
        return {
            "git_readable": False,
            "head": None,
            "tree": None,
            "clean": False,
            "matches_predecessor": False,
        }
    clean = not bool(status_output)
    return {
        "git_readable": True,
        "head": head,
        "tree": tree,
        "clean": clean,
        "matches_predecessor": (
            head == predecessor_sha and tree == predecessor_tree and clean
        ),
    }


def _validate_worktree(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "git_readable",
        "head",
        "tree",
        "clean",
        "matches_predecessor",
    }:
        raise RestoreError("restore worktree evidence has an invalid shape")
    for field in ("git_readable", "clean", "matches_predecessor"):
        if type(value.get(field)) is not bool:
            raise RestoreError("restore worktree evidence is invalid")
    if value["git_readable"]:
        snapshot._require_sha(value.get("head"), "restore worktree HEAD")
        snapshot._require_sha(value.get("tree"), "restore worktree tree")
    elif (
        value.get("head") is not None
        or value.get("tree") is not None
        or value["clean"]
        or value["matches_predecessor"]
    ):
        raise RestoreError("unreadable restore worktree evidence is invalid")
    if value["matches_predecessor"] and not value["clean"]:
        raise RestoreError("matching restore worktree is not clean")
    return dict(value)


def _marker_evidence(runtime_root: Path) -> dict[str, object] | None:
    path = runtime_root / DEPLOY_MARKER_RELATIVE
    if not path.exists() and not path.is_symlink():
        return None
    document, raw_digest = snapshot._load_private_json(path)
    operation_id = _require_deploy_operation(document.get("operation_id"))
    action = document.get("action")
    phase = document.get("phase")
    if not isinstance(action, str) or not action or not isinstance(phase, str) or not phase:
        raise RestoreError("deployment marker action or phase is invalid")
    return {
        "operation_id": operation_id,
        "raw_sha256": raw_digest,
        "action": action,
        "phase": phase,
    }


def _tracked_paths(repository: Path, revision: str) -> list[str]:
    snapshot._require_sha(revision, "restore tracked-tree revision")
    output = snapshot._run_git(
        repository,
        "ls-tree",
        "-r",
        "--name-only",
        "-z",
        revision,
    ).stdout
    paths = output.split("\0")
    if paths and paths[-1] == "":
        paths.pop()
    normalized = [_require_git_path(path, "restore tracked path") for path in paths]
    if len(normalized) > MAX_TRACKED_PATHS or normalized != sorted(set(normalized)):
        raise RestoreError("restore tracked path inventory is not canonical")
    return normalized


def _validate_plan(document: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "policy",
        "restore_operation_id",
        "abandoned_deploy_operation_id",
        "terminal_decision",
        "snapshot_authority_sha256",
        "snapshot_operation_id",
        "target_source_sha",
        "target_source_tree",
        "production_source_sha",
        "production_source_tree",
        "golden_manifest_sha256",
        "live_manifest_sha256",
        "live_manifest_summary",
        "live_git_identity",
        "worktree_before",
        "materialize_worktree",
        "worktree_cleanup_paths",
        "deployment_marker",
        "staging_one",
        "archive_root",
        "mutations",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 1
        or document.get("policy") != POLICY
    ):
        raise RestoreError("restore plan has an invalid shape")
    _require_restore_operation(document.get("restore_operation_id"))
    _require_deploy_operation(document.get("abandoned_deploy_operation_id"))
    _require_decision(document.get("terminal_decision"))
    snapshot._require_operation_id(document.get("snapshot_operation_id"))
    for field in (
        "target_source_sha",
        "target_source_tree",
        "production_source_sha",
        "production_source_tree",
    ):
        snapshot._require_sha(document.get(field), f"restore plan {field}")
    for field in (
        "snapshot_authority_sha256",
        "golden_manifest_sha256",
        "live_manifest_sha256",
    ):
        snapshot._require_digest(document.get(field), f"restore plan {field}")
    summary = document.get("live_manifest_summary")
    if not isinstance(summary, dict) or set(summary) != {
        "records_sha256",
        "file_count",
        "directory_count",
        "total_file_bytes",
    }:
        raise RestoreError("restore live manifest summary is invalid")
    snapshot._require_digest(summary.get("records_sha256"), "restore live records")
    for field in ("file_count", "directory_count", "total_file_bytes"):
        if type(summary.get(field)) is not int or summary[field] < 0:
            raise RestoreError("restore live manifest summary is invalid")
    _validate_identity(document.get("live_git_identity"), "restore live Git identity")
    worktree = _validate_worktree(document.get("worktree_before"))
    materialize = document.get("materialize_worktree")
    if type(materialize) is not bool or materialize is worktree["matches_predecessor"]:
        raise RestoreError("restore worktree decision differs")
    if materialize:
        if (
            not worktree["git_readable"]
            or not worktree["clean"]
            or worktree["head"] != document["target_source_sha"]
            or worktree["tree"] != document["target_source_tree"]
        ):
            raise RestoreError("restore target worktree evidence differs")
    elif (
        not worktree["matches_predecessor"]
        or worktree["head"] != document["production_source_sha"]
        or worktree["tree"] != document["production_source_tree"]
    ):
        raise RestoreError("restore predecessor worktree evidence differs")
    cleanup = document.get("worktree_cleanup_paths")
    if not isinstance(cleanup, list):
        raise RestoreError("restore cleanup path inventory is invalid")
    normalized_cleanup = [
        _require_git_path(path, "restore cleanup path") for path in cleanup
    ]
    if (
        len(normalized_cleanup) > MAX_TRACKED_PATHS
        or normalized_cleanup != sorted(set(normalized_cleanup))
        or (not materialize and normalized_cleanup)
    ):
        raise RestoreError("restore cleanup path inventory is not canonical")
    marker = document.get("deployment_marker")
    if marker is not None:
        if not isinstance(marker, dict) or set(marker) != {
            "operation_id",
            "raw_sha256",
            "action",
            "phase",
        }:
            raise RestoreError("restore deployment marker evidence is invalid")
        _require_deploy_operation(marker.get("operation_id"))
        snapshot._require_digest(marker.get("raw_sha256"), "restore deployment marker")
        if (
            not isinstance(marker.get("action"), str)
            or not marker["action"]
            or not isinstance(marker.get("phase"), str)
            or not marker["phase"]
            or marker["operation_id"]
            != document["abandoned_deploy_operation_id"]
        ):
            raise RestoreError("restore deployment marker evidence is invalid")
    _require_absolute_path(document.get("staging_one"), "restore staging path")
    _require_absolute_path(document.get("archive_root"), "restore archive path")
    expected_mutations = {
        "whole_production_git_exchange": True,
        "displaced_git_worktree_materialization": materialize,
        "individual_ref_repair": False,
        "database": False,
        "services": False,
        "containers": False,
        "golden_snapshot": False,
    }
    if document.get("mutations") != expected_mutations:
        raise RestoreError("restore mutation scope is invalid")
    return dict(document)


def _validate_journal(document: object) -> dict[str, Any]:
    fields = {
        "schema_version",
        "status",
        "phase",
        "plan",
        "plan_sha256",
        "created_at",
        "completed_at",
        "stage_one_identity",
        "exchange_count",
        "displaced_final_manifest_sha256",
        "final_manifest_sha256",
        "final_worktree",
    }
    phases = {
        "intent",
        "before-manifest-sealed",
        "first-staging-ready",
        "first-exchange-intent",
        "first-exchanged",
        "materialize-intent",
        "materialized",
        "archive-intent",
        "archived",
        "completed",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != 1
        or document.get("phase") not in phases
        or not isinstance(document.get("plan"), dict)
        or document.get("plan_sha256")
        != snapshot.canonical_digest(document["plan"])
    ):
        raise RestoreError("restore journal has an invalid shape")
    plan = _validate_plan(document["plan"])
    snapshot._require_digest(document.get("plan_sha256"), "restore journal plan")
    created_at = document.get("created_at")
    completed_at = document.get("completed_at")
    if not isinstance(created_at, str) or snapshot.UTC_RE.fullmatch(created_at) is None:
        raise RestoreError("restore journal creation timestamp is invalid")
    completed = document["phase"] == "completed"
    if completed:
        if (
            document.get("status") != "completed"
            or not isinstance(completed_at, str)
            or snapshot.UTC_RE.fullmatch(completed_at) is None
            or completed_at < created_at
        ):
            raise RestoreError("completed restore journal is invalid")
    elif document.get("status") != "in-progress" or completed_at is not None:
        raise RestoreError("nonterminal restore journal is invalid")
    stage_identity = document.get("stage_one_identity")
    if stage_identity is not None:
        _validate_identity(stage_identity, "restore staging identity")
    phases_after_staging = phases - {"intent", "before-manifest-sealed"}
    if document["phase"] in phases_after_staging and stage_identity is None:
        raise RestoreError("restore journal lost its staging identity")
    exchange_count = document.get("exchange_count")
    if type(exchange_count) is not int or exchange_count not in {0, 1}:
        raise RestoreError("restore exchange count is invalid")
    before_exchange = {
        "intent",
        "before-manifest-sealed",
        "first-staging-ready",
        "first-exchange-intent",
    }
    if (document["phase"] in before_exchange) != (exchange_count == 0):
        raise RestoreError("restore exchange count differs from its phase")
    displaced_digest = document.get("displaced_final_manifest_sha256")
    archive_phases = {"archive-intent", "archived", "completed"}
    if document["phase"] in archive_phases:
        snapshot._require_digest(displaced_digest, "restore displaced Git manifest")
    elif displaced_digest is not None:
        raise RestoreError("restore displaced Git manifest is premature")
    final_digest = document.get("final_manifest_sha256")
    final_worktree = document.get("final_worktree")
    if completed:
        snapshot._require_digest(final_digest, "restore final manifest")
        if not _validate_worktree(final_worktree)["matches_predecessor"]:
            raise RestoreError("completed restore worktree is invalid")
    elif final_digest is not None or final_worktree is not None:
        raise RestoreError("nonterminal restore has final evidence")
    if not plan["materialize_worktree"] and document["phase"] in {
        "materialize-intent",
        "materialized",
    }:
        raise RestoreError("restore journal has an unexpected materialization")
    return dict(document)


def _run_displaced_git(
    git_dir: Path,
    worktree: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    environment = {
        "USER": "devuser",
        "LOGNAME": "devuser",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
    }
    command = [
        "/usr/bin/git",
        f"--git-dir={git_dir}",
        f"--work-tree={worktree}",
        *arguments,
    ]
    try:
        return subprocess.run(
            command,
            cwd=worktree,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
            timeout=900,
            umask=0o077,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RestoreError(f"displaced Git command failed: {' '.join(arguments)}") from exc


class ProductionGitRestoreManager:
    def __init__(
        self,
        production_root: Path,
        runtime_root: Path,
        *,
        allow_test: bool = False,
        checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.production_root = production_root.absolute()
        self.runtime_root = runtime_root.absolute()
        self.git_dir = self.production_root / ".git"
        self.state_root = self.runtime_root / "state"
        self.journal_root = self.runtime_root / RESTORE_JOURNAL_ROOT_RELATIVE
        self.checkpoint = checkpoint or (lambda _name: None)
        if not allow_test and (
            self.production_root != PRODUCTION_ROOT
            or self.runtime_root != RUNTIME_ROOT
        ):
            raise RestoreError("production Git restore requires fixed paths")

    def _journal_path(self, restore_operation_id: str) -> Path:
        return self.journal_root / f"{restore_operation_id}.json"

    def _assert_no_current_state(self) -> None:
        path = self.runtime_root / CURRENT_STATE_RELATIVE
        if path.exists() or path.is_symlink():
            raise RestoreError("golden restore is forbidden after current-state commit")

    def _assert_plan_bindings(
        self,
        plan: Mapping[str, object],
        authority: Mapping[str, object],
        authority_digest: str,
    ) -> None:
        operation_id = str(plan["restore_operation_id"])
        expected_stage = self.production_root / f".git.restore-{operation_id}-one"
        expected_archive = (
            Path(str(authority["backup_git_dir"])).parent
            / "restores"
            / operation_id
        )
        if (
            plan["snapshot_authority_sha256"] != authority_digest
            or plan["snapshot_operation_id"] != authority["operation_id"]
            or plan["target_source_sha"] != authority["target_source_sha"]
            or plan["target_source_tree"] != authority["target_source_tree"]
            or plan["production_source_sha"] != authority["production_source_sha"]
            or plan["production_source_tree"] != authority["production_source_tree"]
            or plan["golden_manifest_sha256"] != authority["manifest_sha256"]
            or plan["staging_one"] != str(expected_stage)
            or plan["archive_root"] != str(expected_archive)
        ):
            raise RestoreError("restore plan differs from snapshot authority")

    def _assert_marker(self, plan: Mapping[str, object]) -> None:
        marker = _marker_evidence(self.runtime_root)
        if marker != plan["deployment_marker"]:
            raise RestoreError("deployment marker changed after restore plan")

    def _build_plan(
        self,
        *,
        restore_operation_id: str,
        abandoned_deploy_operation_id: str,
        terminal_decision: str,
    ) -> tuple[dict[str, Any], dict[str, object]]:
        restore_operation_id = _require_restore_operation(restore_operation_id)
        abandoned_deploy_operation_id = _require_deploy_operation(
            abandoned_deploy_operation_id
        )
        terminal_decision = _require_decision(terminal_decision)
        self._assert_no_current_state()
        authority, authority_digest = snapshot.verify_completed_snapshot(
            self.runtime_root,
            production_root=self.production_root,
            full=True,
        )
        marker = _marker_evidence(self.runtime_root)
        if marker is not None and marker["operation_id"] != abandoned_deploy_operation_id:
            raise RestoreError("deployment marker belongs to another operation")
        live_identity = _directory_identity(self.git_dir)
        first, live_digest = _manifest_digest(self.git_dir)
        second, second_digest = _manifest_digest(self.git_dir)
        if (
            first != second
            or live_digest != second_digest
            or _directory_identity(self.git_dir) != live_identity
        ):
            raise RestoreError("live Git directory changed during restore plan")
        before = _worktree_state(
            self.production_root,
            predecessor_sha=authority["production_source_sha"],
            predecessor_tree=authority["production_source_tree"],
        )
        materialize = not bool(before["matches_predecessor"])
        cleanup_paths: list[str] = []
        if materialize:
            if (
                not before["git_readable"]
                or not before["clean"]
                or before["head"] != authority["target_source_sha"]
                or before["tree"] != authority["target_source_tree"]
            ):
                raise RestoreError(
                    "restore requires a clean predecessor or exact clean target worktree"
                )
            target_paths = _tracked_paths(
                self.production_root,
                authority["target_source_sha"],
            )
            predecessor_paths = set(
                _tracked_paths(
                    self.production_root,
                    authority["production_source_sha"],
                )
            )
            cleanup_paths = sorted(set(target_paths) - predecessor_paths)
        operation_root = (
            Path(authority["backup_git_dir"]).parent
            / "restores"
            / restore_operation_id
        )
        stage_one = self.production_root / f".git.restore-{restore_operation_id}-one"
        for path, label in (
            (stage_one, "restore staging namespace"),
            (operation_root, "restore archive namespace"),
        ):
            if path.exists() or path.is_symlink():
                raise RestoreError(f"{label} is occupied")
        plan = {
            "schema_version": 1,
            "policy": POLICY,
            "restore_operation_id": restore_operation_id,
            "abandoned_deploy_operation_id": abandoned_deploy_operation_id,
            "terminal_decision": terminal_decision,
            "snapshot_authority_sha256": authority_digest,
            "snapshot_operation_id": authority["operation_id"],
            "target_source_sha": authority["target_source_sha"],
            "target_source_tree": authority["target_source_tree"],
            "production_source_sha": authority["production_source_sha"],
            "production_source_tree": authority["production_source_tree"],
            "golden_manifest_sha256": authority["manifest_sha256"],
            "live_manifest_sha256": live_digest,
            "live_manifest_summary": {
                "records_sha256": first["records_sha256"],
                "file_count": first["file_count"],
                "directory_count": first["directory_count"],
                "total_file_bytes": first["total_file_bytes"],
            },
            "live_git_identity": live_identity,
            "worktree_before": before,
            "materialize_worktree": materialize,
            "worktree_cleanup_paths": cleanup_paths,
            "deployment_marker": marker,
            "staging_one": str(stage_one),
            "archive_root": str(operation_root),
            "mutations": {
                "whole_production_git_exchange": True,
                "displaced_git_worktree_materialization": materialize,
                "individual_ref_repair": False,
                "database": False,
                "services": False,
                "containers": False,
                "golden_snapshot": False,
            },
        }
        validated = _validate_plan(plan)
        self._assert_plan_bindings(validated, authority, authority_digest)
        return validated, first

    def plan(
        self,
        *,
        restore_operation_id: str,
        abandoned_deploy_operation_id: str,
        terminal_decision: str,
    ) -> dict[str, object]:
        journal = self._journal_path(restore_operation_id)
        if journal.exists() or journal.is_symlink():
            raise RestoreError("restore transaction already exists")
        plan, _manifest = self._build_plan(
            restore_operation_id=restore_operation_id,
            abandoned_deploy_operation_id=abandoned_deploy_operation_id,
            terminal_decision=terminal_decision,
        )
        return {
            "action": "production-git-restore-plan",
            "apply": False,
            "logical_zero_write": True,
            "atime_zero_write": False,
            "plan": plan,
            "plan_sha256": snapshot.canonical_digest(plan),
        }

    def _write_journal(self, path: Path, journal: Mapping[str, object]) -> None:
        _validate_journal(dict(journal))
        snapshot._atomic_private_json(path, journal)

    def _ensure_before_manifest(
        self,
        plan: Mapping[str, object],
    ) -> dict[str, object]:
        archive_root = Path(str(plan["archive_root"]))
        snapshot._ensure_private_directory(archive_root.parent)
        snapshot._ensure_private_directory(archive_root)
        observed_names = set(os.listdir(archive_root))
        if not observed_names.issubset({"BEFORE-MANIFEST.json"}):
            raise RestoreError("restore archive contains unexpected entries")
        before_path = archive_root / "BEFORE-MANIFEST.json"
        if not before_path.exists() and not before_path.is_symlink():
            manifest, digest = _manifest_digest(self.git_dir)
            if (
                digest != plan["live_manifest_sha256"]
                or _directory_identity(self.git_dir) != plan["live_git_identity"]
            ):
                raise RestoreError("live Git changed before manifest seal")
            snapshot._atomic_private_json(before_path, manifest)
            self.checkpoint("restore-before-manifest-sealed")
        raw, digest = snapshot._load_private_json(before_path)
        manifest = snapshot.validate_manifest(raw)
        if digest != plan["live_manifest_sha256"]:
            raise RestoreError("sealed pre-restore manifest changed")
        return manifest

    def _ensure_golden_staging(
        self,
        path: Path,
        *,
        golden_git: Path,
        golden_manifest: Mapping[str, object],
        expected_identity: Mapping[str, int] | None,
    ) -> dict[str, int]:
        if path.exists() or path.is_symlink():
            if expected_identity is not None:
                if _directory_identity(path) != expected_identity:
                    raise RestoreError("restore staging identity changed")
            else:
                try:
                    observed = snapshot.scan_git_directory(path)
                except snapshot.SnapshotError:
                    observed = None
                if observed != golden_manifest:
                    snapshot._remove_private_tree(path)
        if not path.exists():
            if expected_identity is not None:
                raise RestoreError("restore staging disappeared")
            snapshot.copy_git_directory(golden_git, path, golden_manifest)
            self.checkpoint("restore-first-staging-copied")
        identity = _directory_identity(path)
        if expected_identity is not None and identity != expected_identity:
            raise RestoreError("restore staging identity changed")
        if snapshot.scan_git_directory(path) != golden_manifest:
            raise RestoreError("restore staging differs from golden snapshot")
        return identity

    def _exchange_state(
        self,
        *,
        plan: Mapping[str, object],
        stage_one: Path,
        stage_identity: Mapping[str, int],
        before_manifest: Mapping[str, object],
        golden_manifest: Mapping[str, object],
    ) -> str:
        live_identity = _directory_identity(self.git_dir)
        staged_identity = _directory_identity(stage_one)
        live_manifest = snapshot.scan_git_directory(self.git_dir)
        staged_manifest = snapshot.scan_git_directory(stage_one)
        if (
            live_identity == plan["live_git_identity"]
            and staged_identity == stage_identity
            and live_manifest == before_manifest
            and staged_manifest == golden_manifest
        ):
            return "before"
        if (
            live_identity == stage_identity
            and staged_identity == plan["live_git_identity"]
            and live_manifest == golden_manifest
            and staged_manifest == before_manifest
        ):
            return "after"
        raise RestoreError("first Git exchange state is ambiguous")

    @staticmethod
    def _unlink_target_only_leaf(root: Path, relative: str) -> None:
        parts = PurePosixPath(relative).parts
        descriptor = snapshot._open_directory(root)
        try:
            for component in parts[:-1]:
                try:
                    child = os.open(
                        component,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=descriptor,
                    )
                except OSError as exc:
                    if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                        return
                    raise RestoreError("restore cleanup path cannot be traversed") from exc
                os.close(descriptor)
                descriptor = child
            try:
                metadata = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                os.unlink(parts[-1], dir_fd=descriptor)
                os.fsync(descriptor)
            elif not stat.S_ISDIR(metadata.st_mode):
                raise RestoreError("restore cleanup encountered a special entry")
        finally:
            os.close(descriptor)

    def _materialize_predecessor(
        self,
        *,
        plan: Mapping[str, object],
        stage_one: Path,
        stage_identity: Mapping[str, int],
        golden_manifest: Mapping[str, object],
    ) -> dict[str, object]:
        if (
            _directory_identity(self.git_dir) != stage_identity
            or snapshot.scan_git_directory(self.git_dir) != golden_manifest
            or _directory_identity(stage_one) != plan["live_git_identity"]
        ):
            raise RestoreError("restore materialization identities changed")
        _run_displaced_git(
            stage_one,
            self.production_root,
            "reset",
            "--hard",
            str(plan["production_source_sha"]),
        )
        self.checkpoint("restore-displaced-reset")
        cleanup = list(plan["worktree_cleanup_paths"])
        for offset in range(0, len(cleanup), 256):
            chunk = cleanup[offset : offset + 256]
            snapshot._run_git(
                self.production_root,
                "--literal-pathspecs",
                "clean",
                "-fd",
                "--",
                *chunk,
            )
        predecessor_paths = _tracked_paths(
            self.production_root,
            str(plan["production_source_sha"]),
        )
        predecessor_path_set = set(predecessor_paths)
        for relative in cleanup:
            if relative in predecessor_path_set:
                raise RestoreError("restore cleanup overlaps the predecessor tree")
            prefix = relative + "/"
            position = bisect_left(predecessor_paths, prefix)
            if (
                position < len(predecessor_paths)
                and predecessor_paths[position].startswith(prefix)
            ):
                continue
            self._unlink_target_only_leaf(self.production_root, relative)
        state = _worktree_state(
            self.production_root,
            predecessor_sha=str(plan["production_source_sha"]),
            predecessor_tree=str(plan["production_source_tree"]),
        )
        status_records = snapshot._run_git(
            self.production_root,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ).stdout.split("\0")
        if status_records and status_records[-1] == "":
            status_records.pop()
        staging_prefix = stage_one.name + "/"
        if (
            not state["git_readable"]
            or state["head"] != plan["production_source_sha"]
            or state["tree"] != plan["production_source_tree"]
            or any(
                not record.startswith("?? ")
                or not (
                    record[3:] == stage_one.name
                    or record[3:].startswith(staging_prefix)
                )
                for record in status_records
            )
        ):
            raise RestoreError("predecessor worktree materialization is incomplete")
        if snapshot.scan_git_directory(self.git_dir) != golden_manifest:
            raise RestoreError("golden Git changed during worktree materialization")
        return state

    def _ensure_archived(
        self,
        *,
        source: Path,
        destination: Path,
        expected_identity: Mapping[str, int],
        expected_manifest_sha256: str,
    ) -> None:
        source_exists = source.exists() or source.is_symlink()
        destination_exists = destination.exists() or destination.is_symlink()
        if source_exists and destination_exists:
            raise RestoreError("restore displacement exists in two locations")
        if source_exists:
            if (
                _directory_identity(source) != expected_identity
                or _manifest_digest(source)[1] != expected_manifest_sha256
            ):
                raise RestoreError("displaced Git changed before archive")
            try:
                os.rename(source, destination)
            except OSError as exc:
                raise RestoreError("displaced Git directory cannot be archived") from exc
            parent_fd = snapshot._open_directory(destination.parent)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            self.checkpoint("restore-displaced-archived")
        elif not destination_exists:
            raise RestoreError("displaced Git directory disappeared")
        if (
            _directory_identity(destination) != expected_identity
            or _manifest_digest(destination)[1] != expected_manifest_sha256
        ):
            raise RestoreError("displaced Git archive changed")

    def _verify_final(
        self,
        *,
        plan: Mapping[str, object],
        authority: Mapping[str, object],
        golden_manifest: Mapping[str, object],
        journal: Mapping[str, object],
    ) -> tuple[str, dict[str, object]]:
        final_manifest, final_digest = _manifest_digest(self.git_dir)
        final_fsck = snapshot.strict_fsck(self.git_dir)
        final_worktree = _worktree_state(
            self.production_root,
            predecessor_sha=str(plan["production_source_sha"]),
            predecessor_tree=str(plan["production_source_tree"]),
        )
        archive = Path(str(plan["archive_root"])) / "displaced-live.git"
        if (
            final_manifest != golden_manifest
            or final_digest != plan["golden_manifest_sha256"]
            or final_fsck != authority["fsck"]
            or not final_worktree["matches_predecessor"]
            or _directory_identity(archive) != plan["live_git_identity"]
            or _manifest_digest(archive)[1]
            != journal["displaced_final_manifest_sha256"]
        ):
            raise RestoreError("final production Git restore differs from golden")
        return final_digest, final_worktree

    def apply(
        self,
        *,
        restore_operation_id: str,
        abandoned_deploy_operation_id: str,
        terminal_decision: str,
        confirm_plan_sha256: str,
        confirm_snapshot_authority_sha256: str,
    ) -> dict[str, object]:
        restore_operation_id = _require_restore_operation(restore_operation_id)
        abandoned_deploy_operation_id = _require_deploy_operation(
            abandoned_deploy_operation_id
        )
        terminal_decision = _require_decision(terminal_decision)
        snapshot._require_digest(confirm_plan_sha256, "confirmed restore plan")
        snapshot._require_digest(
            confirm_snapshot_authority_sha256,
            "confirmed snapshot authority",
        )
        snapshot_lock = _open_lock(self.runtime_root / snapshot.LOCK_RELATIVE_PATH)
        try:
            deploy_lock = _open_lock(self.runtime_root / DEPLOY_LOCK_RELATIVE)
            try:
                journal_path = self._journal_path(restore_operation_id)
                if journal_path.exists() or journal_path.is_symlink():
                    raw, _digest = snapshot._load_private_json(journal_path)
                    journal = _validate_journal(raw)
                    plan = _validate_plan(journal["plan"])
                else:
                    plan, _live_manifest = self._build_plan(
                        restore_operation_id=restore_operation_id,
                        abandoned_deploy_operation_id=abandoned_deploy_operation_id,
                        terminal_decision=terminal_decision,
                    )
                    if (
                        snapshot.canonical_digest(plan) != confirm_plan_sha256
                        or plan["snapshot_authority_sha256"]
                        != confirm_snapshot_authority_sha256
                    ):
                        raise RestoreError(
                            "restore confirmations differ from live plan"
                        )
                    snapshot._ensure_private_directory(self.journal_root)
                    journal = {
                        "schema_version": 1,
                        "status": "in-progress",
                        "phase": "intent",
                        "plan": plan,
                        "plan_sha256": confirm_plan_sha256,
                        "created_at": _now_utc(),
                        "completed_at": None,
                        "stage_one_identity": None,
                        "exchange_count": 0,
                        "displaced_final_manifest_sha256": None,
                        "final_manifest_sha256": None,
                        "final_worktree": None,
                    }
                    self._write_journal(journal_path, journal)
                    self.checkpoint("restore-intent")
                if (
                    journal["plan_sha256"] != confirm_plan_sha256
                    or plan["snapshot_authority_sha256"]
                    != confirm_snapshot_authority_sha256
                    or plan["restore_operation_id"] != restore_operation_id
                    or plan["abandoned_deploy_operation_id"]
                    != abandoned_deploy_operation_id
                    or plan["terminal_decision"] != terminal_decision
                ):
                    raise RestoreError("restore replay confirmations differ")
                self._assert_no_current_state()
                self._assert_marker(plan)
                authority, authority_digest = snapshot.verify_completed_snapshot(
                    self.runtime_root,
                    production_root=self.production_root,
                    full=True,
                )
                self._assert_plan_bindings(plan, authority, authority_digest)
                if authority_digest != confirm_snapshot_authority_sha256:
                    raise RestoreError("golden snapshot authority changed")
                golden_git = Path(authority["backup_git_dir"])
                raw_manifest, golden_manifest_digest = snapshot._load_private_json(
                    Path(authority["manifest_path"])
                )
                golden_manifest = snapshot.validate_manifest(raw_manifest)
                if golden_manifest_digest != plan["golden_manifest_sha256"]:
                    raise RestoreError("golden restore manifest changed")
                if journal["phase"] == "completed":
                    final_digest, final_worktree = self._verify_final(
                        plan=plan,
                        authority=authority,
                        golden_manifest=golden_manifest,
                        journal=journal,
                    )
                    if (
                        final_digest != journal["final_manifest_sha256"]
                        or final_worktree != journal["final_worktree"]
                    ):
                        raise RestoreError("completed restore evidence changed")
                    return journal
                archive_root = Path(plan["archive_root"])
                stage_one = Path(plan["staging_one"])
                if journal["phase"] == "intent":
                    before_manifest = self._ensure_before_manifest(plan)
                    journal["phase"] = "before-manifest-sealed"
                    self._write_journal(journal_path, journal)
                else:
                    raw_before, before_digest = snapshot._load_private_json(
                        archive_root / "BEFORE-MANIFEST.json"
                    )
                    before_manifest = snapshot.validate_manifest(raw_before)
                    if before_digest != plan["live_manifest_sha256"]:
                        raise RestoreError("sealed pre-restore manifest changed")
                if journal["phase"] == "before-manifest-sealed":
                    stage_identity = self._ensure_golden_staging(
                        stage_one,
                        golden_git=golden_git,
                        golden_manifest=golden_manifest,
                        expected_identity=journal["stage_one_identity"],
                    )
                    journal["stage_one_identity"] = stage_identity
                    journal["phase"] = "first-staging-ready"
                    self._write_journal(journal_path, journal)
                stage_identity = _validate_identity(
                    journal["stage_one_identity"],
                    "restore staging identity",
                )
                if journal["phase"] == "first-staging-ready":
                    state = self._exchange_state(
                        plan=plan,
                        stage_one=stage_one,
                        stage_identity=stage_identity,
                        before_manifest=before_manifest,
                        golden_manifest=golden_manifest,
                    )
                    if state != "before":
                        raise RestoreError("restore exchange already occurred prematurely")
                    journal["phase"] = "first-exchange-intent"
                    self._write_journal(journal_path, journal)
                    self.checkpoint("restore-first-exchange-intent")
                if journal["phase"] == "first-exchange-intent":
                    exchange_state = self._exchange_state(
                        plan=plan,
                        stage_one=stage_one,
                        stage_identity=stage_identity,
                        before_manifest=before_manifest,
                        golden_manifest=golden_manifest,
                    )
                    if exchange_state == "before":
                        _rename_exchange(
                            self.production_root,
                            self.git_dir.name,
                            stage_one.name,
                        )
                        self.checkpoint("restore-first-exchange")
                        exchange_state = self._exchange_state(
                            plan=plan,
                            stage_one=stage_one,
                            stage_identity=stage_identity,
                            before_manifest=before_manifest,
                            golden_manifest=golden_manifest,
                        )
                    if exchange_state != "after":
                        raise RestoreError("first Git exchange did not commit")
                    journal["phase"] = "first-exchanged"
                    journal["exchange_count"] = 1
                    self._write_journal(journal_path, journal)
                if journal["phase"] == "first-exchanged" and plan[
                    "materialize_worktree"
                ]:
                    journal["phase"] = "materialize-intent"
                    self._write_journal(journal_path, journal)
                    self.checkpoint("restore-materialize-intent")
                if journal["phase"] == "materialize-intent":
                    self._materialize_predecessor(
                        plan=plan,
                        stage_one=stage_one,
                        stage_identity=stage_identity,
                        golden_manifest=golden_manifest,
                    )
                    self.checkpoint("restore-worktree-materialized")
                    journal["phase"] = "materialized"
                    self._write_journal(journal_path, journal)
                if journal["phase"] in {"first-exchanged", "materialized"}:
                    if journal["phase"] == "first-exchanged" and plan[
                        "materialize_worktree"
                    ]:
                        raise RestoreError("restore skipped required materialization")
                    if _directory_identity(stage_one) != plan["live_git_identity"]:
                        raise RestoreError("displaced Git identity changed")
                    _displaced_manifest, displaced_digest = _manifest_digest(stage_one)
                    journal["displaced_final_manifest_sha256"] = displaced_digest
                    journal["phase"] = "archive-intent"
                    self._write_journal(journal_path, journal)
                    self.checkpoint("restore-archive-intent")
                if journal["phase"] == "archive-intent":
                    self._ensure_archived(
                        source=stage_one,
                        destination=archive_root / "displaced-live.git",
                        expected_identity=plan["live_git_identity"],
                        expected_manifest_sha256=journal[
                            "displaced_final_manifest_sha256"
                        ],
                    )
                    journal["phase"] = "archived"
                    self._write_journal(journal_path, journal)
                if journal["phase"] != "archived":
                    raise RestoreError("restore transaction did not archive displaced Git")
                final_digest, final_worktree = self._verify_final(
                    plan=plan,
                    authority=authority,
                    golden_manifest=golden_manifest,
                    journal=journal,
                )
                journal.update(
                    {
                        "status": "completed",
                        "phase": "completed",
                        "completed_at": _now_utc(),
                        "final_manifest_sha256": final_digest,
                        "final_worktree": final_worktree,
                    }
                )
                self._write_journal(journal_path, journal)
                self.checkpoint("restore-completed")
                return journal
            finally:
                os.close(deploy_lock)
        finally:
            os.close(snapshot_lock)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "apply"):
        child = subparsers.add_parser(command)
        child.add_argument("--restore-operation-id", required=True)
        child.add_argument("--abandoned-deploy-operation-id", required=True)
        child.add_argument(
            "--terminal-decision",
            choices=sorted(DECISIONS),
            required=True,
        )
        if command == "apply":
            child.add_argument("--confirm-plan-sha256", required=True)
            child.add_argument(
                "--confirm-snapshot-authority-sha256",
                required=True,
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    manager = ProductionGitRestoreManager(PRODUCTION_ROOT, RUNTIME_ROOT)
    try:
        if arguments.command == "plan":
            result = manager.plan(
                restore_operation_id=arguments.restore_operation_id,
                abandoned_deploy_operation_id=arguments.abandoned_deploy_operation_id,
                terminal_decision=arguments.terminal_decision,
            )
        else:
            result = manager.apply(
                restore_operation_id=arguments.restore_operation_id,
                abandoned_deploy_operation_id=arguments.abandoned_deploy_operation_id,
                terminal_decision=arguments.terminal_decision,
                confirm_plan_sha256=arguments.confirm_plan_sha256,
                confirm_snapshot_authority_sha256=(
                    arguments.confirm_snapshot_authority_sha256
                ),
            )
        sys.stdout.buffer.write(snapshot.canonical_json_bytes(result))
        return 0
    except (RestoreError, snapshot.SnapshotError) as exc:
        print(f"production Git restore failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
