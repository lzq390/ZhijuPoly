from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/adopt_bootstrap_router_successor.py"
SPEC = importlib.util.spec_from_file_location(
    "adopt_bootstrap_router_successor_test",
    SCRIPT,
)
assert SPEC is not None and SPEC.loader is not None
ROUTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROUTER)
SNAPSHOT = ROUTER.snapshot


class InjectedCrash(RuntimeError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _write(path: Path, payload: bytes, mode: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(payload)
    path.chmod(mode)
    return _digest(payload)


def _write_json(path: Path, value: object) -> str:
    return _write(path, _canonical(value), 0o600)


class Fixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="bootstrap-router-successor-"
        )
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.seed = self.root / "seed"
        self.source = self.root / "source"
        self.production = self.root / "production"
        self.runtime = self.root / "runtime"
        self.seed.mkdir(mode=0o700)
        self._git(self.seed, "init", "-b", "main")
        self._git(self.seed, "config", "user.email", "router@example.invalid")
        self._git(self.seed, "config", "user.name", "Router Test")
        self._write_source(b"# predecessor deploy\n", predecessor=True)
        self._git(self.seed, "add", ".")
        self._git(self.seed, "commit", "-m", "predecessor")
        self.production_sha = self._git(self.seed, "rev-parse", "HEAD")
        self.production_tree = self._git(self.seed, "rev-parse", "HEAD^{tree}")
        self._write_source(b"# reviewed target deploy\n", predecessor=False)
        self._git(self.seed, "add", ".")
        self._git(self.seed, "commit", "-m", "target")
        self.target_sha = self._git(self.seed, "rev-parse", "HEAD")
        self.target_tree = self._git(self.seed, "rev-parse", "HEAD^{tree}")
        self._clone(self.seed, self.source)
        self._clone(self.seed, self.production)
        self._git(
            self.production,
            "checkout",
            "-B",
            "main",
            self.production_sha,
        )
        self.runtime.mkdir(mode=0o700)
        for relative in ("state", "bin", "control-releases"):
            (self.runtime / relative).mkdir(mode=0o700)
        _write(self.runtime / "state/deploy.lock", b"", 0o600)
        self._make_private(self.production / ".git")
        self._install_bootstrap()
        self._install_snapshot_and_successors()
        self.operation_id = "adopt-router-test-20260819"

    def close(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _git(root: Path, *arguments: str) -> str:
        return subprocess.run(
            ["/usr/bin/git", "-C", str(root), *arguments],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    @staticmethod
    def _clone(source: Path, target: Path) -> None:
        subprocess.run(
            ["/usr/bin/git", "clone", "--no-local", str(source), str(target)],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    @staticmethod
    def _make_private(root: Path) -> None:
        for path in sorted(
            root.rglob("*"),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)
        root.chmod(0o700)

    def _write_source(self, deploy_payload: bytes, *, predecessor: bool) -> None:
        scripts = self.seed / "scripts"
        scripts.mkdir(mode=0o700, exist_ok=True)
        selector_payload = (
            b"#!/usr/bin/python3\n# predecessor selector fixture\n"
            if predecessor
            else (ROOT / "scripts/control_runtime_selector.py").read_bytes()
        )
        payloads = {
            "control_runtime_selector.py": selector_payload,
            "production_git_snapshot.py": (
                ROOT / "scripts/production_git_snapshot.py"
            ).read_bytes(),
            "restore_production_git_snapshot.py": (
                ROOT / "scripts/restore_production_git_snapshot.py"
            ).read_bytes(),
            "deploy.py": deploy_payload,
        }
        for name, payload in payloads.items():
            (scripts / name).write_bytes(payload)
        manifest = {
            "schema_version": 1,
            "protocol_version": 1,
            "compatibility": {
                "handoff_protocol_versions": [1],
                "descriptor_schema_versions": [2, 4],
                "current_state_schema_versions": [2, 3],
                "marker_schema_versions": [2, 3],
                "worker_slot_schema_versions": [2],
                "prepare_abort_abi_versions": [1],
            },
            "entrypoints": {
                "deploy": {"kind": "python", "file": "deploy.py"},
            },
            "files": [
                {
                    "name": "deploy.py",
                    "source": "scripts/deploy.py",
                    "mode": 0o700,
                }
            ],
        }
        (scripts / "control-release.json").write_text(
            json.dumps(manifest, sort_keys=True),
            encoding="utf-8",
        )

    def _install_bootstrap(self) -> None:
        immutable: dict[str, str] = {}
        for name in ROUTER.BOOTSTRAP_IMMUTABLE_FILES:
            payload = (
                b"#!/usr/bin/python3\n# predecessor selector fixture\n"
                if name == "control_runtime_selector.py"
                else f"fixture {name}\n".encode("ascii")
            )
            immutable[name] = _write(
                self.runtime / "bin" / name,
                payload,
                0o700,
            )
        self.predecessor_selector_sha256 = immutable[
            "control_runtime_selector.py"
        ]
        active = {
            "schema_version": 1,
            "component": "deployment-controls",
            "release_id": "a" * 64,
            "source_sha": "a" * 40,
            "source_tree": "b" * 40,
            "manifest_sha256": "sha256:" + "c" * 64,
            "operation_id": "adopt-controls-test-0001",
        }
        _write_json(self.runtime / "state/active-control.json", active)
        bootstrap = {
            "schema_version": 3,
            "status": "completed",
            "authority_kind": "manual-runtime-adoption",
            "source_sha": "a" * 40,
            "source_tree": "b" * 40,
            "immutable_files": immutable,
            "active_control": active,
        }
        self.bootstrap_digest = _write_json(
            self.runtime / "state/bootstrap-control.json",
            bootstrap,
        )

    def _delivery(self, target_sha: str) -> dict[str, object]:
        return {
            "remote_main": target_sha,
            "ci": {
                "workflow_run_id": 1001,
                "run_attempt": 1,
                "head_sha": target_sha,
                "head_branch": "main",
                "event": "push",
                "path": ".github/workflows/ci.yml",
                "conclusion": "success",
                "required_jobs": ["release-tests"],
            },
        }

    def _install_snapshot_and_successors(self) -> None:
        manager = SNAPSHOT.ProductionGitSnapshotManager(
            self.source,
            self.production,
            self.runtime,
            allow_test=True,
            delivery_gate_probe=self._delivery,
        )
        operation = "snapshot-git-router-test-0001"
        planned = manager.plan(
            target_sha=self.target_sha,
            operation_id=operation,
        )
        with mock.patch.object(
            SNAPSHOT,
            "_fiemap_has_shared_extents",
            return_value=False,
        ):
            manager.apply(
                target_sha=self.target_sha,
                operation_id=operation,
                confirm_plan_sha256=planned["plan_sha256"],
                confirm_snapshot_impact_sha256=planned[
                    "snapshot_impact_sha256"
                ],
            )
        snapshot_authority, self.snapshot_digest = (
            SNAPSHOT.verify_snapshot_integrity(
                self.runtime,
                production_root=self.production,
            )
        )
        source = {
            "schema_version": 2,
            "status": "completed",
            "authority_kind": (
                "manual-runtime-adoption-git-permission-source-successor"
            ),
            "operation_id": "adopt-git-successor-router-test-0001",
            "source_sha": self.target_sha,
            "source_tree": self.target_tree,
            "bootstrap_control_sha256": self.bootstrap_digest,
            "snapshot_authority_sha256": self.snapshot_digest,
            "plan": {
                "production_source": {
                    "source_sha": snapshot_authority[
                        "production_source_sha"
                    ],
                    "source_tree": snapshot_authority[
                        "production_source_tree"
                    ],
                }
            },
        }
        self.source_digest = _write_json(
            self.runtime
            / "state/adopted-git-permission-source-successor.json",
            source,
        )
        unit = {
            "schema_version": 2,
            "status": "completed",
            "authority_kind": (
                "manual-runtime-adoption-unit-permission-hardening"
            ),
            "operation_id": "adopt-unit-permission-router-test-0001",
            "source_sha": self.target_sha,
            "source_tree": self.target_tree,
            "bootstrap_control_sha256": self.bootstrap_digest,
            "adopted_git_permission_source_successor_sha256": (
                self.source_digest
            ),
            "production_source_sha": snapshot_authority[
                "production_source_sha"
            ],
            "production_source_tree": snapshot_authority[
                "production_source_tree"
            ],
            "plan": {
                "git_permission_successor": {
                    "source_successor_authority": {
                        "schema_version": 2,
                        "authority_file_sha256": self.source_digest,
                        "snapshot_authority_sha256": self.snapshot_digest,
                    }
                }
            },
        }
        self.unit_digest = _write_json(
            self.runtime / "state/adopted-unit-permissions.json",
            unit,
        )

    def manager(self, *, checkpoint=None):  # type: ignore[no-untyped-def]
        return ROUTER.BootstrapRouterSuccessorManager(
            self.source,
            self.runtime,
            self.production,
            allow_test=True,
            remote_main_probe=lambda _target: self.target_sha,
            checkpoint=checkpoint,
        )

    def plan(self, manager=None):  # type: ignore[no-untyped-def]
        manager = manager or self.manager()
        return manager.plan(
            target_sha=self.target_sha,
            operation_id=self.operation_id,
        )

    def apply(self, manager, planned):  # type: ignore[no-untyped-def]
        plan = planned["plan"]
        return manager.apply(
            target_sha=self.target_sha,
            operation_id=self.operation_id,
            confirm_plan_sha256=planned["plan_sha256"],
            confirm_router_successor_impact_sha256=planned[
                "router_successor_impact_sha256"
            ],
            confirm_snapshot_authority_sha256=plan[
                "snapshot_authority_sha256"
            ],
            confirm_source_successor_authority_sha256=plan[
                "source_successor_authority_sha256"
            ],
            confirm_unit_permission_authority_sha256=plan[
                "unit_permission_authority_sha256"
            ],
            confirm_predecessor_selector_sha256=plan[
                "predecessor_selector_sha256"
            ],
        )


class BootstrapRouterSuccessorTests(unittest.TestCase):
    def test_plan_rejects_nonstandalone_or_replaced_source_clone(self) -> None:
        for variant in ("shallow", "alternates", "replace"):
            with self.subTest(variant=variant):
                fixture = Fixture()
                try:
                    if variant == "replace":
                        fixture._git(
                            fixture.source,
                            "replace",
                            fixture.target_sha,
                            fixture.production_sha,
                        )
                    else:
                        relative = (
                            ".git/shallow"
                            if variant == "shallow"
                            else ".git/objects/info/alternates"
                        )
                        _write(
                            fixture.source / relative,
                            (fixture.target_sha + "\n").encode("ascii"),
                            0o600,
                        )
                    with self.assertRaisesRegex(
                        ROUTER.RouterSuccessorError,
                        "target source clone",
                    ):
                        fixture.plan()
                finally:
                    fixture.close()

    def test_plan_is_zero_write_and_apply_preserves_active_control(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        before = sorted(
            path.relative_to(fixture.runtime).as_posix()
            for path in fixture.runtime.rglob("*")
        )
        active_before = (
            fixture.runtime / "state/active-control.json"
        ).read_bytes()
        planned = fixture.plan()
        after = sorted(
            path.relative_to(fixture.runtime).as_posix()
            for path in fixture.runtime.rglob("*")
        )
        self.assertEqual(before, after)
        self.assertTrue(planned["logical_zero_write"])

        apply_manager = fixture.manager()
        with mock.patch.object(
            ROUTER,
            "_run_git",
            wraps=ROUTER._run_git,
        ) as run_git:
            authority = fixture.apply(apply_manager, planned)
        fsck_calls = [
            call
            for call in run_git.call_args_list
            if len(call.args) > 1 and call.args[1] == "fsck"
        ]
        self.assertEqual(len(fsck_calls), 1)
        self.assertEqual(authority["status"], "completed")
        self.assertEqual(
            (fixture.runtime / "state/active-control.json").read_bytes(),
            active_before,
        )
        self.assertFalse(
            (fixture.runtime / ROUTER.ROUTER_FENCE_RELATIVE).exists()
        )
        self.assertEqual(
            ROUTER._private_file(
                fixture.runtime / ROUTER.SELECTOR_RELATIVE,
                mode=0o700,
            )[1],
            planned["plan"]["successor_selector_sha256"],
        )
        self.assertEqual(
            fixture.apply(fixture.manager(), planned),
            authority,
        )

    def test_every_commit_window_recovers_idempotently(self) -> None:
        checkpoints = (
            "bootstrap-router-intent",
            "bootstrap-router-interlock-ready",
            "bootstrap-router-control-release-ready",
            "bootstrap-router-files-ready",
            "bootstrap-router-selector-swap-intent",
            "bootstrap-router-selector-intent-published",
            "bootstrap-router-selector-exchanged",
            "bootstrap-router-selector-switched",
            "bootstrap-router-authority-commit-intent",
            "bootstrap-router-authority-published",
            "bootstrap-router-authority-sealed",
            "bootstrap-router-interlock-removed",
        )
        for checkpoint_name in checkpoints:
            with self.subTest(checkpoint=checkpoint_name):
                fixture = Fixture()
                try:
                    planned = fixture.plan()
                    fired = False

                    def checkpoint(name: str) -> None:
                        nonlocal fired
                        if name == checkpoint_name and not fired:
                            fired = True
                            raise InjectedCrash(name)

                    with self.assertRaises(InjectedCrash):
                        fixture.apply(
                            fixture.manager(checkpoint=checkpoint),
                            planned,
                        )
                    authority = fixture.apply(fixture.manager(), planned)
                    self.assertEqual(authority["status"], "completed")
                    self.assertFalse(
                        (fixture.runtime / ROUTER.ROUTER_FENCE_RELATIVE).exists()
                    )
                    self.assertEqual(
                        ROUTER._private_file(
                            fixture.runtime / ROUTER.SELECTOR_RELATIVE,
                            mode=0o700,
                        )[1],
                        planned["plan"]["successor_selector_sha256"],
                    )
                finally:
                    fixture.close()

    def test_authority_drift_fails_before_selector_change(self) -> None:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        planned = fixture.plan()
        unit_path = fixture.runtime / "state/adopted-unit-permissions.json"
        unit = json.loads(unit_path.read_text(encoding="utf-8"))
        unit["operation_id"] = "adopt-unit-permission-router-foreign-0001"
        _write_json(unit_path, unit)

        with self.assertRaisesRegex(
            ROUTER.RouterSuccessorError,
            "confirmations differ",
        ):
            fixture.apply(fixture.manager(), planned)
        self.assertEqual(
            ROUTER._private_file(
                fixture.runtime / ROUTER.SELECTOR_RELATIVE,
                mode=0o700,
            )[1],
            fixture.predecessor_selector_sha256,
        )
        self.assertFalse(
            (fixture.runtime / ROUTER.INTENT_RELATIVE).exists()
        )


if __name__ == "__main__":
    unittest.main()
