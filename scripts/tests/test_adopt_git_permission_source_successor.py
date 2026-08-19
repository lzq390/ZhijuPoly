from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


SOURCE_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = SOURCE_ROOT / "scripts/adopt_git_permission_source_successor.py"
SPEC = importlib.util.spec_from_file_location("source_successor_tested", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
SUCCESSOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUCCESSOR)


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", *arguments],
        cwd=root,
        env={
            "HOME": "/nonexistent",
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _write_private(path: Path, payload: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)


def _write_json(path: Path, value: object) -> str:
    payload = _canonical(value) + b"\n"
    _write_private(path, payload)
    return _digest(payload)


def _make_private(root: Path) -> None:
    previous_inventory: set[str] | None = None
    for _attempt in range(8):
        inventory: set[str] = set()
        raced = False
        walk_errors: list[OSError] = []
        for directory, directories, files in os.walk(
            root,
            onerror=walk_errors.append,
        ):
            current_directory = Path(directory)
            try:
                current_directory.chmod(0o700)
            except FileNotFoundError:
                raced = True
                continue
            inventory.add(current_directory.relative_to(root).as_posix())
            for name in directories:
                path = current_directory / name
                try:
                    metadata = path.lstat()
                    inventory.add(path.relative_to(root).as_posix())
                    if not stat.S_ISLNK(metadata.st_mode):
                        path.chmod(0o700)
                except FileNotFoundError:
                    raced = True
            for name in files:
                path = current_directory / name
                try:
                    metadata = path.lstat()
                    inventory.add(path.relative_to(root).as_posix())
                    if not stat.S_ISLNK(metadata.st_mode):
                        path.chmod(
                            stat.S_IMODE(metadata.st_mode) & ~0o077
                        )
                except FileNotFoundError:
                    raced = True
        root.chmod(0o700)
        if walk_errors:
            raced = True
        if not raced and inventory == previous_inventory:
            return
        previous_inventory = None if raced else inventory
    raise AssertionError("fixture tree did not stabilize while sealing modes")


OLD_BOOTSTRAP = b'''#!/usr/bin/python3 -I
from pathlib import Path
import subprocess

def _git(root, *args):
    return subprocess.run(["/usr/bin/git", *args], cwd=root, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True).stdout.strip()

def bootstrap_source_readiness(root: Path, *, expected_sha=None):
    sha = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    if expected_sha is not None and sha != expected_sha:
        raise RuntimeError("wrong source")
    return {
        "schema_version": 2, "ready": True, "source_root": str(root.absolute()),
        "source_sha": sha, "source_tree": tree, "branch": "main",
        "origin": "git@github.com:lzq390/ZhijuPoly.git",
        "remote_names": ["origin"],
        "origin_fetch_urls": ["git@github.com:lzq390/ZhijuPoly.git"],
        "origin_push_urls": ["git@github.com:lzq390/ZhijuPoly.git"],
        "origin_main_sha": sha, "standalone_object_database": True,
        "shallow": False, "dirty_entries": 0, "ignored_entries": 0,
        "unreachable_objects": 0, "replace_refs": 0,
        "special_index_entries": 0, "sparse_index": False,
        "owner_private": True, "group_or_world_writable": False,
    }

def _delivery_gate(*args, **kwargs):
    raise RuntimeError("fixture injects the delivery probe")
'''

OLD_TRUST = b'''#!/usr/bin/python3 -I
import hashlib
import json
import os
from types import SimpleNamespace

def digest(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()

def evidence(root, source_sha, source_tree):
    loose = []
    for directory, directory_names, file_names in os.walk(root / ".git/refs"):
        del directory_names
        for name in sorted(file_names):
            path = __import__("pathlib").Path(directory) / name
            payload = path.read_bytes()
            loose.append({
                "path": path.relative_to(root / ".git").as_posix(),
                "mode": "0600",
                "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            })
    head_payload = (root / ".git/HEAD").read_bytes()
    body = {
        "schema_version": 3,
        "policy": "fixture-frozen-trust",
        "repository_root": str(root),
        "git_dir": str(root / ".git"),
        "object_dir": str(root / ".git/objects"),
        "index_path": str(root / ".git/index"),
        "source": {"sha": source_sha, "tree": source_tree,
                   "branch": "refs/heads/main", "origin": None},
        "git_binary": {"fixture": True},
        "local_config": {"fixture": True},
        "head": {
            "size": len(head_payload),
            "raw_sha256": (
                "sha256:" + hashlib.sha256(head_payload).hexdigest()
            ),
        },
        "index": {"fixture": True},
        "refs": {"loose_count": 2, "loose_sha256": digest(loose),
                 "packed_refs_sha256": None, "replace_refs": 0},
        "objects": {"fixture": True, "standalone": True,
                    "promisor": False, "alternates": False},
        "forbidden_markers_absent": [],
        "execution_environment": {"fixture": True},
    }
    body["trust_surface_sha256"] = digest(
        {key: value for key, value in body.items() if key != "source"}
    )
    body["evidence_sha256"] = digest(body)
    return body

def verify_repository_permission_takeover(root, marker_path, **kwargs):
    del root, kwargs
    return json.loads(marker_path.read_text())

def repository_preflight_evidence(root, **kwargs):
    del kwargs
    return evidence(root, "0" * 40, "0" * 40)

def run_git(root, *args, **kwargs):
    del kwargs
    if args and args[0] == "for-each-ref":
        deploy = (root / ".git/refs/remotes/nexpoly-deploy/main").read_text().strip()
        return SimpleNamespace(
            stdout=(
                "refs/heads/main\\0" + "1" * 40 + "\\0commit\\0\\n"
                + "refs/remotes/nexpoly-deploy/main\\0" + deploy
                + "\\0commit\\0\\n"
            )
        )
    return SimpleNamespace(stdout="")

def repository_trust_evidence(root, **kwargs):
    return evidence(root, kwargs["source_sha"], kwargs["source_tree"])

def require_stable_trust_surface(before, after):
    if before["trust_surface_sha256"] != after["trust_surface_sha256"]:
        raise RuntimeError("surface changed")
'''

CANDIDATE_BOOTSTRAP = b'''#!/usr/bin/python3 -I
raise RuntimeError("candidate bootstrap executed before authority")
'''
CANDIDATE_TRUST = b'''#!/usr/bin/python3 -I
raise RuntimeError("candidate trust executed before authority")
'''
BRIDGE = b'''#!/usr/bin/python3 -I
raise RuntimeError("bridge contract was executed")
'''


class Fixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.source = self.root / "source"
        self.production = self.root / "production"
        self.runtime = self.root / "runtime"
        self.source.mkdir(mode=0o700)
        self.production.mkdir(mode=0o700)
        self.runtime.mkdir(mode=0o700)
        (self.runtime / "state").mkdir(mode=0o700)
        _write_private(self.runtime / "state/deploy.lock", b"")
        self.operation_id = "adopt-git-successor-test-0001"
        self.required_jobs = ["contracts", "tests"]
        self.delivery_calls = 0
        self._create_repository()
        self._create_runtime_authorities()

    def close(self) -> None:
        self.temporary.cleanup()

    def _create_repository(self) -> None:
        _git(self.source, "init", "-b", "main")
        _git(self.source, "config", "user.name", "Fixture")
        _git(self.source, "config", "user.email", "fixture@example.invalid")
        _git(
            self.source,
            "remote",
            "add",
            "origin",
            SUCCESSOR.REPOSITORY_SSH_URL,
        )
        for relative in SUCCESSOR.TRACKED_SOURCE_FILES:
            path = self.source / relative
            if relative == SUCCESSOR.BOOTSTRAP_PATH:
                payload = OLD_BOOTSTRAP
            elif relative == SUCCESSOR.GIT_TRUST_PATH:
                payload = OLD_TRUST
            elif relative == SUCCESSOR.CI_CONTRACT_PATH:
                payload = BRIDGE
            else:
                payload = b"#!/bin/sh\nexit 0\n"
            mode = (
                0o644
                if relative
                == "ops/config/mutable-data-audit.pg_service.conf.example"
                else 0o755
            )
            _write_private(path, payload, mode)
        _git(self.source, "add", ".")
        _git(self.source, "commit", "-m", "predecessor")
        self.predecessor_sha = _git(self.source, "rev-parse", "HEAD")
        self.predecessor_tree = _git(self.source, "rev-parse", "HEAD^{tree}")
        _write_private(
            self.source / SUCCESSOR.BOOTSTRAP_PATH,
            CANDIDATE_BOOTSTRAP,
            0o755,
        )
        _write_private(
            self.source / SUCCESSOR.GIT_TRUST_PATH,
            CANDIDATE_TRUST,
            0o755,
        )
        _git(
            self.source,
            "add",
            SUCCESSOR.BOOTSTRAP_PATH,
            SUCCESSOR.GIT_TRUST_PATH,
        )
        _git(self.source, "commit", "-m", "reviewed successor")
        self.target_sha = _git(self.source, "rev-parse", "HEAD")
        self.target_tree = _git(self.source, "rev-parse", "HEAD^{tree}")
        _git(
            self.source,
            "update-ref",
            "refs/remotes/origin/main",
            self.target_sha,
        )
        (self.production / ".git/objects").mkdir(
            mode=0o700, parents=True
        )
        _write_private(
            self.production / ".git/refs/heads/main",
            ("1" * 40 + "\n").encode(),
        )
        _write_private(
            self.production / ".git/refs/remotes/nexpoly-deploy/main",
            (self.predecessor_sha + "\n").encode(),
        )
        _write_private(
            self.production / ".git/HEAD",
            b"ref: refs/heads/main\n",
        )
        _write_private(self.production / ".git/config", b"[core]\n")
        _write_private(self.production / ".git/index", b"fixture-index")
        namespace: dict[str, object] = {}
        exec(OLD_TRUST, namespace)
        trust_evidence = namespace["evidence"](
            self.production, "1" * 40, "2" * 40
        )
        self.production_trust_sha256 = trust_evidence["evidence_sha256"]
        self.expected_transitions: dict[str, dict[str, str]] = {}
        for relative in SUCCESSOR.CHANGED_PATHS:
            old_line = _git(
                self.source, "ls-tree", self.predecessor_sha, "--", relative
            ).split()
            new_line = _git(
                self.source, "ls-tree", self.target_sha, "--", relative
            ).split()
            old_payload = subprocess.run(
                ["/usr/bin/git", "show", f"{self.predecessor_sha}:{relative}"],
                cwd=self.source,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            new_payload = (self.source / relative).read_bytes()
            self.expected_transitions[relative] = {
                "predecessor_blob_sha": old_line[2],
                "predecessor_sha256": _digest(old_payload),
                "target_blob_sha": new_line[2],
                "target_sha256": _digest(new_payload),
            }
        _make_private(self.source)
        _make_private(self.production)

    def _create_runtime_authorities(self) -> None:
        d = lambda character: "sha256:" + character * 64
        self.production_sha = "1" * 40
        self.production_tree = "2" * 40
        adopted = {
            "schema_version": 1,
            "status": "adopted",
            "authority_kind": SUCCESSOR.ADOPTION_AUTHORITY_KIND,
            "source_sha": self.production_sha,
            "source_tree": self.production_tree,
        }
        adopted_digest = _write_json(
            self.runtime / SUCCESSOR.ADOPTED_DEPLOYMENT_RELATIVE_PATH,
            adopted,
        )
        bootstrap = {
            "schema_version": 3,
            "status": "completed",
            "authority_kind": SUCCESSOR.ADOPTION_AUTHORITY_KIND,
            "adopted_deployment": adopted,
            "adopted_deployment_sha256": SUCCESSOR._canonical_digest(adopted),
        }
        bootstrap_digest = _write_json(
            self.runtime / SUCCESSOR.BOOTSTRAP_CONTROL_RELATIVE_PATH,
            bootstrap,
        )
        prerequisite_plan = {
            "schema_version": 1,
            "adopted_deployment_sha256": adopted_digest,
        }
        prerequisites = {
            "schema_version": 1,
            "status": "completed",
            "authority_kind": SUCCESSOR.PREREQUISITE_AUTHORITY_KIND,
            "operation_id": "adopt-prerequisite-fixture-0001",
            "source_sha": self.predecessor_sha,
            "source_tree": self.predecessor_tree,
            "adopted_deployment_sha256": adopted_digest,
            "plan_sha256": SUCCESSOR._canonical_digest(prerequisite_plan),
            "plan": prerequisite_plan,
            "completed_at": "2026-08-18T00:00:00Z",
        }
        prerequisites_digest = _write_json(
            self.runtime / SUCCESSOR.ADOPTED_PREREQUISITES_RELATIVE_PATH,
            prerequisites,
        )
        marker = {
            "evidence_sha256": d("3"),
            "inventory_sha256": d("4"),
            "original_permissions_sha256": d("5"),
            "hardened_permissions_sha256": d("6"),
        }
        marker_digest = _write_json(
            self.runtime / SUCCESSOR.PERMISSION_MARKER_RELATIVE_PATH,
            marker,
        )
        old_delivery = self.delivery(self.predecessor_sha)
        old_readiness = {
            "schema_version": 2,
            "ready": True,
            "source_root": str(self.source),
            "source_sha": self.predecessor_sha,
            "source_tree": self.predecessor_tree,
            "branch": "main",
            "origin": SUCCESSOR.REPOSITORY_SSH_URL,
            "remote_names": ["origin"],
            "origin_fetch_urls": [SUCCESSOR.REPOSITORY_SSH_URL],
            "origin_push_urls": [SUCCESSOR.REPOSITORY_SSH_URL],
            "origin_main_sha": self.predecessor_sha,
            "standalone_object_database": True,
            "shallow": False,
            "dirty_entries": 0,
            "ignored_entries": 0,
            "unreachable_objects": 0,
            "replace_refs": 0,
            "special_index_entries": 0,
            "sparse_index": False,
            "owner_private": True,
            "group_or_world_writable": False,
        }
        old_plan = {
            "schema_version": 1,
            "authority_kind": SUCCESSOR.PREDECESSOR_AUTHORITY_KIND,
            "operation_id": "adopt-git-permission-fixture-0001",
            "source_sha": self.predecessor_sha,
            "source_tree": self.predecessor_tree,
            "source_readiness": old_readiness,
            "source_readiness_sha256": SUCCESSOR._canonical_digest(
                old_readiness
            ),
            "delivery_gate": old_delivery,
            "delivery_gate_sha256": SUCCESSOR._canonical_digest(old_delivery),
            "adopted_deployment_sha256": adopted_digest,
            "bootstrap_control_sha256": bootstrap_digest,
            "adopted_prerequisites_sha256": prerequisites_digest,
            "adopted_prerequisites_plan_sha256": SUCCESSOR._canonical_digest(
                prerequisite_plan
            ),
            "production_source": {
                "source_sha": self.production_sha,
                "source_tree": self.production_tree,
            },
            "permission_takeover": marker,
            "permission_impact_sha256": d("7"),
            "mutations": {
                "services": False,
                "source_content": False,
                "source_refs": False,
                "database": False,
                "credentials": False,
                "git_permissions": True,
                "runtime_authority": True,
            },
        }
        authority = {
            "schema_version": 1,
            "status": "completed",
            "authority_kind": SUCCESSOR.PREDECESSOR_AUTHORITY_KIND,
            "operation_id": old_plan["operation_id"],
            "source_sha": self.predecessor_sha,
            "source_tree": self.predecessor_tree,
            "production_source_sha": self.production_sha,
            "production_source_tree": self.production_tree,
            "adopted_deployment_sha256": adopted_digest,
            "bootstrap_control_sha256": bootstrap_digest,
            "adopted_prerequisites_sha256": prerequisites_digest,
            "plan_sha256": SUCCESSOR._canonical_digest(old_plan),
            "permission_impact_sha256": old_plan["permission_impact_sha256"],
            "permission_marker_sha256": marker_digest,
            "permission_evidence_sha256": marker["evidence_sha256"],
            "permission_inventory_sha256": marker["inventory_sha256"],
            "original_permissions_sha256": marker[
                "original_permissions_sha256"
            ],
            "hardened_permissions_sha256": marker[
                "hardened_permissions_sha256"
            ],
            "plan": old_plan,
            "completed_at": "2026-08-18T00:00:00Z",
        }
        authority_digest = _write_json(
            self.runtime / SUCCESSOR.PREDECESSOR_AUTHORITY_RELATIVE_PATH,
            authority,
        )
        journal = {
            "schema_version": 1,
            "status": "completed",
            "phase": "completed",
            "operation_id": authority["operation_id"],
            "plan": old_plan,
            "plan_sha256": authority["plan_sha256"],
            "permission_checkpoint": "permission:hardened",
            "permission_evidence_sha256": authority[
                "permission_evidence_sha256"
            ],
            "permission_impact_sha256": authority[
                "permission_impact_sha256"
            ],
            "permission_marker_sha256": marker_digest,
            "source_trust_sha256": "sha256:" + "a" * 64,
            "created_at": "2026-08-18T00:00:00Z",
            "completed_at": authority["completed_at"],
            "aborted_at": None,
        }
        journal_digest = _write_json(
            self.runtime
            / SUCCESSOR.PREDECESSOR_TRANSACTION_RELATIVE_DIRECTORY
            / f"{authority['operation_id']}.json",
            journal,
        )
        self.expected_provenance = {
            "predecessor_source_sha": self.predecessor_sha,
            "predecessor_source_tree": self.predecessor_tree,
            "predecessor_authority_sha256": authority_digest,
            "predecessor_marker_sha256": marker_digest,
            "predecessor_journal_sha256": journal_digest,
            "adopted_deployment_sha256": adopted_digest,
            "bootstrap_control_sha256": bootstrap_digest,
            "adopted_prerequisites_sha256": prerequisites_digest,
            "production_source_sha": self.production_sha,
            "production_source_tree": self.production_tree,
            "predecessor_source_trust_sha256": "sha256:" + "a" * 64,
            "production_source_trust_sha256": self.production_trust_sha256,
        }
        snapshot_operation = "snapshot-git-source-successor-0001"
        snapshot_root = (
            self.runtime / "backups/production-git" / snapshot_operation
        )
        (snapshot_root / "git").mkdir(parents=True, mode=0o700)
        manifest = {
            "schema_version": 1,
            "policy": "nexpoly-production-git-raw-manifest-v1",
            "root_mode": "0700",
            "records": [],
            "records_sha256": _digest(_canonical([]) + b"\n"),
            "file_count": 0,
            "directory_count": 0,
            "total_file_bytes": 0,
        }
        manifest_path = snapshot_root / "MANIFEST.json"
        manifest_digest = _write_json(manifest_path, manifest)
        snapshot_delivery = self.delivery(self.target_sha)
        snapshot_authority = {
            "schema_version": 1,
            "status": "completed",
            "authority_kind": (
                "manual-runtime-adoption-production-git-snapshot"
            ),
            "policy": "nexpoly-production-git-golden-snapshot-v1",
            "operation_id": snapshot_operation,
            "target_source_sha": self.target_sha,
            "target_source_tree": self.target_tree,
            "production_source_sha": self.production_sha,
            "production_source_tree": self.production_tree,
            "production_git_dir": str(self.production / ".git"),
            "backup_git_dir": str(snapshot_root / "git"),
            "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_digest,
            "manifest_summary": {
                "records_sha256": manifest["records_sha256"],
                "file_count": 0,
                "directory_count": 0,
                "total_file_bytes": 0,
            },
            "fsck": {
                "schema_version": 1,
                "policy": "git-fsck-strict-full-no-reflogs-v1",
                "exit_code": 0,
                "stdout_sha256": d("b"),
                "stderr_sha256": d("c"),
                "stdout_lines": 0,
                "stderr_lines": 0,
            },
            "delivery_gate": snapshot_delivery,
            "delivery_gate_sha256": _digest(
                _canonical(snapshot_delivery) + b"\n"
            ),
            "plan_sha256": d("d"),
            "snapshot_impact_sha256": d("e"),
            "copy_policy": (
                "descriptor-relative-read-write-no-link-no-reflink-v1"
            ),
            "completed_at": "2026-08-18T00:00:00.000000Z",
        }
        self.snapshot_authority_sha256 = _write_json(
            self.runtime / SUCCESSOR.PRODUCTION_GIT_SNAPSHOT_RELATIVE_PATH,
            snapshot_authority,
        )
        _make_private(self.runtime)

    def delivery(self, sha: str) -> dict[str, object]:
        return {
            "remote_main": sha,
            "ci": {
                "workflow_run_id": 501,
                "run_attempt": 2,
                "head_sha": sha,
                "head_branch": "main",
                "event": "push",
                "path": ".github/workflows/ci.yml",
                "conclusion": "success",
                "required_jobs": list(self.required_jobs),
            },
        }

    def probe(
        self,
        bootstrap: object,
        production: Path,
        runtime: Path,
        sha: str,
        jobs: list[str],
        sealed: object | None,
    ) -> object:
        del bootstrap, production, runtime
        self.delivery_calls += 1
        if jobs != self.required_jobs:
            raise RuntimeError("job contract drift")
        evidence = self.delivery(sha)
        if sealed is not None and sealed != evidence:
            raise RuntimeError("sealed gate drift")
        return evidence

    def publisher(self, checkpoint=None):  # type: ignore[no-untyped-def]
        return SUCCESSOR.SourceSuccessorPublisher(
            source_root=self.source,
            production_root=self.production,
            runtime_root=self.runtime,
            checkpoint=checkpoint,
            expected_transitions=self.expected_transitions,
            expected_predecessor_provenance=self.expected_provenance,
            delivery_gate_probe=self.probe,
        )

    def plan(self) -> dict[str, object]:
        return self.publisher().plan(
            source_sha=self.target_sha,
            operation_id=self.operation_id,
        )

    def apply(self, plan: dict[str, object], checkpoint=None):  # type: ignore[no-untyped-def]
        return self.publisher(checkpoint).apply(
            source_sha=self.target_sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=str(plan["plan_sha256"]),
            confirm_source_successor_impact_sha256=str(
                plan["source_successor_impact_sha256"]
            ),
        )


class SourceSuccessorPublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.close()

    @staticmethod
    def snapshot(root: Path) -> dict[str, tuple[object, ...]]:
        result: dict[str, tuple[object, ...]] = {}
        for path in sorted([root, *root.rglob("*")]):
            metadata = path.lstat()
            relative = str(path.relative_to(root)) or "."
            payload = path.read_bytes() if path.is_file() else b""
            result[relative] = (
                stat.S_IFMT(metadata.st_mode),
                stat.S_IMODE(metadata.st_mode),
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                hashlib.sha256(payload).hexdigest(),
            )
        return result

    def test_plan_is_zero_write_and_candidates_are_never_executed(self) -> None:
        before = self.snapshot(self.fixture.root)
        result = self.fixture.plan()
        after = self.snapshot(self.fixture.root)
        self.assertEqual(before, after)
        plan = result["plan"]
        self.assertEqual(plan["changed_paths"], list(SUCCESSOR.CHANGED_PATHS))
        self.assertEqual(
            plan["verifier_agreement"]["candidate_execution"],
            "forbidden-before-authority",
        )
        self.assertEqual(len(plan["files"]), 13)
        self.assertFalse(self.fixture.publisher().authority_path.exists())
        self.assertFalse(self.fixture.publisher().transaction_root.exists())

    def test_auxiliary_inventory_rejects_reflog_directory_symlink(self) -> None:
        external = self.fixture.root / "external-reflog"
        external.mkdir(mode=0o700)
        sentinel = external / "main"
        _write_private(sentinel, b"external-reflog-sentinel\n")
        parent = self.fixture.production / ".git/logs/refs/remotes"
        parent.mkdir(parents=True, mode=0o700)
        for path in (parent, parent.parent, parent.parent.parent):
            path.chmod(0o700)
        (parent / "nexpoly-deploy").symlink_to(
            external,
            target_is_directory=True,
        )

        before = sentinel.read_bytes()
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "private directory is unavailable",
        ):
            SUCCESSOR._git_auxiliary_inventory(self.fixture.production)
        self.assertEqual(sentinel.read_bytes(), before)

    def test_raw_ref_inventory_rejects_refs_root_symlink(self) -> None:
        refs = self.fixture.production / ".git/refs"
        refs.rename(self.fixture.root / "held-refs")
        external = self.fixture.root / "external-refs"
        external.mkdir(mode=0o700)
        sentinel = external / "main"
        _write_private(sentinel, b"external-ref-sentinel\n")
        refs.symlink_to(external, target_is_directory=True)

        before = sentinel.read_bytes()
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "private directory is unavailable",
        ):
            SUCCESSOR._raw_ref_inventory(self.fixture.production)
        self.assertEqual(sentinel.read_bytes(), before)

    def test_raw_ref_inventory_agrees_with_frozen_trust_order(self) -> None:
        refs = self.fixture.production / ".git/refs"
        head_payload = (refs / "heads/main").read_bytes()
        remote_payload = (
            refs / "remotes/nexpoly-deploy/main"
        ).read_bytes()
        shutil.rmtree(refs / "heads")
        shutil.rmtree(refs / "remotes")
        _write_private(
            refs / "remotes/nexpoly-deploy/main",
            remote_payload,
        )
        _write_private(refs / "heads/main", head_payload)
        _make_private(refs)
        root_directory_names = next(os.walk(refs))[1]
        self.assertEqual(set(root_directory_names), {"heads", "remotes"})

        _records, trust_order = SUCCESSOR._raw_ref_inventory(
            self.fixture.production
        )
        namespace: dict[str, object] = {}
        exec(OLD_TRUST, namespace)
        frozen = namespace["evidence"](
            self.fixture.production,
            self.fixture.production_sha,
            self.fixture.production_tree,
        )
        self.assertEqual(
            SUCCESSOR._canonical_digest(trust_order),
            frozen["refs"]["loose_sha256"],
        )

    def test_object_inventory_rejects_objects_root_symlink(self) -> None:
        objects = self.fixture.production / ".git/objects"
        objects.rename(self.fixture.root / "held-objects")
        external = self.fixture.root / "external-objects"
        external.mkdir(mode=0o700)
        sentinel = external / "sentinel"
        _write_private(sentinel, b"external-object-sentinel\n")
        objects.symlink_to(external, target_is_directory=True)

        before = sentinel.read_bytes()
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "Git metadata is not owner-private",
        ):
            SUCCESSOR._git_object_storage_inventory(
                self.fixture.production
            )
        self.assertEqual(sentinel.read_bytes(), before)

    def test_transition_validator_requires_direct_commit_baseline_refs(
        self,
    ) -> None:
        transition = self.fixture.plan()["plan"][
            "production_repository_transition"
        ]
        cases = (
            ("refs/heads/main", "object_type", "tree"),
            (
                "refs/heads/main",
                "symbolic_target",
                "refs/heads/indirect-main",
            ),
            (SUCCESSOR.DEPLOY_REMOTE_REF, "object_type", "tag"),
            (
                SUCCESSOR.DEPLOY_REMOTE_REF,
                "symbolic_target",
                "refs/remotes/nexpoly-deploy/indirect-main",
            ),
        )
        for ref_name, field, replacement in cases:
            with self.subTest(ref=ref_name, field=field):
                changed = json.loads(json.dumps(transition))
                record = next(
                    record
                    for record in changed["logical_refs"]
                    if record["name"] == ref_name
                )
                record[field] = replacement
                changed["logical_refs_sha256"] = (
                    SUCCESSOR._canonical_digest(changed["logical_refs"])
                )
                with self.assertRaisesRegex(
                    SUCCESSOR.SuccessorError,
                    "production logical ref baseline is invalid",
                ):
                    SUCCESSOR._validate_repository_transition(
                        changed,
                        production_root=self.fixture.production,
                        production_sha=self.fixture.production_sha,
                        production_tree=self.fixture.production_tree,
                        target_sha=self.fixture.target_sha,
                        target_tree=self.fixture.target_tree,
                    )

    def test_apply_publishes_canonical_create_once_authority(self) -> None:
        plan = self.fixture.plan()
        authority = self.fixture.apply(plan)
        path = self.fixture.publisher().authority_path
        journal_path = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        journal = json.loads(journal_path.read_bytes())
        self.assertEqual(path.read_bytes(), _canonical(authority) + b"\n")
        self.assertEqual(journal_path.read_bytes(), _canonical(journal) + b"\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual(
            authority["production_source_trust_sha256"],
            self.fixture.production_trust_sha256,
        )
        self.assertEqual(
            authority["plan"]["predecessor"]["source_trust_sha256"],
            "sha256:" + "a" * 64,
        )
        self.assertEqual(
            authority["plan"]["production_source_trust_sha256"],
            authority["production_source_trust_sha256"],
        )
        self.assertEqual(
            authority["plan"]["source_successor_impact"][
                "production_source_trust_sha256"
            ],
            authority["production_source_trust_sha256"],
        )
        self.assertEqual(
            journal["production_source_trust_sha256"],
            authority["production_source_trust_sha256"],
        )
        calls = self.fixture.delivery_calls
        self.assertEqual(self.fixture.apply(plan), authority)
        self.assertEqual(self.fixture.delivery_calls, calls)
        names = [
            name
            for name in os.listdir(self.fixture.runtime / "state")
            if name.startswith(".adopted-git-permission-source-successor")
        ]
        self.assertEqual(names, [])

    def test_historical_and_current_trust_provenance_are_distinct(self) -> None:
        historical = SUCCESSOR.EXPECTED_PREDECESSOR_PROVENANCE[
            "predecessor_source_trust_sha256"
        ]
        current = SUCCESSOR.EXPECTED_PREDECESSOR_PROVENANCE[
            "production_source_trust_sha256"
        ]
        self.assertEqual(
            historical,
            "sha256:dd8c493199fd02daf621e7ffbcd51ca35ebf7da0e6f77fefd0759137c7a408d4",
        )
        self.assertEqual(
            current,
            "sha256:ba12709eb87ebc3ca51ac6ebcaca425be50487420c3529b80ec8696cb8602a3b",
        )
        self.assertNotEqual(historical, current)

    def test_confirmation_mismatch_is_zero_write(self) -> None:
        plan = self.fixture.plan()
        before = self.snapshot(self.fixture.root)
        with self.assertRaisesRegex(SUCCESSOR.SuccessorError, "confirmations differ"):
            self.fixture.publisher().apply(
                source_sha=self.fixture.target_sha,
                operation_id=self.fixture.operation_id,
                confirm_plan_sha256="sha256:" + "0" * 64,
                confirm_source_successor_impact_sha256=str(
                    plan["source_successor_impact_sha256"]
                ),
            )
        self.assertEqual(before, self.snapshot(self.fixture.root))

    def test_recovery_does_not_query_moving_delivery_gate(self) -> None:
        plan = self.fixture.plan()

        def crash(label: str) -> None:
            if label == "source-successor-intent":
                raise RuntimeError("power loss")

        with self.assertRaisesRegex(RuntimeError, "power loss"):
            self.fixture.apply(plan, crash)
        calls = self.fixture.delivery_calls

        def forbidden_probe(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("moving remote was queried during recovery")

        publisher = self.fixture.publisher()
        publisher.delivery_gate_probe = forbidden_probe
        authority = publisher.apply(
            source_sha=self.fixture.target_sha,
            operation_id=self.fixture.operation_id,
            confirm_plan_sha256=str(plan["plan_sha256"]),
            confirm_source_successor_impact_sha256=str(
                plan["source_successor_impact_sha256"]
            ),
        )
        self.assertEqual(authority["status"], "completed")
        self.assertEqual(self.fixture.delivery_calls, calls)

    def test_initial_journal_staged_recovery_is_durable_and_offline(
        self,
    ) -> None:
        plan = self.fixture.plan()

        def crash(label: str) -> None:
            if label == "source-successor-initial-journal-staged":
                raise RuntimeError("power loss after durable initial journal")

        with self.assertRaisesRegex(
            RuntimeError,
            "power loss after durable initial journal",
        ):
            self.fixture.apply(plan, crash)
        journal_path = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        journal = json.loads(journal_path.read_bytes())
        self.assertEqual(journal_path.read_bytes(), _canonical(journal) + b"\n")
        self.assertEqual(stat.S_IMODE(journal_path.stat().st_mode), 0o600)
        self.assertEqual(journal_path.stat().st_nlink, 1)
        self.assertEqual(journal["status"], "applying")
        self.assertEqual(journal["phase"], "intent")
        self.assertEqual(journal["plan_sha256"], plan["plan_sha256"])
        delivery_calls = self.fixture.delivery_calls

        def forbidden_probe(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("delivery network was queried during recovery")

        publisher = self.fixture.publisher()
        publisher.delivery_gate_probe = forbidden_probe
        authority = publisher.apply(
            source_sha=self.fixture.target_sha,
            operation_id=self.fixture.operation_id,
            confirm_plan_sha256=str(plan["plan_sha256"]),
            confirm_source_successor_impact_sha256=str(
                plan["source_successor_impact_sha256"]
            ),
        )
        self.assertEqual(authority["status"], "completed")
        self.assertEqual(self.fixture.delivery_calls, delivery_calls)

    def test_abort_before_commit_and_commit_intent_is_forward_only(self) -> None:
        plan = self.fixture.plan()

        def crash(label: str) -> None:
            if label == "source-successor-predecessor-verified":
                raise RuntimeError("power loss")

        with self.assertRaises(RuntimeError):
            self.fixture.apply(plan, crash)
        aborted = self.fixture.publisher().abort(
            source_sha=self.fixture.target_sha,
            operation_id=self.fixture.operation_id,
            confirm_plan_sha256=str(plan["plan_sha256"]),
            confirm_source_successor_impact_sha256=str(
                plan["source_successor_impact_sha256"]
            ),
        )
        self.assertEqual(aborted["status"], "aborted")
        self.assertFalse(self.fixture.publisher().authority_path.exists())
        self.assertFalse(self.fixture.publisher().transaction_root.exists())

        fixture = Fixture()
        try:
            commit_plan = fixture.plan()

            def crash_at_commit(label: str) -> None:
                if label == "source-successor-authority-commit-intent":
                    raise RuntimeError("power loss")

            with self.assertRaises(RuntimeError):
                fixture.apply(commit_plan, crash_at_commit)
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError, "forward-only"
            ):
                fixture.publisher().abort(
                    source_sha=fixture.target_sha,
                    operation_id=fixture.operation_id,
                    confirm_plan_sha256=str(commit_plan["plan_sha256"]),
                    confirm_source_successor_impact_sha256=str(
                        commit_plan["source_successor_impact_sha256"]
                    ),
                )
            self.assertEqual(fixture.apply(commit_plan)["status"], "completed")
        finally:
            fixture.close()

    def test_preplanted_publication_names_fail_closed(self) -> None:
        plan = self.fixture.plan()
        final = SUCCESSOR.AUTHORITY_RELATIVE_PATH.name
        names = (
            final,
            f".{final}.create-{self.fixture.operation_id}",
            f".{final}.create-{self.fixture.operation_id}.quarantine",
        )
        for index, name in enumerate(names):
            with self.subTest(name=name):
                if index:
                    prior = self.fixture.runtime / "state" / names[index - 1]
                    prior.unlink()
                _write_private(self.fixture.runtime / "state" / name, b"preplant")
                with self.assertRaisesRegex(
                    SUCCESSOR.SuccessorError, "namespace is occupied"
                ):
                    self.fixture.apply(plan)

    def test_initial_journal_target_race_cannot_replace_preplant(self) -> None:
        plan = self.fixture.plan()
        original = SUCCESSOR._link_anonymous_noreplace
        preplant = b"same-uid target preplant\n"
        injected = False

        def plant_target_then_link(
            source_fd: int,
            target_directory_fd: int,
            target: str,
        ) -> None:
            nonlocal injected
            if (
                not injected
                and target == f"{self.fixture.operation_id}.json"
            ):
                injected = True
                target_path = (
                    self.fixture.runtime
                    / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
                    / target
                )
                _write_private(target_path, preplant)
            original(
                source_fd,
                target_directory_fd,
                target,
            )

        with mock.patch.object(
            SUCCESSOR,
            "_link_anonymous_noreplace",
            plant_target_then_link,
        ):
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "journal target appeared before publication",
            ):
                self.fixture.apply(plan)
        target_path = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        self.assertTrue(injected)
        self.assertEqual(target_path.read_bytes(), preplant)
        self.assertFalse(self.fixture.publisher().authority_path.exists())

    def test_initial_journal_source_inode_swap_is_rejected(self) -> None:
        plan = self.fixture.plan()
        original = SUCCESSOR._link_anonymous_noreplace
        injected = False

        def substitute_source_inode_then_link(
            source_fd: int,
            target_directory_fd: int,
            target: str,
        ) -> None:
            nonlocal injected
            if (
                not injected
                and target == f"{self.fixture.operation_id}.json"
            ):
                injected = True
                payload, _metadata = SUCCESSOR._read_descriptor(
                    source_fd,
                    maximum_bytes=SUCCESSOR.JSON_MAX_BYTES,
                    label="anonymous journal test source",
                )
                replacement_fd, _replacement_metadata = (
                    SUCCESSOR._seal_anonymous_payload_at(
                        target_directory_fd,
                        payload,
                        maximum_bytes=SUCCESSOR.JSON_MAX_BYTES,
                        label="anonymous journal test replacement",
                    )
                )
                try:
                    original(
                        replacement_fd,
                        target_directory_fd,
                        target,
                    )
                finally:
                    os.close(replacement_fd)
                return
            original(source_fd, target_directory_fd, target)

        with mock.patch.object(
            SUCCESSOR,
            "_link_anonymous_noreplace",
            substitute_source_inode_then_link,
        ):
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "journal initial linked inode CAS",
            ):
                self.fixture.apply(plan)
        self.assertTrue(injected)
        self.assertFalse(self.fixture.publisher().authority_path.exists())

    def test_third_hard_link_is_rejected_during_recovery(self) -> None:
        plan = self.fixture.plan()

        def crash(label: str) -> None:
            if label == "source-successor-authority-linked":
                raise RuntimeError("power loss")

        with self.assertRaises(RuntimeError):
            self.fixture.apply(plan, crash)
        final = self.fixture.publisher().authority_path
        third = self.fixture.runtime / "state/unowned-third-link"
        os.link(final, third)
        with self.assertRaisesRegex(SUCCESSOR.SuccessorError, "unsafe"):
            self.fixture.apply(plan)

    def test_another_changed_fixed_path_is_rejected(self) -> None:
        bridge = self.fixture.source / SUCCESSOR.CI_CONTRACT_PATH
        bridge.write_bytes(BRIDGE + b"# drift\n")
        bridge.chmod(0o700)
        _git(self.fixture.source, "add", SUCCESSOR.CI_CONTRACT_PATH)
        _git(self.fixture.source, "commit", "-m", "unreviewed bridge drift")
        self.fixture.target_sha = _git(self.fixture.source, "rev-parse", "HEAD")
        _git(
            self.fixture.source,
            "update-ref",
            "refs/remotes/origin/main",
            self.fixture.target_sha,
        )
        _make_private(self.fixture.source)
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError, "changed fixed paths"
        ):
            self.fixture.plan()

    def test_every_publication_checkpoint_recovers(self) -> None:
        checkpoints = (
            "source-successor-initial-journal-staged",
            "source-successor-transaction-directory-ready",
            "source-successor-initial-journal-linked",
            "source-successor-intent",
            "source-successor-journal-staging-sealed",
            "source-successor-journal-exchange-evidence-sealed",
            "source-successor-journal-exchange-evidence-published",
            "source-successor-journal-staging-published",
            "source-successor-journal-before-replace-cas",
            "source-successor-journal-exchanged",
            "source-successor-journal-retired-quarantined",
            "source-successor-journal-retired-generation-removed",
            "source-successor-journal-exchange-evidence-quarantined",
            "source-successor-journal-exchange-evidence-removed",
            "source-successor-predecessor-verified",
            "source-successor-source-verified",
            "source-successor-authority-commit-intent",
            "source-successor-authority-staged",
            "source-successor-authority-linked",
            "source-successor-authority-staging-unlinked",
            "source-successor-completed",
        )
        for label in checkpoints:
            fixture = Fixture()
            try:
                plan = fixture.plan()
                crashed = False

                def checkpoint(observed: str) -> None:
                    nonlocal crashed
                    if not crashed and observed == label:
                        crashed = True
                        raise RuntimeError("power loss")

                with self.subTest(label=label):
                    with self.assertRaisesRegex(RuntimeError, "power loss"):
                        fixture.apply(plan, checkpoint)
                    transaction_root = (
                        fixture.runtime
                        / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
                    )
                    journal = transaction_root / f"{fixture.operation_id}.json"
                    temporary = transaction_root / f".{journal.name}.tmp"
                    quarantine = Path(f"{temporary}.quarantine")
                    evidence = Path(f"{temporary}.exchange-evidence")
                    evidence_quarantine = Path(f"{evidence}.quarantine")
                    if label in {
                        "source-successor-journal-staging-sealed",
                        "source-successor-journal-exchange-evidence-sealed",
                    }:
                        self.assertFalse(temporary.exists())
                        self.assertFalse(evidence.exists())
                    elif label == (
                        "source-successor-journal-exchange-evidence-published"
                    ):
                        self.assertFalse(temporary.exists())
                        self.assertTrue(evidence.exists())
                    elif label in {
                        "source-successor-journal-staging-published",
                        "source-successor-journal-before-replace-cas",
                        "source-successor-journal-exchanged",
                    }:
                        self.assertTrue(temporary.exists())
                        self.assertTrue(evidence.exists())
                    elif label == (
                        "source-successor-journal-retired-quarantined"
                    ):
                        self.assertFalse(temporary.exists())
                        self.assertTrue(quarantine.exists())
                        self.assertTrue(evidence.exists())
                    elif label == (
                        "source-successor-journal-retired-generation-removed"
                    ):
                        self.assertFalse(temporary.exists())
                        self.assertFalse(quarantine.exists())
                        self.assertTrue(evidence.exists())
                    elif label == (
                        "source-successor-journal-exchange-evidence-quarantined"
                    ):
                        self.assertFalse(evidence.exists())
                        self.assertTrue(evidence_quarantine.exists())
                    elif label == (
                        "source-successor-journal-exchange-evidence-removed"
                    ):
                        self.assertFalse(evidence.exists())
                        self.assertFalse(evidence_quarantine.exists())
                    authority = fixture.apply(plan)
                    self.assertEqual(authority["status"], "completed")
                    self.assertEqual(
                        os.listdir(
                            fixture.runtime
                            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
                        ),
                        [f"{fixture.operation_id}.json"],
                    )
                    self.assertEqual(
                        fixture.publisher().authority_path.stat().st_nlink,
                        1,
                    )
            finally:
                fixture.close()

    def test_prelink_evidence_cleanup_reissues_exact_inode_after_loss(
        self,
    ) -> None:
        plan = self.fixture.plan()
        def crash_evidence(label: str) -> None:
            if label == (
                "source-successor-journal-exchange-evidence-published"
            ):
                raise RuntimeError("evidence power loss")

        with self.assertRaisesRegex(RuntimeError, "evidence power loss"):
            self.fixture.apply(plan, crash_evidence)
        journal = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        temporary = journal.parent / f".{journal.name}.tmp"
        evidence = Path(f"{temporary}.exchange-evidence")
        evidence_quarantine = Path(f"{evidence}.quarantine")
        self.assertTrue(evidence.exists())
        self.assertFalse(temporary.exists())

        def crash_evidence_quarantine(label: str) -> None:
            if label == (
                "source-successor-journal-exchange-evidence-quarantined"
            ):
                raise RuntimeError("evidence quarantine power loss")

        with self.assertRaisesRegex(
            RuntimeError, "evidence quarantine power loss"
        ):
            self.fixture.apply(plan, crash_evidence_quarantine)
        self.assertFalse(evidence.exists())
        self.assertTrue(evidence_quarantine.exists())
        self.assertFalse(temporary.exists())
        self.assertEqual(json.loads(journal.read_bytes())["phase"], "intent")

        original_seal = SUCCESSOR._seal_anonymous_payload_at
        reissued_staging_inodes: list[int] = []

        def record_reissued_staging(*args, **kwargs):  # type: ignore[no-untyped-def]
            descriptor, metadata = original_seal(*args, **kwargs)
            if kwargs.get("label") == "journal staging":
                reissued_staging_inodes.append(metadata.st_ino)
            return descriptor, metadata

        with mock.patch.object(
            SUCCESSOR,
            "_seal_anonymous_payload_at",
            record_reissued_staging,
        ):
            with self.assertRaisesRegex(RuntimeError, "evidence power loss"):
                self.fixture.apply(plan, crash_evidence)
        second_staging_inode = json.loads(evidence.read_bytes())["staging"][
            "inode"
        ]
        self.assertTrue(reissued_staging_inodes)
        self.assertEqual(second_staging_inode, reissued_staging_inodes[-1])
        self.assertEqual(
            json.loads(evidence.read_bytes())["current"]["inode"],
            journal.stat().st_ino,
        )
        self.assertFalse(evidence_quarantine.exists())
        self.assertEqual(self.fixture.apply(plan)["status"], "completed")
        self.assertFalse(evidence.exists())
        self.assertFalse(temporary.exists())

        removed = Fixture()
        try:
            removed_plan = removed.plan()
            with self.assertRaisesRegex(RuntimeError, "evidence power loss"):
                removed.apply(removed_plan, crash_evidence)

            def crash_evidence_removed(label: str) -> None:
                if label == (
                    "source-successor-journal-exchange-evidence-removed"
                ):
                    raise RuntimeError("evidence removed power loss")

            with self.assertRaisesRegex(
                RuntimeError, "evidence removed power loss"
            ):
                removed.apply(removed_plan, crash_evidence_removed)
            removed_journal = (
                removed.runtime
                / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
                / f"{removed.operation_id}.json"
            )
            removed_temporary = (
                removed_journal.parent / f".{removed_journal.name}.tmp"
            )
            self.assertFalse(
                Path(f"{removed_temporary}.exchange-evidence").exists()
            )
            self.assertFalse(
                Path(
                    f"{removed_temporary}.exchange-evidence.quarantine"
                ).exists()
            )
            self.assertFalse(removed_temporary.exists())
            self.assertEqual(removed.apply(removed_plan)["status"], "completed")
        finally:
            removed.close()

    def test_active_anonymous_links_never_accept_existing_targets(self) -> None:
        plan = self.fixture.plan()
        original_link = SUCCESSOR._link_anonymous_noreplace
        planted_staging = False

        def plant_same_payload_staging(
            source_fd: int,
            target_directory_fd: int,
            target: str,
        ) -> None:
            nonlocal planted_staging
            expected = f".{self.fixture.operation_id}.json.tmp"
            if target == expected and not planted_staging:
                directory = Path(
                    os.readlink(f"/proc/self/fd/{target_directory_fd}")
                )
                os.lseek(source_fd, 0, os.SEEK_SET)
                _write_private(directory / target, os.read(source_fd, 1 << 24))
                planted_staging = True
            original_link(source_fd, target_directory_fd, target)

        with mock.patch.object(
            SUCCESSOR,
            "_link_anonymous_noreplace",
            plant_same_payload_staging,
        ):
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "staging publication target exists",
            ):
                self.fixture.apply(plan)
        self.assertTrue(planted_staging)
        self.assertFalse(self.fixture.publisher().authority_path.exists())
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "do not match durable exchange evidence",
        ):
            self.fixture.apply(plan)

        lost = Fixture()
        try:
            lost_plan = lost.plan()
            lost_response = False

            def link_evidence_then_lose(
                source_fd: int,
                target_directory_fd: int,
                target: str,
            ) -> None:
                nonlocal lost_response
                original_link(source_fd, target_directory_fd, target)
                if target.endswith(".exchange-evidence") and not lost_response:
                    lost_response = True
                    raise OSError(errno.EIO, "link response lost")

            with mock.patch.object(
                SUCCESSOR,
                "_link_anonymous_noreplace",
                link_evidence_then_lose,
            ):
                with self.assertRaisesRegex(
                    SUCCESSOR.SuccessorError,
                    "evidence publication target exists",
                ):
                    lost.apply(lost_plan)
            self.assertTrue(lost_response)
            self.assertEqual(lost.apply(lost_plan)["status"], "completed")
        finally:
            lost.close()

        staged = Fixture()
        try:
            staged_plan = staged.plan()
            staged_response_lost = False

            def link_staging_then_lose(
                source_fd: int,
                target_directory_fd: int,
                target: str,
            ) -> None:
                nonlocal staged_response_lost
                original_link(source_fd, target_directory_fd, target)
                expected = f".{staged.operation_id}.json.tmp"
                if target == expected and not staged_response_lost:
                    staged_response_lost = True
                    raise OSError(errno.EIO, "staging link response lost")

            with mock.patch.object(
                SUCCESSOR,
                "_link_anonymous_noreplace",
                link_staging_then_lose,
            ):
                with self.assertRaisesRegex(
                    SUCCESSOR.SuccessorError,
                    "staging publication target exists",
                ):
                    staged.apply(staged_plan)
            self.assertTrue(staged_response_lost)
            self.assertEqual(
                staged.apply(staged_plan)["status"],
                "completed",
            )
        finally:
            staged.close()

    def test_every_abort_cleanup_checkpoint_recovers(self) -> None:
        for label in (
            "source-successor-aborted",
            "source-successor-abort-journal-quarantined",
            "source-successor-abort-journal-unlinked",
            "source-successor-abort-directory-removed",
        ):
            fixture = Fixture()
            try:
                plan = fixture.plan()

                def crash_intent(observed: str) -> None:
                    if observed == "source-successor-intent":
                        raise RuntimeError("intent power loss")

                with self.assertRaises(RuntimeError):
                    fixture.apply(plan, crash_intent)
                crashed = False

                def crash_abort(observed: str) -> None:
                    nonlocal crashed
                    if not crashed and observed == label:
                        crashed = True
                        raise RuntimeError("abort power loss")

                with self.subTest(label=label):
                    with self.assertRaisesRegex(RuntimeError, "abort power loss"):
                        fixture.publisher(crash_abort).abort(
                            source_sha=fixture.target_sha,
                            operation_id=fixture.operation_id,
                            confirm_plan_sha256=str(plan["plan_sha256"]),
                            confirm_source_successor_impact_sha256=str(
                                plan["source_successor_impact_sha256"]
                            ),
                        )
                    result = fixture.publisher().abort(
                        source_sha=fixture.target_sha,
                        operation_id=fixture.operation_id,
                        confirm_plan_sha256=str(plan["plan_sha256"]),
                        confirm_source_successor_impact_sha256=str(
                            plan["source_successor_impact_sha256"]
                        ),
                    )
                    self.assertEqual(result["status"], "aborted")
                    self.assertFalse(fixture.publisher().transaction_root.exists())
            finally:
                fixture.close()

    def test_partial_update_journal_recovers_from_durable_final(self) -> None:
        plan = self.fixture.plan()

        def crash_intent(label: str) -> None:
            if label == "source-successor-intent":
                raise RuntimeError("power loss")

        with self.assertRaises(RuntimeError):
            self.fixture.apply(plan, crash_intent)
        journal = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        temporary = journal.parent / f".{journal.name}.tmp"
        temporary.write_bytes(journal.read_bytes()[:91])
        temporary.chmod(0o600)
        authority = self.fixture.apply(plan)
        self.assertEqual(authority["status"], "completed")
        self.assertFalse(temporary.exists())

    def test_partial_legacy_initial_residue_is_cleaned_before_intent(
        self,
    ) -> None:
        plan = self.fixture.plan()
        staging = (
            self.fixture.runtime
            / "state"
            / self.fixture.publisher()._initial_transaction_staging(
                self.fixture.operation_id
            )
        )
        _write_private(staging, b'{"partial":')
        authority = self.fixture.apply(plan)
        self.assertEqual(authority["status"], "completed")
        self.assertFalse(staging.exists())
        self.assertFalse(Path(f"{staging}.quarantine").exists())

    def test_operation_owned_journal_quarantine_recovers_and_aborts(self) -> None:
        def crash_at(fixture: Fixture, plan: dict[str, object], wanted: str) -> None:
            def checkpoint(label: str) -> None:
                if label == wanted:
                    raise RuntimeError("power loss")

            with self.assertRaisesRegex(RuntimeError, "power loss"):
                fixture.apply(plan, checkpoint)

        def journal_residue(fixture: Fixture) -> tuple[Path, Path, Path]:
            journal = (
                fixture.runtime
                / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
                / f"{fixture.operation_id}.json"
            )
            temporary = journal.parent / f".{journal.name}.tmp"
            return journal, temporary, Path(f"{temporary}.quarantine")

        intent_plan = self.fixture.plan()
        crash_at(
            self.fixture,
            intent_plan,
            "source-successor-intent",
        )
        _journal, _temporary, quarantine = journal_residue(self.fixture)
        _write_private(quarantine, b'{"partial":')
        self.assertEqual(self.fixture.apply(intent_plan)["status"], "completed")
        self.assertFalse(quarantine.exists())

        committed = Fixture()
        try:
            committed_plan = committed.plan()
            crash_at(
                committed,
                committed_plan,
                "source-successor-authority-linked",
            )
            _journal, _temporary, quarantine = journal_residue(committed)
            _write_private(quarantine, b'{"partial":')
            self.assertTrue(committed.publisher().authority_path.exists())
            self.assertEqual(
                committed.apply(committed_plan)["status"],
                "completed",
            )
            self.assertFalse(quarantine.exists())
        finally:
            committed.close()

        aborted = Fixture()
        try:
            aborted_plan = aborted.plan()
            crash_at(aborted, aborted_plan, "source-successor-intent")
            _journal, _temporary, quarantine = journal_residue(aborted)
            _write_private(quarantine, b'{"partial":')
            result = aborted.publisher().abort(
                source_sha=aborted.target_sha,
                operation_id=aborted.operation_id,
                confirm_plan_sha256=str(aborted_plan["plan_sha256"]),
                confirm_source_successor_impact_sha256=str(
                    aborted_plan["source_successor_impact_sha256"]
                ),
            )
            self.assertEqual(result["status"], "aborted")
            self.assertFalse(aborted.publisher().transaction_root.exists())
        finally:
            aborted.close()

        conflicting = Fixture()
        try:
            conflicting_plan = conflicting.plan()
            crash_at(
                conflicting,
                conflicting_plan,
                "source-successor-intent",
            )
            _journal, temporary, quarantine = journal_residue(conflicting)
            _write_private(temporary, b'{"partial":')
            _write_private(quarantine, b'{"partial":')
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "staging and quarantine both exist",
            ):
                conflicting.apply(conflicting_plan)
        finally:
            conflicting.close()

        foreign = Fixture()
        try:
            foreign_plan = foreign.plan()
            crash_at(foreign, foreign_plan, "source-successor-intent")
            foreign_name = (
                foreign.publisher().transaction_root
                / ".adopt-git-successor-foreign.json.tmp.quarantine"
            )
            _write_private(foreign_name, b'{"partial":')
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "another source successor operation exists",
            ):
                foreign.apply(foreign_plan)
        finally:
            foreign.close()

    def test_final_absent_journal_tmp_is_never_recovery_authority(self) -> None:
        plan = self.fixture.plan()
        transaction_root = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
        )
        transaction_root.mkdir(mode=0o700)
        transaction_root.chmod(0o700)
        temporary = transaction_root / f".{self.fixture.operation_id}.json.tmp"
        document = {
            "schema_version": 1,
            "status": "applying",
            "phase": "intent",
            "operation_id": self.fixture.operation_id,
            "plan": plan["plan"],
            "plan_sha256": plan["plan_sha256"],
            "source_successor_impact_sha256": plan[
                "source_successor_impact_sha256"
            ],
            "production_source_trust_sha256": plan["plan"][
                "production_source_trust_sha256"
            ],
            "created_at": "2026-08-18T00:00:00Z",
            "completed_at": None,
            "aborted_at": None,
        }
        _write_private(temporary, _canonical(document) + b"\n")

        stale_probe_calls = 0

        def stale_probe(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal stale_probe_calls
            del args, kwargs
            stale_probe_calls += 1
            raise AssertionError("stale delivery gate must not be accepted")

        publisher = self.fixture.publisher()
        publisher.delivery_gate_probe = stale_probe
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "durable journal exists during preliminary plan",
        ):
            publisher.apply(
                source_sha=self.fixture.target_sha,
                operation_id=self.fixture.operation_id,
                confirm_plan_sha256=str(plan["plan_sha256"]),
                confirm_source_successor_impact_sha256=str(
                    plan["source_successor_impact_sha256"]
                ),
            )
        self.assertEqual(stale_probe_calls, 0)
        self.assertTrue(temporary.exists())
        self.assertFalse(
            (transaction_root / f"{self.fixture.operation_id}.json").exists()
        )
        self.assertFalse(self.fixture.publisher().authority_path.exists())

    def test_reverse_valid_journal_residue_requires_exchange_evidence(self) -> None:
        plan = self.fixture.plan()

        def crash_intent(label: str) -> None:
            if label == "source-successor-intent":
                raise RuntimeError("power loss")

        with self.assertRaisesRegex(RuntimeError, "power loss"):
            self.fixture.apply(plan, crash_intent)
        journal = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        temporary = journal.parent / f".{journal.name}.tmp"
        current_payload = journal.read_bytes()
        staged = json.loads(current_payload)
        staged["phase"] = "predecessor-verified"
        _write_private(temporary, current_payload)
        _write_private(journal, _canonical(staged) + b"\n")
        publisher = self.fixture.publisher()
        self.assertTrue(
            publisher._staged_transaction_follows(
                json.loads(temporary.read_bytes()),
                json.loads(journal.read_bytes()),
            )
        )
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "named journal staging lacks durable exchange evidence",
        ):
            self.fixture.apply(plan)

    def test_forward_valid_journal_staging_without_evidence_is_rejected(
        self,
    ) -> None:
        plan = self.fixture.plan()

        def crash_intent(label: str) -> None:
            if label == "source-successor-intent":
                raise RuntimeError("power loss")

        with self.assertRaisesRegex(RuntimeError, "power loss"):
            self.fixture.apply(plan, crash_intent)
        journal = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        temporary = journal.parent / f".{journal.name}.tmp"
        evidence = Path(f"{temporary}.exchange-evidence")
        current_payload = journal.read_bytes()
        current_inode = journal.stat().st_ino
        staged = json.loads(current_payload)
        staged["phase"] = "predecessor-verified"
        _write_private(temporary, _canonical(staged) + b"\n")
        self.assertTrue(
            self.fixture.publisher()._staged_transaction_follows(
                json.loads(current_payload), staged
            )
        )
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "named journal staging lacks durable exchange evidence",
        ):
            self.fixture.apply(plan)
        self.assertEqual(journal.read_bytes(), current_payload)
        self.assertEqual(journal.stat().st_ino, current_inode)
        self.assertTrue(temporary.exists())
        self.assertFalse(evidence.exists())

        quarantined = Fixture()
        try:
            quarantined_plan = quarantined.plan()
            with self.assertRaisesRegex(RuntimeError, "power loss"):
                quarantined.apply(quarantined_plan, crash_intent)
            quarantined_journal = (
                quarantined.runtime
                / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
                / f"{quarantined.operation_id}.json"
            )
            quarantine = (
                quarantined_journal.parent
                / f".{quarantined_journal.name}.tmp.quarantine"
            )
            quarantined_staged = json.loads(
                quarantined_journal.read_bytes()
            )
            quarantined_staged["phase"] = "predecessor-verified"
            _write_private(
                quarantine, _canonical(quarantined_staged) + b"\n"
            )
            quarantine_payload = quarantine.read_bytes()
            quarantine_inode = quarantine.stat().st_ino
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "canonical journal quarantine lacks durable exchange evidence",
            ):
                quarantined.apply(quarantined_plan)
            self.assertEqual(quarantine.read_bytes(), quarantine_payload)
            self.assertEqual(quarantine.stat().st_ino, quarantine_inode)
            self.assertEqual(
                json.loads(quarantined_journal.read_bytes())["phase"],
                "intent",
            )
        finally:
            quarantined.close()

    def test_evidence_rejects_altered_staging_and_identical_generations(
        self,
    ) -> None:
        plan = self.fixture.plan()

        def crash_source_verified(label: str) -> None:
            if label == "source-successor-source-verified":
                raise RuntimeError("source power loss")

        with self.assertRaisesRegex(RuntimeError, "source power loss"):
            self.fixture.apply(plan, crash_source_verified)

        def crash_staging(label: str) -> None:
            if label == "source-successor-journal-staging-published":
                raise RuntimeError("staging power loss")

        with self.assertRaisesRegex(RuntimeError, "staging power loss"):
            self.fixture.apply(plan, crash_staging)
        journal = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        temporary = journal.parent / f".{journal.name}.tmp"
        evidence = Path(f"{temporary}.exchange-evidence")
        final_payload = journal.read_bytes()
        final_inode = journal.stat().st_ino
        altered = json.loads(temporary.read_bytes())
        self.assertEqual(altered["phase"], "authority-commit-intent")
        altered["completed_at"] = "2099-01-01T00:00:00Z"
        _write_private(temporary, _canonical(altered) + b"\n")
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "do not match durable exchange evidence",
        ):
            self.fixture.apply(plan)
        self.assertEqual(journal.read_bytes(), final_payload)
        self.assertEqual(journal.stat().st_ino, final_inode)
        self.assertTrue(temporary.exists())
        self.assertTrue(evidence.exists())

        invalid = Fixture()
        try:
            invalid_plan = invalid.plan()

            def crash_evidence(label: str) -> None:
                if label == (
                    "source-successor-journal-exchange-evidence-published"
                ):
                    raise RuntimeError("evidence power loss")

            with self.assertRaisesRegex(RuntimeError, "evidence power loss"):
                invalid.apply(invalid_plan, crash_evidence)
            invalid_journal = (
                invalid.runtime
                / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
                / f"{invalid.operation_id}.json"
            )
            invalid_evidence = (
                invalid_journal.parent
                / f".{invalid_journal.name}.tmp.exchange-evidence"
            )
            forged = json.loads(invalid_evidence.read_bytes())
            forged["staging_document"] = json.loads(
                invalid_journal.read_bytes()
            )
            forged["staging"] = SUCCESSOR._exchange_identity(
                invalid_journal.read_bytes(), invalid_journal.stat()
            )
            invalid_evidence.write_bytes(_canonical(forged) + b"\n")
            invalid_evidence.chmod(0o600)
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "exchange evidence staging differs",
            ):
                invalid.apply(invalid_plan)
        finally:
            invalid.close()

    def test_exchange_evidence_preplants_and_publication_race_fail_closed(self) -> None:
        def crash_intent(fixture: Fixture, plan: dict[str, object]) -> Path:
            def checkpoint(label: str) -> None:
                if label == "source-successor-intent":
                    raise RuntimeError("power loss")

            with self.assertRaisesRegex(RuntimeError, "power loss"):
                fixture.apply(plan, checkpoint)
            return (
                fixture.runtime
                / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
                / f"{fixture.operation_id}.json"
            )

        plan = self.fixture.plan()
        journal = crash_intent(self.fixture, plan)
        evidence = journal.parent / f".{journal.name}.tmp.exchange-evidence"
        _write_private(evidence, b'{"preplant":true}\n')
        before = evidence.read_bytes()
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "exchange evidence is invalid",
        ):
            self.fixture.apply(plan)
        self.assertEqual(evidence.read_bytes(), before)

        foreign = Fixture()
        try:
            foreign_plan = foreign.plan()
            foreign_journal = crash_intent(foreign, foreign_plan)
            _write_private(
                foreign_journal.parent
                / ".adopt-git-successor-foreign.json.tmp.exchange-evidence",
                b"preplant",
            )
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "another source successor operation exists",
            ):
                foreign.apply(foreign_plan)
        finally:
            foreign.close()

        conflicting = Fixture()
        try:
            conflicting_plan = conflicting.plan()

            def crash_published(label: str) -> None:
                if label == (
                    "source-successor-journal-exchange-evidence-published"
                ):
                    raise RuntimeError("evidence power loss")

            with self.assertRaisesRegex(RuntimeError, "evidence power loss"):
                conflicting.apply(conflicting_plan, crash_published)
            conflicting_journal = (
                conflicting.runtime
                / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
                / f"{conflicting.operation_id}.json"
            )
            conflicting_evidence = (
                conflicting_journal.parent
                / f".{conflicting_journal.name}.tmp.exchange-evidence"
            )
            _write_private(
                Path(f"{conflicting_evidence}.quarantine"),
                conflicting_evidence.read_bytes(),
            )
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "evidence and quarantine both exist",
            ):
                conflicting.apply(conflicting_plan)
        finally:
            conflicting.close()

        raced = Fixture()
        try:
            raced_plan = raced.plan()
            raced_journal = crash_intent(raced, raced_plan)
            original_link = SUCCESSOR._link_anonymous_noreplace
            planted = False

            def preplant_at_link(
                source_fd: int,
                target_directory_fd: int,
                target: str,
            ) -> None:
                nonlocal planted
                if not planted:
                    directory = Path(
                        os.readlink(f"/proc/self/fd/{target_directory_fd}")
                    )
                    _write_private(directory / target, b'{"raced":true}\n')
                    planted = True
                original_link(source_fd, target_directory_fd, target)

            with mock.patch.object(
                SUCCESSOR,
                "_link_anonymous_noreplace",
                preplant_at_link,
            ):
                with self.assertRaisesRegex(
                    SUCCESSOR.SuccessorError,
                    "exchange evidence publication target exists",
                ):
                    raced.apply(raced_plan)
            raced_evidence = (
                raced_journal.parent
                / f".{raced_journal.name}.tmp.exchange-evidence"
            )
            self.assertTrue(planted)
            self.assertEqual(raced_evidence.read_bytes(), b'{"raced":true}\n')
        finally:
            raced.close()

        replaced = Fixture()
        try:
            replaced_plan = replaced.plan()
            original_publish = SUCCESSOR._publish_exchange_evidence_at
            replaced_inode = False

            def replace_evidence_before_caller_reopen(
                *args, **kwargs  # type: ignore[no-untyped-def]
            ):
                nonlocal replaced_inode
                result = original_publish(*args, **kwargs)
                directory_fd = args[0]
                temporary = args[3]
                evidence_name = SUCCESSOR._exchange_evidence_name(temporary)
                directory = Path(
                    os.readlink(f"/proc/self/fd/{directory_fd}")
                )
                evidence = directory / evidence_name
                original_inode = evidence.stat().st_ino
                attacker = directory / ".same-content-evidence.json"
                _write_private(attacker, evidence.read_bytes())
                os.replace(attacker, evidence)
                replaced_inode = evidence.stat().st_ino != original_inode
                return result

            with mock.patch.object(
                SUCCESSOR,
                "_publish_exchange_evidence_at",
                replace_evidence_before_caller_reopen,
            ):
                with self.assertRaisesRegex(
                    SUCCESSOR.SuccessorError,
                    "exchange evidence published inode differs",
                ):
                    replaced.apply(replaced_plan)
            self.assertTrue(replaced_inode)
            self.assertFalse(replaced.publisher().authority_path.exists())
        finally:
            replaced.close()

    def test_journal_final_to_tmp_race_fails_closed_under_lock(self) -> None:
        plan = self.fixture.plan()

        def crash_at_intent(label: str) -> None:
            if label == "source-successor-intent":
                raise RuntimeError("power loss")

        with self.assertRaisesRegex(RuntimeError, "power loss"):
            self.fixture.apply(plan, crash_at_intent)
        journal = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        temporary = journal.parent / f".{journal.name}.tmp"
        moved = False

        def move_after_lock(label: str) -> None:
            nonlocal moved
            if label == "source-successor-apply-lock-acquired" and not moved:
                journal.rename(temporary)
                moved = True

        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "journal staging has no durable final",
        ):
            self.fixture.apply(plan, move_after_lock)
        self.assertTrue(moved)
        self.assertTrue(temporary.exists())
        self.assertFalse(journal.exists())
        self.assertFalse(self.fixture.publisher().authority_path.exists())

    def test_exchange_syscall_races_restore_same_uid_final(self) -> None:
        def crash_intent(fixture: Fixture, plan: dict[str, object]) -> Path:
            def checkpoint(label: str) -> None:
                if label == "source-successor-intent":
                    raise RuntimeError("power loss")

            with self.assertRaisesRegex(RuntimeError, "power loss"):
                fixture.apply(plan, checkpoint)
            return (
                fixture.runtime
                / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
                / f"{fixture.operation_id}.json"
            )

        def race_one_exchange(
            fixture: Fixture,
            plan: dict[str, object],
            journal: Path,
        ) -> None:
            original_exchange = SUCCESSOR._rename_exchange
            raced = False
            attacker_inode: int | None = None

            def swap_after_precheck(
                directory_fd: int,
                first: str,
                second: str,
            ) -> None:
                nonlocal raced, attacker_inode
                if not raced and second == journal.name:
                    attacker = journal.parent / ".same-uid-attacker.json"
                    _write_private(attacker, journal.read_bytes())
                    os.replace(attacker, journal)
                    attacker_inode = journal.stat().st_ino
                    raced = True
                original_exchange(directory_fd, first, second)

            with mock.patch.object(
                SUCCESSOR,
                "_rename_exchange",
                swap_after_precheck,
            ):
                with self.assertRaisesRegex(
                    SUCCESSOR.SuccessorError,
                    "exchange CAS changed; evidence retained after rollback",
                ):
                    fixture.apply(plan)
            self.assertTrue(raced)
            self.assertEqual(journal.stat().st_ino, attacker_inode)
            self.assertTrue((journal.parent / f".{journal.name}.tmp").exists())
            evidence = (
                journal.parent
                / f".{journal.name}.tmp.exchange-evidence"
            )
            self.assertTrue(evidence.exists())
            self.assertFalse(fixture.publisher().authority_path.exists())

        plan = self.fixture.plan()
        journal = crash_intent(self.fixture, plan)
        race_one_exchange(self.fixture, plan, journal)
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "do not match durable exchange evidence",
        ):
            self.fixture.apply(plan)

        recovering = Fixture()
        try:
            recovering_plan = recovering.plan()

            def crash_evidence(label: str) -> None:
                if label == (
                    "source-successor-journal-exchange-evidence-published"
                ):
                    raise RuntimeError("evidence power loss")

            with self.assertRaisesRegex(RuntimeError, "evidence power loss"):
                recovering.apply(recovering_plan, crash_evidence)
            recovering_journal = (
                recovering.runtime
                / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
                / f"{recovering.operation_id}.json"
            )
            race_one_exchange(
                recovering,
                recovering_plan,
                recovering_journal,
            )
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "do not match durable exchange evidence",
            ):
                recovering.apply(recovering_plan)
        finally:
            recovering.close()

    def test_recovery_never_exchanges_foreign_retired_inode_into_final(
        self,
    ) -> None:
        plan = self.fixture.plan()

        def crash_after_exchange(label: str) -> None:
            if label == "source-successor-journal-exchanged":
                raise RuntimeError("post-exchange power loss")

        with self.assertRaisesRegex(RuntimeError, "post-exchange power loss"):
            self.fixture.apply(plan, crash_after_exchange)
        journal = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        temporary = journal.parent / f".{journal.name}.tmp"
        evidence = Path(f"{temporary}.exchange-evidence")
        final_payload = journal.read_bytes()
        final_inode = journal.stat().st_ino
        retired_payload = temporary.read_bytes()
        retired_inode = temporary.stat().st_ino
        attacker = journal.parent / ".foreign-retired-generation.json"
        _write_private(attacker, retired_payload)
        os.replace(attacker, temporary)
        foreign_inode = temporary.stat().st_ino
        self.assertNotEqual(foreign_inode, retired_inode)

        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "do not match durable exchange evidence",
        ):
            self.fixture.apply(plan)
        self.assertEqual(journal.read_bytes(), final_payload)
        self.assertEqual(journal.stat().st_ino, final_inode)
        self.assertEqual(temporary.read_bytes(), retired_payload)
        self.assertEqual(temporary.stat().st_ino, foreign_inode)
        self.assertTrue(evidence.exists())

    def test_abort_quarantine_syscall_race_never_unlinks_swapped_final(self) -> None:
        plan = self.fixture.plan()

        def crash_intent(label: str) -> None:
            if label == "source-successor-intent":
                raise RuntimeError("power loss")

        with self.assertRaisesRegex(RuntimeError, "power loss"):
            self.fixture.apply(plan, crash_intent)
        journal = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        quarantine = journal.parent / f".{journal.name}.tmp.quarantine"
        original_noreplace = SUCCESSOR._rename_noreplace
        raced = False
        attacker_inode: int | None = None

        def swap_inside_quarantine_syscall(
            source_directory_fd: int,
            source: str,
            target_directory_fd: int,
            target: str,
        ) -> None:
            nonlocal raced, attacker_inode
            if not raced and source == journal.name and target == quarantine.name:
                attacker = journal.parent / ".same-uid-abort-attacker.json"
                _write_private(attacker, journal.read_bytes())
                os.replace(attacker, journal)
                attacker_inode = journal.stat().st_ino
                raced = True
            original_noreplace(
                source_directory_fd,
                source,
                target_directory_fd,
                target,
            )

        with mock.patch.object(
            SUCCESSOR,
            "_rename_noreplace",
            swap_inside_quarantine_syscall,
        ):
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "unlink CAS changed; displaced generation restored",
            ):
                self.fixture.publisher().abort(
                    source_sha=self.fixture.target_sha,
                    operation_id=self.fixture.operation_id,
                    confirm_plan_sha256=str(plan["plan_sha256"]),
                    confirm_source_successor_impact_sha256=str(
                        plan["source_successor_impact_sha256"]
                    ),
                )
        self.assertTrue(raced)
        self.assertTrue(journal.exists())
        self.assertEqual(journal.stat().st_ino, attacker_inode)
        self.assertFalse(quarantine.exists())
        result = self.fixture.publisher().abort(
            source_sha=self.fixture.target_sha,
            operation_id=self.fixture.operation_id,
            confirm_plan_sha256=str(plan["plan_sha256"]),
            confirm_source_successor_impact_sha256=str(
                plan["source_successor_impact_sha256"]
            ),
        )
        self.assertEqual(result["status"], "aborted")
        self.assertFalse(self.fixture.publisher().transaction_root.exists())

    def test_staging_swap_inside_exchange_keeps_evidence_fail_closed(self) -> None:
        plan = self.fixture.plan()

        def crash_source_verified(label: str) -> None:
            if label == "source-successor-source-verified":
                raise RuntimeError("power loss")

        with self.assertRaisesRegex(RuntimeError, "power loss"):
            self.fixture.apply(plan, crash_source_verified)
        journal = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        temporary = journal.parent / f".{journal.name}.tmp"
        evidence = journal.parent / f"{temporary.name}.exchange-evidence"
        original_exchange = SUCCESSOR._rename_exchange
        raced = False

        def replace_staging_after_all_prechecks(
            directory_fd: int,
            first: str,
            second: str,
        ) -> None:
            nonlocal raced
            intended = (
                json.loads(temporary.read_bytes())
                if first == temporary.name
                else None
            )
            if (
                not raced
                and first == temporary.name
                and second == journal.name
                and isinstance(intended, dict)
                and intended.get("phase") == "authority-commit-intent"
            ):
                intended["completed_at"] = "2099-01-01T00:00:00Z"
                attacker = journal.parent / ".legal-forward-attacker.json"
                _write_private(attacker, _canonical(intended) + b"\n")
                os.replace(attacker, temporary)
                raced = True
            original_exchange(directory_fd, first, second)

        with mock.patch.object(
            SUCCESSOR,
            "_rename_exchange",
            replace_staging_after_all_prechecks,
        ):
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "exchange CAS changed; evidence retained after rollback",
            ):
                self.fixture.apply(plan)
        self.assertTrue(raced)
        self.assertTrue(evidence.exists())
        current = json.loads(journal.read_bytes())
        altered = json.loads(temporary.read_bytes())
        publisher = self.fixture.publisher()
        current = publisher._validate_transaction_document(
            current, self.fixture.operation_id
        )
        altered = publisher._validate_transaction_document(
            altered, self.fixture.operation_id
        )
        self.assertTrue(publisher._staged_transaction_follows(current, altered))
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "do not match durable exchange evidence",
        ):
            self.fixture.apply(plan)

        recovering = Fixture()
        try:
            recovering_plan = recovering.plan()

            def crash_source_verified_recovery(label: str) -> None:
                if label == "source-successor-source-verified":
                    raise RuntimeError("source verified power loss")

            with self.assertRaisesRegex(
                RuntimeError, "source verified power loss"
            ):
                recovering.apply(
                    recovering_plan, crash_source_verified_recovery
                )

            def crash_evidence(label: str) -> None:
                if label == (
                    "source-successor-journal-exchange-evidence-published"
                ):
                    raise RuntimeError("evidence power loss")

            with self.assertRaisesRegex(RuntimeError, "evidence power loss"):
                recovering.apply(recovering_plan, crash_evidence)
            recovering_journal = (
                recovering.runtime
                / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
                / f"{recovering.operation_id}.json"
            )
            recovering_temporary = (
                recovering_journal.parent
                / f".{recovering_journal.name}.tmp"
            )
            recovering_raced = False

            def swap_recovery_staging(
                directory_fd: int,
                first: str,
                second: str,
            ) -> None:
                nonlocal recovering_raced
                if not recovering_raced and first == recovering_temporary.name:
                    attacker = recovering_journal.parent / ".recovery-attacker.json"
                    altered_recovery = json.loads(
                        recovering_temporary.read_bytes()
                    )
                    self.assertEqual(
                        altered_recovery["phase"],
                        "authority-commit-intent",
                    )
                    altered_recovery["completed_at"] = (
                        "2099-01-01T00:00:00Z"
                    )
                    _write_private(
                        attacker, _canonical(altered_recovery) + b"\n"
                    )
                    os.replace(attacker, recovering_temporary)
                    recovering_raced = True
                original_exchange(directory_fd, first, second)

            with mock.patch.object(
                SUCCESSOR,
                "_rename_exchange",
                swap_recovery_staging,
            ):
                with self.assertRaisesRegex(
                    SUCCESSOR.SuccessorError,
                    "evidence retained after rollback",
                ):
                    recovering.apply(recovering_plan)
            self.assertTrue(recovering_raced)
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "do not match durable exchange evidence",
            ):
                recovering.apply(recovering_plan)
        finally:
            recovering.close()

    def test_power_loss_after_altered_staging_exchange_restores_exact_current(
        self,
    ) -> None:
        plan = self.fixture.plan()

        def crash_source_verified(label: str) -> None:
            if label == "source-successor-source-verified":
                raise RuntimeError("source verified power loss")

        with self.assertRaisesRegex(RuntimeError, "source verified power loss"):
            self.fixture.apply(plan, crash_source_verified)
        journal = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        temporary = journal.parent / f".{journal.name}.tmp"
        current_payload = journal.read_bytes()
        current_inode = journal.stat().st_ino
        original_exchange = SUCCESSOR._rename_exchange
        altered_inside_syscall = False

        def alter_staging_then_exchange(
            directory_fd: int,
            first: str,
            second: str,
        ) -> None:
            nonlocal altered_inside_syscall
            if not altered_inside_syscall and first == temporary.name:
                altered = json.loads(temporary.read_bytes())
                self.assertEqual(altered["phase"], "authority-commit-intent")
                altered["completed_at"] = "2099-01-01T00:00:00Z"
                attacker = journal.parent / ".power-loss-staging.json"
                _write_private(attacker, _canonical(altered) + b"\n")
                os.replace(attacker, temporary)
                altered_inside_syscall = True
            original_exchange(directory_fd, first, second)

        def crash_after_exchange(label: str) -> None:
            if label == "source-successor-journal-exchanged":
                raise RuntimeError("exchange power loss")

        with mock.patch.object(
            SUCCESSOR,
            "_rename_exchange",
            alter_staging_then_exchange,
        ):
            with self.assertRaisesRegex(RuntimeError, "exchange power loss"):
                self.fixture.apply(plan, crash_after_exchange)
        self.assertTrue(altered_inside_syscall)
        self.assertNotEqual(journal.read_bytes(), current_payload)
        self.assertEqual(temporary.read_bytes(), current_payload)
        self.assertEqual(temporary.stat().st_ino, current_inode)

        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "evidenced journal CAS mismatch was rolled back",
        ):
            self.fixture.apply(plan)
        self.assertEqual(journal.read_bytes(), current_payload)
        self.assertEqual(journal.stat().st_ino, current_inode)
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "do not match durable exchange evidence",
        ):
            self.fixture.apply(plan)

    def test_evidenced_restore_syscall_race_keeps_foreign_tmp_out_of_final(
        self,
    ) -> None:
        plan = self.fixture.plan()

        def crash_source_verified(label: str) -> None:
            if label == "source-successor-source-verified":
                raise RuntimeError("source verified power loss")

        with self.assertRaisesRegex(RuntimeError, "source verified power loss"):
            self.fixture.apply(plan, crash_source_verified)
        journal = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        temporary = journal.parent / f".{journal.name}.tmp"
        original_exchange = SUCCESSOR._rename_exchange

        def alter_staging_then_exchange(
            directory_fd: int,
            first: str,
            second: str,
        ) -> None:
            if first == temporary.name:
                altered = json.loads(temporary.read_bytes())
                altered["completed_at"] = "2099-01-01T00:00:00Z"
                attacker = journal.parent / ".restore-race-staging.json"
                _write_private(attacker, _canonical(altered) + b"\n")
                os.replace(attacker, temporary)
            original_exchange(directory_fd, first, second)

        def crash_after_exchange(label: str) -> None:
            if label == "source-successor-journal-exchanged":
                raise RuntimeError("exchange power loss")

        with mock.patch.object(
            SUCCESSOR,
            "_rename_exchange",
            alter_staging_then_exchange,
        ):
            with self.assertRaisesRegex(RuntimeError, "exchange power loss"):
                self.fixture.apply(plan, crash_after_exchange)
        unexpected_final_payload = journal.read_bytes()
        unexpected_final_inode = journal.stat().st_ino
        current_payload = temporary.read_bytes()
        foreign_inode: int | None = None

        def replace_current_inside_restore(
            directory_fd: int,
            first: str,
            second: str,
        ) -> None:
            nonlocal foreign_inode
            if foreign_inode is None and first == temporary.name:
                attacker = journal.parent / ".restore-race-current.json"
                _write_private(attacker, current_payload)
                os.replace(attacker, temporary)
                foreign_inode = temporary.stat().st_ino
            original_exchange(directory_fd, first, second)

        with mock.patch.object(
            SUCCESSOR,
            "_rename_exchange",
            replace_current_inside_restore,
        ):
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "evidenced journal mismatch rollback failed",
            ):
                self.fixture.apply(plan)
        self.assertIsNotNone(foreign_inode)
        self.assertEqual(journal.read_bytes(), unexpected_final_payload)
        self.assertEqual(journal.stat().st_ino, unexpected_final_inode)
        self.assertEqual(temporary.read_bytes(), current_payload)
        self.assertEqual(temporary.stat().st_ino, foreign_inode)

    def test_partial_authority_staging_recovers_after_commit_intent(self) -> None:
        plan = self.fixture.plan()

        def crash(label: str) -> None:
            if label == "source-successor-authority-commit-intent":
                raise RuntimeError("power loss")

        with self.assertRaises(RuntimeError):
            self.fixture.apply(plan, crash)
        final = SUCCESSOR.AUTHORITY_RELATIVE_PATH.name
        staging = (
            self.fixture.runtime
            / "state"
            / f".{final}.create-{self.fixture.operation_id}"
        )
        _write_private(staging, b'{"partial":')
        authority = self.fixture.apply(plan)
        self.assertEqual(authority["status"], "completed")
        self.assertFalse(staging.exists())

    def test_journal_exchange_and_fsync_lost_responses_recover(self) -> None:
        plan = self.fixture.plan()
        original_exchange = SUCCESSOR._rename_exchange
        lost_exchange = False

        def exchange_then_lose(
            directory_fd: int,
            first: str,
            second: str,
        ) -> None:
            nonlocal lost_exchange
            original_exchange(directory_fd, first, second)
            if (
                not lost_exchange
                and second == f"{self.fixture.operation_id}.json"
            ):
                lost_exchange = True
                raise RuntimeError("exchange response lost")

        with mock.patch.object(
            SUCCESSOR, "_rename_exchange", exchange_then_lose
        ):
            with self.assertRaisesRegex(RuntimeError, "response lost"):
                self.fixture.apply(plan)
        self.assertEqual(self.fixture.apply(plan)["status"], "completed")

        fixture = Fixture()
        try:
            second_plan = fixture.plan()
            original_fsync = SUCCESSOR.os.fsync
            lost_fsync = False
            transaction_directory = (
                fixture.runtime
                / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            )
            initial_journal = (
                transaction_directory / f"{fixture.operation_id}.json"
            )

            def fsync_then_lose(descriptor: int) -> None:
                nonlocal lost_fsync
                original_fsync(descriptor)
                try:
                    descriptor_metadata = os.fstat(descriptor)
                    directory_metadata = transaction_directory.stat()
                except OSError:
                    return
                if (
                    not lost_fsync
                    and stat.S_ISDIR(descriptor_metadata.st_mode)
                    and (
                        descriptor_metadata.st_dev,
                        descriptor_metadata.st_ino,
                    )
                    == (
                        directory_metadata.st_dev,
                        directory_metadata.st_ino,
                    )
                    and initial_journal.is_file()
                ):
                    lost_fsync = True
                    raise RuntimeError("fsync response lost")

            with mock.patch.object(SUCCESSOR.os, "fsync", fsync_then_lose):
                with self.assertRaisesRegex(RuntimeError, "response lost"):
                    fixture.apply(second_plan)
            self.assertTrue(lost_fsync)
            self.assertTrue(initial_journal.is_file())
            self.assertEqual(fixture.apply(second_plan)["status"], "completed")
        finally:
            fixture.close()

        link_fixture = Fixture()
        try:
            link_plan = link_fixture.plan()
            original_link = SUCCESSOR._link_anonymous_noreplace
            lost_link = False

            def link_then_lose(
                source_fd: int,
                target_directory_fd: int,
                target: str,
            ) -> None:
                nonlocal lost_link
                original_link(
                    source_fd,
                    target_directory_fd,
                    target,
                )
                if (
                    not lost_link
                    and target == f"{link_fixture.operation_id}.json"
                ):
                    lost_link = True
                    raise RuntimeError("anonymous link response lost")

            with mock.patch.object(
                SUCCESSOR,
                "_link_anonymous_noreplace",
                link_then_lose,
            ):
                with self.assertRaisesRegex(RuntimeError, "response lost"):
                    link_fixture.apply(link_plan)
            self.assertTrue(lost_link)
            self.assertEqual(
                link_fixture.apply(link_plan)["status"],
                "completed",
            )
        finally:
            link_fixture.close()

    def test_plan_rejects_transaction_staging_and_concurrent_operation(self) -> None:
        name = (
            f"{SUCCESSOR.TRANSACTION_STAGING_PREFIX}"
            "another-operation.json"
        )
        _write_private(self.fixture.runtime / "state" / name, b"preplant")
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError, "initial namespace is occupied"
        ):
            self.fixture.plan()
        (self.fixture.runtime / "state" / name).unlink()
        first_plan = self.fixture.plan()
        second_operation = "adopt-git-successor-test-0002"
        second_plan = self.fixture.publisher().plan(
            source_sha=self.fixture.target_sha,
            operation_id=second_operation,
        )

        def crash(label: str) -> None:
            if label == "source-successor-intent":
                raise RuntimeError("power loss")

        with self.assertRaises(RuntimeError):
            self.fixture.apply(first_plan, crash)
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError, "durable journal exists"
        ):
            self.fixture.publisher().apply(
                source_sha=self.fixture.target_sha,
                operation_id=second_operation,
                confirm_plan_sha256=str(second_plan["plan_sha256"]),
                confirm_source_successor_impact_sha256=str(
                    second_plan["source_successor_impact_sha256"]
                ),
            )

    def test_apply_lock_replacement_fails_before_transaction_mutation(
        self,
    ) -> None:
        plan = self.fixture.plan()
        state = self.fixture.runtime / "state"
        lock_path = state / "deploy.lock"
        held_lock = state / "deploy.lock.original-inode"
        source_before = self.snapshot(self.fixture.source)
        production_before = self.snapshot(self.fixture.production)
        replacement_fd: int | None = None
        replacement_locked = False

        def replace_lock(label: str) -> None:
            nonlocal replacement_fd, replacement_locked
            if label != "source-successor-apply-lock-acquired":
                return
            lock_path.rename(held_lock)
            _write_private(lock_path, b"replacement deployment lock\n")
            replacement_fd = os.open(lock_path, os.O_RDWR)
            SUCCESSOR.fcntl.flock(
                replacement_fd,
                SUCCESSOR.fcntl.LOCK_EX | SUCCESSOR.fcntl.LOCK_NB,
            )
            replacement_locked = True

        try:
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "deployment lock path changed",
            ):
                self.fixture.apply(plan, replace_lock)
            self.assertTrue(replacement_locked)
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(held_lock.stat().st_mode), 0o600)
            self.assertFalse(self.fixture.publisher().authority_path.exists())
            self.assertFalse(self.fixture.publisher().transaction_root.exists())
            self.assertFalse(
                any(
                    name.startswith(
                        ".adopted-git-permission-source-successor"
                    )
                    for name in os.listdir(state)
                )
            )
            self.assertEqual(source_before, self.snapshot(self.fixture.source))
            self.assertEqual(
                production_before,
                self.snapshot(self.fixture.production),
            )
        finally:
            if replacement_fd is not None:
                SUCCESSOR.fcntl.flock(
                    replacement_fd,
                    SUCCESSOR.fcntl.LOCK_UN,
                )
                os.close(replacement_fd)

    def test_state_path_swap_and_production_injection_fail_closed(self) -> None:
        plan = self.fixture.plan()
        state = self.fixture.runtime / "state"
        held = self.fixture.runtime / "state-held"
        swapped = False

        def swap(label: str) -> None:
            nonlocal swapped
            if label == "source-successor-apply-lock-acquired" and not swapped:
                state.rename(held)
                state.mkdir(mode=0o700)
                swapped = True

        try:
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError, "state directory path changed"
            ):
                self.fixture.apply(plan, swap)
        finally:
            if swapped:
                state.rmdir()
                held.rename(state)
        with tempfile.TemporaryDirectory() as temporary:
            fixed_root = Path(temporary)
            fixed_root.chmod(0o700)
            fixed_production = fixed_root / "production"
            fixed_runtime = fixed_root / "runtime"
            fixed_production.mkdir(mode=0o700)
            fixed_runtime.mkdir(mode=0o700)
            with (
                mock.patch.object(
                    SUCCESSOR, "PRODUCTION_ROOT", fixed_production
                ),
                mock.patch.object(SUCCESSOR, "RUNTIME_ROOT", fixed_runtime),
                self.assertRaisesRegex(
                    SUCCESSOR.SuccessorError,
                    "forbids injected trust seams",
                ),
            ):
                SUCCESSOR.SourceSuccessorPublisher(
                    source_root=self.fixture.source,
                    production_root=fixed_production,
                    runtime_root=fixed_runtime,
                    delivery_gate_probe=self.fixture.probe,
                )

    def test_commit_intent_root_swaps_precede_authority_publication(
        self,
    ) -> None:
        for label, expected_error in (
            ("runtime-state", "runtime state directory path changed"),
            ("production-root", "production root path changed"),
            ("source-root", "source root path changed"),
        ):
            fixture = Fixture()
            try:
                plan = fixture.plan()
                if label == "runtime-state":
                    live_path = fixture.runtime / "state"
                elif label == "production-root":
                    live_path = fixture.production
                else:
                    live_path = fixture.source
                held_path = live_path.with_name(
                    f"{live_path.name}-held-at-commit-intent"
                )
                journal_payload: bytes | None = None
                swapped = False

                def swap_root(checkpoint: str) -> None:
                    nonlocal journal_payload, swapped
                    if (
                        checkpoint
                        != "source-successor-authority-commit-intent"
                        or swapped
                    ):
                        return
                    journal = (
                        fixture.runtime
                        / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
                        / f"{fixture.operation_id}.json"
                    )
                    journal_payload = journal.read_bytes()
                    live_path.rename(held_path)
                    live_path.mkdir(mode=0o700)
                    swapped = True

                with self.subTest(root=label), self.assertRaisesRegex(
                    SUCCESSOR.SuccessorError,
                    expected_error,
                ):
                    fixture.apply(plan, swap_root)
                self.assertTrue(swapped)
                self.assertIsNotNone(journal_payload)
                journal_state = (
                    held_path
                    if label == "runtime-state"
                    else fixture.runtime / "state"
                )
                journal_path = (
                    journal_state
                    / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY.name
                    / f"{fixture.operation_id}.json"
                )
                self.assertEqual(journal_path.read_bytes(), journal_payload)
                journal = json.loads(journal_payload)
                self.assertEqual(journal["status"], "applying")
                self.assertEqual(
                    journal["phase"],
                    "authority-commit-intent",
                )
                authority_name = SUCCESSOR.AUTHORITY_RELATIVE_PATH.name
                live_authority = (
                    fixture.runtime / "state" / authority_name
                )
                self.assertFalse(live_authority.exists())
                self.assertFalse(live_authority.is_symlink())
                for state_path in {journal_state, fixture.runtime / "state"}:
                    self.assertFalse((state_path / authority_name).exists())
                    self.assertFalse(
                        any(
                            name.startswith(f".{authority_name}.create-")
                            for name in os.listdir(state_path)
                        )
                    )
            finally:
                fixture.close()

    def test_commit_intent_in_place_content_drift_precedes_authority_create(
        self,
    ) -> None:
        for label in (
            "production-deploy-ref",
            "production-head",
            "production-fetch-head",
            "source-main-ref",
        ):
            fixture = Fixture()
            try:
                if label == "production-deploy-ref":
                    guarded_path = (
                        fixture.production
                        / ".git/refs/remotes/nexpoly-deploy/main"
                    )
                    drift_payload = (fixture.target_sha + "\n").encode()
                elif label == "production-head":
                    guarded_path = fixture.production / ".git/HEAD"
                    drift_payload = b"ref: refs/heads/drifted-main\n"
                elif label == "production-fetch-head":
                    guarded_path = fixture.production / ".git/FETCH_HEAD"
                    _write_private(
                        guarded_path,
                        b"baseline fixture fetch evidence\n",
                    )
                    drift_payload = b"drifted fixture fetch evidence\n"
                else:
                    guarded_path = fixture.source / ".git/refs/heads/main"
                    drift_payload = (fixture.predecessor_sha + "\n").encode()
                original_payload = guarded_path.read_bytes()
                original_identity = (
                    guarded_path.stat().st_dev,
                    guarded_path.stat().st_ino,
                )
                plan = fixture.plan()
                journal_payload: bytes | None = None
                drifted = False

                def overwrite_in_place(payload: bytes) -> None:
                    with guarded_path.open("r+b") as stream:
                        stream.seek(0)
                        stream.write(payload)
                        stream.truncate()
                        stream.flush()
                        os.fsync(stream.fileno())

                def drift_after_commit_intent(checkpoint: str) -> None:
                    nonlocal drifted, journal_payload
                    if (
                        checkpoint
                        != "source-successor-authority-commit-intent"
                        or drifted
                    ):
                        return
                    journal = (
                        fixture.runtime
                        / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
                        / f"{fixture.operation_id}.json"
                    )
                    journal_payload = journal.read_bytes()
                    overwrite_in_place(drift_payload)
                    drifted = True

                with self.subTest(content=label), self.assertRaises(
                    SUCCESSOR.SuccessorError
                ):
                    fixture.apply(plan, drift_after_commit_intent)
                self.assertTrue(drifted)
                self.assertEqual(
                    (
                        guarded_path.stat().st_dev,
                        guarded_path.stat().st_ino,
                    ),
                    original_identity,
                )
                self.assertIsNotNone(journal_payload)
                journal_path = (
                    fixture.runtime
                    / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
                    / f"{fixture.operation_id}.json"
                )
                self.assertEqual(journal_path.read_bytes(), journal_payload)
                journal = json.loads(journal_payload)
                self.assertEqual(journal["status"], "applying")
                self.assertEqual(
                    journal["phase"],
                    "authority-commit-intent",
                )
                authority_path = (
                    fixture.runtime / SUCCESSOR.AUTHORITY_RELATIVE_PATH
                )
                self.assertFalse(authority_path.exists())
                authority_name = SUCCESSOR.AUTHORITY_RELATIVE_PATH.name
                self.assertFalse(
                    any(
                        name.startswith(f".{authority_name}.create-")
                        for name in os.listdir(fixture.runtime / "state")
                    )
                )
                if label == "production-deploy-ref":
                    overwrite_in_place(original_payload)
                    authority = fixture.apply(plan)
                    self.assertEqual(authority["status"], "completed")
                    self.assertEqual(
                        (
                            guarded_path.stat().st_dev,
                            guarded_path.stat().st_ino,
                        ),
                        original_identity,
                    )
            finally:
                fixture.close()

    def test_authority_staging_content_drift_precedes_final_hard_link(
        self,
    ) -> None:
        plan = self.fixture.plan()
        guarded_path = (
            self.fixture.production
            / ".git/refs/remotes/nexpoly-deploy/main"
        )
        original_payload = guarded_path.read_bytes()
        original_identity = (
            guarded_path.stat().st_dev,
            guarded_path.stat().st_ino,
        )
        drift_payload = (self.fixture.target_sha + "\n").encode()
        drifted = False

        def overwrite_in_place(payload: bytes) -> None:
            with guarded_path.open("r+b") as stream:
                stream.seek(0)
                stream.write(payload)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())

        def drift_after_staging(checkpoint: str) -> None:
            nonlocal drifted
            if (
                checkpoint != "source-successor-authority-staged"
                or drifted
            ):
                return
            overwrite_in_place(drift_payload)
            drifted = True

        with self.assertRaises(SUCCESSOR.SuccessorError):
            self.fixture.apply(plan, drift_after_staging)
        self.assertTrue(drifted)
        self.assertEqual(
            (
                guarded_path.stat().st_dev,
                guarded_path.stat().st_ino,
            ),
            original_identity,
        )

        journal_path = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        journal = json.loads(journal_path.read_bytes())
        self.assertEqual(journal["status"], "applying")
        self.assertEqual(journal["phase"], "authority-commit-intent")

        state = self.fixture.runtime / "state"
        authority = state / SUCCESSOR.AUTHORITY_RELATIVE_PATH.name
        staging = state / (
            f".{SUCCESSOR.AUTHORITY_RELATIVE_PATH.name}.create-"
            f"{self.fixture.operation_id}"
        )
        self.assertFalse(authority.exists())
        self.assertTrue(staging.is_file())
        self.assertEqual(staging.stat().st_nlink, 1)

        overwrite_in_place(original_payload)
        completed = self.fixture.apply(plan)
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(authority.is_file())
        self.assertEqual(authority.stat().st_nlink, 1)
        self.assertFalse(staging.exists())
        completed_journal = json.loads(journal_path.read_bytes())
        self.assertEqual(completed_journal["status"], "completed")
        self.assertEqual(completed_journal["phase"], "completed")

    def test_create_once_prelink_failure_leaves_recoverable_staging(
        self,
    ) -> None:
        state = self.fixture.runtime / "state"
        name = "prelink-unit-authority.json"
        staging = state / f".{name}.create-{self.fixture.operation_id}"
        final = state / name
        checkpoints: list[str] = []
        state_fd = os.open(
            state,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            def reject_prelink() -> None:
                self.assertEqual(
                    checkpoints[-1],
                    "source-successor-authority-staged",
                )
                self.assertTrue(staging.is_file())
                self.assertEqual(staging.stat().st_nlink, 1)
                self.assertFalse(final.exists())
                raise RuntimeError("pre-link reproof failed")

            with self.assertRaisesRegex(RuntimeError, "pre-link reproof"):
                SUCCESSOR._create_json_once_at(
                    state_fd,
                    name,
                    {"status": "completed"},
                    operation_id=self.fixture.operation_id,
                    checkpoint=checkpoints.append,
                    before_link=reject_prelink,
                )
            self.assertTrue(staging.is_file())
            self.assertEqual(staging.stat().st_nlink, 1)
            self.assertFalse(final.exists())

            SUCCESSOR._create_json_once_at(
                state_fd,
                name,
                {"status": "completed"},
                operation_id=self.fixture.operation_id,
                checkpoint=checkpoints.append,
            )
            self.assertTrue(final.is_file())
            self.assertEqual(final.stat().st_nlink, 1)
            self.assertFalse(staging.exists())
        finally:
            os.close(state_fd)

    def test_full_content_reproof_count_is_bounded(self) -> None:
        plan = self.fixture.plan()

        def apply_with_count() -> tuple[dict[str, object], int]:
            publisher = self.fixture.publisher()
            original = publisher._validate_durable_plan
            calls = 0

            def counted(*args, **kwargs):  # type: ignore[no-untyped-def]
                nonlocal calls
                calls += 1
                return original(*args, **kwargs)

            publisher._validate_durable_plan = counted  # type: ignore[method-assign]
            authority = publisher.apply(
                source_sha=self.fixture.target_sha,
                operation_id=self.fixture.operation_id,
                confirm_plan_sha256=str(plan["plan_sha256"]),
                confirm_source_successor_impact_sha256=str(
                    plan["source_successor_impact_sha256"]
                ),
            )
            return authority, calls

        authority, initial_calls = apply_with_count()
        self.assertEqual(authority["status"], "completed")
        self.assertGreaterEqual(initial_calls, 3)
        self.assertLessEqual(initial_calls, 8)

        repeated, repeated_calls = apply_with_count()
        self.assertEqual(repeated, authority)
        self.assertGreaterEqual(repeated_calls, 1)
        self.assertLessEqual(repeated_calls, 3)

    def test_realpath_alias_and_unsafe_seam_components_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            fixed_production = root / "production"
            fixed_runtime = root / "runtime"
            fixed_production.mkdir(mode=0o700)
            fixed_runtime.mkdir(mode=0o700)
            production_alias = root / "production-alias"
            runtime_alias = root / "runtime-alias"
            production_alias.symlink_to(
                fixed_production, target_is_directory=True
            )
            runtime_alias.symlink_to(fixed_runtime, target_is_directory=True)
            with (
                mock.patch.object(
                    SUCCESSOR, "PRODUCTION_ROOT", fixed_production
                ),
                mock.patch.object(SUCCESSOR, "RUNTIME_ROOT", fixed_runtime),
                self.assertRaisesRegex(
                    SUCCESSOR.SuccessorError,
                    "forbids injected trust seams",
                ),
            ):
                SUCCESSOR.SourceSuccessorPublisher(
                    source_root=self.fixture.source,
                    production_root=production_alias,
                    runtime_root=runtime_alias,
                    delivery_gate_probe=self.fixture.probe,
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            unsafe = root / "unsafe"
            unsafe.mkdir(mode=0o755)
            unsafe.chmod(0o755)
            paths = [unsafe / name for name in ("source", "production", "runtime")]
            for path in paths:
                path.mkdir(mode=0o700)
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "owner-private symlink-free paths",
            ):
                SUCCESSOR.SourceSuccessorPublisher(
                    source_root=paths[0],
                    production_root=paths[1],
                    runtime_root=paths[2],
                    checkpoint=lambda _label: None,
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            real = root / "real"
            real.mkdir(mode=0o700)
            source = real / "source"
            production = root / "production"
            runtime = root / "runtime"
            for path in (source, production, runtime):
                path.mkdir(mode=0o700)
            (root / "alias").symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "canonical symlink-free paths",
            ):
                SUCCESSOR.SourceSuccessorPublisher(
                    source_root=root / "alias/source",
                    production_root=production,
                    runtime_root=runtime,
                    checkpoint=lambda _label: None,
                )

    def test_transaction_terminal_timestamps_must_be_monotonic(self) -> None:
        plan = self.fixture.plan()

        def crash_intent(label: str) -> None:
            if label == "source-successor-intent":
                raise RuntimeError("power loss")

        with self.assertRaisesRegex(RuntimeError, "power loss"):
            self.fixture.apply(plan, crash_intent)
        journal = (
            self.fixture.runtime
            / SUCCESSOR.TRANSACTION_RELATIVE_DIRECTORY
            / f"{self.fixture.operation_id}.json"
        )
        intent = json.loads(journal.read_bytes())
        publisher = self.fixture.publisher()

        completed = dict(intent)
        completed.update(
            {
                "status": "completed",
                "phase": "completed",
                "completed_at": "2000-01-01T00:00:00Z",
            }
        )
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "timestamps are not monotonic",
        ):
            publisher._validate_transaction_document(
                completed,
                self.fixture.operation_id,
            )

        aborted = dict(intent)
        aborted.update(
            {
                "status": "aborted",
                "phase": "aborted",
                "aborted_at": "2000-01-01T00:00:00Z",
            }
        )
        with self.assertRaisesRegex(
            SUCCESSOR.SuccessorError,
            "timestamps are not monotonic",
        ):
            publisher._validate_transaction_document(
                aborted,
                self.fixture.operation_id,
            )

    def test_mode_drift_and_old_journal_drift_are_rejected(self) -> None:
        bridge = self.fixture.source / SUCCESSOR.CI_CONTRACT_PATH
        bridge.chmod(0o600)
        _git(self.fixture.source, "add", SUCCESSOR.CI_CONTRACT_PATH)
        _git(self.fixture.source, "commit", "-m", "mode drift")
        self.fixture.target_sha = _git(self.fixture.source, "rev-parse", "HEAD")
        _git(
            self.fixture.source,
            "update-ref",
            "refs/remotes/origin/main",
            self.fixture.target_sha,
        )
        _make_private(self.fixture.source)
        with self.assertRaisesRegex(SUCCESSOR.SuccessorError, "mode drifted"):
            self.fixture.plan()

        other = Fixture()
        try:
            journal = next(
                (
                    other.runtime
                    / SUCCESSOR.PREDECESSOR_TRANSACTION_RELATIVE_DIRECTORY
                ).iterdir()
            )
            document = json.loads(journal.read_text())
            document["permission_checkpoint"] = "permission:captured"
            _write_private(journal, _canonical(document) + b"\n")
            with self.assertRaisesRegex(
                SUCCESSOR.SuccessorError,
                "predecessor permission journal is invalid",
            ):
                other.plan()
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
