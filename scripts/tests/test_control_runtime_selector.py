from __future__ import annotations

import importlib.util
import fcntl
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/control_runtime_selector.py"
SPEC = importlib.util.spec_from_file_location("control_runtime_selector_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
SELECTOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SELECTOR)


class SelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="control-selector-")
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name) / "runtime"
        for relative in ("bin", "control-releases", "state", "state/prepared"):
            path = self.runtime / relative
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        self.immutable: dict[str, str] = {}
        for name in SELECTOR.BOOTSTRAP_IMMUTABLE_FILES:
            payload = f"fixture {name}\n".encode()
            self._write(self.runtime / "bin" / name, payload, 0o700)
            self.immutable[name] = SELECTOR.sha256_bytes(payload)

    @staticmethod
    def _write(path: Path, payload: bytes, mode: int) -> None:
        path.write_bytes(payload)
        os.chmod(path, mode)

    def release(
        self,
        *,
        source_sha: str,
        source_tree: str,
        variant: str,
        dft: bool = False,
        alias: bool = True,
        imports_sibling: bool = False,
    ) -> tuple[dict[str, object], Path]:
        payloads = {
            "deploy.py": (
                (
                    "import importlib.util\n"
                    "from pathlib import Path\n"
                    "p=Path(__file__).with_name('env.py')\n"
                    "s=importlib.util.spec_from_file_location('sealed_env', p)\n"
                    "m=importlib.util.module_from_spec(s)\n"
                    "s.loader.exec_module(m)\n"
                ).encode()
                if imports_sibling
                else f"# deploy {variant}\n".encode()
            ),
            "env.py": f"# env {variant}\n".encode(),
            "md.py": f"# md {variant}\n".encode(),
        }
        entrypoints: dict[str, object] = {
            "deploy": {"kind": "python", "file": "deploy.py"},
            "monomer-md": {
                "kind": "worker",
                "environment_loader": "env.py",
                "launcher": "md.py",
                "config_relative": "config/worker.env",
            },
        }
        if dft:
            payloads["dft.py"] = f"# dft {variant}\n".encode()
            entrypoints["monomer-dft"] = {
                "kind": "worker",
                "environment_loader": "env.py",
                "launcher": "dft.py",
                "config_relative": "config/dft-worker.env",
            }
        if alias:
            payloads["alias.py"] = f"# alias {variant}\n".encode()
            entrypoints["reconcile-production-0005-alias"] = {
                "kind": "python",
                "file": "alias.py",
            }
        files = {
            name: {
                "sha256": SELECTOR.sha256_bytes(payload),
                "size": len(payload),
                "mode": 0o700,
            }
            for name, payload in payloads.items()
        }
        identity = {
            "schema_version": 1,
            "protocol_version": 1,
            "source_sha": source_sha,
            "source_tree": source_tree,
            "compatibility": {
                "handoff_protocol_versions": [1],
                "descriptor_schema_versions": [2],
                "current_state_schema_versions": [2],
                "marker_schema_versions": [2, 3],
                "worker_slot_schema_versions": [2],
            },
            "entrypoints": entrypoints,
            "files": files,
        }
        release_id = SELECTOR.release_identity(identity)
        manifest = {**identity, "release_id": release_id}
        root = self.runtime / "control-releases" / release_id
        root.mkdir(mode=0o700)
        for name, payload in payloads.items():
            self._write(root / name, payload, 0o700)
        self._write(
            root / SELECTOR.CONTROL_MANIFEST_NAME,
            SELECTOR.canonical_json_bytes(manifest) + b"\n",
            0o600,
        )
        SELECTOR.load_control_release(self.runtime, release_id)
        return manifest, root

    def candidate(
        self, manifest: dict[str, object], *, operation_id: str
    ) -> dict[str, object]:
        root = self.runtime / "control-releases" / str(manifest["release_id"])
        return {
            "schema_version": 1,
            "protocol_version": 1,
            "component": "deployment-controls",
            "release_id": manifest["release_id"],
            "source_sha": manifest["source_sha"],
            "source_tree": manifest["source_tree"],
            "manifest_sha256": SELECTOR.sha256_file(
                root / SELECTOR.CONTROL_MANIFEST_NAME
            ),
            "operation_id": operation_id,
            "prepared_at": "2026-07-16T00:00:00+00:00",
        }

    def activate(
        self, manifest: dict[str, object], *, operation_id: str, generation: int = 1
    ) -> dict[str, object]:
        candidate = self.candidate(manifest, operation_id=operation_id)
        active = {
            "schema_version": 1,
            "protocol_version": 1,
            "component": "deployment-controls",
            "generation": generation,
            "release_id": candidate["release_id"],
            "source_sha": candidate["source_sha"],
            "source_tree": candidate["source_tree"],
            "manifest_sha256": candidate["manifest_sha256"],
            "operation_id": operation_id,
            "previous_release_id": None,
            "activated_at": "2026-07-16T00:00:00+00:00",
        }
        self._write(
            self.runtime / "state/active-control.json",
            SELECTOR.canonical_json_bytes(active) + b"\n",
            0o600,
        )
        authority = self.runtime / "state/bootstrap-control.json"
        if not authority.exists():
            source_readiness = {
                "schema_version": 1,
                "ready": True,
                "source_root": str(
                    self.runtime.parent / "bootstrap-source"
                ),
                "source_sha": candidate["source_sha"],
                "source_tree": candidate["source_tree"],
                "branch": "main",
                "origin": "git@github.com:lzq390/ZhijuPoly.git",
                "standalone_object_database": True,
                "shallow": False,
                "dirty_entries": 0,
                "ignored_entries": 0,
                "unreachable_objects": 0,
                "owner_private": True,
                "group_or_world_writable": False,
            }
            takeover = {
                "schema_version": 1,
                "operation_id": "takeover-selector-fixture",
                "authority_sha": candidate["source_sha"],
                "authority_tree": candidate["source_tree"],
                "install_manifest_sha256": "sha256:" + "3" * 64,
                "classification_sha256": "sha256:" + "4" * 64,
                "runtime_identity_sha256": "sha256:" + "5" * 64,
                "git_identity": {
                    "branch": "refs/heads/main",
                    "head_sha": "0" * 40,
                    "head_tree": "0" * 40,
                    "local_main_sha": "0" * 40,
                },
                "pre_stopped_fence_sha256": "sha256:" + "6" * 64,
                "control_layout_sha256": "sha256:" + "7" * 64,
                "checkout_permissions_sha256": "sha256:" + "9" * 64,
                "applied_record_sha256": "sha256:" + "8" * 64,
            }
            takeover["binding_sha256"] = SELECTOR.canonical_json_digest(
                takeover
            )
            self._write(
                authority,
                SELECTOR.canonical_json_bytes(
                    {
                        "schema_version": 2,
                        "status": "completed",
                        "source_sha": candidate["source_sha"],
                        "source_tree": candidate["source_tree"],
                        "source_readiness": source_readiness,
                        "source_readiness_sha256": (
                            SELECTOR.canonical_json_digest(source_readiness)
                        ),
                        "legacy_takeover": takeover,
                        "delivery_gate": {"fixture": True},
                        "production_repository": {"fixture": True},
                        "immutable_files": self.immutable,
                        "worker_unit_takeover": {"fixture": True},
                        "candidate_control": candidate,
                        "active_control": active,
                    }
                )
                + b"\n",
                0o600,
            )
        alias_marker = self.runtime / SELECTOR.ALIAS_MARKER_RELATIVE
        if not alias_marker.exists():
            self._complete_alias_gate(manifest)
        return active

    def _complete_alias_gate(self, manifest: dict[str, object]) -> None:
        operation_id = "alias-0005-fixture"
        audit_dir = (
            self.runtime / SELECTOR.ALIAS_AUDIT_ROOT_RELATIVE / operation_id
        )
        backup_dir = (
            self.runtime / SELECTOR.ALIAS_BACKUP_ROOT_RELATIVE / operation_id
        )
        audit_dir.mkdir(parents=True, mode=0o700)
        backup_dir.mkdir(parents=True, mode=0o700)
        os.chmod(audit_dir.parent, 0o700)
        os.chmod(backup_dir.parent, 0o700)
        os.chmod(audit_dir, 0o700)
        os.chmod(backup_dir, 0o700)
        dump = backup_dir / "nexpoly-before.dump"
        self._write(dump, b"fixture database dump\n", 0o600)
        dump_sha = SELECTOR.sha256_file(dump).removeprefix("sha256:")
        self._write(
            backup_dir / "nexpoly-before.dump.sha256",
            (dump_sha + "\n").encode(),
            0o600,
        )
        restore_list = audit_dir / "pg-restore.list"
        self._write(
            restore_list,
            b"TABLE DATA generation polytao_jobs\n"
            b"TABLE DATA governance schema_migrations\n",
            0o600,
        )
        def ledger_rows(pairs: list[tuple[str, str]]) -> list[dict[str, str]]:
            return [
                {
                    "version": version,
                    "checksum": checksum,
                    "applied_at": (
                        SELECTOR.ALIAS_APPLIED_AT
                        if version == SELECTOR.ALIAS_VERSION
                        else f"2026-07-08T02:{index:02d}:00.000000Z"
                    ),
                }
                for index, (version, checksum) in enumerate(pairs)
            ]

        archive = {
            "row_count": 12,
            "status_counts": {"completed": 8, "failed": 4},
            "rows_sha256": "c" * 64,
            "schema_sha256": SELECTOR.ALIAS_EXPECTED_SCHEMA_SHA256,
            "structure_counts": SELECTOR.ALIAS_EXPECTED_STRUCTURE_COUNTS,
        }
        relation = {
            "kind": "r",
            "persistence": "p",
            "is_partition": False,
            "row_security": False,
            "force_row_security": False,
            "owner": "polyprop",
            "parents": 0,
            "children": 0,
        }
        before = {
            "database": "nexpoly",
            "current_user": "polyprop",
            "database_owner": "polyprop",
            "server_version_num": 160014,
            "in_recovery": False,
            "system_identifier": SELECTOR.ALIAS_SYSTEM_IDENTIFIER,
            "ledger": ledger_rows(SELECTOR.ALIAS_PRE_LEDGER),
            "archive": archive,
            "ledger_schema_sha256": SELECTOR.ALIAS_EXPECTED_LEDGER_SCHEMA_SHA256,
            "ledger_structure_counts": (
                SELECTOR.ALIAS_EXPECTED_LEDGER_STRUCTURE_COUNTS
            ),
            "polytao_relation": relation,
            "ledger_relation": relation,
        }
        after = {
            **before,
            "ledger": [
                row
                for row in before["ledger"]
                if row["version"] != SELECTOR.ALIAS_VERSION
            ],
        }
        restored = {
            **before,
            "database": "nexpoly_alias_restore",
            "current_user": "postgres",
            "database_owner": "postgres",
            "system_identifier": "123456789",
            "polytao_relation": {**relation, "owner": "postgres"},
            "ledger_relation": {**relation, "owner": "postgres"},
        }
        root = self.runtime / "control-releases" / str(manifest["release_id"])
        entrypoint = manifest["entrypoints"]["reconcile-production-0005-alias"]
        control = {
            "release_id": manifest["release_id"],
            "source_sha": manifest["source_sha"],
            "source_tree": manifest["source_tree"],
            "manifest_sha256": SELECTOR.sha256_file(
                root / SELECTOR.CONTROL_MANIFEST_NAME
            ).removeprefix("sha256:"),
            "script_sha256": SELECTOR.sha256_file(
                root / entrypoint["file"]
            ).removeprefix("sha256:"),
        }
        binary_hashes = {"/fixture/bin": "b" * 64}
        identity = {
            "operation_id": operation_id,
            "control": control,
            "legacy_source": {"sha": "a" * 40, "tree": "b" * 40},
            "binaries_sha256": binary_hashes,
            "database_endpoint": SELECTOR.ALIAS_DATABASE_ENDPOINT,
            "database_system_identifier": SELECTOR.ALIAS_SYSTEM_IDENTIFIER,
            "restore_image": {
                "digest_ref": SELECTOR.ALIAS_RESTORE_IMAGE,
                "image_id": "sha256:" + "d" * 64,
            },
            "alias": {
                "version": SELECTOR.ALIAS_VERSION,
                "checksum": SELECTOR.ALIAS_CHECKSUM,
                "applied_at": SELECTOR.ALIAS_APPLIED_AT,
            },
        }
        backup = {
            "dump_path": str(dump),
            "dump_sha256": dump_sha,
            "dump_size": dump.stat().st_size,
            "restore_list_sha256": SELECTOR.sha256_file(
                restore_list
            ).removeprefix("sha256:"),
        }
        restore = {
            "image": {
                "digest_ref": SELECTOR.ALIAS_RESTORE_IMAGE,
                "image_id": "sha256:" + "d" * 64,
            },
            "container_name": "nexpoly-alias-restore-fixture",
            "network_mode": "none",
            "dump_sha256": dump_sha,
            "archive": before["archive"],
            "ledger_schema_sha256": before["ledger_schema_sha256"],
            "database_inventory": restored,
            "verified_at": "2026-07-17T00:00:00Z",
        }
        self._write(
            audit_dir / "isolated-postgres16-restore.json",
            SELECTOR.canonical_json_bytes(restore) + b"\n",
            0o600,
        )
        self._write(
            audit_dir / "database-after.json",
            SELECTOR.canonical_json_bytes(after) + b"\n",
            0o600,
        )
        files = SELECTOR._alias_evidence_files(audit_dir, backup_dir)
        completed_at = "2026-07-17T00:00:01Z"
        audit = {
            "schema_version": 1,
            "operation_id": operation_id,
            "outcome": "completed",
            "identity": identity,
            "database_before": before,
            "database_after": after,
            "database_backup": backup,
            "isolated_restore": restore,
            "binaries": {"/fixture/bin": {"sha256": "b" * 64}},
            "files": files,
            "completed_at": completed_at,
        }
        audit_path = audit_dir / "AUDIT-MANIFEST.json"
        self._write(
            audit_path, SELECTOR.canonical_json_bytes(audit) + b"\n", 0o600
        )
        marker = {
            "schema_version": 1,
            "action": SELECTOR.ALIAS_ACTION,
            "phase": "completed",
            "identity": identity,
            "operation_directories": {
                "audit": str(audit_dir),
                "backup": str(backup_dir),
            },
            "started_at": "2026-07-17T00:00:00Z",
            "updated_at": completed_at,
            "runtime_stop_fence": {"fixture": True},
            "before": before,
            "database_backup": backup,
            "restore_container": {"name": "fixture"},
            "isolated_restore": restore,
            "mutation_intent": {
                "database_system_identifier": SELECTOR.ALIAS_SYSTEM_IDENTIFIER,
                "alias": identity["alias"],
                "pre_ledger": before["ledger"],
                "archive": before["archive"],
                "dump_sha256": dump_sha,
                "restore_dump_sha256": dump_sha,
            },
            "after": after,
            "audit_manifest_sha256": SELECTOR.sha256_file(
                audit_path
            ).removeprefix("sha256:"),
            "completed_at": completed_at,
        }
        marker_path = self.runtime / SELECTOR.ALIAS_MARKER_RELATIVE
        marker_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(marker_path.parent, 0o700)
        self._write(
            marker_path, SELECTOR.canonical_json_bytes(marker) + b"\n", 0o600
        )

    def test_manifest_inventory_hash_mode_and_extra_are_fail_closed(self) -> None:
        manifest, root = self.release(
            source_sha="1" * 40, source_tree="2" * 40, variant="a"
        )
        self.activate(manifest, operation_id="bootstrap-controls-a")
        active, loaded, loaded_root = SELECTOR.load_active_control(self.runtime)
        self.assertEqual(active["release_id"], manifest["release_id"])
        self.assertEqual(loaded, manifest)
        self.assertEqual(loaded_root, root)
        extra = root / "extra.py"
        self._write(extra, b"# extra\n", 0o700)
        with self.assertRaisesRegex(SELECTOR.ControlRuntimeError, "inventory"):
            SELECTOR.load_active_control(self.runtime)
        extra.unlink()
        (root / "deploy.py").write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(SELECTOR.ControlRuntimeError, "differs"):
            SELECTOR.load_active_control(self.runtime)

    def test_active_control_is_unusable_until_bootstrap_authority_completes(self) -> None:
        manifest, _root = self.release(
            source_sha="1" * 40, source_tree="2" * 40, variant="prepared"
        )
        self.activate(manifest, operation_id="bootstrap-controls-prepared")
        authority_path = self.runtime / "state/bootstrap-control.json"
        authority = SELECTOR._load_private_json(authority_path)
        authority["status"] = "prepared"
        self._write(
            authority_path,
            SELECTOR.canonical_json_bytes(authority) + b"\n",
            0o600,
        )
        with self.assertRaisesRegex(
            SELECTOR.ControlRuntimeError, "completed bootstrap authority"
        ):
            SELECTOR.load_active_control(self.runtime)

    def test_ready_routes_apply_to_exact_candidate_and_rejects_duplicate_operation(self) -> None:
        first, _ = self.release(
            source_sha="1" * 40, source_tree="2" * 40, variant="a"
        )
        second, second_root = self.release(
            source_sha="3" * 40, source_tree="4" * 40, variant="b"
        )
        self.activate(first, operation_id="bootstrap-controls-a")
        operation = "deploy-20260716-0001"
        candidate = self.candidate(second, operation_id=operation)
        prepared = self.runtime / "state/prepared" / operation
        prepared.mkdir(mode=0o700)
        ready = {
            "schema_version": 1,
            "status": "ready",
            "operation_id": operation,
            "executor_control": candidate,
            "executor_control_sha256": SELECTOR.canonical_json_digest(candidate),
        }
        self._write(
            prepared / "ready.json",
            SELECTOR.canonical_json_bytes(ready) + b"\n",
            0o600,
        )
        _manifest, selected = SELECTOR._selected_release(
            self.runtime,
            "deploy",
            ["apply", "--sha", "3" * 40, "--operation-id", operation],
        )
        self.assertEqual(selected, second_root)
        with self.assertRaisesRegex(SELECTOR.ControlRuntimeError, "exactly once"):
            SELECTOR._selected_release(
                self.runtime,
                "deploy",
                [
                    "apply",
                    "--operation-id",
                    operation,
                    "--operation-id",
                    "deploy-20260716-0002",
                ],
            )

    def test_marker_routes_supported_future_schema_and_fences_operation(self) -> None:
        first, _ = self.release(
            source_sha="1" * 40, source_tree="2" * 40, variant="a"
        )
        second, second_root = self.release(
            source_sha="3" * 40, source_tree="4" * 40, variant="b"
        )
        self.activate(first, operation_id="bootstrap-controls-a")
        operation = "deploy-20260716-0001"
        candidate = self.candidate(second, operation_id=operation)
        marker = {
            "schema_version": 3,
            "action": "deploy",
            "operation_id": operation,
            "executor_control": candidate,
            "executor_control_sha256": SELECTOR.canonical_json_digest(candidate),
        }
        self._write(
            self.runtime / "state/deploy-in-progress.json",
            SELECTOR.canonical_json_bytes(marker) + b"\n",
            0o600,
        )
        _manifest, selected = SELECTOR._selected_release(
            self.runtime,
            "deploy",
            ["apply", "--operation-id", operation, "--sha", "3" * 40],
        )
        self.assertEqual(selected, second_root)
        with self.assertRaisesRegex(SELECTOR.ControlRuntimeError, "differs"):
            SELECTOR._selected_release(
                self.runtime,
                "deploy",
                ["apply", "--operation-id", "deploy-20260716-0002"],
            )
        with self.assertRaisesRegex(
            SELECTOR.ControlRuntimeError, "blocked by an interrupted"
        ):
            SELECTOR._selected_release(self.runtime, "contract-0012", ["apply"])

    def test_dynamic_dft_role_and_environment_allowlist_need_no_router_change(self) -> None:
        manifest, root = self.release(
            source_sha="5" * 40,
            source_tree="6" * 40,
            variant="dft",
            dft=True,
        )
        self.activate(manifest, operation_id="bootstrap-controls-dft")
        captured: dict[str, object] = {}

        def fake_exec(path: str, argv: list[str], environment: dict[str, str]) -> None:
            captured.update(path=path, argv=argv, environment=environment)
            raise RuntimeError("captured")

        with mock.patch.object(SELECTOR.os, "execve", fake_exec):
            with self.assertRaisesRegex(RuntimeError, "captured"):
                SELECTOR._exec_role(
                    "monomer-dft",
                    [],
                    {
                        "HOME": "/evil",
                        "LD_PRELOAD": "/tmp/evil.so",
                        "LC_ALL": "C",
                        "NEXPOLY_ALLOW_TEST_ROOT": "1",
                        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1001/bus",
                    },
                    runtime_root=self.runtime,
                )
        argv = captured["argv"]
        environment = captured["environment"]
        self.assertIn(str(root / "dft.py"), argv)
        self.assertNotIn("LD_PRELOAD", environment)
        self.assertNotIn("NEXPOLY_ALLOW_TEST_ROOT", environment)
        self.assertEqual(environment["HOME"], "/home/devuser")
        self.assertEqual(environment["LC_ALL"], "C.UTF-8")
        self.assertEqual(
            environment["NEXPOLY_ACTIVE_CONTROL_RELEASE_ID"],
            manifest["release_id"],
        )

    def test_alias_role_passes_only_the_dedicated_dsn_and_blocks_contract_marker(
        self,
    ) -> None:
        manifest, root = self.release(
            source_sha="9" * 40,
            source_tree="a" * 40,
            variant="alias",
            alias=True,
        )
        self.activate(manifest, operation_id="bootstrap-controls-alias")
        captured: dict[str, object] = {}

        def fake_exec(path: str, argv: list[str], environment: dict[str, str]) -> None:
            captured.update(path=path, argv=argv, environment=environment)
            raise RuntimeError("captured")

        secret = "postgresql://operator:secret@db.invalid/nexpoly"
        with mock.patch.object(SELECTOR.os, "execve", fake_exec):
            with self.assertRaisesRegex(RuntimeError, "captured"):
                SELECTOR._exec_role(
                    "reconcile-production-0005-alias",
                    ["--operation-id", "alias-20260717-0001"],
                    {
                        "NEXPOLY_PRODUCTION_POSTGRES_DSN": secret,
                        "LD_PRELOAD": "/tmp/evil.so",
                        "PYTHONPATH": "/tmp/evil",
                    },
                    runtime_root=self.runtime,
                )
        self.assertIn(str(root / "alias.py"), captured["argv"])
        environment = dict(captured["environment"])
        self.assertEqual(environment["NEXPOLY_PRODUCTION_POSTGRES_DSN"], secret)
        self.assertNotIn("LD_PRELOAD", environment)
        self.assertNotIn("PYTHONPATH", environment)

        self._write(
            self.runtime / "state/contract-0012-in-progress.json",
            b"{}\n",
            0o600,
        )
        with self.assertRaisesRegex(SELECTOR.ControlRuntimeError, "0012"):
            SELECTOR._selected_release(
                self.runtime,
                "reconcile-production-0005-alias",
                ["--operation-id", "alias-20260717-0001"],
            )

    def test_alias_role_rejects_missing_or_control_character_dsn(self) -> None:
        manifest, _root = self.release(
            source_sha="b" * 40,
            source_tree="c" * 40,
            variant="alias-dsn",
            alias=True,
        )
        self.activate(manifest, operation_id="bootstrap-controls-alias-dsn")
        for environment in (
            {},
            {"NEXPOLY_PRODUCTION_POSTGRES_DSN": ""},
            {"NEXPOLY_PRODUCTION_POSTGRES_DSN": "postgresql://db/nexpoly\nleak"},
        ):
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(
                    SELECTOR.ControlRuntimeError, "DSN is unavailable or malformed"
                ):
                    SELECTOR._exec_role(
                        "reconcile-production-0005-alias",
                        [],
                        environment,
                        runtime_root=self.runtime,
                    )

    def test_deploy_plan_prepare_allow_missing_alias_but_not_interrupted_alias(
        self,
    ) -> None:
        manifest, root = self.release(
            source_sha="d" * 40,
            source_tree="e" * 40,
            variant="deploy-before-alias",
        )
        self.activate(manifest, operation_id="bootstrap-controls-before-alias")
        marker_path = self.runtime / SELECTOR.ALIAS_MARKER_RELATIVE
        completed = SELECTOR._load_private_json(marker_path)
        marker_path.unlink()

        for command in ("plan", "prepare"):
            with self.subTest(command=command):
                selected, selected_root = SELECTOR._selected_release(
                    self.runtime,
                    "deploy",
                    [command, "--operation-id", "deploy-before-alias"],
                )
                self.assertEqual(selected["release_id"], manifest["release_id"])
                self.assertEqual(selected_root, root)
        with self.assertRaisesRegex(
            SELECTOR.ControlRuntimeError, "reconciliation is required"
        ):
            SELECTOR._selected_release(
                self.runtime,
                "deploy",
                ["apply", "--operation-id", "deploy-before-alias"],
            )

        interrupted = {**completed, "phase": "planned"}
        self._write(
            marker_path,
            SELECTOR.canonical_json_bytes(interrupted) + b"\n",
            0o600,
        )
        for command in ("plan", "prepare"):
            with self.subTest(command=command):
                with self.assertRaisesRegex(
                    SELECTOR.ControlRuntimeError,
                    "interrupted alias reconciliation",
                ):
                    SELECTOR._selected_release(
                        self.runtime,
                        "deploy",
                        [command, "--operation-id", "deploy-before-alias"],
                    )

    def test_completed_alias_gate_accepts_dynamic_rows_and_binds_restore_image(
        self,
    ) -> None:
        manifest, _root = self.release(
            source_sha="a" * 40,
            source_tree="b" * 40,
            variant="dynamic-alias-evidence",
        )
        self.activate(manifest, operation_id="bootstrap-controls-dynamic-alias")
        gate = SELECTOR.load_production_0005_alias_gate(
            self.runtime, require_completed=True
        )
        self.assertEqual(gate["before"]["archive"]["row_count"], 12)
        self.assertEqual(
            gate["isolated_restore"]["image"],
            gate["identity"]["restore_image"],
        )
        self.assertNotEqual(
            gate["identity"]["restore_image"]["image_id"],
            "sha256:"
            + SELECTOR.ALIAS_RESTORE_IMAGE.rsplit("@sha256:", 1)[1],
        )

        marker_path = self.runtime / SELECTOR.ALIAS_MARKER_RELATIVE
        tampered = SELECTOR._load_private_json(marker_path)
        tampered["identity"]["restore_image"]["image_id"] = "sha256:" + "e" * 64
        self._write(
            marker_path,
            SELECTOR.canonical_json_bytes(tampered) + b"\n",
            0o600,
        )
        with self.assertRaisesRegex(
            SELECTOR.ControlRuntimeError, "completion evidence differs"
        ):
            SELECTOR.load_production_0005_alias_gate(
                self.runtime, require_completed=True
            )

    def test_completed_alias_replay_uses_its_recorded_control_release(self) -> None:
        original, original_root = self.release(
            source_sha="1" * 40,
            source_tree="2" * 40,
            variant="alias-original",
        )
        self.activate(original, operation_id="bootstrap-controls-alias-original")
        replacement, _replacement_root = self.release(
            source_sha="3" * 40,
            source_tree="4" * 40,
            variant="alias-replacement",
        )
        self.activate(replacement, operation_id="deploy-controls-alias-replacement")

        selected, selected_root = SELECTOR._selected_release(
            self.runtime,
            "reconcile-production-0005-alias",
            ["--operation-id", "alias-0005-fixture", "--apply"],
        )
        self.assertEqual(selected["release_id"], original["release_id"])
        self.assertEqual(selected_root, original_root)

    def test_python_roles_are_bytecode_free_and_release_inventory_stays_exact(self) -> None:
        manifest, root = self.release(
            source_sha="7" * 40,
            source_tree="8" * 40,
            variant="imports",
            imports_sibling=True,
        )
        self.activate(manifest, operation_id="bootstrap-controls-bytecode-free")
        captured: dict[str, object] = {}

        def fake_exec(path: str, argv: list[str], environment: dict[str, str]) -> None:
            captured.update(path=path, argv=argv, environment=environment)
            raise RuntimeError("captured")

        with mock.patch.object(SELECTOR.os, "execve", fake_exec):
            with self.assertRaisesRegex(RuntimeError, "captured"):
                SELECTOR._exec_role(
                    "deploy", ["plan"], {}, runtime_root=self.runtime
                )
        argv = list(captured["argv"])
        environment = dict(captured["environment"])
        self.assertEqual(argv[:3], ["/usr/bin/python3", "-I", "-B"])
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        for _ in range(2):
            completed = subprocess.run(
                argv,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            SELECTOR.load_active_control(self.runtime)
        self.assertFalse((root / "__pycache__").exists())
        self.assertEqual(
            {path.name for path in root.iterdir()},
            {
                SELECTOR.CONTROL_MANIFEST_NAME,
                "deploy.py",
                "env.py",
                "md.py",
                "alias.py",
            },
        )

    def test_three_immutable_releases_preserve_old_rollback_targets(self) -> None:
        releases = [
            self.release(
                source_sha=str(index) * 40,
                source_tree=str(index + 3) * 40,
                variant=str(index),
            )
            for index in (1, 2, 3)
        ]
        self.activate(releases[-1][0], operation_id="deploy-20260716-0003")
        for manifest, root in releases:
            loaded, loaded_root = SELECTOR.load_control_release(
                self.runtime, manifest["release_id"]
            )
            self.assertEqual(loaded, manifest)
            self.assertEqual(loaded_root, root)

    def test_worker_role_binds_current_state_and_marker_owned_transition(self) -> None:
        manifest, root = self.release(
            source_sha="9" * 40,
            source_tree="a" * 40,
            variant="worker-authority",
        )
        operation = "deploy-20260716-worker-authority"
        active = self.activate(manifest, operation_id=operation)
        asset = self.runtime / "asset-release"
        asset.mkdir(mode=0o700)
        (self.runtime / "state/current-assets").symlink_to(asset)
        launcher = manifest["files"]["md.py"]["sha256"]
        unit = {
            "control_release_id": manifest["release_id"],
            "launcher_sha256": launcher,
        }
        slot = {
            "source_sha": manifest["source_sha"],
            "source_tree": manifest["source_tree"],
            "operation_id": operation,
        }
        current = {
            "schema_version": 2,
            "operation_id": operation,
            "source_sha": manifest["source_sha"],
            "source_tree": manifest["source_tree"],
            "active_control": active,
            "monomer_md_systemd_unit": unit,
            "active_monomer_md_slot": slot,
            "asset_identity": {"root": str(asset)},
        }
        self._write(
            self.runtime / "state/current-deployment.json",
            SELECTOR.canonical_json_bytes(current) + b"\n",
            0o600,
        )
        selected, selected_root = SELECTOR._selected_release(
            self.runtime, "monomer-md", []
        )
        self.assertEqual(selected, manifest)
        self.assertEqual(selected_root, root)

        candidate = self.candidate(manifest, operation_id=operation)
        descriptor = {
            "repository": {
                "target_sha": manifest["source_sha"],
                "target_tree": manifest["source_tree"],
            },
            "controller": {
                "executor_control": candidate,
                "executor_control_sha256": SELECTOR.canonical_json_digest(candidate),
                "previous_active_control": None,
            },
            "monomer_md": {"systemd_unit": unit, "slot_record": slot},
            "release_input": {"asset": {"root": str(asset)}},
            "previous_deployment": None,
        }
        prepared = self.runtime / "state/prepared" / operation
        prepared.mkdir(mode=0o700)
        descriptor_path = prepared / "descriptor.json"
        self._write(
            descriptor_path,
            SELECTOR.canonical_json_bytes(descriptor) + b"\n",
            0o600,
        )
        marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": operation,
            "descriptor_sha256": SELECTOR.sha256_file(descriptor_path),
            "executor_control": candidate,
            "executor_control_sha256": SELECTOR.canonical_json_digest(candidate),
            "runtime_stopped": True,
            "source_switched": True,
            "slot_switched": True,
            "unit_switched": True,
            "control_switched": True,
            "asset_switched": False,
        }
        marker_path = self.runtime / "state/deploy-in-progress.json"
        self._write(
            marker_path,
            SELECTOR.canonical_json_bytes(marker) + b"\n",
            0o600,
        )
        lock_path = self.runtime / "state/deploy.lock"
        self._write(lock_path, b"", 0o600)
        with lock_path.open("rb") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            with self.assertRaisesRegex(
                SELECTOR.ControlRuntimeError,
                "differ from governed deployment authority",
            ):
                SELECTOR._selected_release(self.runtime, "monomer-md", [])
            marker["asset_switched"] = True
            self._write(
                marker_path,
                SELECTOR.canonical_json_bytes(marker) + b"\n",
                0o600,
            )
            selected, selected_root = SELECTOR._selected_release(
                self.runtime, "monomer-md", []
            )
            self.assertEqual(selected, manifest)
            self.assertEqual(selected_root, root)
        with self.assertRaisesRegex(
            SELECTOR.ControlRuntimeError, "not lock-owned"
        ):
            SELECTOR._selected_release(self.runtime, "monomer-md", [])

    def test_worker_pre_stop_marker_routes_exact_unchanged_previous_runtime(self) -> None:
        previous_manifest, previous_root = self.release(
            source_sha="1" * 40,
            source_tree="2" * 40,
            variant="pre-stop-previous",
        )
        candidate_manifest, _candidate_root = self.release(
            source_sha="3" * 40,
            source_tree="4" * 40,
            variant="pre-stop-candidate",
        )
        previous_operation = "deploy-20260716-previous"
        operation = "deploy-20260716-pre-stop"
        active = self.activate(
            previous_manifest, operation_id=previous_operation
        )
        asset = self.runtime / "pre-stop-asset"
        asset.mkdir(mode=0o700)
        (self.runtime / "state/current-assets").symlink_to(asset)
        previous = {
            "schema_version": 2,
            "operation_id": previous_operation,
            "source_sha": previous_manifest["source_sha"],
            "source_tree": previous_manifest["source_tree"],
            "active_control": active,
            "monomer_md_systemd_unit": {
                "control_release_id": previous_manifest["release_id"],
                "launcher_sha256": previous_manifest["files"]["md.py"]["sha256"],
            },
            "active_monomer_md_slot": {
                "source_sha": previous_manifest["source_sha"],
                "source_tree": previous_manifest["source_tree"],
                "operation_id": previous_operation,
            },
            "asset_identity": {"root": str(asset)},
        }
        current_path = self.runtime / "state/current-deployment.json"
        self._write(
            current_path,
            SELECTOR.canonical_json_bytes(previous) + b"\n",
            0o600,
        )
        candidate = self.candidate(
            candidate_manifest, operation_id=operation
        )
        descriptor = {
            "repository": {
                "target_sha": candidate_manifest["source_sha"],
                "target_tree": candidate_manifest["source_tree"],
            },
            "controller": {
                "executor_control": candidate,
                "executor_control_sha256": SELECTOR.canonical_json_digest(
                    candidate
                ),
                "previous_active_control": active,
            },
            "monomer_md": {},
            "release_input": {},
            "previous_deployment": previous,
        }
        prepared = self.runtime / "state/prepared" / operation
        prepared.mkdir(mode=0o700)
        descriptor_path = prepared / "descriptor.json"
        self._write(
            descriptor_path,
            SELECTOR.canonical_json_bytes(descriptor) + b"\n",
            0o600,
        )
        base_marker = {
            "schema_version": 2,
            "action": "deploy",
            "operation_id": operation,
            "descriptor_sha256": SELECTOR.sha256_file(descriptor_path),
            "executor_control": candidate,
            "executor_control_sha256": SELECTOR.canonical_json_digest(candidate),
            "runtime_stopped": False,
            "source_switched": False,
            "slot_switched": False,
            "unit_switched": False,
            "control_switched": False,
            "asset_switched": False,
            "database_change_started": False,
        }
        marker_path = self.runtime / "state/deploy-in-progress.json"
        lock_path = self.runtime / "state/deploy.lock"
        self._write(lock_path, b"", 0o600)
        with lock_path.open("rb") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            for phase, failed_phase in (
                ("prepared", None),
                ("drain-started", None),
                ("drained", None),
                ("failed", "drained"),
            ):
                marker = {**base_marker, "phase": phase}
                if failed_phase is not None:
                    marker["failed_phase"] = failed_phase
                self._write(
                    marker_path,
                    SELECTOR.canonical_json_bytes(marker) + b"\n",
                    0o600,
                )
                selected, selected_root = SELECTOR._selected_release(
                    self.runtime, "monomer-md", []
                )
                self.assertEqual(selected, previous_manifest)
                self.assertEqual(selected_root, previous_root)

            marker = {**base_marker, "phase": "drained", "asset_switched": True}
            self._write(
                marker_path,
                SELECTOR.canonical_json_bytes(marker) + b"\n",
                0o600,
            )
            with self.assertRaisesRegex(
                SELECTOR.ControlRuntimeError,
                "differ from governed deployment authority",
            ):
                SELECTOR._selected_release(self.runtime, "monomer-md", [])


if __name__ == "__main__":
    unittest.main()
