from __future__ import annotations

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
MODULE_PATH = SOURCE_ROOT / "scripts/adopt_runtime_prerequisites.py"
SPEC = importlib.util.spec_from_file_location(
    "nexpoly_adopt_runtime_prerequisites", MODULE_PATH
)
if SPEC is None or SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("cannot load prerequisite adopter")
ADOPTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADOPTER)


def _run(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        list(arguments),
        cwd=directory,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _write_private(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)


def _inventory(root: Path) -> list[tuple[str, int, str]]:
    result: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            identity = "directory"
        elif stat.S_ISREG(metadata.st_mode):
            identity = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            identity = "special"
        result.append((relative, stat.S_IMODE(metadata.st_mode), identity))
    return result


def _make_tree_private(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_symlink():
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o077)
    root.chmod(0o700)


class AdoptRuntimePrerequisiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source = root / "source"
        self.runtime = root / "runtime"
        self.source.mkdir(mode=0o700)
        self.runtime.mkdir(mode=0o700)
        runtime_directories = (
            self.runtime,
            self.runtime / "config",
            self.runtime / "state",
            self.runtime / "audit",
            self.runtime / "audit/adoption",
        )
        for directory in runtime_directories[1:]:
            directory.mkdir(mode=0o700)
        for directory in runtime_directories:
            directory.chmod(0o700)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        _write_private(self.runtime / "state/deploy.lock", b"", 0o600)
        adopted = {"schema_version": 1, "status": "adopted"}
        _write_private(
            self.runtime / "state/adopted-deployment.json",
            json.dumps(adopted, sort_keys=True).encode() + b"\n",
            0o600,
        )
        _write_private(
            self.runtime / "state/bootstrap-control.json",
            json.dumps(
                {
                    "schema_version": 3,
                    "status": "completed",
                    "authority_kind": ADOPTER.ADOPTION_AUTHORITY_KIND,
                    "adopted_deployment": adopted,
                    "adopted_deployment_sha256": ADOPTER._canonical_digest(adopted),
                },
                sort_keys=True,
            ).encode()
            + b"\n",
            0o600,
        )
        self.pgpass_payload = b"127.0.0.1:55432:nexpoly:nexpoly_mutable_audit:secret\n"
        _write_private(
            self.runtime / "config/mutable-data-audit.pgpass",
            self.pgpass_payload,
            0o600,
        )

        for source_path, _name, mode, _classification in ADOPTER.TRACKED_INSTALLS:
            source = SOURCE_ROOT / source_path
            destination = self.source / source_path
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(mode)
        bootstrap_source = SOURCE_ROOT / "scripts/bootstrap_pull_deploy.py"
        bootstrap_destination = self.source / "scripts/bootstrap_pull_deploy.py"
        bootstrap_destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(bootstrap_source, bootstrap_destination)
        bootstrap_destination.chmod(0o700)
        _run(self.source, "/usr/bin/git", "init", "--initial-branch=main")
        _run(self.source, "/usr/bin/git", "config", "user.name", "Prerequisite Test")
        _run(
            self.source,
            "/usr/bin/git",
            "config",
            "user.email",
            "prerequisite@example.invalid",
        )
        _run(self.source, "/usr/bin/git", "add", ".")
        _run(self.source, "/usr/bin/git", "commit", "-m", "fixture")
        _run(
            self.source,
            "/usr/bin/git",
            "remote",
            "add",
            "origin",
            ADOPTER.REPOSITORY_SSH_URL,
        )
        _run(
            self.source,
            "/usr/bin/git",
            "update-ref",
            "refs/remotes/origin/main",
            "HEAD",
        )
        _make_tree_private(self.source)
        self.sha = _run(self.source, "/usr/bin/git", "rev-parse", "HEAD")
        self.operation_id = "adopt-prereq-test-0001"
        self.delivery_gate = {
            "remote_main": self.sha,
            "ci": {
                "workflow_run_id": 42,
                "run_attempt": 1,
                "head_sha": self.sha,
                "head_branch": "main",
                "event": "push",
                "path": ".github/workflows/ci.yml",
                "conclusion": "success",
                "required_jobs": ["fixture-gate"],
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def installer(
        self,
        checkpoint=None,
        delivery_probe=None,
        source_readiness_probe=None,
    ):  # type: ignore[no-untyped-def]
        def default_delivery_probe(
            _source: Path,
            _runtime: Path,
            source_sha: str,
            sealed: dict[str, object] | None,
        ) -> dict[str, object]:
            self.assertEqual(source_sha, self.sha)
            if sealed is not None:
                self.assertEqual(sealed, self.delivery_gate)
            return json.loads(json.dumps(self.delivery_gate))

        return ADOPTER.PrerequisiteInstaller(
            self.source,
            self.runtime,
            checkpoint=checkpoint,
            delivery_gate_probe=delivery_probe or default_delivery_probe,
            source_readiness_probe=source_readiness_probe,
        )

    def permission_installer(
        self,
        checkpoint=None,
    ):  # type: ignore[no-untyped-def]
        if not hasattr(self, "production"):
            self.production = Path(self.temporary.name) / "production"
            self.production.mkdir(mode=0o755)
            (self.production / "tracked.txt").write_text("adopted production\n")
            _run(
                self.production,
                "/usr/bin/git",
                "init",
                "--initial-branch=main",
            )
            _run(
                self.production,
                "/usr/bin/git",
                "config",
                "user.name",
                "Permission Test",
            )
            _run(
                self.production,
                "/usr/bin/git",
                "config",
                "user.email",
                "permission@example.invalid",
            )
            _run(self.production, "/usr/bin/git", "add", ".")
            _run(
                self.production,
                "/usr/bin/git",
                "commit",
                "-m",
                "adopted production",
            )
            self.production_sha = _run(
                self.production,
                "/usr/bin/git",
                "rev-parse",
                "HEAD",
            )
            self.production_tree = _run(
                self.production,
                "/usr/bin/git",
                "rev-parse",
                "HEAD^{tree}",
            )
            for path in sorted(
                (self.production / ".git").rglob("*"),
                reverse=True,
            ):
                if path.is_dir():
                    path.chmod(0o755)
                elif path.is_file():
                    path.chmod(0o644)
            (self.production / ".git").chmod(0o755)
            (self.production / "tracked.txt").chmod(0o644)
            self.production.chmod(0o755)

            trust_source = SOURCE_ROOT / "scripts/git_source_trust.py"
            trust_target = self.source / "scripts/git_source_trust.py"
            shutil.copyfile(trust_source, trust_target)
            trust_target.chmod(0o700)
            bridge_source = SOURCE_ROOT / "scripts/bridge_deploy_core.py"
            bridge_target = self.source / "scripts/bridge_deploy_core.py"
            shutil.copyfile(bridge_source, bridge_target)
            bridge_target.chmod(0o700)
            _run(
                self.source,
                "/usr/bin/git",
                "add",
                "scripts/git_source_trust.py",
                "scripts/bridge_deploy_core.py",
            )
            _run(
                self.source,
                "/usr/bin/git",
                "commit",
                "-m",
                "add permission trust policy",
            )
            self.sha = _run(
                self.source, "/usr/bin/git", "rev-parse", "HEAD"
            )
            _run(
                self.source,
                "/usr/bin/git",
                "update-ref",
                "refs/remotes/origin/main",
                self.sha,
            )
            _make_tree_private(self.source)
            self.delivery_gate["remote_main"] = self.sha
            self.delivery_gate["ci"]["head_sha"] = self.sha

            adopted = {
                "schema_version": 1,
                "status": "adopted",
                "authority_kind": ADOPTER.ADOPTION_AUTHORITY_KIND,
                "source_sha": self.production_sha,
                "source_tree": self.production_tree,
            }
            if hasattr(self, "unit_adoption_bindings"):
                adopted.update(
                    json.loads(json.dumps(self.unit_adoption_bindings))
                )
            _write_private(
                self.runtime / "state/adopted-deployment.json",
                json.dumps(adopted, sort_keys=True).encode() + b"\n",
                0o600,
            )
            _write_private(
                self.runtime / "state/bootstrap-control.json",
                json.dumps(
                    {
                        "schema_version": 3,
                        "status": "completed",
                        "authority_kind": ADOPTER.ADOPTION_AUTHORITY_KIND,
                        "adopted_deployment": adopted,
                        "adopted_deployment_sha256": (
                            ADOPTER._canonical_digest(adopted)
                        ),
                    },
                    sort_keys=True,
                ).encode()
                + b"\n",
                0o600,
            )
            base_plan = self.installer().plan(
                source_sha=self.sha,
                operation_id=self.operation_id,
            )
            self.installer().apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=base_plan["plan_sha256"],
            )
            self.permission_operation_id = (
                "adopt-git-permission-test-0001"
            )
        return ADOPTER.PermissionHardeningInstaller(
            self.source,
            self.runtime,
            production_root=self.production,
            checkpoint=checkpoint,
            delivery_gate_probe=self.installer().delivery_gate_probe,
        )

    def unit_permission_installer(
        self,
        checkpoint=None,
    ):  # type: ignore[no-untyped-def]
        if not hasattr(self, "unit_parent"):
            self.unit_parent = Path(self.temporary.name) / "systemd/user"
            self.unit_parent.mkdir(mode=0o700, parents=True)
            self.unit_parent.chmod(0o700)
            self.md_unit = self.unit_parent / ADOPTER.MD_UNIT_NAME
            self.dft_unit = self.unit_parent / ADOPTER.DFT_UNIT_NAME
            self.md_unit_payload = b"[Service]\nExecStart=/fixture/md\n"
            self.dft_unit_payload = b"[Service]\nExecStart=/fixture/dft\n"
            _write_private(self.md_unit, self.md_unit_payload, 0o664)
            _write_private(self.dft_unit, self.dft_unit_payload, 0o600)
            self.unit_reload_calls = 0
            self.unit_processes = {
                ADOPTER.MD_UNIT_NAME: {
                    "main_pid": 41001,
                    "invocation_id": "1" * 32,
                },
                ADOPTER.DFT_UNIT_NAME: {
                    "main_pid": 41002,
                    "invocation_id": "2" * 32,
                },
            }

            def adopted_unit(path: Path, payload: bytes) -> dict[str, object]:
                process = self.unit_processes[path.name]
                return {
                    "systemd_unit": {
                        "target_path": str(path),
                        "sha256": ADOPTER._digest(payload),
                        "systemd_state": {
                            "LoadState": "loaded",
                            "FragmentPath": str(path),
                            "DropInPaths": "",
                            "NeedDaemonReload": "no",
                            "UnitFileState": "enabled",
                            "ActiveState": "active",
                            "SubState": "running",
                        },
                        "process_identity": dict(process),
                    }
                }

            self.unit_adoption_bindings = {
                "monomer_md": adopted_unit(
                    self.md_unit, self.md_unit_payload
                ),
                "monomer_dft": adopted_unit(
                    self.dft_unit, self.dft_unit_payload
                ),
            }
            permission = self.permission_installer()
            permission_plan = permission.plan(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
            )
            permission.apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=permission_plan["plan_sha256"],
                confirm_permission_impact_sha256=permission_plan[
                    "permission_impact_sha256"
                ],
            )
            self.git_permission_source_sha = self.sha
            self.sha = self.advance_remote_tracking_ref()
            _run(
                self.source,
                "/usr/bin/git",
                "reset",
                "--hard",
                self.sha,
            )
            _make_tree_private(self.source)
            self.delivery_gate["remote_main"] = self.sha
            self.delivery_gate["ci"]["head_sha"] = self.sha
            self.unit_operation_id = "adopt-unit-permission-test-0001"

        def systemd_probe(name: str, path: Path) -> dict[str, str]:
            process = self.unit_processes[name]
            reload_pending = (
                name == ADOPTER.MD_UNIT_NAME
                and stat.S_IMODE(path.stat().st_mode) == 0o600
                and self.unit_reload_calls == 0
            )
            return {
                "LoadState": "loaded",
                "FragmentPath": str(path),
                "DropInPaths": "",
                "NeedDaemonReload": "yes" if reload_pending else "no",
                "UnitFileState": "enabled",
                "ActiveState": "active",
                "SubState": "running",
                "MainPID": str(process["main_pid"]),
                "InvocationID": str(process["invocation_id"]),
            }

        def daemon_reload() -> None:
            self.unit_reload_calls += 1

        return ADOPTER.UnitPermissionHardeningInstaller(
            self.source,
            self.runtime,
            production_root=self.production,
            md_unit_path=self.md_unit,
            dft_unit_path=self.dft_unit,
            checkpoint=checkpoint,
            delivery_gate_probe=self.installer().delivery_gate_probe,
            systemd_probe=systemd_probe,
            daemon_reload=daemon_reload,
        )

    def git_blob_identity(
        self,
        commit: str,
        relative: str,
    ) -> dict[str, str]:
        raw = _run(
            self.source,
            "/usr/bin/git",
            "ls-tree",
            commit,
            "--",
            relative,
        )
        header, observed_path = raw.split("\t", 1)
        mode, object_type, blob_sha = header.split()
        self.assertEqual(observed_path, relative)
        payload = subprocess.run(
            [
                "/usr/bin/git",
                "show",
                f"{commit}:{relative}",
            ],
            cwd=self.source,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
        return {
            "object_type": object_type,
            "mode": mode,
            "blob_sha": blob_sha,
            "sha256": ADOPTER._digest(payload),
        }

    def seed_source_successor_authority(
        self,
        *,
        root_journal_source_trust_sha256: str | None = None,
    ):  # type: ignore[no-untyped-def]
        """Publish a validator-exact completed source-successor fixture."""

        installer = self.unit_permission_installer()
        root_sha = self.git_permission_source_sha
        for relative in ADOPTER.SOURCE_SUCCESSOR_ALLOWED_CHANGED_BLOBS:
            path = self.source / relative
            path.write_bytes(
                path.read_bytes()
                + b"\n# source-successor unit consumer fixture\n"
            )
            path.chmod(0o700)
        _run(
            self.source,
            "/usr/bin/git",
            "add",
            *ADOPTER.SOURCE_SUCCESSOR_ALLOWED_CHANGED_BLOBS,
        )
        _run(
            self.source,
            "/usr/bin/git",
            "commit",
            "-m",
            "source successor fixture",
        )
        self.sha = _run(
            self.source,
            "/usr/bin/git",
            "rev-parse",
            "HEAD",
        )
        _run(
            self.source,
            "/usr/bin/git",
            "update-ref",
            "refs/remotes/origin/main",
            self.sha,
        )
        _make_tree_private(self.source)
        self.delivery_gate["remote_main"] = self.sha
        self.delivery_gate["ci"]["head_sha"] = self.sha

        root_path = self.runtime / ADOPTER.PERMISSION_AUTHORITY_PATH
        root_authority, root_digest = ADOPTER._load_json_with_digest(
            root_path
        )
        root_journal_path = (
            self.runtime
            / ADOPTER.PERMISSION_TRANSACTION_DIRECTORY
            / f"{root_authority['operation_id']}.json"
        )
        root_journal, root_journal_digest = (
            ADOPTER._load_json_with_digest(root_journal_path)
        )
        if root_journal_source_trust_sha256 is not None:
            root_journal = json.loads(json.dumps(root_journal))
            root_journal["source_trust_sha256"] = (
                root_journal_source_trust_sha256
            )
            _write_private(
                root_journal_path,
                ADOPTER._canonical_bytes(root_journal) + b"\n",
                0o600,
            )
            root_journal, root_journal_digest = (
                ADOPTER._load_json_with_digest(root_journal_path)
            )
        root_source_trust_sha256 = root_journal["source_trust_sha256"]
        target_tree, readiness, delivery = installer._source_authority(
            self.sha
        )
        self.assertEqual(delivery, self.delivery_gate)
        files: list[dict[str, object]] = []
        for relative in ADOPTER.UNIT_PERMISSION_SUCCESSOR_V2_BLOBS:
            predecessor = self.git_blob_identity(root_sha, relative)
            target = self.git_blob_identity(self.sha, relative)
            files.append(
                {
                    "path": relative,
                    "relation": (
                        "byte-identical"
                        if predecessor == target
                        else "changed"
                    ),
                    "predecessor": predecessor,
                    "target": target,
                }
            )
        changed_paths = list(
            ADOPTER.SOURCE_SUCCESSOR_ALLOWED_CHANGED_BLOBS
        )
        operation_id = "adopt-git-successor-test-0001"
        publication = installer._source_successor_publication_plan(
            operation_id,
            self.runtime / "state",
        )
        predecessor = {
            "authority_kind": root_authority["authority_kind"],
            "operation_id": root_authority["operation_id"],
            "source_sha": root_authority["source_sha"],
            "source_tree": root_authority["source_tree"],
            "authority_sha256": root_digest,
            "plan_sha256": root_authority["plan_sha256"],
            "permission_marker_sha256": root_authority[
                "permission_marker_sha256"
            ],
            "permission_evidence_sha256": root_authority[
                "permission_evidence_sha256"
            ],
            "permission_inventory_sha256": root_authority[
                "permission_inventory_sha256"
            ],
            "original_permissions_sha256": root_authority[
                "original_permissions_sha256"
            ],
            "hardened_permissions_sha256": root_authority[
                "hardened_permissions_sha256"
            ],
            "completed_journal_sha256": root_journal_digest,
            "source_trust_sha256": root_source_trust_sha256,
        }
        marker = {
            "path": str(installer.permission_marker_path),
            "raw_sha256": root_authority["permission_marker_sha256"],
            "evidence_sha256": root_authority[
                "permission_evidence_sha256"
            ],
            "inventory_sha256": root_authority[
                "permission_inventory_sha256"
            ],
            "original_permissions_sha256": root_authority[
                "original_permissions_sha256"
            ],
            "hardened_permissions_sha256": root_authority[
                "hardened_permissions_sha256"
            ],
        }
        records_by_path = {
            str(record["path"]): record for record in files
        }
        verifier_agreement = {
            "schema_version": 1,
            "policy": ADOPTER.SOURCE_SUCCESSOR_VERIFIER_POLICY,
            "candidate_execution": "forbidden-before-authority",
            "predecessor_source_sha": root_authority["source_sha"],
            "predecessor_source_tree": root_authority["source_tree"],
            "bootstrap": records_by_path[
                "scripts/bootstrap_pull_deploy.py"
            ],
            "git_source_trust": records_by_path[
                "scripts/git_source_trust.py"
            ],
            "ci_contract": records_by_path[
                "scripts/bridge_deploy_core.py"
            ],
            "required_jobs": delivery["ci"]["required_jobs"],
            "required_jobs_sha256": ADOPTER._canonical_digest(
                delivery["ci"]["required_jobs"]
            ),
        }
        production_source = {
            "source_sha": root_authority["production_source_sha"],
            "source_tree": root_authority["production_source_tree"],
        }
        production_source_trust_sha256 = (
            installer._production_source_trust(
                {"production_source": production_source}
            )
        )
        stable_projection = {
            "schema_version": 1,
            "policy": "nexpoly-production-repository-stable-projection-v1",
            "repository_root": str(self.production),
            "git_dir": str(self.production / ".git"),
            "object_dir": str(self.production / ".git/objects"),
            "index_path": str(self.production / ".git/index"),
            "source": {
                "sha": production_source["source_sha"],
                "tree": production_source["source_tree"],
                "branch": "refs/heads/main",
                "origin": None,
            },
            "git_binary": "/usr/bin/git",
            "local_config": [],
            "head": {"kind": "symbolic", "target": "refs/heads/main"},
            "index": {"version": 2, "entries": 1},
            "forbidden_markers_absent": True,
            "execution_environment": {},
        }
        logical_refs = [
            {
                "name": "refs/heads/main",
                "object_sha": production_source["source_sha"],
                "object_type": "commit",
                "symbolic_target": None,
            },
            {
                "name": ADOPTER.SOURCE_SUCCESSOR_DEPLOY_REMOTE_REF,
                "object_sha": root_authority["source_sha"],
                "object_type": "commit",
                "symbolic_target": None,
            },
        ]
        raw_refs = [
            {"path": "refs", "kind": "directory", "mode": "0700"}
        ]
        target_objects = [
            {"oid": self.sha, "type": "commit", "size": 123}
        ]
        repository_transition = {
            "schema_version": 1,
            "policy": (
                ADOPTER.SOURCE_SUCCESSOR_REPOSITORY_TRANSITION_POLICY
            ),
            "source": {
                "sha": production_source["source_sha"],
                "tree": production_source["source_tree"],
            },
            "target": {"sha": self.sha, "tree": target_tree},
            "baseline_evidence_sha256": (
                production_source_trust_sha256
            ),
            "stable_projection": stable_projection,
            "stable_projection_sha256": ADOPTER._canonical_digest(
                stable_projection
            ),
            "logical_refs": logical_refs,
            "logical_refs_sha256": ADOPTER._canonical_digest(logical_refs),
            "raw_ref_inventory": raw_refs,
            "raw_ref_inventory_sha256": ADOPTER._canonical_digest(raw_refs),
            "baseline_auxiliary_inventory": [],
            "baseline_auxiliary_inventory_sha256": (
                ADOPTER._canonical_digest([])
            ),
            "baseline_semantic_object_count": 0,
            "baseline_semantic_objects_sha256": ADOPTER._canonical_digest(
                []
            ),
            "baseline_only_object_count": 0,
            "baseline_only_objects_sha256": ADOPTER._canonical_digest([]),
            "target_reachable_object_count": 1,
            "target_reachable_objects_sha256": ADOPTER._canonical_digest(
                target_objects
            ),
            "expected_materialized_object_count": 1,
            "expected_materialized_objects_sha256": (
                ADOPTER._canonical_digest(target_objects)
            ),
            "mutable_refs": {
                "deploy_remote": ADOPTER.SOURCE_SUCCESSOR_DEPLOY_REMOTE_REF,
                "prepared_prefix": (
                    ADOPTER.SOURCE_SUCCESSOR_PREPARED_REF_PREFIX
                ),
            },
            "storage_policy": {
                "standalone": True,
                "promisor": False,
                "alternates": False,
                "replace_refs": 0,
            },
            "auxiliary_policy": (
                ADOPTER.SOURCE_SUCCESSOR_GIT_AUXILIARY_POLICY
            ),
            "object_storage_policy": (
                ADOPTER.SOURCE_SUCCESSOR_GIT_OBJECT_STORAGE_POLICY
            ),
            "object_materialization_policy": (
                "strict-fsck-owner-private-content-addressed-target-closure-v1"
            ),
        }
        repository_transition_sha256 = ADOPTER._canonical_digest(
            repository_transition
        )
        impact = {
            "schema_version": 1,
            "policy": ADOPTER.SOURCE_SUCCESSOR_IMPACT_POLICY,
            "predecessor_authority_sha256": root_digest,
            "predecessor_marker_sha256": root_authority[
                "permission_marker_sha256"
            ],
            "production_source_trust_sha256": (
                production_source_trust_sha256
            ),
            "production_repository_transition_sha256": (
                repository_transition_sha256
            ),
            "target": {
                "source_sha": self.sha,
                "source_tree": target_tree,
            },
            "files": files,
            "files_sha256": ADOPTER._canonical_digest(files),
            "changed_paths": changed_paths,
            "changed_paths_sha256": ADOPTER._canonical_digest(
                changed_paths
            ),
            "authority_publication": publication,
            "mutations": dict(ADOPTER.SOURCE_SUCCESSOR_MUTATIONS),
        }
        plan: dict[str, object] = {
            "schema_version": 1,
            "authority_kind": ADOPTER.SOURCE_SUCCESSOR_AUTHORITY_KIND,
            "policy": ADOPTER.SOURCE_SUCCESSOR_POLICY,
            "operation_id": operation_id,
            "source_sha": self.sha,
            "source_tree": target_tree,
            "source_readiness": readiness,
            "source_readiness_sha256": ADOPTER._canonical_digest(readiness),
            "delivery_gate": delivery,
            "delivery_gate_sha256": ADOPTER._canonical_digest(delivery),
            "adopted_deployment_sha256": root_authority[
                "adopted_deployment_sha256"
            ],
            "bootstrap_control_sha256": root_authority[
                "bootstrap_control_sha256"
            ],
            "adopted_prerequisites_sha256": root_authority[
                "adopted_prerequisites_sha256"
            ],
            "production_source_trust_sha256": (
                production_source_trust_sha256
            ),
            "production_repository_transition": repository_transition,
            "production_repository_transition_sha256": (
                repository_transition_sha256
            ),
            "production_source": production_source,
            "predecessor": predecessor,
            "marker": marker,
            "verifier_agreement": verifier_agreement,
            "files": files,
            "files_sha256": ADOPTER._canonical_digest(files),
            "changed_paths": changed_paths,
            "changed_paths_sha256": ADOPTER._canonical_digest(
                changed_paths
            ),
            "authority_publication": publication,
            "source_successor_impact": impact,
            "source_successor_impact_sha256": ADOPTER._canonical_digest(
                impact
            ),
            "mutations": dict(ADOPTER.SOURCE_SUCCESSOR_MUTATIONS),
        }
        completed_at = "2026-08-18T12:00:00Z"
        authority: dict[str, object] = {
            "schema_version": 1,
            "status": "completed",
            "authority_kind": ADOPTER.SOURCE_SUCCESSOR_AUTHORITY_KIND,
            "policy": ADOPTER.SOURCE_SUCCESSOR_POLICY,
            "operation_id": operation_id,
            "source_sha": self.sha,
            "source_tree": target_tree,
            "predecessor_source_sha": root_authority["source_sha"],
            "predecessor_source_tree": root_authority["source_tree"],
            "predecessor_authority_sha256": root_digest,
            "predecessor_marker_sha256": root_authority[
                "permission_marker_sha256"
            ],
            "adopted_deployment_sha256": root_authority[
                "adopted_deployment_sha256"
            ],
            "bootstrap_control_sha256": root_authority[
                "bootstrap_control_sha256"
            ],
            "adopted_prerequisites_sha256": root_authority[
                "adopted_prerequisites_sha256"
            ],
            "plan_sha256": ADOPTER._canonical_digest(plan),
            "source_successor_impact_sha256": plan[
                "source_successor_impact_sha256"
            ],
            "files_sha256": plan["files_sha256"],
            "changed_paths": changed_paths,
            "changed_paths_sha256": plan["changed_paths_sha256"],
            "delivery_gate": delivery,
            "delivery_gate_sha256": plan["delivery_gate_sha256"],
            "verifier_agreement_sha256": ADOPTER._canonical_digest(
                verifier_agreement
            ),
            "production_source_trust_sha256": (
                production_source_trust_sha256
            ),
            "production_repository_transition_sha256": (
                repository_transition_sha256
            ),
            "plan": plan,
            "completed_at": completed_at,
        }
        authority_path = (
            self.runtime / ADOPTER.SOURCE_SUCCESSOR_AUTHORITY_PATH
        )
        _write_private(
            authority_path,
            ADOPTER._canonical_bytes(authority) + b"\n",
            0o600,
        )
        transaction_root = (
            self.runtime / ADOPTER.SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY
        )
        transaction_root.mkdir(mode=0o700)
        transaction_root.chmod(0o700)
        journal = {
            "schema_version": 1,
            "status": "completed",
            "phase": "completed",
            "operation_id": operation_id,
            "plan": plan,
            "plan_sha256": authority["plan_sha256"],
            "source_successor_impact_sha256": authority[
                "source_successor_impact_sha256"
            ],
            "production_source_trust_sha256": (
                production_source_trust_sha256
            ),
            "created_at": "2026-08-18T11:59:00Z",
            "completed_at": completed_at,
            "aborted_at": None,
        }
        journal_path = transaction_root / f"{operation_id}.json"
        _write_private(
            journal_path,
            ADOPTER._canonical_bytes(journal) + b"\n",
            0o600,
        )
        successor_digest = ADOPTER._file_digest(
            authority_path,
            mode=0o600,
        )
        self.source_successor_fixture = {
            "authority": authority,
            "authority_path": authority_path,
            "journal": journal,
            "journal_path": journal_path,
            "root_authority": root_authority,
            "root_digest": root_digest,
            "root_journal": root_journal,
            "root_journal_path": root_journal_path,
            "root_journal_digest": root_journal_digest,
            "root_source_trust_sha256": root_source_trust_sha256,
            "successor_digest": successor_digest,
            "repository_transition": repository_transition,
            "repository_transition_sha256": (
                repository_transition_sha256
            ),
        }
        return installer, self.source_successor_fixture

    def rewrite_source_successor_fixture(
        self,
        authority: dict[str, object],
    ) -> None:
        fixture = self.source_successor_fixture
        authority["plan_sha256"] = ADOPTER._canonical_digest(
            authority["plan"]
        )
        _write_private(
            fixture["authority_path"],
            ADOPTER._canonical_bytes(authority) + b"\n",
            0o600,
        )
        journal = json.loads(json.dumps(fixture["journal"]))
        journal["plan"] = authority["plan"]
        journal["plan_sha256"] = authority["plan_sha256"]
        journal["source_successor_impact_sha256"] = authority[
            "source_successor_impact_sha256"
        ]
        journal["production_source_trust_sha256"] = authority[
            "production_source_trust_sha256"
        ]
        _write_private(
            fixture["journal_path"],
            ADOPTER._canonical_bytes(journal) + b"\n",
            0o600,
        )

    def prepare_abort_residue(
        self,
    ):  # type: ignore[no-untyped-def]
        installer = self.unit_permission_installer()
        operation_id = "deploy-prepare-abort-test"
        operation = self.runtime / "state/prepared" / operation_id
        archive = (
            self.runtime
            / "state/prepare-aborts/archives"
            / operation_id
        )
        operation.mkdir(mode=0o700, parents=True)
        archive.mkdir(mode=0o700, parents=True)
        for directory in (
            self.runtime / "state/prepared",
            operation,
            self.runtime / "state/prepare-aborts",
            self.runtime / "state/prepare-aborts/archives",
            archive,
        ):
            directory.chmod(0o700)
        created_at = "2026-08-14T18:20:52Z"
        prepare_owner = {
            "schema_version": 1,
            "operation_id": operation_id,
            "target_sha": self.sha,
            "controller_sha256": "sha256:" + "3" * 64,
            "created_at": "2026-08-14T18:06:48Z",
        }
        prepare_owner_sha256 = ADOPTER._canonical_digest(prepare_owner)
        for name in (
            "wheel-staging",
            "wheel-staging-tombstones",
            "monomer-dft-runtime",
        ):
            (archive / name).mkdir(mode=0o700)
        _write_private(
            archive / "ARCHIVE-OWNER.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "prepare_owner_sha256": prepare_owner_sha256,
                    "created_at": created_at,
                },
                sort_keys=True,
            ).encode()
            + b"\n",
            0o600,
        )
        journal = {
            "schema_version": 1,
            "operation_id": operation_id,
            "status": "aborting",
            "phase": "operation-archive-intent",
            "prepare_owner": prepare_owner,
            "prepare_owner_sha256": prepare_owner_sha256,
            "target_sha": self.sha,
            "target_tree": None,
            "control_handoff_schema_version": None,
            "executor_control_sha256": None,
            "archive_path": str(archive),
            "archive_inventory_sha256": None,
            "operation_inventory_sha256": (
                ADOPTER._private_tree_inventory_digest(operation)
            ),
            "descriptor_sha256": None,
            "control_handoff_sha256": None,
            "prepare_staging": {
                "live_inventory_sha256": None,
                "tombstone_inventory_sha256": None,
            },
            "wheel_staging": [],
            "dft_staging": {
                "staging_inventory_sha256": None,
                "cache_inventory_sha256": None,
                "incomplete_release_inventory_sha256": None,
                "ready_sha256": None,
                "ready_runtime_inventory_sha256": None,
                "ready_owner_sha256": None,
            },
            "owned_slots": [],
            "prepared_ref": {
                "name": f"refs/nexpoly/prepared/{operation_id}",
                "target_sha": None,
            },
            "current_state_sha256": None,
            "active_control_sha256": "sha256:" + "4" * 64,
            "active_slot_sha256": None,
            "active_slot": None,
            "bridge_token_sha256": None,
            "bridge_token_operation_id": None,
            "bridge_token_status": None,
            "created_at": created_at,
            "completed_at": None,
        }
        _write_private(
            self.runtime
            / "state/prepare-aborts"
            / f"{operation_id}.json",
            json.dumps(journal, sort_keys=True).encode() + b"\n",
            0o600,
        )
        return installer, archive

    def grow_unit_parent_on_staging(
        self,
        installer: ADOPTER.UnitPermissionHardeningInstaller,
    ):  # type: ignore[no-untyped-def]
        """Force the replacement transaction across a directory-size boundary."""

        original = installer._repair_or_create_exact_file_at
        staging_name, _retired_name = installer._replacement_names(
            self.unit_operation_id
        )
        growth: dict[str, int] = {}

        def grow(
            directory_fd: int,
            name: str,
            payload: bytes,
            **kwargs,
        ):  # type: ignore[no-untyped-def]
            if name == staging_name and not growth:
                transaction = json.loads(
                    installer._unit_transaction_path(
                        self.unit_operation_id
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    transaction["replacement_checkpoint"],
                    "staging-create-intent",
                )
                before = self.unit_parent.stat().st_size
                created: list[Path] = []
                expanded = before
                for index in range(4096):
                    path = self.unit_parent / (
                        f".nexpoly-parent-growth-{index:05d}-"
                        + "x" * 64
                    )
                    _write_private(path, b"x\n", 0o600)
                    created.append(path)
                    expanded = self.unit_parent.stat().st_size
                    if expanded > before:
                        break
                self.assertGreater(expanded, before)
                for path in created:
                    path.unlink()
                after_cleanup = self.unit_parent.stat().st_size
                # ext4 retains allocated directory blocks after unlink; this
                # is the production failure mode exercised by these tests.
                self.assertGreater(after_cleanup, before)
                growth.update(
                    before=before,
                    expanded=expanded,
                    after_cleanup=after_cleanup,
                )
            return original(
                directory_fd,
                name,
                payload,
                **kwargs,
            )

        return mock.patch.object(
            installer,
            "_repair_or_create_exact_file_at",
            side_effect=grow,
        ), growth

    def partial_exact_write_fault(
        self,
        installer: ADOPTER.UnitPermissionHardeningInstaller,
        *,
        target_name: str,
        fault_point: str,
    ):  # type: ignore[no-untyped-def]
        """Leave an exact expected prefix at one deterministic O_EXCL name."""

        original = installer._write_exact_file_at
        faulted = False

        def write(
            directory_fd: int,
            name: str,
            payload: bytes,
            *,
            mode: int,
        ) -> None:
            nonlocal faulted
            if name != target_name or faulted:
                original(directory_fd, name, payload, mode=mode)
                return
            faulted = True
            descriptor = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                mode,
                dir_fd=directory_fd,
            )
            try:
                prefix = payload[: max(1, len(payload) // 2)]
                self.assertEqual(os.write(descriptor, prefix), len(prefix))
                if fault_point in {"file-fsync", "parent-fsync"}:
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if fault_point == "parent-fsync":
                os.fsync(directory_fd)
            raise RuntimeError(f"{fault_point} fault at {target_name}")

        return mock.patch.object(
            installer,
            "_write_exact_file_at",
            side_effect=write,
        ), lambda: faulted

    def journal_parent_fsync_lost_response(
        self,
        installer,  # type: ignore[no-untyped-def]
        *,
        writer_name: str,
        predicate,  # type: ignore[no-untyped-def]
    ):  # type: ignore[no-untyped-def]
        """Raise after the atomic writer, including its parent fsync, ran."""

        durable_write = getattr(installer, writer_name)
        lost: dict[str, object] = {}

        def write(document: dict[str, object]) -> None:
            durable_write(document)
            if not lost and predicate(document):
                lost["document"] = json.loads(json.dumps(document))
                raise RuntimeError("journal parent fsync response lost")

        return mock.patch.object(
            installer,
            writer_name,
            side_effect=write,
        ), lost

    def assert_unit_backup_partial_fault_replays(
        self,
        *,
        target_name: str,
        fault_point: str,
    ) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        installer = self.unit_permission_installer()
        fault_patch, did_fault = self.partial_exact_write_fault(
            installer,
            target_name=target_name,
            fault_point=fault_point,
        )
        with fault_patch, self.assertRaisesRegex(
            RuntimeError,
            f"{fault_point} fault",
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertTrue(did_fault())
        durable = json.loads(
            installer._unit_transaction_path(
                self.unit_operation_id
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            durable["replacement_checkpoint"],
            "backup-create-intent",
        )
        self.assertIsNone(durable["backup"])
        runtime_before = _inventory(self.runtime)
        units_before = _inventory(self.unit_parent)
        replay = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        self.assertEqual(replay["plan_sha256"], planned["plan_sha256"])
        self.assertEqual(_inventory(self.runtime), runtime_before)
        self.assertEqual(_inventory(self.unit_parent), units_before)
        authority = self.unit_permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o600)

    def assert_unit_checkpoint_replays(self, checkpoint: str) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        expected_processes = json.loads(json.dumps(self.unit_processes))
        crashed = False

        def crash(phase: str) -> None:
            nonlocal crashed
            if phase == checkpoint and not crashed:
                crashed = True
                raise RuntimeError(f"crash at {checkpoint}")

        with self.assertRaisesRegex(RuntimeError, f"crash at {checkpoint}"):
            self.unit_permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertTrue(crashed)
        authority = self.unit_permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["status"], "completed")
        for index, role in enumerate(
            (ADOPTER.MD_UNIT_NAME, ADOPTER.DFT_UNIT_NAME)
        ):
            expected = expected_processes[role]
            self.assertEqual(
                authority["original_units"][index]["process_identity"],
                expected,
            )
            self.assertEqual(
                authority["hardened_units"][index]["process_identity"],
                expected,
            )
        self.assertEqual(
            self.unit_permission_installer().apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            ),
            authority,
        )

    @staticmethod
    def unit_authority_publication_paths(
        plan: dict[str, object],
    ) -> dict[str, Path]:
        publication = plan["authority_publication"]
        assert isinstance(publication, dict)
        entries = publication["entries"]
        assert isinstance(entries, list)
        return {
            str(entry["role"]): Path(str(entry["path"]))
            for entry in entries
            if isinstance(entry, dict)
        }

    @staticmethod
    def unit_file_identity(path: Path) -> tuple[int, ...]:
        metadata = path.stat(follow_symlinks=False)
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_mode,
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    def assert_unit_authority_preplant_after_intent_fails(
        self,
        role: str,
        *,
        extra_hard_link: bool = False,
    ) -> None:
        installer = self.unit_permission_installer()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        md_identity = self.unit_file_identity(self.md_unit)
        dft_identity = self.unit_file_identity(self.dft_unit)
        md_payload = self.md_unit.read_bytes()
        dft_payload = self.dft_unit.read_bytes()

        def crash(phase: str) -> None:
            if phase == "unit-permission-intent":
                raise RuntimeError("crash after unit permission intent")

        with self.assertRaisesRegex(RuntimeError, "after unit permission intent"):
            self.unit_permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        transaction = installer._load_unit_transaction(
            self.unit_operation_id
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertEqual(transaction["phase"], "intent")
        self.assertEqual(self.unit_reload_calls, 0)
        paths = self.unit_authority_publication_paths(planned["plan"])
        preplant = paths[role]
        if role == "final":
            # This deliberately satisfies the old shallow same-operation
            # discriminator while omitting the complete sealed authority.
            weak_authority = {
                "schema_version": 1,
                "operation_id": self.unit_operation_id,
                "backup": {
                    "schema_version": 1,
                    "owner": {"schema_version": 1},
                },
            }
            payload = ADOPTER._canonical_bytes(weak_authority) + b"\n"
        else:
            payload = b'{"schema_version":1'
        _write_private(preplant, payload, 0o600)
        if extra_hard_link:
            os.link(
                preplant,
                self.runtime / "state/.unowned-unit-authority-link",
            )

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            (
                "unit permission authority schema is invalid"
                if role == "final"
                else "publication namespace predates commit intent"
            ),
        ):
            self.unit_permission_installer().apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertEqual(self.unit_file_identity(self.md_unit), md_identity)
        self.assertEqual(self.unit_file_identity(self.dft_unit), dft_identity)
        self.assertEqual(self.md_unit.read_bytes(), md_payload)
        self.assertEqual(self.dft_unit.read_bytes(), dft_payload)
        self.assertEqual(self.unit_reload_calls, 0)
        self.assertEqual(
            installer._load_unit_transaction(self.unit_operation_id),
            transaction,
        )

    def assert_unit_authority_commit_residue_replays(
        self,
        residue: str,
    ) -> None:
        installer = self.unit_permission_installer()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        processes = json.loads(json.dumps(self.unit_processes))
        dft_identity = self.unit_file_identity(self.dft_unit)
        dft_payload = self.dft_unit.read_bytes()

        def first_crash(phase: str) -> None:
            if phase == "unit-permission-authority-commit-intent":
                raise RuntimeError("first power loss at authority commit intent")

        with self.assertRaisesRegex(RuntimeError, "first power loss"):
            self.unit_permission_installer(first_crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        transaction = installer._load_unit_transaction(
            self.unit_operation_id
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertEqual(transaction["phase"], "authority-commit-intent")
        self.assertEqual(transaction["status"], "applying")
        expected_authority = installer._unit_authority(transaction)
        expected_payload = ADOPTER._canonical_bytes(expected_authority) + b"\n"
        paths = self.unit_authority_publication_paths(transaction["plan"])
        final_path = paths["final"]
        staging_path = paths["staging"]
        quarantine_path = paths["staging-quarantine"]
        if residue == "staging-prefix":
            _write_private(
                staging_path,
                expected_payload[: max(1, len(expected_payload) // 2)],
                0o600,
            )
        elif residue == "quarantine-prefix":
            _write_private(
                quarantine_path,
                expected_payload[: max(1, len(expected_payload) // 2)],
                0o600,
            )
        elif residue == "linked-final":
            _write_private(staging_path, expected_payload, 0o600)
            os.link(staging_path, final_path)
        else:  # pragma: no cover - test helper contract
            self.fail(f"unknown publication residue: {residue}")
        residue_before_second_loss = {
            role: (
                self.unit_file_identity(path),
                path.read_bytes(),
            )
            for role, path in paths.items()
            if path.exists()
        }
        hardened_md_identity = self.unit_file_identity(self.md_unit)
        self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o600)
        self.assertEqual(self.unit_reload_calls, 1)

        resealed = False

        def second_crash(phase: str) -> None:
            nonlocal resealed
            if phase == "unit-permission-journal-resealed" and not resealed:
                resealed = True
                raise RuntimeError("second power loss after journal reseal")

        with self.assertRaisesRegex(RuntimeError, "second power loss"):
            self.unit_permission_installer(second_crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertTrue(resealed)
        self.assertEqual(
            {
                role: (
                    self.unit_file_identity(path),
                    path.read_bytes(),
                )
                for role, path in paths.items()
                if path.exists()
            },
            residue_before_second_loss,
        )
        self.assertEqual(
            installer._load_unit_transaction(self.unit_operation_id),
            transaction,
        )
        self.assertEqual(self.unit_file_identity(self.md_unit), hardened_md_identity)
        self.assertEqual(self.unit_reload_calls, 1)

        authority = self.unit_permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority, expected_authority)
        self.assertEqual(final_path.read_bytes(), expected_payload)
        self.assertEqual(final_path.stat().st_nlink, 1)
        self.assertFalse(staging_path.exists())
        self.assertFalse(quarantine_path.exists())
        self.assertEqual(self.unit_file_identity(self.md_unit), hardened_md_identity)
        self.assertEqual(self.unit_file_identity(self.dft_unit), dft_identity)
        self.assertEqual(self.dft_unit.read_bytes(), dft_payload)
        self.assertEqual(self.unit_reload_calls, 1)
        for index, unit_name in enumerate(
            (ADOPTER.MD_UNIT_NAME, ADOPTER.DFT_UNIT_NAME)
        ):
            self.assertEqual(
                authority["hardened_units"][index]["process_identity"],
                processes[unit_name],
            )

    def advance_remote_tracking_ref(self) -> str:
        environment = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Prerequisite Test",
            "GIT_AUTHOR_EMAIL": "prerequisite@example.invalid",
            "GIT_COMMITTER_NAME": "Prerequisite Test",
            "GIT_COMMITTER_EMAIL": "prerequisite@example.invalid",
        }
        advanced = subprocess.run(
            [
                "/usr/bin/git",
                "commit-tree",
                f"{self.sha}^{{tree}}",
                "-p",
                self.sha,
                "-m",
                "advanced protected main",
            ],
            cwd=self.source,
            env=environment,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()
        _run(
            self.source,
            "/usr/bin/git",
            "update-ref",
            "refs/remotes/origin/main",
            advanced,
        )
        _make_tree_private(self.source)
        return advanced

    def test_plan_is_zero_write_and_deterministic(self) -> None:
        runtime_before = _inventory(self.runtime)
        source_before = _inventory(self.source)
        git_environments: list[dict[str, str]] = []
        original_run = subprocess.run

        def capture_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            command = args[0] if args else kwargs.get("args")
            environment = kwargs.get("env")
            if isinstance(command, list) and "/usr/bin/git" in command and environment:
                git_environments.append(dict(environment))
            return original_run(*args, **kwargs)

        with mock.patch.object(subprocess, "run", side_effect=capture_run):
            first = self.installer().plan(
                source_sha=self.sha, operation_id=self.operation_id
            )
            second = self.installer().plan(
                source_sha=self.sha, operation_id=self.operation_id
            )

        self.assertEqual(first, second)
        self.assertEqual(_inventory(self.runtime), runtime_before)
        self.assertEqual(_inventory(self.source), source_before)
        self.assertTrue(git_environments)
        self.assertTrue(
            all(environment.get("GIT_OPTIONAL_LOCKS") == "0" for environment in git_environments)
        )
        self.assertFalse(first["apply"])
        self.assertIs(first["logical_zero_write"], True)
        self.assertIsInstance(first["atime_zero_write"], bool)
        self.assertEqual(len(first["plan"]["files"]), 10)
        self.assertEqual(
            first["plan"]["preserved_pgpass"]["sha256"],
            "sha256:" + hashlib.sha256(self.pgpass_payload).hexdigest(),
        )
        self.assertNotIn("secret", json.dumps(first))
        self.assertEqual(first["plan"]["delivery_gate"], self.delivery_gate)
        self.assertEqual(first["plan"]["source_readiness"]["ready"], True)

    def test_plan_atime_claim_is_conservative_and_explicit(self) -> None:
        with mock.patch.object(
            ADOPTER, "_mount_suppresses_atime", side_effect=[True, False]
        ):
            unproven = self.installer().plan(
                source_sha=self.sha,
                operation_id=self.operation_id,
            )
        with mock.patch.object(
            ADOPTER, "_mount_suppresses_atime", return_value=True
        ):
            proven = self.installer().plan(
                source_sha=self.sha,
                operation_id=self.operation_id,
            )

        self.assertIs(unproven["logical_zero_write"], True)
        self.assertIs(unproven["atime_zero_write"], False)
        self.assertIs(proven["atime_zero_write"], True)
        self.assertEqual(unproven["plan"], proven["plan"])
        self.assertEqual(unproven["plan_sha256"], proven["plan_sha256"])

    def test_dirty_git_filter_is_rejected_before_any_filter_execution(self) -> None:
        marker = self.source / "filter-executed"
        attributes = self.source / ".gitattributes"
        attributes.write_text("ops/config/bootstrap-quiesce.example filter=evil\n")
        attributes.chmod(0o600)
        _run(
            self.source,
            "/usr/bin/git",
            "config",
            "filter.evil.clean",
            f"/usr/bin/touch {marker}",
        )

        with mock.patch.object(
            ADOPTER.subprocess,
            "run",
            side_effect=AssertionError("Git ran before the pure source gate"),
        ) as git_run:
            with self.assertRaisesRegex(
                ADOPTER.PrerequisiteError,
                "executable or unsupported Git policy|executable Git attribute",
            ):
                self.installer().plan(
                    source_sha=self.sha,
                    operation_id=self.operation_id,
                )

        git_run.assert_not_called()
        self.assertFalse(marker.exists())

    def test_source_policy_is_regated_before_delivery_contract_git(self) -> None:
        clean = self.installer().plan(
            source_sha=self.sha,
            operation_id=self.operation_id,
        )
        marker = self.source / "delivery-filter-executed"
        delivery_called = False

        def mutate_after_readiness(
            _source_root: Path,
            _source_sha: str,
        ) -> dict[str, object]:
            attributes = self.source / ".gitattributes"
            attributes.write_text(
                "ops/config/bootstrap-quiesce.example filter=delivery-evil\n"
            )
            attributes.chmod(0o600)
            _run(
                self.source,
                "/usr/bin/git",
                "config",
                "filter.delivery-evil.clean",
                f"/usr/bin/touch {marker}",
            )
            return json.loads(json.dumps(clean["plan"]["source_readiness"]))

        def forbidden_delivery(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            nonlocal delivery_called
            delivery_called = True
            return json.loads(json.dumps(self.delivery_gate))

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "executable or unsupported Git policy|executable Git attribute",
        ):
            self.installer(
                delivery_probe=forbidden_delivery,
                source_readiness_probe=mutate_after_readiness,
            ).plan(
                source_sha=self.sha,
                operation_id=self.operation_id,
            )

        self.assertFalse(delivery_called)
        self.assertFalse(marker.exists())

    def test_apply_is_create_only_idempotent_and_preserves_pgpass(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        applied = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        first_inventory = _inventory(self.runtime)
        replayed = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )

        self.assertEqual(replayed, applied)
        self.assertEqual(_inventory(self.runtime), first_inventory)
        self.assertEqual(
            (self.runtime / "config/mutable-data-audit.pgpass").read_bytes(),
            self.pgpass_payload,
        )

        self.assertEqual(applied["authority_kind"], ADOPTER.AUTHORITY_KIND)
        for record in applied["plan"]["files"]:
            target = Path(record["destination"])
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), int(record["mode"], 8))
            self.assertEqual(ADOPTER._file_digest(target), record["sha256"])
        self.assertEqual(
            self.installer().plan(
                source_sha=self.sha, operation_id=self.operation_id
            ),
            planned,
        )

    def test_permission_plan_is_zero_write_and_apply_binds_marker(self) -> None:
        installer = self.permission_installer()
        production_before = _inventory(self.production)
        runtime_before = _inventory(self.runtime)
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
        )

        self.assertEqual(_inventory(self.production), production_before)
        self.assertEqual(_inventory(self.runtime), runtime_before)
        self.assertTrue(planned["logical_zero_write"])
        self.assertEqual(
            planned["permission_impact_sha256"],
            planned["plan"]["permission_impact_sha256"],
        )
        self.assertEqual(
            planned["plan"]["production_source"],
            {
                "source_sha": self.production_sha,
                "source_tree": self.production_tree,
            },
        )
        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "impact confirmation differs",
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256="sha256:" + "0" * 64,
            )
        self.assertFalse(installer.permission_marker_path.exists())

        authority = installer.apply(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_permission_impact_sha256=planned[
                "permission_impact_sha256"
            ],
        )
        marker = ADOPTER.GIT_SOURCE_TRUST.verify_repository_permission_takeover(
            self.production,
            installer.permission_marker_path,
        )
        self.assertEqual(authority["status"], "completed")
        self.assertEqual(
            authority["authority_kind"],
            ADOPTER.PERMISSION_AUTHORITY_KIND,
        )
        self.assertEqual(
            authority["permission_marker_sha256"],
            ADOPTER._file_digest(installer.permission_marker_path, mode=0o600),
        )
        self.assertEqual(
            authority["permission_evidence_sha256"],
            marker["evidence_sha256"],
        )
        self.assertEqual(
            authority["permission_inventory_sha256"],
            marker["inventory_sha256"],
        )
        self.assertEqual(stat.S_IMODE(self.production.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(
                (self.production / "tracked.txt").stat().st_mode
            ),
            0o644,
        )
        self.assertEqual(
            installer.apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            ),
            authority,
        )

    def test_source_successor_unit_plan_and_authority_bind_both_roots(
        self,
    ) -> None:
        installer, fixture = self.seed_source_successor_authority(
            root_journal_source_trust_sha256="sha256:" + "f" * 64,
        )

        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        plan = planned["plan"]
        successor = plan["git_permission_successor"]
        self.assertEqual(plan["schema_version"], 2)
        self.assertEqual(successor["schema_version"], 2)
        self.assertEqual(
            successor["mode"],
            "protected-main-ci-exact-target",
        )
        self.assertEqual(
            plan["adopted_git_permissions_sha256"],
            fixture["root_digest"],
        )
        self.assertEqual(
            plan[
                "adopted_git_permission_source_successor_sha256"
            ],
            fixture["successor_digest"],
        )
        self.assertEqual(
            successor["root_authority"]["raw_sha256"],
            fixture["root_digest"],
        )
        self.assertEqual(
            successor["source_successor_authority"][
                "authority_file_sha256"
            ],
            fixture["successor_digest"],
        )
        self.assertEqual(
            successor["source_successor_authority"][
                "production_repository_transition"
            ],
            fixture["repository_transition"],
        )
        self.assertEqual(
            successor["source_successor_authority"][
                "production_repository_transition_sha256"
            ],
            fixture["repository_transition_sha256"],
        )
        self.assertEqual(
            fixture["authority"]["plan"]["predecessor"][
                "completed_journal_sha256"
            ],
            fixture["root_journal_digest"],
        )
        self.assertEqual(
            fixture["authority"]["plan"]["predecessor"][
                "source_trust_sha256"
            ],
            fixture["root_source_trust_sha256"],
        )
        self.assertEqual(
            {
                fixture["authority"][
                    "production_source_trust_sha256"
                ],
                fixture["authority"]["plan"][
                    "production_source_trust_sha256"
                ],
                fixture["authority"]["plan"][
                    "source_successor_impact"
                ]["production_source_trust_sha256"],
                fixture["journal"][
                    "production_source_trust_sha256"
                ],
            },
            {
                fixture["authority"][
                    "production_source_trust_sha256"
                ]
            },
        )
        self.assertNotEqual(
            fixture["root_source_trust_sha256"],
            fixture["authority"]["production_source_trust_sha256"],
        )
        self.assertEqual(
            successor["target"],
            {
                "source_sha": self.sha,
                "source_tree": plan["source_tree"],
            },
        )
        self.assertEqual(
            len(successor["files"]),
            len(ADOPTER.UNIT_PERMISSION_SUCCESSOR_V2_BLOBS),
        )
        self.assertEqual(len(successor["files"]), 13)

        authority = installer.apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["schema_version"], 2)
        self.assertEqual(authority["plan"]["schema_version"], 2)
        self.assertEqual(
            authority["adopted_git_permissions_sha256"],
            fixture["root_digest"],
        )
        self.assertEqual(
            authority[
                "adopted_git_permission_source_successor_sha256"
            ],
            fixture["successor_digest"],
        )
        self.assertEqual(
            authority["plan"]["git_permission_successor"],
            successor,
        )
        replanned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        self.assertEqual(replanned, planned)
        self.assertEqual(
            installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            ),
            authority,
        )

    def test_source_successor_v2_intent_crash_resumes(self) -> None:
        installer, _fixture = self.seed_source_successor_authority()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )

        def crash(phase: str) -> None:
            if phase == "unit-permission-intent":
                raise RuntimeError("crash after v2 unit intent")

        with self.assertRaisesRegex(RuntimeError, "after v2 unit intent"):
            self.unit_permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        transaction = installer._load_unit_transaction(
            self.unit_operation_id
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertEqual(transaction["schema_version"], 2)
        self.assertEqual(transaction["plan"]["schema_version"], 2)
        self.assertEqual(transaction["phase"], "intent")

        authority = self.unit_permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["schema_version"], 2)
        self.assertEqual(
            self.unit_permission_installer().plan(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
            ),
            planned,
        )

    def test_unit_transaction_inventory_rejects_v2_schema_drift(self) -> None:
        installer, _fixture = self.seed_source_successor_authority()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )

        def crash(phase: str) -> None:
            if phase == "unit-permission-intent":
                raise RuntimeError("crash after v2 inventory intent")

        with self.assertRaisesRegex(RuntimeError, "v2 inventory intent"):
            self.unit_permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        transaction_path = installer._unit_transaction_path(
            self.unit_operation_id
        )
        baseline = json.loads(transaction_path.read_text(encoding="utf-8"))

        def reseal(document: dict[str, object]) -> None:
            plan = document["plan"]
            document["plan_sha256"] = ADOPTER._canonical_digest(plan)
            document["unit_permission_impact_sha256"] = plan[
                "unit_permission_impact_sha256"
            ]
            _write_private(
                transaction_path,
                ADOPTER._canonical_bytes(document) + b"\n",
                0o600,
            )

        def top_bool(document: dict[str, object]) -> None:
            document["schema_version"] = True

        def top_float(document: dict[str, object]) -> None:
            document["schema_version"] = 2.0

        def plan_bool(document: dict[str, object]) -> None:
            document["plan"]["schema_version"] = True

        def plan_float(document: dict[str, object]) -> None:
            document["plan"]["schema_version"] = 2.0

        def mismatch(document: dict[str, object]) -> None:
            document["schema_version"] = 1

        def successor_bool(document: dict[str, object]) -> None:
            document["plan"]["git_permission_successor"][
                "schema_version"
            ] = True

        def successor_float(document: dict[str, object]) -> None:
            document["plan"]["git_permission_successor"][
                "schema_version"
            ] = 2.0

        def missing_successor_digest(document: dict[str, object]) -> None:
            document["plan"].pop(
                "adopted_git_permission_source_successor_sha256"
            )

        def extra_successor_field(document: dict[str, object]) -> None:
            successor = document["plan"]["git_permission_successor"]
            successor["unexpected_source_successor"] = True
            body = dict(successor)
            body.pop("identity_sha256")
            successor["identity_sha256"] = ADOPTER._canonical_digest(body)

        def missing_repository_transition(
            document: dict[str, object],
        ) -> None:
            successor = document["plan"]["git_permission_successor"]
            compact = successor["source_successor_authority"]
            compact.pop("production_repository_transition")
            compact_body = dict(compact)
            compact_body.pop("identity_sha256")
            compact["identity_sha256"] = ADOPTER._canonical_digest(
                compact_body
            )
            successor_body = dict(successor)
            successor_body.pop("identity_sha256")
            successor["identity_sha256"] = ADOPTER._canonical_digest(
                successor_body
            )

        variants = (
            ("top-bool", top_bool),
            ("top-float", top_float),
            ("plan-bool", plan_bool),
            ("plan-float", plan_float),
            ("top-plan-mismatch", mismatch),
            ("successor-bool", successor_bool),
            ("successor-float", successor_float),
            ("missing-successor-digest", missing_successor_digest),
            ("extra-successor-field", extra_successor_field),
            ("missing-repository-transition", missing_repository_transition),
        )
        for label, mutate in variants:
            with self.subTest(schema_drift=label):
                transaction = json.loads(json.dumps(baseline))
                mutate(transaction)
                reseal(transaction)
                with self.assertRaisesRegex(
                    ADOPTER.PrerequisiteError,
                    "transaction inventory is invalid",
                ):
                    installer._assert_unit_exclusive(
                        self.unit_operation_id
                    )

    def test_unit_transaction_inventory_rejects_v1_successor_extension(
        self,
    ) -> None:
        installer = self.unit_permission_installer()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )

        def crash(phase: str) -> None:
            if phase == "unit-permission-intent":
                raise RuntimeError("crash after v1 inventory intent")

        with self.assertRaisesRegex(RuntimeError, "v1 inventory intent"):
            self.unit_permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        transaction_path = installer._unit_transaction_path(
            self.unit_operation_id
        )
        transaction = json.loads(
            transaction_path.read_text(encoding="utf-8")
        )
        transaction["plan"][
            "adopted_git_permission_source_successor_sha256"
        ] = "sha256:" + "0" * 64
        transaction["plan_sha256"] = ADOPTER._canonical_digest(
            transaction["plan"]
        )
        _write_private(
            transaction_path,
            ADOPTER._canonical_bytes(transaction) + b"\n",
            0o600,
        )

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "transaction inventory is invalid",
        ):
            installer._assert_unit_exclusive(self.unit_operation_id)

    def test_source_successor_missing_final_fails_without_v1_fallback(
        self,
    ) -> None:
        installer, fixture = self.seed_source_successor_authority()
        fixture["authority_path"].unlink()

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "source-successor|source successor|private JSON is unavailable",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
            )

    def test_source_successor_residue_fails_without_v1_fallback(self) -> None:
        installer, fixture = self.seed_source_successor_authority()
        staging = (
            self.runtime
            / "state"
            / (
                f".{ADOPTER.SOURCE_SUCCESSOR_AUTHORITY_PATH.name}"
                f".create-{fixture['authority']['operation_id']}"
            )
        )
        _write_private(staging, b"{}\n", 0o600)

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "lineage contains publication residue",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
            )

    def test_source_successor_tamper_fails_without_v1_fallback(self) -> None:
        installer, fixture = self.seed_source_successor_authority()
        authority = json.loads(json.dumps(fixture["authority"]))
        authority["unexpected"] = True
        self.rewrite_source_successor_fixture(authority)

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "source successor authority has an invalid shape",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
            )

    def test_source_successor_noncanonical_authority_raw_is_rejected(
        self,
    ) -> None:
        installer, fixture = self.seed_source_successor_authority()
        _write_private(
            fixture["authority_path"],
            json.dumps(
                fixture["authority"],
                sort_keys=True,
                indent=2,
            ).encode("utf-8")
            + b"\n",
            0o600,
        )

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "source successor authority has an invalid shape",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
            )

    def test_source_successor_completed_journal_raw_and_shape_are_exact(
        self,
    ) -> None:
        installer, fixture = self.seed_source_successor_authority()
        baseline = fixture["journal"]
        variants = []
        variants.append(
            (
                "noncanonical-raw",
                json.dumps(
                    baseline,
                    sort_keys=True,
                    indent=2,
                ).encode("utf-8")
                + b"\n",
            )
        )
        extra_field = json.loads(json.dumps(baseline))
        extra_field["unexpected"] = True
        variants.append(
            (
                "extra-field",
                ADOPTER._canonical_bytes(extra_field) + b"\n",
            )
        )

        for label, payload in variants:
            with self.subTest(journal=label):
                _write_private(fixture["journal_path"], payload, 0o600)
                with self.assertRaisesRegex(
                    ADOPTER.PrerequisiteError,
                    "source successor completed journal differs",
                ):
                    installer.plan(
                        source_sha=self.sha,
                        operation_id=self.unit_operation_id,
                    )

    def test_source_successor_completed_journal_time_matches_authority(
        self,
    ) -> None:
        installer, fixture = self.seed_source_successor_authority()
        journal = json.loads(json.dumps(fixture["journal"]))
        journal["completed_at"] = "2026-08-18T12:00:01Z"
        _write_private(
            fixture["journal_path"],
            ADOPTER._canonical_bytes(journal) + b"\n",
            0o600,
        )

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "source successor completed journal differs",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
            )

    def test_source_successor_completed_journal_time_is_ordered(
        self,
    ) -> None:
        installer, fixture = self.seed_source_successor_authority()
        journal = json.loads(json.dumps(fixture["journal"]))
        journal["created_at"] = "2026-08-18T12:00:01Z"
        _write_private(
            fixture["journal_path"],
            ADOPTER._canonical_bytes(journal) + b"\n",
            0o600,
        )

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "source successor completed journal differs",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
            )

    def test_source_successor_timestamps_are_real_canonical_utc(self) -> None:
        installer, fixture = self.seed_source_successor_authority()
        baseline_authority = fixture["authority"]
        baseline_journal = fixture["journal"]
        variants = (
            ("invalid-calendar", "2026-02-30T11:59:00Z"),
            ("noncanonical-offset", "2026-08-18T11:59:00+00:00"),
        )

        for label, timestamp in variants:
            with self.subTest(journal_timestamp=label):
                journal = json.loads(json.dumps(baseline_journal))
                journal["created_at"] = timestamp
                _write_private(
                    fixture["journal_path"],
                    ADOPTER._canonical_bytes(journal) + b"\n",
                    0o600,
                )
                with self.assertRaisesRegex(
                    ADOPTER.PrerequisiteError,
                    "source successor completed journal differs",
                ):
                    installer.plan(
                        source_sha=self.sha,
                        operation_id=self.unit_operation_id,
                    )

        for label, timestamp in variants:
            with self.subTest(authority_timestamp=label):
                authority = json.loads(json.dumps(baseline_authority))
                authority["completed_at"] = timestamp
                journal = json.loads(json.dumps(baseline_journal))
                journal["completed_at"] = timestamp
                _write_private(
                    fixture["authority_path"],
                    ADOPTER._canonical_bytes(authority) + b"\n",
                    0o600,
                )
                _write_private(
                    fixture["journal_path"],
                    ADOPTER._canonical_bytes(journal) + b"\n",
                    0o600,
                )
                with self.assertRaises(ADOPTER.PrerequisiteError):
                    installer.plan(
                        source_sha=self.sha,
                        operation_id=self.unit_operation_id,
                    )

    def test_source_successor_journal_swap_between_snapshots_is_rejected(
        self,
    ) -> None:
        installer, fixture = self.seed_source_successor_authority()
        alternate = json.loads(json.dumps(fixture["journal"]))
        alternate["created_at"] = "2026-08-18T11:58:00Z"
        read_snapshot = installer._read_source_successor_authority
        read_count = 0

        def read_then_swap():  # type: ignore[no-untyped-def]
            nonlocal read_count
            snapshot = read_snapshot()
            read_count += 1
            if read_count == 1:
                _write_private(
                    fixture["journal_path"],
                    ADOPTER._canonical_bytes(alternate) + b"\n",
                    0o600,
                )
            return snapshot

        with mock.patch.object(
            installer,
            "_read_source_successor_authority",
            side_effect=read_then_swap,
        ), self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "source successor authority changed while reading",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
            )
        self.assertEqual(read_count, 2)

    def test_source_successor_root_completed_journal_is_canonical_and_exact(
        self,
    ) -> None:
        installer, fixture = self.seed_source_successor_authority()
        baseline = fixture["root_journal"]
        extra_field = json.loads(json.dumps(baseline))
        extra_field["unexpected"] = True
        variants = (
            (
                "noncanonical-raw",
                json.dumps(
                    baseline,
                    sort_keys=True,
                    indent=2,
                ).encode("utf-8")
                + b"\n",
            ),
            (
                "extra-field",
                ADOPTER._canonical_bytes(extra_field) + b"\n",
            ),
        )

        for label, payload in variants:
            with self.subTest(root_journal=label):
                _write_private(
                    fixture["root_journal_path"],
                    payload,
                    0o600,
                )
                with self.assertRaisesRegex(
                    ADOPTER.PrerequisiteError,
                    "adopted Git permission completed journal differs",
                ):
                    installer.plan(
                        source_sha=self.sha,
                        operation_id=self.unit_operation_id,
                    )

    def test_source_successor_predecessor_journal_binding_drift_is_rejected(
        self,
    ) -> None:
        installer, fixture = self.seed_source_successor_authority()
        baseline = fixture["authority"]
        for field in (
            "completed_journal_sha256",
            "source_trust_sha256",
        ):
            with self.subTest(predecessor_field=field):
                authority = json.loads(json.dumps(baseline))
                authority["plan"]["predecessor"][field] = (
                    "sha256:" + "0" * 64
                )
                self.rewrite_source_successor_fixture(authority)
                with self.assertRaisesRegex(
                    ADOPTER.PrerequisiteError,
                    "source successor authority differs from its root or target",
                ):
                    installer.plan(
                        source_sha=self.sha,
                        operation_id=self.unit_operation_id,
                    )

    def test_source_successor_production_trust_four_way_drift_is_rejected(
        self,
    ) -> None:
        installer, fixture = self.seed_source_successor_authority()
        baseline_authority = fixture["authority"]
        baseline_journal = fixture["journal"]
        changed = "sha256:" + "0" * 64

        for layer in ("authority", "plan", "impact", "journal"):
            with self.subTest(production_trust_layer=layer):
                authority = json.loads(json.dumps(baseline_authority))
                if layer == "authority":
                    authority["production_source_trust_sha256"] = changed
                    self.rewrite_source_successor_fixture(authority)
                elif layer == "plan":
                    authority["plan"][
                        "production_source_trust_sha256"
                    ] = changed
                    self.rewrite_source_successor_fixture(authority)
                elif layer == "impact":
                    impact = authority["plan"][
                        "source_successor_impact"
                    ]
                    impact["production_source_trust_sha256"] = changed
                    impact_digest = ADOPTER._canonical_digest(impact)
                    authority["plan"][
                        "source_successor_impact_sha256"
                    ] = impact_digest
                    authority[
                        "source_successor_impact_sha256"
                    ] = impact_digest
                    self.rewrite_source_successor_fixture(authority)
                else:
                    self.rewrite_source_successor_fixture(authority)
                    journal = json.loads(json.dumps(baseline_journal))
                    journal[
                        "production_source_trust_sha256"
                    ] = changed
                    _write_private(
                        fixture["journal_path"],
                        ADOPTER._canonical_bytes(journal) + b"\n",
                        0o600,
                    )
                with self.assertRaises(ADOPTER.PrerequisiteError):
                    installer.plan(
                        source_sha=self.sha,
                        operation_id=self.unit_operation_id,
                    )

    def test_source_successor_transaction_staging_is_lineage_residue(
        self,
    ) -> None:
        installer, fixture = self.seed_source_successor_authority()
        residue = (
            self.runtime
            / "state"
            / (
                f".{ADOPTER.SOURCE_SUCCESSOR_TRANSACTION_DIRECTORY.name}"
                f".create-{fixture['authority']['operation_id']}.json"
            )
        )
        _write_private(residue, b"{}\n", 0o600)

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "lineage contains publication residue",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
            )

    def test_source_successor_identity_and_manifest_drift_fail_closed(
        self,
    ) -> None:
        installer, fixture = self.seed_source_successor_authority()
        baseline = fixture["authority"]

        def target_drift(document: dict[str, object]) -> None:
            document["source_sha"] = "0" * 40

        def tree_drift(document: dict[str, object]) -> None:
            document["source_tree"] = "0" * 40

        def delivery_drift(document: dict[str, object]) -> None:
            document["plan"]["delivery_gate"]["ci"][
                "conclusion"
            ] = "failure"

        def count_drift(document: dict[str, object]) -> None:
            document["plan"]["files"].pop()

        def mode_drift(document: dict[str, object]) -> None:
            record = document["plan"]["files"][0]
            record["predecessor"]["mode"] = "100644"
            record["target"]["mode"] = "100644"

        def blob_drift(document: dict[str, object]) -> None:
            record = document["plan"]["files"][0]
            record["predecessor"]["sha256"] = "sha256:" + "0" * 64
            record["target"]["sha256"] = "sha256:" + "0" * 64

        def relation_drift(document: dict[str, object]) -> None:
            document["plan"]["files"][0]["relation"] = "changed"

        def repository_transition_drift(
            document: dict[str, object],
        ) -> None:
            document["plan"]["production_repository_transition"][
                "target"
            ]["sha"] = "0" * 40

        mutations = (
            ("target", target_drift),
            ("tree", tree_drift),
            ("delivery", delivery_drift),
            ("13-file-count", count_drift),
            ("file-mode", mode_drift),
            ("file-blob", blob_drift),
            ("file-relation", relation_drift),
            ("repository-transition", repository_transition_drift),
        )
        for label, mutate in mutations:
            with self.subTest(drift=label):
                authority = json.loads(json.dumps(baseline))
                mutate(authority)
                self.rewrite_source_successor_fixture(authority)
                with self.assertRaises(ADOPTER.PrerequisiteError):
                    installer.plan(
                        source_sha=self.sha,
                        operation_id=self.unit_operation_id,
                    )

    def test_unit_permission_plan_apply_and_authority_evidence(self) -> None:
        installer = self.unit_permission_installer()
        runtime_before = _inventory(self.runtime)
        source_before = _inventory(self.source)
        units_before = _inventory(self.unit_parent)
        md_before = self.md_unit.stat(follow_symlinks=False)
        dft_before = self.dft_unit.stat(follow_symlinks=False)

        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        self.assertEqual(_inventory(self.runtime), runtime_before)
        self.assertEqual(_inventory(self.source), source_before)
        self.assertEqual(_inventory(self.unit_parent), units_before)
        self.assertTrue(planned["logical_zero_write"])
        self.assertEqual(
            planned["plan"]["git_permission_successor"]["mode"],
            "ancestor-byte-identical",
        )

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "impact confirmation differs"
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256="sha256:" + "0" * 64,
            )
        self.assertEqual(_inventory(self.runtime), runtime_before)
        self.assertEqual(_inventory(self.unit_parent), units_before)

        authority = installer.apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        md_after = self.md_unit.stat(follow_symlinks=False)
        dft_after = self.dft_unit.stat(follow_symlinks=False)
        self.assertNotEqual(md_after.st_ino, md_before.st_ino)
        self.assertEqual(stat.S_IMODE(md_after.st_mode), 0o600)
        self.assertEqual(self.md_unit.read_bytes(), self.md_unit_payload)
        self.assertEqual(
            (
                dft_after.st_dev,
                dft_after.st_ino,
                dft_after.st_uid,
                dft_after.st_gid,
                dft_after.st_mode,
                dft_after.st_nlink,
                dft_after.st_size,
                dft_after.st_mtime_ns,
                dft_after.st_ctime_ns,
            ),
            (
                dft_before.st_dev,
                dft_before.st_ino,
                dft_before.st_uid,
                dft_before.st_gid,
                dft_before.st_mode,
                dft_before.st_nlink,
                dft_before.st_size,
                dft_before.st_mtime_ns,
                dft_before.st_ctime_ns,
            ),
        )
        self.assertEqual(self.unit_reload_calls, 1)
        self.assertEqual(
            authority["original_units_sha256"],
            ADOPTER._canonical_digest(authority["original_units"]),
        )
        self.assertEqual(
            authority["hardened_units_sha256"],
            ADOPTER._canonical_digest(authority["hardened_units"]),
        )
        self.assertEqual(
            authority["backup_sha256"],
            ADOPTER._canonical_digest(authority["backup"]),
        )
        self.assertEqual(
            authority["backup"]["owner_sha256"],
            ADOPTER._canonical_digest(authority["backup"]["owner"]),
        )
        self.assertEqual(
            authority["original_units"][0]["process_identity"],
            authority["hardened_units"][0]["process_identity"],
        )
        self.assertEqual(
            authority["original_units"][1],
            authority["hardened_units"][1],
        )
        self.assertEqual(
            installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            ),
            authority,
        )
        self.assertEqual(self.unit_reload_calls, 1)

    def test_unit_permission_plan_schema_discriminators_require_exact_int(
        self,
    ) -> None:
        installer = self.unit_permission_installer()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        original = planned["plan"]

        for bad_version in (True, 1.0):
            for discriminator, expected_error in (
                (
                    "plan",
                    "unit permission hardening plan schema changed",
                ),
                ("git-successor", "predecessor authority changed"),
            ):
                plan = json.loads(json.dumps(original))
                if discriminator == "plan":
                    plan["schema_version"] = bad_version
                else:
                    plan["git_permission_successor"][
                        "schema_version"
                    ] = bad_version
                with self.subTest(
                    discriminator=discriminator,
                    bad_version=bad_version,
                ), self.assertRaisesRegex(
                    ADOPTER.PrerequisiteError,
                    expected_error,
                ):
                    installer._validate_unit_plan_context(
                        plan,
                        self.sha,
                        self.unit_operation_id,
                        durable=False,
                    )

    def test_unit_permission_transaction_and_authority_schema_require_exact_int(
        self,
    ) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )

        def crash(phase: str) -> None:
            if phase == "unit-permission-intent":
                raise RuntimeError("crash after unit intent")

        with self.assertRaisesRegex(RuntimeError, "after unit intent"):
            self.unit_permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        installer = self.unit_permission_installer()
        transaction_path = installer._unit_transaction_path(
            self.unit_operation_id
        )
        original_transaction = json.loads(
            transaction_path.read_text(encoding="utf-8")
        )
        for bad_version in (True, 1.0):
            transaction = json.loads(json.dumps(original_transaction))
            transaction["schema_version"] = bad_version
            _write_private(
                transaction_path,
                json.dumps(transaction, sort_keys=True).encode() + b"\n",
                0o600,
            )
            with self.subTest(
                record="transaction",
                bad_version=bad_version,
            ), self.assertRaisesRegex(
                ADOPTER.PrerequisiteError,
                "transaction is invalid",
            ):
                installer._load_unit_transaction(self.unit_operation_id)
        _write_private(
            transaction_path,
            json.dumps(original_transaction, sort_keys=True).encode() + b"\n",
            0o600,
        )

        authority = installer.apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        authority_path = installer.unit_authority_path
        for bad_version in (True, 1.0):
            tampered = json.loads(json.dumps(authority))
            tampered["schema_version"] = bad_version
            _write_private(
                authority_path,
                json.dumps(tampered, sort_keys=True).encode() + b"\n",
                0o600,
            )
            with self.subTest(
                record="authority",
                bad_version=bad_version,
            ), self.assertRaisesRegex(
                ADOPTER.PrerequisiteError,
                "authority schema is invalid",
            ):
                installer._load_unit_authority()
        _write_private(
            authority_path,
            json.dumps(authority, sort_keys=True).encode() + b"\n",
            0o600,
        )

    def test_unit_backup_schema_discriminators_require_exact_int(self) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )

        def crash(phase: str) -> None:
            if phase == "unit-backup-ready":
                raise RuntimeError("crash after unit backup")

        with self.assertRaisesRegex(RuntimeError, "after unit backup"):
            self.unit_permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        installer = self.unit_permission_installer()
        transaction = installer._load_unit_transaction(
            self.unit_operation_id
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None
        for bad_version in (True, 1.0):
            for discriminator in ("backup", "owner"):
                tampered = json.loads(json.dumps(transaction))
                backup = tampered["backup"]
                if discriminator == "backup":
                    backup["schema_version"] = bad_version
                else:
                    backup["owner"]["schema_version"] = bad_version
                    backup["owner_sha256"] = ADOPTER._canonical_digest(
                        backup["owner"]
                    )
                    backup["claim_sha256"] = backup["owner_sha256"]
                with self.subTest(
                    discriminator=discriminator,
                    bad_version=bad_version,
                ), self.assertRaisesRegex(
                    ADOPTER.PrerequisiteError,
                    "backup authority is unavailable",
                ):
                    installer._load_backup_payload(tampered)

    def test_unit_backup_claim_partial_write_replays_forward(self) -> None:
        self.unit_permission_installer()
        self.assert_unit_backup_partial_fault_replays(
            target_name=f".{self.unit_operation_id}.owner.json",
            fault_point="write",
        )

    def test_unit_backup_owner_file_fsync_replays_forward(self) -> None:
        self.assert_unit_backup_partial_fault_replays(
            target_name=".owner.json",
            fault_point="file-fsync",
        )

    def test_unit_backup_file_parent_fsync_replays_forward(self) -> None:
        self.assert_unit_backup_partial_fault_replays(
            target_name=ADOPTER.MD_UNIT_NAME,
            fault_point="parent-fsync",
        )

    def test_unit_replacement_intent_checkpoint_replays(self) -> None:
        self.assert_unit_checkpoint_replays("unit-replacement-intent")

    def test_unit_backup_create_intent_checkpoint_replays(self) -> None:
        self.assert_unit_checkpoint_replays("unit-backup-create-intent")

    def test_unit_backup_ready_checkpoint_replays(self) -> None:
        self.assert_unit_checkpoint_replays("unit-backup-ready")

    def test_unit_retired_unlinked_checkpoint_replays(self) -> None:
        self.assert_unit_checkpoint_replays("unit-retired-unlinked")

    def test_unit_daemon_reloaded_checkpoint_replays(self) -> None:
        self.assert_unit_checkpoint_replays("unit-daemon-reloaded")

    def test_unit_ready_checkpoint_replays(self) -> None:
        self.assert_unit_checkpoint_replays("unit-permission-ready")

    def test_unit_source_verified_checkpoint_replays(self) -> None:
        self.assert_unit_checkpoint_replays(
            "unit-permission-source-verified"
        )

    def test_unit_authority_commit_intent_checkpoint_replays(self) -> None:
        self.assert_unit_checkpoint_replays(
            "unit-permission-authority-commit-intent"
        )

    def test_unit_authority_linked_checkpoint_replays(self) -> None:
        self.assert_unit_checkpoint_replays("authority-linked")

    def test_authority_existing_exact_staging_reseals_across_two_power_losses(
        self,
    ) -> None:
        for fault_point in ("write", "file-fsync"):
            with self.subTest(fault_point=fault_point):
                authority_root = (
                    Path(self.temporary.name)
                    / f"authority-reseal-{fault_point}"
                )
                authority_root.mkdir(mode=0o700)
                authority_root.chmod(0o700)
                directory_fd = os.open(
                    authority_root,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                )
                name = "adopted-unit-permissions.json"
                operation_id = (
                    "adopt-unit-permission-authority-"
                    f"{fault_point}-0001"
                )
                document = {
                    "schema_version": 1,
                    "status": "completed",
                    "operation_id": operation_id,
                }
                expected_payload = ADOPTER._canonical_bytes(document) + b"\n"
                temporary_name = f".{name}.create-{operation_id}"
                temporary = authority_root / temporary_name
                authority = authority_root / name
                durable_write = ADOPTER.os.write
                durable_fsync = ADOPTER.os.fsync
                first_faulted = False

                def lose_first_write_response(
                    descriptor: int,
                    chunk: bytes,
                ) -> int:
                    nonlocal first_faulted
                    written = durable_write(descriptor, chunk)
                    if (
                        fault_point == "write"
                        and not first_faulted
                        and temporary.exists()
                    ):
                        opened = os.fstat(descriptor)
                        observed = temporary.stat(follow_symlinks=False)
                        if (
                            (opened.st_dev, opened.st_ino)
                            == (observed.st_dev, observed.st_ino)
                            and temporary.read_bytes() == expected_payload
                        ):
                            first_faulted = True
                            raise RuntimeError(
                                "authority staging write response lost"
                            )
                    return written

                def lose_first_file_fsync_response(descriptor: int) -> None:
                    nonlocal first_faulted
                    durable_fsync(descriptor)
                    if (
                        fault_point != "file-fsync"
                        or first_faulted
                        or not temporary.exists()
                    ):
                        return
                    opened = os.fstat(descriptor)
                    observed = temporary.stat(follow_symlinks=False)
                    if (opened.st_dev, opened.st_ino) == (
                        observed.st_dev,
                        observed.st_ino,
                    ):
                        first_faulted = True
                        raise RuntimeError(
                            "authority staging file fsync response lost"
                        )

                try:
                    with mock.patch.object(
                        ADOPTER.os,
                        "write",
                        side_effect=lose_first_write_response,
                    ), mock.patch.object(
                        ADOPTER.os,
                        "fsync",
                        side_effect=lose_first_file_fsync_response,
                    ), self.assertRaisesRegex(
                        RuntimeError,
                        "authority staging .* response lost",
                    ):
                        ADOPTER._create_owned_json_once_at(
                            directory_fd,
                            name,
                            document,
                            operation_id=operation_id,
                            checkpoint=lambda _phase: None,
                        )
                    self.assertTrue(first_faulted)
                    self.assertEqual(temporary.read_bytes(), expected_payload)
                    self.assertFalse(authority.exists())

                    staging_identity = (
                        temporary.stat().st_dev,
                        temporary.stat().st_ino,
                    )
                    root_identity = (
                        authority_root.stat().st_dev,
                        authority_root.stat().st_ino,
                    )
                    staging_resealed = False
                    second_faulted = False

                    def lose_link_parent_fsync_response(
                        descriptor: int,
                    ) -> None:
                        nonlocal staging_resealed, second_faulted
                        metadata = os.fstat(descriptor)
                        identity = (metadata.st_dev, metadata.st_ino)
                        if identity == staging_identity:
                            durable_fsync(descriptor)
                            staging_resealed = True
                            return
                        if (
                            identity == root_identity
                            and staging_resealed
                            and not second_faulted
                            and temporary.exists()
                            and authority.exists()
                        ):
                            authority_metadata = authority.stat(
                                follow_symlinks=False
                            )
                            temporary_metadata = temporary.stat(
                                follow_symlinks=False
                            )
                            if (
                                authority_metadata.st_dev,
                                authority_metadata.st_ino,
                            ) == (
                                temporary_metadata.st_dev,
                                temporary_metadata.st_ino,
                            ):
                                durable_fsync(descriptor)
                                second_faulted = True
                                raise RuntimeError(
                                    "authority link parent fsync response lost"
                                )
                        durable_fsync(descriptor)

                    with mock.patch.object(
                        ADOPTER.os,
                        "fsync",
                        side_effect=lose_link_parent_fsync_response,
                    ), self.assertRaisesRegex(
                        RuntimeError,
                        "authority link parent fsync response lost",
                    ):
                        ADOPTER._create_owned_json_once_at(
                            directory_fd,
                            name,
                            document,
                            operation_id=operation_id,
                            checkpoint=lambda _phase: None,
                        )
                    self.assertTrue(staging_resealed)
                    self.assertTrue(second_faulted)
                    self.assertTrue(temporary.exists())
                    self.assertTrue(authority.exists())
                    self.assertEqual(
                        (authority.stat().st_dev, authority.stat().st_ino),
                        staging_identity,
                    )

                    recovered_inode_fsyncs = 0

                    def record_recovery_fsync(descriptor: int) -> None:
                        nonlocal recovered_inode_fsyncs
                        metadata = os.fstat(descriptor)
                        if (
                            metadata.st_dev,
                            metadata.st_ino,
                        ) == staging_identity:
                            recovered_inode_fsyncs += 1
                        durable_fsync(descriptor)

                    with mock.patch.object(
                        ADOPTER.os,
                        "fsync",
                        side_effect=record_recovery_fsync,
                    ):
                        ADOPTER._create_owned_json_once_at(
                            directory_fd,
                            name,
                            document,
                            operation_id=operation_id,
                            checkpoint=lambda _phase: None,
                        )
                    self.assertGreaterEqual(recovered_inode_fsyncs, 2)
                    self.assertFalse(temporary.exists())
                    self.assertEqual(authority.read_bytes(), expected_payload)
                    self.assertEqual(authority.stat().st_nlink, 1)

                    stable_inode_fsyncs = 0

                    def record_stable_fsync(descriptor: int) -> None:
                        nonlocal stable_inode_fsyncs
                        metadata = os.fstat(descriptor)
                        if (
                            metadata.st_dev,
                            metadata.st_ino,
                        ) == staging_identity:
                            stable_inode_fsyncs += 1
                        durable_fsync(descriptor)

                    with mock.patch.object(
                        ADOPTER.os,
                        "fsync",
                        side_effect=record_stable_fsync,
                    ):
                        ADOPTER._create_owned_json_once_at(
                            directory_fd,
                            name,
                            document,
                            operation_id=operation_id,
                            checkpoint=lambda _phase: None,
                        )
                    self.assertGreaterEqual(stable_inode_fsyncs, 2)
                finally:
                    os.close(directory_fd)

    def test_unit_authority_weak_same_operation_final_preplant_fails_before_md_mutation(
        self,
    ) -> None:
        self.assert_unit_authority_preplant_after_intent_fails("final")

    def test_unit_authority_staging_preplant_fails_before_md_mutation(
        self,
    ) -> None:
        self.assert_unit_authority_preplant_after_intent_fails("staging")

    def test_unit_authority_quarantine_preplant_fails_before_md_mutation(
        self,
    ) -> None:
        self.assert_unit_authority_preplant_after_intent_fails(
            "staging-quarantine"
        )

    def test_unit_authority_unowned_third_link_preplant_fails_before_md_mutation(
        self,
    ) -> None:
        self.assert_unit_authority_preplant_after_intent_fails(
            "final",
            extra_hard_link=True,
        )

    def test_unit_authority_absence_cas_rejects_post_fsync_preplant_before_md_mutation(
        self,
    ) -> None:
        installer = self.unit_permission_installer()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )

        def crash(phase: str) -> None:
            if phase == "unit-permission-intent":
                raise RuntimeError("crash after unit permission intent")

        with self.assertRaisesRegex(RuntimeError, "after unit permission intent"):
            self.unit_permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        transaction = installer._load_unit_transaction(
            self.unit_operation_id
        )
        self.assertIsNotNone(transaction)
        md_identity = self.unit_file_identity(self.md_unit)
        dft_identity = self.unit_file_identity(self.dft_unit)
        paths = self.unit_authority_publication_paths(planned["plan"])
        staging_path = paths["staging"]
        state_identity = self.unit_file_identity(self.runtime / "state")[:2]
        durable_fsync = ADOPTER.os.fsync
        raced = False

        def preplant_after_parent_fsync(descriptor: int) -> None:
            nonlocal raced
            metadata = os.fstat(descriptor)
            durable_fsync(descriptor)
            if not raced and (metadata.st_dev, metadata.st_ino) == state_identity:
                raced = True
                _write_private(staging_path, b"foreign publication source\n", 0o600)

        with mock.patch.object(
            ADOPTER.os,
            "fsync",
            side_effect=preplant_after_parent_fsync,
        ), self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "publication namespace predates commit intent",
        ):
            self.unit_permission_installer().apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertTrue(raced)
        self.assertEqual(self.unit_file_identity(self.md_unit), md_identity)
        self.assertEqual(self.unit_file_identity(self.dft_unit), dft_identity)
        self.assertEqual(self.unit_reload_calls, 0)
        self.assertEqual(
            installer._load_unit_transaction(self.unit_operation_id),
            transaction,
        )

    def test_unit_authority_staging_prefix_replays_after_second_power_loss(
        self,
    ) -> None:
        self.assert_unit_authority_commit_residue_replays("staging-prefix")

    def test_unit_authority_quarantine_prefix_replays_after_second_power_loss(
        self,
    ) -> None:
        self.assert_unit_authority_commit_residue_replays(
            "quarantine-prefix"
        )

    def test_unit_authority_linked_final_replays_after_second_power_loss(
        self,
    ) -> None:
        self.assert_unit_authority_commit_residue_replays("linked-final")

    def test_completed_unit_authority_is_resealed_before_idempotent_return(
        self,
    ) -> None:
        installer = self.unit_permission_installer()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        authority = installer.apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        authority_path = installer.unit_authority_path
        authority_identity = self.unit_file_identity(authority_path)
        authority_payload = authority_path.read_bytes()
        transaction = installer._load_unit_transaction(
            self.unit_operation_id
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertEqual(transaction["phase"], "completed")
        self.assertEqual(transaction["status"], "completed")
        crashed = False

        def crash_after_reseal(phase: str) -> None:
            nonlocal crashed
            if phase == "unit-permission-journal-resealed" and not crashed:
                crashed = True
                raise RuntimeError("power loss after completed journal reseal")

        with self.assertRaisesRegex(RuntimeError, "completed journal reseal"):
            self.unit_permission_installer(crash_after_reseal).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertTrue(crashed)
        self.assertEqual(
            self.unit_file_identity(authority_path),
            authority_identity,
        )
        self.assertEqual(authority_path.read_bytes(), authority_payload)
        self.assertEqual(
            installer._load_unit_transaction(self.unit_operation_id),
            transaction,
        )
        self.assertEqual(self.unit_reload_calls, 1)
        self.assertEqual(
            self.unit_permission_installer().apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            ),
            authority,
        )
        self.assertEqual(
            self.unit_file_identity(authority_path),
            authority_identity,
        )
        self.assertEqual(authority_path.read_bytes(), authority_payload)
        self.assertEqual(self.unit_reload_calls, 1)

    def test_unit_authority_publish_before_completed_journal_replays(
        self,
    ) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        installer = self.unit_permission_installer()
        durable_write = installer._write_unit_transaction
        crashed = False

        def crash_before_completed(
            document: dict[str, object],
        ) -> None:
            nonlocal crashed
            if (
                document.get("status") == "completed"
                and installer._unit_authority_exists()
                and not crashed
            ):
                crashed = True
                raise RuntimeError("crash before completed unit journal")
            durable_write(document)

        with mock.patch.object(
            installer,
            "_write_unit_transaction",
            side_effect=crash_before_completed,
        ), self.assertRaisesRegex(
            RuntimeError,
            "before completed unit journal",
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertTrue(crashed)
        self.assertTrue(installer._unit_authority_exists())
        durable = installer._load_unit_transaction(self.unit_operation_id)
        self.assertIsNotNone(durable)
        assert durable is not None
        self.assertEqual(durable["phase"], "authority-commit-intent")
        self.assertEqual(durable["status"], "applying")
        authority = self.unit_permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["status"], "completed")
        completed = installer._load_unit_transaction(self.unit_operation_id)
        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(completed["phase"], "completed")
        self.assertEqual(completed["status"], "completed")

    def test_unit_staging_intent_parent_fsync_lost_response_reseals(
        self,
    ) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        installer = self.unit_permission_installer()
        fault_patch, lost = self.journal_parent_fsync_lost_response(
            installer,
            writer_name="_write_unit_transaction",
            predicate=lambda document: (
                document.get("replacement_checkpoint")
                == "staging-create-intent"
            ),
        )
        with fault_patch, self.assertRaisesRegex(
            RuntimeError,
            "journal parent fsync response lost",
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        visible = installer._load_unit_transaction(self.unit_operation_id)
        self.assertEqual(visible, lost["document"])
        assert visible is not None
        staging_name, _retired_name = installer._replacement_names(
            self.unit_operation_id
        )
        self.assertFalse((self.unit_parent / staging_name).exists())

        resealed = False

        def second_crash(phase: str) -> None:
            nonlocal resealed
            if phase == "unit-permission-journal-resealed" and not resealed:
                resealed = True
                raise RuntimeError("second crash after unit journal reseal")

        with self.assertRaisesRegex(RuntimeError, "second crash"):
            self.unit_permission_installer(second_crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertTrue(resealed)
        self.assertFalse((self.unit_parent / staging_name).exists())
        self.assertEqual(
            installer._load_unit_transaction(self.unit_operation_id),
            visible,
        )

        authority = self.unit_permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertFalse((self.unit_parent / staging_name).exists())

    def test_unit_staging_absence_cas_retries_two_fsync_lost_responses(
        self,
    ) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        unit_parent_identity = (
            self.unit_parent.stat().st_dev,
            self.unit_parent.stat().st_ino,
        )
        durable_fsync = ADOPTER.os.fsync

        for attempt in range(2):
            installer = self.unit_permission_installer()
            faulted = False

            def lose_absence_fsync_response(descriptor: int) -> None:
                nonlocal faulted
                metadata = os.fstat(descriptor)
                durable_fsync(descriptor)
                if (
                    not faulted
                    and (metadata.st_dev, metadata.st_ino)
                    == unit_parent_identity
                ):
                    faulted = True
                    raise RuntimeError(
                        f"unit staging absence fsync response lost {attempt}"
                    )

            with mock.patch.object(
                ADOPTER.os,
                "fsync",
                side_effect=lose_absence_fsync_response,
            ), mock.patch.object(
                installer,
                "_write_unit_transaction",
                side_effect=AssertionError(
                    "unit intent preceded durable staging absence"
                ),
            ), self.assertRaisesRegex(
                RuntimeError,
                "unit staging absence fsync response lost",
            ):
                installer.apply(
                    source_sha=self.sha,
                    operation_id=self.unit_operation_id,
                    confirm_plan_sha256=planned["plan_sha256"],
                    confirm_unit_permission_impact_sha256=planned[
                        "unit_permission_impact_sha256"
                    ],
                )
            self.assertTrue(faulted)
            self.assertIsNone(
                installer._load_unit_transaction(self.unit_operation_id)
            )
            self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o664)

        authority = self.unit_permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["status"], "completed")

    def test_unit_backup_absence_cas_retries_two_fsync_lost_responses(
        self,
    ) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        backup_root = (
            self.runtime / ADOPTER.UNIT_PERMISSION_BACKUP_DIRECTORY
        )
        backup_root.mkdir(mode=0o700)
        backup_root.chmod(0o700)
        backup_root_identity = (
            backup_root.stat().st_dev,
            backup_root.stat().st_ino,
        )
        durable_fsync = ADOPTER.os.fsync

        for attempt in range(2):
            installer = self.unit_permission_installer()
            faulted = False

            def lose_absence_fsync_response(descriptor: int) -> None:
                nonlocal faulted
                metadata = os.fstat(descriptor)
                durable_fsync(descriptor)
                if (
                    not faulted
                    and (metadata.st_dev, metadata.st_ino)
                    == backup_root_identity
                ):
                    faulted = True
                    raise RuntimeError(
                        f"unit backup absence fsync response lost {attempt}"
                    )

            with mock.patch.object(
                ADOPTER.os,
                "fsync",
                side_effect=lose_absence_fsync_response,
            ), mock.patch.object(
                installer,
                "_write_unit_transaction",
                side_effect=AssertionError(
                    "unit intent preceded durable backup absence"
                ),
            ), self.assertRaisesRegex(
                RuntimeError,
                "unit backup absence fsync response lost",
            ):
                installer.apply(
                    source_sha=self.sha,
                    operation_id=self.unit_operation_id,
                    confirm_plan_sha256=planned["plan_sha256"],
                    confirm_unit_permission_impact_sha256=planned[
                        "unit_permission_impact_sha256"
                    ],
                )
            self.assertTrue(faulted)
            self.assertIsNone(
                installer._load_unit_transaction(self.unit_operation_id)
            )
            self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o664)

        authority = self.unit_permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["status"], "completed")

    def test_unit_absent_backup_root_retries_two_state_fsync_lost_responses(
        self,
    ) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        backup_root = (
            self.runtime / ADOPTER.UNIT_PERMISSION_BACKUP_DIRECTORY
        )
        self.assertFalse(backup_root.exists())
        state = self.runtime / "state"
        state_identity = (state.stat().st_dev, state.stat().st_ino)
        durable_fsync = ADOPTER.os.fsync

        for attempt in range(2):
            installer = self.unit_permission_installer()
            durable_assert_absent = installer._assert_backup_operation_absent
            checking_backup_absence = False
            faulted = False

            def assert_backup_absent(operation_id: str) -> None:
                nonlocal checking_backup_absence
                checking_backup_absence = True
                try:
                    durable_assert_absent(operation_id)
                finally:
                    checking_backup_absence = False

            def lose_absence_fsync_response(descriptor: int) -> None:
                nonlocal faulted
                metadata = os.fstat(descriptor)
                durable_fsync(descriptor)
                if (
                    checking_backup_absence
                    and not faulted
                    and (metadata.st_dev, metadata.st_ino) == state_identity
                ):
                    faulted = True
                    raise RuntimeError(
                        "absent unit backup root parent fsync response lost "
                        f"{attempt}"
                    )

            with mock.patch.object(
                installer,
                "_assert_backup_operation_absent",
                side_effect=assert_backup_absent,
            ), mock.patch.object(
                ADOPTER.os,
                "fsync",
                side_effect=lose_absence_fsync_response,
            ), mock.patch.object(
                installer,
                "_write_unit_transaction",
                side_effect=AssertionError(
                    "unit intent preceded durable absent backup root CAS"
                ),
            ), self.assertRaisesRegex(
                RuntimeError,
                "absent unit backup root parent fsync response lost",
            ):
                installer.apply(
                    source_sha=self.sha,
                    operation_id=self.unit_operation_id,
                    confirm_plan_sha256=planned["plan_sha256"],
                    confirm_unit_permission_impact_sha256=planned[
                        "unit_permission_impact_sha256"
                    ],
                )
            self.assertTrue(faulted)
            self.assertFalse(backup_root.exists())
            self.assertIsNone(
                installer._load_unit_transaction(self.unit_operation_id)
            )
            self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o664)

        authority = self.unit_permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["status"], "completed")

    def test_unit_transaction_directory_lost_response_reseals(self) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        transaction_root = (
            self.runtime / ADOPTER.UNIT_PERMISSION_TRANSACTION_DIRECTORY
        )
        durable_mkdir = ADOPTER.os.mkdir
        created = False

        def mkdir_then_crash(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal created
            result = durable_mkdir(path, *args, **kwargs)
            if (
                path == ADOPTER.UNIT_PERMISSION_TRANSACTION_DIRECTORY.name
                and not created
            ):
                created = True
                raise RuntimeError(
                    "unit transaction directory mkdir response lost"
                )
            return result

        with mock.patch.object(
            ADOPTER.os,
            "mkdir",
            side_effect=mkdir_then_crash,
        ), self.assertRaisesRegex(RuntimeError, "mkdir response lost"):
            self.unit_permission_installer().apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertTrue(created)
        self.assertTrue(transaction_root.is_dir())
        self.assertEqual(list(transaction_root.iterdir()), [])

        state = self.runtime / "state"
        state_identity = (state.stat().st_dev, state.stat().st_ino)
        durable_fsync = ADOPTER.os.fsync
        state_resealed = False
        installer = self.unit_permission_installer()
        durable_atomic = ADOPTER._atomic_owned_json_at

        def track_state_fsync(descriptor: int) -> None:
            nonlocal state_resealed
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) == state_identity:
                state_resealed = True
            durable_fsync(descriptor)

        def require_parent_reseal(
            directory_fd: int,
            name: str,
            document: object,
        ) -> None:
            self.assertTrue(state_resealed)
            durable_atomic(directory_fd, name, document)

        with mock.patch.object(
            ADOPTER.os,
            "fsync",
            side_effect=track_state_fsync,
        ), mock.patch.object(
            ADOPTER,
            "_atomic_owned_json_at",
            side_effect=require_parent_reseal,
        ):
            authority = installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertTrue(state_resealed)
        self.assertEqual(authority["status"], "completed")

    def test_unit_authority_intent_parent_fsync_lost_response_reseals(
        self,
    ) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        expected_processes = json.loads(json.dumps(self.unit_processes))
        installer = self.unit_permission_installer()
        fault_patch, lost = self.journal_parent_fsync_lost_response(
            installer,
            writer_name="_write_unit_transaction",
            predicate=lambda document: (
                document.get("phase") == "authority-commit-intent"
            ),
        )
        with fault_patch, self.assertRaisesRegex(
            RuntimeError,
            "journal parent fsync response lost",
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        intent = installer._load_unit_transaction(self.unit_operation_id)
        self.assertEqual(intent, lost["document"])
        assert intent is not None
        completed_at = intent["completed_at"]
        self.assertIsInstance(completed_at, str)
        self.assertFalse(installer._unit_authority_exists())

        resealed = False

        def second_crash(phase: str) -> None:
            nonlocal resealed
            if phase == "unit-permission-journal-resealed" and not resealed:
                resealed = True
                raise RuntimeError("second crash after unit journal reseal")

        with self.assertRaisesRegex(RuntimeError, "second crash"):
            self.unit_permission_installer(second_crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertTrue(resealed)
        self.assertFalse(installer._unit_authority_exists())

        authority = self.unit_permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["completed_at"], completed_at)
        final = installer._load_unit_transaction(self.unit_operation_id)
        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual(final["completed_at"], completed_at)
        for index, role in enumerate(
            (ADOPTER.MD_UNIT_NAME, ADOPTER.DFT_UNIT_NAME)
        ):
            self.assertEqual(
                authority["hardened_units"][index]["process_identity"],
                expected_processes[role],
            )

    def test_unit_permission_parent_directory_growth_is_a_valid_transition(
        self,
    ) -> None:
        installer = self.unit_permission_installer()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        growth_patch, growth = self.grow_unit_parent_on_staging(installer)

        with growth_patch:
            authority = installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )

        self.assertEqual(authority["status"], "completed")
        self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o600)
        self.assertEqual(
            authority["original_units"][1]["parent"]["size"],
            growth["before"],
        )
        self.assertEqual(
            authority["hardened_units"][1]["parent"]["size"],
            growth["after_cleanup"],
        )
        self.assertGreater(
            authority["hardened_units"][1]["parent"]["size"],
            authority["original_units"][1]["parent"]["size"],
        )

    def test_unit_permission_parent_growth_replays_after_staging_crash(
        self,
    ) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        crashed = False

        def crash(phase: str) -> None:
            nonlocal crashed
            if phase == "unit-replacement-staged" and not crashed:
                crashed = True
                raise RuntimeError("crash after size-growing staging")

        installer = self.unit_permission_installer(crash)
        growth_patch, growth = self.grow_unit_parent_on_staging(installer)
        with growth_patch, self.assertRaisesRegex(
            RuntimeError, "size-growing staging"
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )

        self.assertTrue(crashed)
        self.assertGreater(growth["after_cleanup"], growth["before"])
        self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o664)
        replay = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        self.assertEqual(replay["plan_sha256"], planned["plan_sha256"])

        authority = self.unit_permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o600)

    def test_unit_permission_parent_growth_replays_pre_staged_journal_crash(
        self,
    ) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        installer = self.unit_permission_installer()
        durable_write = installer._write_unit_transaction
        crashed = False

        def crash_before_staged_journal(
            document: dict[str, object],
        ) -> None:
            nonlocal crashed
            if (
                document.get("replacement_checkpoint") == "staged"
                and isinstance(document.get("staging"), dict)
                and not crashed
            ):
                crashed = True
                raise RuntimeError("crash before staged journal")
            durable_write(document)

        growth_patch, growth = self.grow_unit_parent_on_staging(installer)
        with (
            growth_patch,
            mock.patch.object(
                installer,
                "_write_unit_transaction",
                side_effect=crash_before_staged_journal,
            ),
            self.assertRaisesRegex(RuntimeError, "before staged journal"),
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )

        self.assertTrue(crashed)
        self.assertGreater(growth["after_cleanup"], growth["before"])
        durable = json.loads(
            installer._unit_transaction_path(
                self.unit_operation_id
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(durable["phase"], "replacement-intent")
        self.assertEqual(
            durable["replacement_checkpoint"],
            "staging-create-intent",
        )
        self.assertIsInstance(durable["backup"], dict)
        self.assertIsNone(durable["staging"])
        self.assertIsNone(durable["replacement"])
        staging_name, _retired_name = installer._replacement_names(
            self.unit_operation_id
        )
        staging_path = self.unit_parent / staging_name
        staging_metadata = staging_path.stat(follow_symlinks=False)
        self.assertEqual(stat.S_IMODE(staging_metadata.st_mode), 0o600)
        self.assertEqual(staging_metadata.st_nlink, 1)
        self.assertEqual(staging_metadata.st_dev, self.md_unit.stat().st_dev)
        self.assertEqual(staging_path.read_bytes(), self.md_unit_payload)
        self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o664)

        runtime_before = _inventory(self.runtime)
        units_before = _inventory(self.unit_parent)
        replay = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        self.assertEqual(replay["plan_sha256"], planned["plan_sha256"])
        self.assertEqual(_inventory(self.runtime), runtime_before)
        self.assertEqual(_inventory(self.unit_parent), units_before)

        staged_recovered = False

        def crash_after_staged_recovery(phase: str) -> None:
            nonlocal staged_recovered
            if phase == "unit-replacement-staged" and not staged_recovered:
                staged_recovered = True
                raise RuntimeError("crash after recovered staged journal")

        with self.assertRaisesRegex(
            RuntimeError,
            "after recovered staged journal",
        ):
            self.unit_permission_installer(crash_after_staged_recovery).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertTrue(staged_recovered)
        recovered = json.loads(
            installer._unit_transaction_path(
                self.unit_operation_id
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(recovered["replacement_checkpoint"], "staged")
        self.assertIsInstance(recovered["staging"], dict)
        self.assertIsNone(recovered["replacement"])
        self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o664)

        authority = self.unit_permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o600)
        self.assertFalse(staging_path.exists())
        self.assertGreater(
            authority["hardened_units"][1]["parent"]["size"],
            authority["original_units"][1]["parent"]["size"],
        )

    def test_unit_staging_create_intent_absent_replays_forward(self) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )

        def crash(phase: str) -> None:
            if phase == "unit-staging-create-intent":
                raise RuntimeError("crash before staging create")

        with self.assertRaisesRegex(RuntimeError, "before staging create"):
            self.unit_permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        installer = self.unit_permission_installer()
        staging_name, _retired_name = installer._replacement_names(
            self.unit_operation_id
        )
        self.assertFalse((self.unit_parent / staging_name).exists())
        durable = json.loads(
            installer._unit_transaction_path(
                self.unit_operation_id
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            durable["replacement_checkpoint"],
            "staging-create-intent",
        )
        runtime_before = _inventory(self.runtime)
        units_before = _inventory(self.unit_parent)
        replay = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        self.assertEqual(replay["plan_sha256"], planned["plan_sha256"])
        self.assertEqual(_inventory(self.runtime), runtime_before)
        self.assertEqual(_inventory(self.unit_parent), units_before)
        authority = installer.apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["status"], "completed")

    def test_unit_staging_partial_write_with_growth_replays_forward(
        self,
    ) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        installer = self.unit_permission_installer()
        staging_name, _retired_name = installer._replacement_names(
            self.unit_operation_id
        )
        fault_patch, did_fault = self.partial_exact_write_fault(
            installer,
            target_name=staging_name,
            fault_point="file-fsync",
        )
        growth_patch, growth = self.grow_unit_parent_on_staging(installer)
        with (
            growth_patch,
            fault_patch,
            self.assertRaisesRegex(RuntimeError, "file-fsync fault"),
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertTrue(did_fault())
        self.assertGreater(growth["after_cleanup"], growth["before"])
        durable = json.loads(
            installer._unit_transaction_path(
                self.unit_operation_id
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            durable["replacement_checkpoint"],
            "staging-create-intent",
        )
        runtime_before = _inventory(self.runtime)
        units_before = _inventory(self.unit_parent)
        replay = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        self.assertEqual(replay["plan_sha256"], planned["plan_sha256"])
        self.assertEqual(_inventory(self.runtime), runtime_before)
        self.assertEqual(_inventory(self.unit_parent), units_before)
        authority = self.unit_permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertFalse((self.unit_parent / staging_name).exists())

    def test_unit_staging_exact_preplant_fails_before_forward_intent(
        self,
    ) -> None:
        installer = self.unit_permission_installer()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        staging_name, _retired_name = installer._replacement_names(
            self.unit_operation_id
        )
        staging_path = self.unit_parent / staging_name
        _write_private(staging_path, self.md_unit_payload, 0o600)
        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "staging namespace predates operation intent",
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        transaction = installer._load_unit_transaction(
            self.unit_operation_id
        )
        self.assertIsNone(transaction)
        self.assertFalse(
            installer._unit_transaction_path(
                self.unit_operation_id
            ).exists()
        )
        staging_path.unlink()

    def test_unit_staging_unsafe_intent_residue_fails_closed(self) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )

        def crash(phase: str) -> None:
            if phase == "unit-staging-create-intent":
                raise RuntimeError("crash before staging residue")

        with self.assertRaisesRegex(RuntimeError, "before staging residue"):
            self.unit_permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        installer = self.unit_permission_installer()
        staging_name, _retired_name = installer._replacement_names(
            self.unit_operation_id
        )
        staging_path = self.unit_parent / staging_name
        _write_private(staging_path, b"not-an-expected-prefix\n", 0o600)
        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "staging is not operation-owned",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
            )
        staging_path.unlink()
        staging_path.symlink_to(self.md_unit)
        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "staging is not operation-owned",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
            )

    def test_unit_permission_exchange_crash_replays_forward(self) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        crashed = False

        def crash(phase: str) -> None:
            nonlocal crashed
            if phase == "unit-replacement-exchanged" and not crashed:
                crashed = True
                raise RuntimeError("crash after unit exchange")

        with self.assertRaisesRegex(RuntimeError, "after unit exchange"):
            self.unit_permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        staging, _retired = self.unit_permission_installer()._replacement_names(
            self.unit_operation_id
        )
        self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o600)
        self.assertTrue((self.unit_parent / staging).exists())
        self.assertEqual(self.unit_reload_calls, 0)

        authority = self.unit_permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertTrue(crashed)
        self.assertEqual(authority["status"], "completed")
        self.assertFalse((self.unit_parent / staging).exists())
        self.assertEqual(self.unit_reload_calls, 1)

    def test_unit_exchange_and_retired_prejournal_crashes_replay_forward(
        self,
    ) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        installer = self.unit_permission_installer()
        durable_write = installer._write_unit_transaction
        exchanged_crashed = False

        def crash_before_exchanged(
            document: dict[str, object],
        ) -> None:
            nonlocal exchanged_crashed
            if (
                document.get("replacement_checkpoint") == "exchanged"
                and not exchanged_crashed
            ):
                exchanged_crashed = True
                raise RuntimeError("crash before exchanged journal")
            durable_write(document)

        with mock.patch.object(
            installer,
            "_write_unit_transaction",
            side_effect=crash_before_exchanged,
        ), self.assertRaisesRegex(RuntimeError, "before exchanged journal"):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertTrue(exchanged_crashed)
        staging_name, _retired_name = installer._replacement_names(
            self.unit_operation_id
        )
        staging_path = self.unit_parent / staging_name
        self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o600)
        self.assertTrue(staging_path.exists())
        durable = installer._load_unit_transaction(self.unit_operation_id)
        self.assertIsNotNone(durable)
        assert durable is not None
        self.assertEqual(durable["replacement_checkpoint"], "staged")
        self.assertIsNone(durable["replacement"])

        replay = self.unit_permission_installer()
        replay_write = replay._write_unit_transaction
        retired_crashed = False

        def crash_before_retired(
            document: dict[str, object],
        ) -> None:
            nonlocal retired_crashed
            if (
                document.get("replacement_checkpoint") == "retired-unlinked"
                and not retired_crashed
            ):
                retired_crashed = True
                raise RuntimeError("crash before retired journal")
            replay_write(document)

        with mock.patch.object(
            replay,
            "_write_unit_transaction",
            side_effect=crash_before_retired,
        ), self.assertRaisesRegex(RuntimeError, "before retired journal"):
            replay.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertTrue(retired_crashed)
        self.assertFalse(staging_path.exists())
        durable = replay._load_unit_transaction(self.unit_operation_id)
        self.assertIsNotNone(durable)
        assert durable is not None
        self.assertEqual(durable["replacement_checkpoint"], "exchanged")
        self.assertIsInstance(durable["replacement"], dict)

        authority = self.unit_permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o600)

    def test_unit_permission_abort_boundary_and_backup_ownership(self) -> None:
        planned = self.unit_permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )

        def crash(phase: str) -> None:
            if phase == "unit-permission-intent":
                raise RuntimeError("crash at abortable unit intent")

        with self.assertRaisesRegex(RuntimeError, "abortable unit intent"):
            self.unit_permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        with mock.patch.object(
            ADOPTER.os,
            "fsync",
            side_effect=AssertionError("wrong confirmation wrote state"),
        ), self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "confirmation differs",
        ):
            self.unit_permission_installer().abort(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256="sha256:" + "0" * 64,
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        aborted = self.unit_permission_installer().abort(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(aborted["status"], "aborted")
        self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o664)
        self.assertFalse(
            (
                self.runtime
                / ADOPTER.UNIT_PERMISSION_BACKUP_DIRECTORY
                / self.unit_operation_id
            ).exists()
        )

        def crash_after_terminal_reseal(phase: str) -> None:
            if phase == "unit-permission-journal-resealed":
                raise RuntimeError("crash after aborted unit reseal")

        with self.assertRaisesRegex(RuntimeError, "aborted unit"):
            self.unit_permission_installer(
                crash_after_terminal_reseal
            ).abort(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        replayed = self.unit_permission_installer().abort(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_unit_permission_impact_sha256=planned[
                "unit_permission_impact_sha256"
            ],
        )
        self.assertEqual(replayed, aborted)

    def test_unit_permission_rejects_preexisting_unowned_backup(self) -> None:
        installer = self.unit_permission_installer()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        backup_root = (
            self.runtime / ADOPTER.UNIT_PERMISSION_BACKUP_DIRECTORY
        )
        operation_backup = backup_root / self.unit_operation_id
        backup_root.mkdir(mode=0o700)
        backup_root.chmod(0o700)
        operation_backup.mkdir(mode=0o700)
        operation_backup.chmod(0o700)

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "not created by this transaction"
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o664)

    def test_unit_permission_rejects_unsafe_backup_root(self) -> None:
        installer = self.unit_permission_installer()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        backup_root = (
            self.runtime / ADOPTER.UNIT_PERMISSION_BACKUP_DIRECTORY
        )
        backup_root.mkdir(mode=0o700)
        backup_root.chmod(0o755)
        md_identity = self.unit_file_identity(self.md_unit)

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "required private directory is unsafe"
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertIsNone(
            installer._load_unit_transaction(self.unit_operation_id)
        )
        self.assertEqual(self.unit_file_identity(self.md_unit), md_identity)
        self.assertEqual(self.unit_reload_calls, 0)

    def test_unit_permission_rejects_backup_mkdir_race_after_claim(self) -> None:
        installer = self.unit_permission_installer()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        operation_backup = (
            self.runtime
            / ADOPTER.UNIT_PERMISSION_BACKUP_DIRECTORY
            / self.unit_operation_id
        )
        original = installer._open_or_create_private_directory_at
        raced = False

        def race(parent_fd: int, name: str):  # type: ignore[no-untyped-def]
            nonlocal raced
            if name == self.unit_operation_id and not raced:
                raced = True
                operation_backup.mkdir(mode=0o700)
                operation_backup.chmod(0o700)
            return original(parent_fd, name)

        with mock.patch.object(
            installer,
            "_open_or_create_private_directory_at",
            side_effect=race,
        ), self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "appeared before operation ownership"
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_unit_permission_impact_sha256=planned[
                    "unit_permission_impact_sha256"
                ],
            )
        self.assertTrue(raced)
        self.assertEqual(set(operation_backup.iterdir()), set())
        self.assertEqual(stat.S_IMODE(self.md_unit.stat().st_mode), 0o664)

    def test_prepared_ref_absence_observer_uses_real_git_semantics(self) -> None:
        installer = self.unit_permission_installer()
        reference = "refs/nexpoly/prepared/deploy-ref-observer-test"
        self.assertRegex(
            _run(self.production, "/usr/bin/git", "--version"),
            r"^git version 2\.",
        )
        installer._assert_prepared_ref_absent(reference)

        _run(
            self.production,
            "/usr/bin/git",
            "update-ref",
            reference,
            self.production_sha,
        )
        with self.assertRaisesRegex(ADOPTER.PrerequisiteError, "ref remains"):
            installer._assert_prepared_ref_absent(reference)
        _run(self.production, "/usr/bin/git", "update-ref", "-d", reference)

        _run(
            self.production,
            "/usr/bin/git",
            "tag",
            "-a",
            "prepared-observer-tag",
            "-m",
            "prepared observer tag",
        )
        tag_object = _run(
            self.production,
            "/usr/bin/git",
            "rev-parse",
            "refs/tags/prepared-observer-tag",
        )
        _run(
            self.production,
            "/usr/bin/git",
            "update-ref",
            reference,
            tag_object,
        )
        with self.assertRaisesRegex(ADOPTER.PrerequisiteError, "ref remains"):
            installer._assert_prepared_ref_absent(reference)
        _run(self.production, "/usr/bin/git", "update-ref", "-d", reference)

        _run(
            self.production,
            "/usr/bin/git",
            "update-ref",
            reference,
            self.production_tree,
        )
        with self.assertRaisesRegex(ADOPTER.PrerequisiteError, "ref remains"):
            installer._assert_prepared_ref_absent(reference)
        _run(self.production, "/usr/bin/git", "update-ref", "-d", reference)

        _run(
            self.production,
            "/usr/bin/git",
            "symbolic-ref",
            reference,
            "refs/heads/main",
        )
        with self.assertRaisesRegex(ADOPTER.PrerequisiteError, "symbolic ref"):
            installer._assert_prepared_ref_absent(reference)

    def test_prepare_abort_rejects_unrecorded_handoff_archive(self) -> None:
        installer, archive = self.prepare_abort_residue()
        installer._validate_prepare_abort_gate()
        _write_private(
            archive / "control-handoff.json",
            b"{}\n",
            0o600,
        )

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "unrecorded prepare handoff"
        ):
            installer._validate_prepare_abort_gate()

    def test_prepare_abort_rejects_unrecorded_ref_archive_symlink(self) -> None:
        installer, archive = self.prepare_abort_residue()
        installer._validate_prepare_abort_gate()
        evidence = archive / "prepared-ref.json"
        evidence.symlink_to(archive / "missing-prepared-ref-target.json")
        self.assertFalse(evidence.exists())
        self.assertTrue(evidence.is_symlink())

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "unrecorded prepared ref"
        ):
            installer._validate_prepare_abort_gate()

    def test_prepare_abort_rejects_orphan_journal_when_prepared_empty(
        self,
    ) -> None:
        installer, _archive = self.prepare_abort_residue()
        operation = (
            self.runtime
            / "state/prepared/deploy-prepare-abort-test"
        )
        operation.rmdir()
        runtime_before = _inventory(self.runtime)

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "must be live or archived exactly once"
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
            )
        self.assertEqual(_inventory(self.runtime), runtime_before)

    def test_prepare_abort_rejects_corrupt_journal_when_prepared_empty(
        self,
    ) -> None:
        installer, _archive = self.prepare_abort_residue()
        operation = (
            self.runtime
            / "state/prepared/deploy-prepare-abort-test"
        )
        operation.rmdir()
        journal_path = (
            self.runtime
            / "state/prepare-aborts/deploy-prepare-abort-test.json"
        )
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal["unsealed_field"] = True
        _write_private(
            journal_path,
            json.dumps(journal, sort_keys=True).encode() + b"\n",
            0o600,
        )

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "journal has an invalid shape"
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
            )

    def test_prepare_abort_schema_discriminators_require_exact_int(
        self,
    ) -> None:
        installer, _archive = self.prepare_abort_residue()
        operation_id = "deploy-prepare-abort-test"
        journal_path = (
            self.runtime / "state/prepare-aborts" / f"{operation_id}.json"
        )
        original = json.loads(journal_path.read_text(encoding="utf-8"))

        for bad_version in (True, 1.0):
            for discriminator, expected_error in (
                ("journal", "journal has an invalid shape"),
                ("owner", "owner identity is invalid"),
                ("active-slot", "active slot is invalid"),
                ("handoff", "handoff schema is invalid"),
            ):
                document = json.loads(json.dumps(original))
                if discriminator == "journal":
                    document["schema_version"] = bad_version
                elif discriminator == "owner":
                    document["prepare_owner"]["schema_version"] = bad_version
                    document["prepare_owner_sha256"] = (
                        ADOPTER._canonical_digest(document["prepare_owner"])
                    )
                elif discriminator == "active-slot":
                    active_slot = {
                        "schema_version": bad_version,
                        "component": "monomer-md",
                        "slot": "a",
                        "source_sha": self.sha,
                        "source_tree": self.production_tree,
                        "worker_lock_sha256": "sha256:" + "5" * 64,
                        "slot_record_sha256": "sha256:" + "6" * 64,
                        "operation_id": operation_id,
                        "activated_at": "2026-08-14T18:06:48Z",
                    }
                    document["active_slot"] = active_slot
                    document["active_slot_sha256"] = (
                        ADOPTER._canonical_digest(active_slot)
                    )
                else:
                    document["target_tree"] = self.production_tree
                    document["control_handoff_sha256"] = (
                        "sha256:" + "7" * 64
                    )
                    document["control_handoff_schema_version"] = bad_version
                    document["executor_control_sha256"] = (
                        "sha256:" + "8" * 64
                    )
                with self.subTest(
                    discriminator=discriminator,
                    bad_version=bad_version,
                ), self.assertRaisesRegex(
                    ADOPTER.PrerequisiteError,
                    expected_error,
                ):
                    installer._validate_prepare_abort_journal_record(
                        document,
                        operation_id,
                    )

    def test_prepare_abort_archive_owner_schema_requires_exact_int(
        self,
    ) -> None:
        installer, archive = self.prepare_abort_residue()
        owner_path = archive / "ARCHIVE-OWNER.json"
        original = json.loads(owner_path.read_text(encoding="utf-8"))

        for bad_version in (True, 1.0):
            owner = dict(original)
            owner["schema_version"] = bad_version
            _write_private(
                owner_path,
                json.dumps(owner, sort_keys=True).encode() + b"\n",
                0o600,
            )
            with self.subTest(bad_version=bad_version), self.assertRaisesRegex(
                ADOPTER.PrerequisiteError,
                "archive owner differs",
            ):
                installer._validate_prepare_abort_gate()
        _write_private(
            owner_path,
            json.dumps(original, sort_keys=True).encode() + b"\n",
            0o600,
        )

    def test_prepare_abort_rejects_orphan_handoff_and_prepared_ref(self) -> None:
        installer = self.unit_permission_installer()
        handoffs = self.runtime / "state/control-handoffs"
        handoffs.mkdir(mode=0o700)
        _write_private(handoffs / "orphan-operation.json", b"{}\n", 0o600)
        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "orphan entry"
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
            )
        (handoffs / "orphan-operation.json").unlink()
        reference = "refs/nexpoly/prepared/orphan-operation"
        _run(
            self.production,
            "/usr/bin/git",
            "update-ref",
            reference,
            self.production_sha,
        )
        prepared_ref = self.production / ".git" / reference
        prepared_ref.chmod(0o600)
        prepared_ref.parent.chmod(0o700)
        prepared_ref.parent.parent.chmod(0o700)
        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "prepared Git ref remains"
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.unit_operation_id,
            )

    def test_prepare_abort_accepts_archived_operation_lost_response_zero_write(
        self,
    ) -> None:
        installer, archive = self.prepare_abort_residue()
        operation = (
            self.runtime
            / "state/prepared/deploy-prepare-abort-test"
        )
        os.rename(operation, archive / "operation")
        runtime_before = _inventory(self.runtime)
        unit_before = _inventory(self.unit_parent)

        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.unit_operation_id,
        )
        self.assertTrue(planned["logical_zero_write"])
        self.assertEqual(_inventory(self.runtime), runtime_before)
        self.assertEqual(_inventory(self.unit_parent), unit_before)

    def test_permission_change_intent_without_marker_replays_forward(self) -> None:
        planned = self.permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
        )

        def crash(phase: str) -> None:
            if phase == "permission-change-intent":
                raise RuntimeError("crash before first marker")

        with self.assertRaisesRegex(RuntimeError, "before first marker"):
            self.permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        self.assertFalse(
            self.permission_installer().permission_marker_path.exists()
        )
        self.assertEqual(
            self.permission_installer().plan(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
            ),
            planned,
        )
        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "forward-only",
        ):
            self.permission_installer().abort(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        completed = self.permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_permission_impact_sha256=planned[
                "permission_impact_sha256"
            ],
        )
        self.assertEqual(completed["status"], "completed")

    def test_permission_change_intent_parent_fsync_lost_response_reseals(
        self,
    ) -> None:
        planned = self.permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
        )
        installer = self.permission_installer()
        fault_patch, lost = self.journal_parent_fsync_lost_response(
            installer,
            writer_name="_write_permission_transaction",
            predicate=lambda document: (
                document.get("phase") == "permission-change-intent"
            ),
        )
        with fault_patch, self.assertRaisesRegex(
            RuntimeError,
            "journal parent fsync response lost",
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        visible = installer._load_permission_transaction(
            self.permission_operation_id
        )
        self.assertEqual(visible, lost["document"])
        self.assertFalse(installer.permission_marker_path.exists())
        self.assertEqual(stat.S_IMODE(self.production.stat().st_mode), 0o755)

        resealed = False

        def second_crash(phase: str) -> None:
            nonlocal resealed
            if phase == "permission-journal-resealed" and not resealed:
                resealed = True
                raise RuntimeError(
                    "second crash after permission journal reseal"
                )

        with self.assertRaisesRegex(RuntimeError, "second crash"):
            self.permission_installer(second_crash).apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        self.assertTrue(resealed)
        self.assertFalse(installer.permission_marker_path.exists())
        self.assertEqual(
            installer._load_permission_transaction(
                self.permission_operation_id
            ),
            visible,
        )

        authority = self.permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_permission_impact_sha256=planned[
                "permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["status"], "completed")

    def test_permission_transaction_directory_lost_response_reseals(
        self,
    ) -> None:
        planned = self.permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
        )
        transaction_root = (
            self.runtime / ADOPTER.PERMISSION_TRANSACTION_DIRECTORY
        )
        durable_mkdir = ADOPTER.os.mkdir
        created = False

        def mkdir_then_crash(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal created
            result = durable_mkdir(path, *args, **kwargs)
            if (
                path == ADOPTER.PERMISSION_TRANSACTION_DIRECTORY.name
                and not created
            ):
                created = True
                raise RuntimeError(
                    "permission transaction directory mkdir response lost"
                )
            return result

        with mock.patch.object(
            ADOPTER.os,
            "mkdir",
            side_effect=mkdir_then_crash,
        ), self.assertRaisesRegex(RuntimeError, "mkdir response lost"):
            self.permission_installer().apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        self.assertTrue(created)
        self.assertTrue(transaction_root.is_dir())
        self.assertEqual(list(transaction_root.iterdir()), [])

        state = self.runtime / "state"
        state_identity = (state.stat().st_dev, state.stat().st_ino)
        durable_fsync = ADOPTER.os.fsync
        state_resealed = False
        installer = self.permission_installer()
        durable_atomic = ADOPTER._atomic_owned_json_at

        def track_state_fsync(descriptor: int) -> None:
            nonlocal state_resealed
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) == state_identity:
                state_resealed = True
            durable_fsync(descriptor)

        def require_parent_reseal(
            directory_fd: int,
            name: str,
            document: object,
        ) -> None:
            self.assertTrue(state_resealed)
            durable_atomic(directory_fd, name, document)

        with mock.patch.object(
            ADOPTER.os,
            "fsync",
            side_effect=track_state_fsync,
        ), mock.patch.object(
            ADOPTER,
            "_atomic_owned_json_at",
            side_effect=require_parent_reseal,
        ):
            authority = installer.apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        self.assertTrue(state_resealed)
        self.assertEqual(authority["status"], "completed")

    def test_permission_authority_intent_parent_fsync_lost_response_reseals(
        self,
    ) -> None:
        planned = self.permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
        )
        installer = self.permission_installer()
        fault_patch, lost = self.journal_parent_fsync_lost_response(
            installer,
            writer_name="_write_permission_transaction",
            predicate=lambda document: (
                document.get("phase") == "authority-commit-intent"
            ),
        )
        with fault_patch, self.assertRaisesRegex(
            RuntimeError,
            "journal parent fsync response lost",
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        intent = installer._load_permission_transaction(
            self.permission_operation_id
        )
        self.assertEqual(intent, lost["document"])
        assert intent is not None
        completed_at = intent["completed_at"]
        self.assertIsInstance(completed_at, str)
        self.assertFalse(installer._permission_authority_exists())

        resealed = False

        def second_crash(phase: str) -> None:
            nonlocal resealed
            if phase == "permission-journal-resealed" and not resealed:
                resealed = True
                raise RuntimeError(
                    "second crash after permission journal reseal"
                )

        with self.assertRaisesRegex(RuntimeError, "second crash"):
            self.permission_installer(second_crash).apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        self.assertTrue(resealed)
        self.assertFalse(installer._permission_authority_exists())

        authority = self.permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_permission_impact_sha256=planned[
                "permission_impact_sha256"
            ],
        )
        self.assertEqual(authority["completed_at"], completed_at)
        final = installer._load_permission_transaction(
            self.permission_operation_id
        )
        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual(final["completed_at"], completed_at)

    def test_permission_abort_is_allowed_only_before_change_intent(self) -> None:
        planned = self.permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
        )

        def crash(phase: str) -> None:
            if phase == "permission-intent":
                raise RuntimeError("crash at abortable intent")

        with self.assertRaisesRegex(RuntimeError, "abortable intent"):
            self.permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        with mock.patch.object(
            ADOPTER.os,
            "fsync",
            side_effect=AssertionError("wrong confirmation wrote state"),
        ), self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "confirmation differs",
        ):
            self.permission_installer().abort(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256="sha256:" + "0" * 64,
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        aborted = self.permission_installer().abort(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_permission_impact_sha256=planned[
                "permission_impact_sha256"
            ],
        )
        self.assertEqual(aborted["status"], "aborted")
        self.assertFalse(
            self.permission_installer().permission_marker_path.exists()
        )
        self.assertEqual(stat.S_IMODE(self.production.stat().st_mode), 0o755)

        def crash_after_terminal_reseal(phase: str) -> None:
            if phase == "permission-journal-resealed":
                raise RuntimeError("crash after aborted permission reseal")

        with self.assertRaisesRegex(RuntimeError, "aborted permission"):
            self.permission_installer(crash_after_terminal_reseal).abort(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        replayed = self.permission_installer().abort(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_permission_impact_sha256=planned[
                "permission_impact_sha256"
            ],
        )
        self.assertEqual(replayed, aborted)

    def test_permission_authority_link_crash_replays_create_only(self) -> None:
        planned = self.permission_installer().plan(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
        )
        crashed = False

        def crash(phase: str) -> None:
            nonlocal crashed
            if phase == "authority-linked" and not crashed:
                crashed = True
                raise RuntimeError("crash after permission authority link")

        with self.assertRaisesRegex(RuntimeError, "after permission authority"):
            self.permission_installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        authority_path = (
            self.runtime / ADOPTER.PERMISSION_AUTHORITY_PATH
        )
        self.assertTrue(authority_path.exists())
        self.assertEqual(authority_path.stat().st_nlink, 2)

        completed = self.permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_permission_impact_sha256=planned[
                "permission_impact_sha256"
            ],
        )
        self.assertTrue(crashed)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(authority_path.stat().st_nlink, 1)

    def test_create_owned_json_once_honors_large_explicit_limit(self) -> None:
        directory = self.runtime / "state"
        directory_fd = ADOPTER._open_private_directory(directory)
        try:
            ADOPTER._create_owned_json_once_at(
                directory_fd,
                "large-authority.json",
                {"payload": "x" * (9 * 1024 * 1024)},
                operation_id=self.operation_id,
                checkpoint=lambda _phase: None,
                maximum_bytes=10 * 1024 * 1024,
            )
        finally:
            os.close(directory_fd)
        authority = directory / "large-authority.json"
        self.assertGreater(authority.stat().st_size, 8 * 1024 * 1024)
        self.assertEqual(authority.stat().st_nlink, 1)

    def test_create_owned_json_once_rejects_oversize_before_staging(self) -> None:
        directory = self.runtime / "state"
        directory_fd = ADOPTER._open_private_directory(directory)
        try:
            with self.assertRaisesRegex(
                ADOPTER.PrerequisiteError,
                "authority is oversized",
            ):
                ADOPTER._create_owned_json_once_at(
                    directory_fd,
                    "oversized-authority.json",
                    {"payload": "x" * 2048},
                    operation_id=self.operation_id,
                    checkpoint=lambda _phase: None,
                    maximum_bytes=1024,
                )
        finally:
            os.close(directory_fd)
        target = directory / "oversized-authority.json"
        staging = directory / (
            f".{target.name}.create-{self.operation_id}"
        )
        quarantine = staging.with_name(staging.name + ".quarantine")
        self.assertFalse(target.exists())
        self.assertFalse(staging.exists())
        self.assertFalse(quarantine.exists())

    def test_permission_state_path_swap_fails_closed_and_same_op_recovers(
        self,
    ) -> None:
        installer = self.permission_installer()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
        )
        state = self.runtime / "state"
        displaced = self.runtime / "state-permission-displaced"
        replacement = self.runtime / "state-permission-replacement"
        replacement.mkdir(mode=0o700)
        swapped = False

        def swap(phase: str) -> None:
            nonlocal swapped
            if phase == "permission:captured" and not swapped:
                swapped = True
                os.rename(state, displaced)
                os.rename(replacement, state)

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "hardening did not complete|pinned prerequisite state",
        ):
            self.permission_installer(swap).apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        self.assertTrue(swapped)
        self.assertFalse(
            (self.runtime / ADOPTER.PERMISSION_AUTHORITY_PATH).exists()
        )

        os.rename(state, replacement)
        os.rename(displaced, state)
        completed = self.permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_permission_impact_sha256=planned[
                "permission_impact_sha256"
            ],
        )
        self.assertEqual(completed["status"], "completed")

    def test_permission_production_root_swap_before_publish_recovers(
        self,
    ) -> None:
        installer = self.permission_installer()
        planned = installer.plan(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
        )
        displaced = self.production.parent / "production-permission-displaced"
        replacement = self.production.parent / "production-permission-replacement"
        shutil.copytree(self.production, replacement, copy_function=shutil.copy2)
        swapped = False

        def swap(phase: str) -> None:
            nonlocal swapped
            if phase == "permission-authority-commit-intent" and not swapped:
                swapped = True
                os.rename(self.production, displaced)
                os.rename(replacement, self.production)

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "pinned production permission authority changed",
        ):
            self.permission_installer(swap).apply(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_permission_impact_sha256=planned[
                    "permission_impact_sha256"
                ],
            )
        self.assertTrue(swapped)
        self.assertFalse(
            (self.runtime / ADOPTER.PERMISSION_AUTHORITY_PATH).exists()
        )

        os.rename(self.production, replacement)
        os.rename(displaced, self.production)
        completed = self.permission_installer().apply(
            source_sha=self.sha,
            operation_id=self.permission_operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_permission_impact_sha256=planned[
                "permission_impact_sha256"
            ],
        )
        self.assertEqual(completed["status"], "completed")

    def test_permission_base_authority_atomic_swap_is_rejected(self) -> None:
        installer = self.permission_installer()
        base_path = self.runtime / ADOPTER.AUTHORITY_PATH
        original_reader = installer._read_adoption_permission_authorities
        original_inode = base_path.stat().st_ino
        replaced = False

        def read_then_replace():  # type: ignore[no-untyped-def]
            nonlocal replaced
            observed = original_reader()
            if not replaced:
                replacement = json.loads(base_path.read_text(encoding="utf-8"))
                replacement["completed_at"] = "2099-01-01T00:00:00Z"
                staging = base_path.parent / ".base-authority-replacement"
                _write_private(
                    staging,
                    json.dumps(replacement, sort_keys=True).encode() + b"\n",
                    0o600,
                )
                os.replace(staging, base_path)
                replaced = True
            return observed

        with mock.patch.object(
            installer,
            "_read_adoption_permission_authorities",
            side_effect=read_then_replace,
        ), self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "authorities changed while validating",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
            )
        self.assertTrue(replaced)
        self.assertNotEqual(base_path.stat().st_ino, original_inode)

    def test_permission_base_authority_in_place_write_is_rejected(self) -> None:
        installer = self.permission_installer()
        base_path = self.runtime / ADOPTER.AUTHORITY_PATH
        original_inode = base_path.stat().st_ino
        original_reader = ADOPTER._descriptor_bytes
        rewritten = False

        def rewrite_open_inode(
            descriptor: int,
            *,
            maximum_bytes: int,
        ) -> bytes:
            nonlocal rewritten
            payload = original_reader(
                descriptor,
                maximum_bytes=maximum_bytes,
            )
            try:
                opened_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            except OSError:
                opened_path = Path("")
            if opened_path == base_path and not rewritten:
                with base_path.open("r+b", buffering=0) as stream:
                    stream.seek(0)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                rewritten = True
            return payload

        with mock.patch.object(
            ADOPTER,
            "_descriptor_bytes",
            side_effect=rewrite_open_inode,
        ), self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "changed while reading",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
            )
        self.assertTrue(rewritten)
        self.assertEqual(base_path.stat().st_ino, original_inode)

    def test_permission_plan_is_restricted_to_raw_manual_adoption(self) -> None:
        installer = self.permission_installer()
        _write_private(
            self.runtime / "state/current-deployment.json",
            b"{}\n",
            0o600,
        )
        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "restricted to raw manual adoption",
        ):
            installer.plan(
                source_sha=self.sha,
                operation_id=self.permission_operation_id,
            )
        self.assertFalse(installer.permission_marker_path.exists())

    def test_locked_apply_recomputes_plan_after_target_race(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        first = planned["plan"]["files"][0]
        target = Path(first["destination"])

        def race(phase: str) -> None:
            if phase == "apply-lock-acquired":
                _write_private(
                    target,
                    (self.source / first["source_path"]).read_bytes(),
                    int(first["mode"], 8),
                )

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "plan changed before locked apply"
        ):
            self.installer(race).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.assertTrue(target.exists())
        self.assertFalse(
            (self.runtime / ADOPTER.TRANSACTION_DIRECTORY).exists()
        )

    def test_eexist_after_intent_never_acquires_or_aborts_foreign_target(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        first = planned["plan"]["files"][0]
        target = Path(first["destination"])
        raced = False

        def race(phase: str) -> None:
            nonlocal raced
            if phase == "install-intent:bootstrap-quiesce" and not raced:
                raced = True
                _write_private(
                    target,
                    (self.source / first["source_path"]).read_bytes(),
                    int(first["mode"], 8),
                )

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "without operation ownership"
        ):
            self.installer(race).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        foreign_identity = (target.stat().st_dev, target.stat().st_ino)
        aborted = self.installer().abort(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(aborted["status"], "aborted")
        self.assertEqual((target.stat().st_dev, target.stat().st_ino), foreign_identity)

    def test_distinct_operation_cannot_claim_completed_authority(self) -> None:
        other_operation = "adopt-prereq-test-0002"
        first = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        second = self.installer().plan(
            source_sha=self.sha, operation_id=other_operation
        )
        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=first["plan_sha256"],
        )

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "another authority"
        ):
            self.installer().apply(
                source_sha=self.sha,
                operation_id=other_operation,
                confirm_plan_sha256=second["plan_sha256"],
            )
        self.assertEqual(
            json.loads((self.runtime / ADOPTER.AUTHORITY_PATH).read_text()), authority
        )

    def test_abort_removes_only_operation_created_exact_files(self) -> None:
        existing_record = ADOPTER.TRACKED_INSTALLS[4]
        source_path, name, mode, _classification = existing_record
        existing = self.runtime / "config" / name
        _write_private(existing, (self.source / source_path).read_bytes(), mode)
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "target-created:bootstrap-quiesce":
                raise RuntimeError("injected crash")

        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        with mock.patch.object(
            ADOPTER.os,
            "fsync",
            side_effect=AssertionError("wrong confirmation wrote state"),
        ), self.assertRaisesRegex(
            ADOPTER.PrerequisiteError,
            "confirmation differs",
        ):
            self.installer().abort(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256="sha256:" + "0" * 64,
            )
        aborted = self.installer().abort(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )

        self.assertEqual(aborted["status"], "aborted")
        self.assertFalse((self.runtime / "config/bootstrap-quiesce").exists())
        self.assertTrue(existing.exists())
        self.assertEqual(
            (self.runtime / "config/mutable-data-audit.pgpass").read_bytes(),
            self.pgpass_payload,
        )

        def crash_after_terminal_reseal(phase: str) -> None:
            if phase == "prerequisite-journal-resealed":
                raise RuntimeError("crash after aborted prerequisite reseal")

        with self.assertRaisesRegex(RuntimeError, "aborted prerequisite"):
            self.installer(crash_after_terminal_reseal).abort(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        replayed = self.installer().abort(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(replayed, aborted)

    def test_abort_refuses_cas_drift(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "target-created:bootstrap-quiesce":
                raise RuntimeError("injected crash")

        with self.assertRaises(RuntimeError):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        target = self.runtime / "config/bootstrap-quiesce"
        target.write_bytes(b"drift\n")
        target.chmod(0o700)
        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "identity differs"
        ):
            self.installer().abort(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )

    def test_abort_quarantine_never_unlinks_substituted_inode(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "target-created:bootstrap-quiesce":
                raise RuntimeError("injected crash")

        with self.assertRaises(RuntimeError):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        target = self.runtime / "config/bootstrap-quiesce"
        replacement = self.runtime / "config/.replacement"
        original_rename = ADOPTER._rename_noreplace
        substituted = False

        def substitute_then_rename(
            source_directory: int,
            source_name: str,
            target_directory: int,
            target_name: str,
        ) -> None:
            nonlocal substituted
            if source_name == "bootstrap-quiesce" and not substituted:
                substituted = True
                _write_private(replacement, target.read_bytes(), 0o700)
                os.replace(replacement, target)
            original_rename(
                source_directory,
                source_name,
                target_directory,
                target_name,
            )

        with mock.patch.object(
            ADOPTER, "_rename_noreplace", side_effect=substitute_then_rename
        ):
            with self.assertRaisesRegex(
                ADOPTER.PrerequisiteError, "raced during quarantine"
            ):
                self.installer().abort(
                    source_sha=self.sha,
                    operation_id=self.operation_id,
                    confirm_plan_sha256=planned["plan_sha256"],
                )
        self.assertTrue(substituted)
        self.assertTrue(target.is_file())
        self.assertEqual(
            ADOPTER._file_digest(target), planned["plan"]["files"][0]["sha256"]
        )

    def test_abort_refuses_completed_authority(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        with self.assertRaisesRegex(ADOPTER.PrerequisiteError, "cannot be aborted"):
            self.installer().abort(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )

    def test_prerequisite_install_intent_parent_fsync_lost_response_reseals(
        self,
    ) -> None:
        planned = self.installer().plan(
            source_sha=self.sha,
            operation_id=self.operation_id,
        )
        first = planned["plan"]["files"][0]
        target = Path(first["destination"])
        installer = self.installer()
        fault_patch, lost = self.journal_parent_fsync_lost_response(
            installer,
            writer_name="_write_transaction",
            predicate=lambda document: (
                document.get("phase") == "installing"
                and document.get("install_intent") == first["name"]
            ),
        )
        with fault_patch, self.assertRaisesRegex(
            RuntimeError,
            "journal parent fsync response lost",
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        visible = installer._load_transaction(self.operation_id)
        self.assertEqual(visible, lost["document"])
        self.assertFalse(target.exists())

        resealed = False

        def second_crash(phase: str) -> None:
            nonlocal resealed
            if phase == "prerequisite-journal-resealed" and not resealed:
                resealed = True
                raise RuntimeError(
                    "second crash after prerequisite journal reseal"
                )

        with self.assertRaisesRegex(RuntimeError, "second crash"):
            self.installer(second_crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.assertTrue(resealed)
        self.assertFalse(target.exists())
        self.assertEqual(installer._load_transaction(self.operation_id), visible)

        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["status"], "completed")

    def test_prerequisite_transaction_directory_lost_response_reseals(
        self,
    ) -> None:
        planned = self.installer().plan(
            source_sha=self.sha,
            operation_id=self.operation_id,
        )
        transaction_root = self.runtime / ADOPTER.TRANSACTION_DIRECTORY
        durable_mkdir = ADOPTER.os.mkdir
        created = False

        def mkdir_then_crash(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal created
            result = durable_mkdir(path, *args, **kwargs)
            if path == ADOPTER.TRANSACTION_DIRECTORY.name and not created:
                created = True
                raise RuntimeError("transaction directory mkdir response lost")
            return result

        with mock.patch.object(
            ADOPTER.os,
            "mkdir",
            side_effect=mkdir_then_crash,
        ), self.assertRaisesRegex(RuntimeError, "mkdir response lost"):
            self.installer().apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.assertTrue(created)
        self.assertTrue(transaction_root.is_dir())
        self.assertEqual(list(transaction_root.iterdir()), [])

        state = self.runtime / "state"
        state_identity = (state.stat().st_dev, state.stat().st_ino)
        durable_fsync = ADOPTER.os.fsync
        state_resealed = False
        installer = self.installer()
        durable_write = installer._write_transaction

        def track_state_fsync(descriptor: int) -> None:
            nonlocal state_resealed
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) == state_identity:
                state_resealed = True
            durable_fsync(descriptor)

        def require_parent_reseal(document: dict[str, object]) -> None:
            self.assertTrue(state_resealed)
            durable_write(document)

        with mock.patch.object(
            ADOPTER.os,
            "fsync",
            side_effect=track_state_fsync,
        ), mock.patch.object(
            installer,
            "_write_transaction",
            side_effect=require_parent_reseal,
        ):
            authority = installer.apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.assertTrue(state_resealed)
        self.assertEqual(authority["status"], "completed")

    def test_prerequisite_target_link_two_failure_replay_reseals_config(
        self,
    ) -> None:
        planned = self.installer().plan(
            source_sha=self.sha,
            operation_id=self.operation_id,
        )
        first = planned["plan"]["files"][0]
        target = Path(first["destination"])
        staging = target.with_name(
            f".adopt-prereq-{self.operation_id}-{first['name']}.tmp"
        )
        durable_link = ADOPTER.os.link
        linked = False

        def link_then_crash(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal linked
            result = durable_link(*args, **kwargs)
            destination = args[1] if len(args) > 1 else kwargs.get("dst")
            if destination == first["name"] and not linked:
                linked = True
                raise RuntimeError("crash before target link parent fsync")
            return result

        with mock.patch.object(
            ADOPTER.os,
            "link",
            side_effect=link_then_crash,
        ), self.assertRaisesRegex(RuntimeError, "target link"):
            self.installer().apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.assertTrue(linked)
        self.assertTrue(target.exists())
        self.assertTrue(staging.exists())
        self.assertEqual(target.stat().st_ino, staging.stat().st_ino)
        self.assertEqual(target.stat().st_nlink, 2)

        config_identity = (
            target.parent.stat().st_dev,
            target.parent.stat().st_ino,
        )
        durable_fsync = ADOPTER.os.fsync
        resealed = False

        def fsync_then_crash(descriptor: int) -> None:
            nonlocal resealed
            durable_fsync(descriptor)
            metadata = os.fstat(descriptor)
            if (
                (metadata.st_dev, metadata.st_ino) == config_identity
                and target.exists()
                and staging.exists()
                and not resealed
            ):
                resealed = True
                raise RuntimeError("second crash after target namespace reseal")

        with mock.patch.object(
            ADOPTER.os,
            "fsync",
            side_effect=fsync_then_crash,
        ), self.assertRaisesRegex(RuntimeError, "second crash"):
            self.installer().apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.assertTrue(resealed)
        transaction = self.installer()._load_transaction(self.operation_id)
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertEqual(transaction["install_intent"], first["name"])
        self.assertEqual(transaction["installed"], [])

        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertFalse(staging.exists())
        self.assertEqual(target.stat().st_nlink, 1)

    def test_prerequisite_staging_unlink_two_failure_replay_reseals_config(
        self,
    ) -> None:
        planned = self.installer().plan(
            source_sha=self.sha,
            operation_id=self.operation_id,
        )
        first = planned["plan"]["files"][0]
        target = Path(first["destination"])
        staging = target.with_name(
            f".adopt-prereq-{self.operation_id}-{first['name']}.tmp"
        )
        quarantine = target.with_name(
            f".adopt-prereq-{self.operation_id}-"
            f"{first['name']}.staging-quarantine"
        )
        durable_unlink = ADOPTER.os.unlink
        unlinked = False

        def unlink_then_crash(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal unlinked
            result = durable_unlink(path, *args, **kwargs)
            if path == quarantine.name and not unlinked:
                unlinked = True
                raise RuntimeError("crash before staging unlink parent fsync")
            return result

        with mock.patch.object(
            ADOPTER.os,
            "unlink",
            side_effect=unlink_then_crash,
        ), self.assertRaisesRegex(RuntimeError, "staging unlink"):
            self.installer().apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.assertTrue(unlinked)
        self.assertTrue(target.exists())
        self.assertFalse(staging.exists())
        self.assertFalse(quarantine.exists())
        self.assertEqual(target.stat().st_nlink, 1)

        config_identity = (
            target.parent.stat().st_dev,
            target.parent.stat().st_ino,
        )
        durable_fsync = ADOPTER.os.fsync
        resealed = False

        def fsync_then_crash(descriptor: int) -> None:
            nonlocal resealed
            durable_fsync(descriptor)
            metadata = os.fstat(descriptor)
            if (
                (metadata.st_dev, metadata.st_ino) == config_identity
                and not staging.exists()
                and not quarantine.exists()
                and not resealed
            ):
                resealed = True
                raise RuntimeError("second crash after staging absence reseal")

        with mock.patch.object(
            ADOPTER.os,
            "fsync",
            side_effect=fsync_then_crash,
        ), self.assertRaisesRegex(RuntimeError, "second crash"):
            self.installer().apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.assertTrue(resealed)
        transaction = self.installer()._load_transaction(self.operation_id)
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertIn(first["name"], transaction["installed"])

        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["status"], "completed")

    def test_prerequisite_authority_intent_parent_fsync_lost_response_reseals(
        self,
    ) -> None:
        planned = self.installer().plan(
            source_sha=self.sha,
            operation_id=self.operation_id,
        )
        installer = self.installer()
        fault_patch, lost = self.journal_parent_fsync_lost_response(
            installer,
            writer_name="_write_transaction",
            predicate=lambda document: (
                document.get("phase") == "authority-commit-intent"
            ),
        )
        with fault_patch, self.assertRaisesRegex(
            RuntimeError,
            "journal parent fsync response lost",
        ):
            installer.apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        intent = installer._load_transaction(self.operation_id)
        self.assertEqual(intent, lost["document"])
        assert intent is not None
        completed_at = intent["completed_at"]
        self.assertIsInstance(completed_at, str)
        self.assertFalse((self.runtime / ADOPTER.AUTHORITY_PATH).exists())

        resealed = False

        def second_crash(phase: str) -> None:
            nonlocal resealed
            if phase == "prerequisite-journal-resealed" and not resealed:
                resealed = True
                raise RuntimeError(
                    "second crash after prerequisite journal reseal"
                )

        with self.assertRaisesRegex(RuntimeError, "second crash"):
            self.installer(second_crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.assertTrue(resealed)
        self.assertFalse((self.runtime / ADOPTER.AUTHORITY_PATH).exists())

        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["completed_at"], completed_at)
        final = installer._load_transaction(self.operation_id)
        self.assertIsNotNone(final)
        assert final is not None
        self.assertEqual(final["completed_at"], completed_at)

    def test_authority_commit_intent_is_replayed_and_cannot_abort(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "authority-commit-intent":
                raise RuntimeError("injected crash")

        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.assertFalse((self.runtime / ADOPTER.AUTHORITY_PATH).exists())
        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "authority commit cannot be aborted"
        ):
            self.installer().abort(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )

        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertEqual(
            json.loads((self.runtime / ADOPTER.AUTHORITY_PATH).read_text()),
            authority,
        )
        replayed = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(replayed, authority)

    def test_replay_uses_sealed_local_evidence_after_remote_main_advances(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "target-created:bootstrap-quiesce":
                raise RuntimeError("injected crash")

        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        advanced = self.advance_remote_tracking_ref()
        self.assertNotEqual(advanced, self.sha)

        def forbidden_probe(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("durable replay called a mutable source probe")

        authority = self.installer(
            delivery_probe=forbidden_probe,
            source_readiness_probe=forbidden_probe,
        ).apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertEqual(authority["plan"]["delivery_gate"], self.delivery_gate)

    def test_abort_uses_sealed_local_evidence_after_remote_main_advances(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "target-created:bootstrap-quiesce":
                raise RuntimeError("injected crash")

        with self.assertRaises(RuntimeError):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.advance_remote_tracking_ref()

        def forbidden_probe(*_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("durable abort called a mutable source probe")

        aborted = self.installer(
            delivery_probe=forbidden_probe,
            source_readiness_probe=forbidden_probe,
        ).abort(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(aborted["status"], "aborted")

    def test_partial_target_staging_write_is_recovered(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "install-intent:bootstrap-quiesce":
                raise RuntimeError("injected crash")

        with self.assertRaises(RuntimeError):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        temporary = self.runtime / "config" / (
            f".adopt-prereq-{self.operation_id}-bootstrap-quiesce.tmp"
        )
        _write_private(temporary, b"partial staging write", 0o700)

        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertFalse(temporary.exists())
        self.assertFalse(
            temporary.with_name(
                f".adopt-prereq-{self.operation_id}-bootstrap-quiesce.staging-quarantine"
            ).exists()
        )

    def test_transaction_temporary_quarantine_is_recovered(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "install-intent:bootstrap-quiesce":
                raise RuntimeError("injected crash")

        with self.assertRaises(RuntimeError):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        transaction = self.runtime / ADOPTER.TRANSACTION_DIRECTORY / (
            f"{self.operation_id}.json"
        )
        temporary = transaction.with_name(f".{transaction.name}.tmp")
        quarantine = transaction.with_name(f".{transaction.name}.tmp.quarantine")
        _write_private(temporary, b"partial journal", 0o600)
        os.rename(temporary, quarantine)

        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertFalse(temporary.exists())
        self.assertFalse(quarantine.exists())

    def test_staging_quarantine_sigkill_window_is_replayed(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        original_rename = ADOPTER._rename_noreplace
        crashed = False

        def rename_then_crash(
            source_directory: int,
            source_name: str,
            target_directory: int,
            target_name: str,
        ) -> None:
            nonlocal crashed
            original_rename(
                source_directory,
                source_name,
                target_directory,
                target_name,
            )
            if target_name.endswith("bootstrap-quiesce.staging-quarantine") and not crashed:
                crashed = True
                raise RuntimeError("injected sigkill window")

        with mock.patch.object(
            ADOPTER, "_rename_noreplace", side_effect=rename_then_crash
        ):
            with self.assertRaisesRegex(RuntimeError, "sigkill window"):
                self.installer().apply(
                    source_sha=self.sha,
                    operation_id=self.operation_id,
                    confirm_plan_sha256=planned["plan_sha256"],
                )
        quarantine = self.runtime / "config" / (
            f".adopt-prereq-{self.operation_id}-bootstrap-quiesce.staging-quarantine"
        )
        target = self.runtime / "config/bootstrap-quiesce"
        self.assertTrue(quarantine.exists())
        self.assertEqual(target.stat().st_nlink, 2)

        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertFalse(quarantine.exists())
        self.assertEqual(target.stat().st_nlink, 1)

    def test_abort_target_quarantine_sigkill_window_is_replayed(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash_after_ownership(phase: str) -> None:
            if phase == "ownership-recorded:bootstrap-quiesce":
                raise RuntimeError("injected apply crash")

        with self.assertRaises(RuntimeError):
            self.installer(crash_after_ownership).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        original_rename = ADOPTER._rename_noreplace
        crashed = False

        def rename_then_crash(
            source_directory: int,
            source_name: str,
            target_directory: int,
            target_name: str,
        ) -> None:
            nonlocal crashed
            original_rename(
                source_directory,
                source_name,
                target_directory,
                target_name,
            )
            if target_name.endswith("bootstrap-quiesce.abort-target") and not crashed:
                crashed = True
                raise RuntimeError("injected abort sigkill window")

        with mock.patch.object(
            ADOPTER, "_rename_noreplace", side_effect=rename_then_crash
        ):
            with self.assertRaisesRegex(RuntimeError, "abort sigkill window"):
                self.installer().abort(
                    source_sha=self.sha,
                    operation_id=self.operation_id,
                    confirm_plan_sha256=planned["plan_sha256"],
                )
        target = self.runtime / "config/bootstrap-quiesce"
        quarantine = self.runtime / "config" / (
            f".adopt-prereq-{self.operation_id}-bootstrap-quiesce.abort-target"
        )
        self.assertFalse(target.exists())
        self.assertTrue(quarantine.exists())

        aborted = self.installer().abort(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(aborted["status"], "aborted")
        self.assertFalse(target.exists())
        self.assertFalse(quarantine.exists())

    def test_authority_hardlink_sigkill_window_is_replayed(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )

        def crash(phase: str) -> None:
            if phase == "authority-linked":
                raise RuntimeError("injected authority link crash")

        with self.assertRaisesRegex(RuntimeError, "authority link crash"):
            self.installer(crash).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        authority_path = self.runtime / ADOPTER.AUTHORITY_PATH
        staging = authority_path.with_name(
            f".{authority_path.name}.create-{self.operation_id}"
        )
        self.assertTrue(authority_path.exists())
        self.assertTrue(staging.exists())
        self.assertEqual(authority_path.stat().st_ino, staging.stat().st_ino)
        self.assertEqual(authority_path.stat().st_nlink, 2)

        authority = self.installer().apply(
            source_sha=self.sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
        )
        self.assertEqual(authority["status"], "completed")
        self.assertFalse(staging.exists())
        self.assertEqual(authority_path.stat().st_nlink, 1)

    def test_authority_unlink_lost_response_reseals_state_directory(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha,
            operation_id=self.operation_id,
        )
        authority_path = self.runtime / ADOPTER.AUTHORITY_PATH
        staging = authority_path.with_name(
            f".{authority_path.name}.create-{self.operation_id}"
        )
        durable_unlink = ADOPTER.os.unlink
        unlinked = False

        def unlink_then_crash(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal unlinked
            result = durable_unlink(path, *args, **kwargs)
            if path == staging.name and not unlinked:
                unlinked = True
                raise RuntimeError("crash before authority unlink parent fsync")
            return result

        with mock.patch.object(
            ADOPTER.os,
            "unlink",
            side_effect=unlink_then_crash,
        ), self.assertRaisesRegex(RuntimeError, "authority unlink"):
            self.installer().apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.assertTrue(unlinked)
        self.assertTrue(authority_path.exists())
        self.assertFalse(staging.exists())
        self.assertEqual(authority_path.stat().st_nlink, 1)
        transaction = self.installer()._load_transaction(self.operation_id)
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertEqual(transaction["phase"], "authority-commit-intent")

        state_identity = (
            self.runtime.joinpath("state").stat().st_dev,
            self.runtime.joinpath("state").stat().st_ino,
        )
        durable_fsync = ADOPTER.os.fsync
        state_fsyncs = 0

        def track_state_fsync(descriptor: int) -> None:
            nonlocal state_fsyncs
            metadata = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) == state_identity:
                state_fsyncs += 1
            durable_fsync(descriptor)

        with mock.patch.object(
            ADOPTER.os,
            "fsync",
            side_effect=track_state_fsync,
        ):
            authority = self.installer().apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )
        self.assertGreaterEqual(state_fsyncs, 1)
        self.assertEqual(authority["status"], "completed")
        self.assertFalse(staging.exists())
        self.assertEqual(authority_path.stat().st_nlink, 1)

    def test_existing_exact_target_with_extra_hardlink_is_rejected(self) -> None:
        source_path, name, mode, _classification = ADOPTER.TRACKED_INSTALLS[0]
        target = self.runtime / "config" / name
        _write_private(target, (self.source / source_path).read_bytes(), mode)
        os.link(target, self.runtime / "config/.unowned-hardlink")

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "unsafe|identity differs"
        ):
            self.installer().plan(
                source_sha=self.sha,
                operation_id=self.operation_id,
            )

    def test_deploy_lock_symlink_and_hardlink_are_rejected(self) -> None:
        lock = self.runtime / "state/deploy.lock"
        backing = self.runtime / "state/.foreign-lock"
        _write_private(backing, b"", 0o600)
        lock.unlink()
        lock.symlink_to(backing)
        with self.assertRaises(ADOPTER.PrerequisiteError):
            self.installer().plan(
                source_sha=self.sha,
                operation_id=self.operation_id,
            )

        lock.unlink()
        os.link(backing, lock)
        with self.assertRaises(ADOPTER.PrerequisiteError):
            self.installer().plan(
                source_sha=self.sha,
                operation_id=self.operation_id,
            )

    def test_deploy_lock_path_swap_after_flock_is_rejected(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        lock = self.runtime / "state/deploy.lock"
        replacement = self.runtime / "state/.replacement-lock"

        def swap(phase: str) -> None:
            if phase == "apply-lock-acquired":
                _write_private(replacement, b"", 0o600)
                os.replace(replacement, lock)

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "deploy lock changed"
        ):
            self.installer(swap).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )

    def test_config_directory_path_swap_after_flock_is_rejected(self) -> None:
        planned = self.installer().plan(
            source_sha=self.sha, operation_id=self.operation_id
        )
        config = self.runtime / "config"
        displaced = self.runtime / "config-displaced"

        def swap(phase: str) -> None:
            if phase == "apply-lock-acquired":
                os.rename(config, displaced)
                config.mkdir(mode=0o700)

        with self.assertRaisesRegex(
            ADOPTER.PrerequisiteError, "config directory changed"
        ):
            self.installer(swap).apply(
                source_sha=self.sha,
                operation_id=self.operation_id,
                confirm_plan_sha256=planned["plan_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
