from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controller_module = load_script(
    "pull_deploy_controller_dft_helpers",
    "scripts/pull_deploy_controller.py",
)
environment_module = load_script(
    "monomer_dft_worker_env_tests",
    "scripts/monomer_dft_worker_env.py",
)
launcher_module = load_script(
    "monomer_dft_worker_launcher_tests",
    "scripts/monomer_dft_worker_launcher.py",
)


SHA_A = "a" * 40
TREE_B = "b" * 40
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
OPERATION = "deploy-20260814-dft-helper"


def private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def private_json(path: Path, document: dict[str, object]) -> None:
    private_directory(path.parent)
    path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


class DftGuardEvidenceTests(unittest.TestCase):
    def descriptor(self, path: Path) -> dict[str, object]:
        return {
            "monomer_dft": {
                "gpu": {
                    "guard_state_path": str(path),
                    "guard_schema_version": 1,
                    "index": "2",
                    "uuid": controller_module.MONOMER_DFT_GPU_UUID,
                }
            }
        }

    def write_guard(self, path: Path, observed_at: str) -> None:
        private_json(
            path,
            {
                "schema_version": 1,
                "gpu_index": "2",
                "gpu_uuid": controller_module.MONOMER_DFT_GPU_UUID,
                "status": "ready",
                "unknown_processes": [],
                "observed_at": observed_at,
            },
        )

    def test_accepts_only_canonical_second_resolution_utc(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dft-guard-") as raw:
            path = Path(raw) / "guard.json"
            now = controller_module.utc_now()
            self.write_guard(path, now)
            observed = controller_module.SystemLifecycle._validate_dft_guard_observation(
                self.descriptor(path)
            )
            self.assertEqual(observed["observed_at"], now)

            for noncanonical in (
                now.removesuffix("Z") + "+00:00",
                now.removesuffix("Z") + ".000Z",
                now.replace("T", " "),
            ):
                self.write_guard(path, noncanonical)
                with self.assertRaisesRegex(
                    controller_module.PullDeployError,
                    "timestamp is invalid",
                ):
                    controller_module.SystemLifecycle._validate_dft_guard_observation(
                        self.descriptor(path)
                    )

    def test_deep_guard_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dft-guard-") as raw:
            path = Path(raw) / "guard.json"
            path.write_bytes(b"{}")
            path.chmod(0o600)
            with (
                mock.patch.object(
                    controller_module.json,
                    "loads",
                    side_effect=RecursionError("too deep"),
                ),
                self.assertRaisesRegex(
                    controller_module.PullDeployError,
                    "observation is invalid",
                ),
            ):
                controller_module.SystemLifecycle._validate_dft_guard_observation(
                    self.descriptor(path)
                )


class CheckoutReaderFenceTests(unittest.TestCase):
    @staticmethod
    def process(proc_root: Path, pid: str, *, checkout_argv: Path | None) -> None:
        process = private_directory(proc_root / pid)
        outside = private_directory(proc_root.parent / "outside")
        os.symlink(outside, process / "cwd")
        os.symlink("/usr/bin/python3", process / "exe")
        # Field 22 (starttime) is index 19 after the comm field is removed.
        fields = ["S", *("0" for _ in range(18)), "12345"]
        (process / "stat").write_text(
            f"{pid} (escaped child) {' '.join(fields)}\n",
            encoding="utf-8",
        )
        argv = [b"python3"]
        if checkout_argv is not None:
            argv.append(os.fsencode(checkout_argv))
        (process / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
        (process / "maps").write_text("", encoding="utf-8")
        private_directory(process / "fd")

    def test_absolute_argv_from_checkout_catches_escaped_child(self) -> None:
        with tempfile.TemporaryDirectory(prefix="checkout-fence-") as raw:
            root = Path(raw)
            checkout = private_directory(root / "production")
            proc_root = private_directory(root / "proc")
            self.process(
                proc_root,
                "4242",
                checkout_argv=checkout / "workers/escaped.py",
            )
            with self.assertRaisesRegex(
                controller_module.PullDeployError,
                "live checkout retains 1",
            ):
                controller_module.SystemLifecycle._assert_no_checkout_readers(
                    checkout,
                    proc_root=proc_root,
                )

    def test_malformed_same_uid_proc_evidence_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="checkout-fence-") as raw:
            root = Path(raw)
            checkout = private_directory(root / "production")
            proc_root = private_directory(root / "proc")
            process = private_directory(proc_root / "4243")
            (process / "stat").write_text("4243 malformed\n", encoding="utf-8")
            with self.assertRaisesRegex(
                controller_module.PullDeployError,
                "process identity is invalid",
            ):
                controller_module.SystemLifecycle._assert_no_checkout_readers(
                    checkout,
                    proc_root=proc_root,
                )


class DftRuntimeInventoryTests(unittest.TestCase):
    def runtime(self, root: Path) -> Path:
        runtime = private_directory(root / "runtime")
        private_directory(runtime / "aimnet-cache")
        payload = runtime / "aimnet-cache/model.pt"
        payload.write_bytes(b"model")
        payload.chmod(0o600)
        private_directory(runtime / "venv/bin")
        os.symlink("/usr/bin/python3.12", runtime / "venv/bin/python")
        return runtime

    def test_controller_and_launcher_share_exact_immutable_inventory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dft-inventory-") as raw:
            root = Path(raw)
            runtime = self.runtime(root)
            controller_digest = controller_module.PullDeployController._dft_runtime_inventory(
                runtime
            )
            self.assertEqual(
                launcher_module._runtime_inventory(runtime),
                controller_digest,
            )
            external_warp = private_directory(root / "state/warp")
            (external_warp / "kernel.bin").write_bytes(b"mutable")
            self.assertEqual(
                controller_module.PullDeployController._dft_runtime_inventory(
                    runtime
                ),
                controller_digest,
            )

    def test_launcher_runtime_inventory_never_uses_read_all_payload_helper(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dft-streaming-inventory-") as raw:
            runtime = self.runtime(Path(raw))
            with mock.patch.object(
                launcher_module,
                "_pinned_payload",
                side_effect=AssertionError("runtime files must be streamed"),
            ):
                observed = launcher_module._runtime_inventory(runtime)
            self.assertEqual(
                observed,
                controller_module.PullDeployController._dft_runtime_inventory(
                    runtime
                ),
            )

    def test_hard_link_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dft-inventory-") as raw:
            runtime = self.runtime(Path(raw))
            os.link(
                runtime / "aimnet-cache/model.pt",
                runtime / "aimnet-cache/model-copy.pt",
            )
            with self.assertRaises(controller_module.PullDeployError):
                controller_module.PullDeployController._dft_runtime_inventory(runtime)
            with self.assertRaises(launcher_module.LauncherError):
                launcher_module._runtime_inventory(runtime)

    def test_runtime_validation_is_bound_to_canonical_sha_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dft-root-bind-") as raw:
            root = Path(raw)
            production = private_directory(root / "production")
            runtime_root = private_directory(root / "control")
            wrong = private_directory(root / "wrong")
            controller = controller_module.PullDeployController(
                production,
                runtime_root,
            )
            identity = {
                "root": str(wrong),
                "runtime_manifest_path": str(wrong / "runtime.json"),
                "runtime_manifest_sha256": DIGEST_C,
                "release_sha": SHA_A,
                "source_tree": TREE_B,
                "python": str(wrong / "venv/bin/python"),
                "requirements_lock_sha256": DIGEST_C,
                "aimnet_source_lock_sha256": DIGEST_D,
                "runtime_inventory_sha256": DIGEST_C,
                "models": {
                    f"model-{index}.pt": DIGEST_D for index in range(6)
                },
            }
            with self.assertRaisesRegex(
                controller_module.PullDeployError,
                "runtime changed",
            ):
                controller._validate_dft_runtime_directory(identity)


class DftAimnetRecordTests(unittest.TestCase):
    def test_malformed_csv_record_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dft-record-") as raw:
            venv = Path(raw) / "venv"
            metadata = venv / "lib/python3.12/site-packages/aimnet-x.dist-info"
            metadata.mkdir(parents=True)
            (metadata / "RECORD").write_text('"unterminated\n', encoding="utf-8")
            with self.assertRaisesRegex(
                controller_module.PullDeployError,
                "RECORD is invalid",
            ):
                controller_module.PullDeployController._validate_installed_dft_aimnet(
                    venv,
                    {
                        "source": {},
                        "registry": {
                            "path": "aimnet/registry.json",
                            "sha256": "0" * 64,
                        },
                    },
                )


class DftEnvironmentLoaderTests(unittest.TestCase):
    @staticmethod
    def environment_fixture(runtime_root: Path) -> tuple[Path, dict[str, str]]:
        release_root = runtime_root / "worker-venvs/dft" / SHA_A
        values = {
            "MONOMER_DFT_RELEASE_SHA": SHA_A,
            "MONOMER_DFT_RUNTIME_CONTRACT_SHA256": DIGEST_C,
            "MONOMER_DFT_RUNTIME_INVENTORY_SHA256": DIGEST_D,
            "MONOMER_DFT_PYTHON": str(release_root / "venv/bin/python"),
            "AIMNET_CACHE_DIR": str(release_root / "aimnet-cache"),
            "WARP_CACHE_PATH": str(
                runtime_root / "state/monomer-dft-warp-cache" / SHA_A
            ),
            "NEXPOLY_DFT_GPU_GUARD_MODE": "observe",
        }
        path = runtime_root / "runtime.env"
        path.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path, values

    def test_sealed_environment_fixes_queue_and_discards_arbitrary_variables(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dft-env-") as raw:
            runtime_root = private_directory(Path(raw) / "runtime")
            path, values = self.environment_fixture(runtime_root)
            with mock.patch.object(
                environment_module,
                "RUNTIME_ROOT",
                runtime_root,
            ):
                loaded = environment_module.load_runtime_environment(path)
                built = environment_module.build_environment(
                    loaded,
                    {"HOME": "/home/test", "SECRET": "must-not-pass"},
                )
            self.assertEqual(loaded, values)
            self.assertEqual(built["MONOMER_DFT_MAX_CONCURRENT_JOBS"], "1")
            self.assertEqual(built["MONOMER_DFT_MAX_QUEUED_JOBS"], "8")
            self.assertEqual(built["NEXPOLY_DFT_GPU_GUARD_MODE"], "observe")
            self.assertNotIn("SECRET", built)

    def test_environment_read_rejects_mode_and_link_count_drift(self) -> None:
        for mutation in ("chmod", "link"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                prefix="dft-env-race-"
            ) as raw:
                runtime_root = private_directory(Path(raw) / "runtime")
                path, _values = self.environment_fixture(runtime_root)
                original_read = environment_module.os.read
                changed = False

                def mutating_read(descriptor: int, size: int) -> bytes:
                    nonlocal changed
                    payload = original_read(descriptor, size)
                    if payload and not changed:
                        changed = True
                        if mutation == "chmod":
                            path.chmod(0o400)
                        else:
                            os.link(path, runtime_root / "runtime.env.link")
                    return payload

                with (
                    mock.patch.object(
                        environment_module.os,
                        "read",
                        side_effect=mutating_read,
                    ),
                    self.assertRaisesRegex(
                        environment_module.EnvironmentError,
                        "changed while reading",
                    ),
                ):
                    environment_module.load_runtime_environment(path)


class DftPinnedShellLauncherTests(unittest.TestCase):
    def test_python_launcher_injects_governed_fd_marker(self) -> None:
        with (
            mock.patch.object(launcher_module, "validate", return_value=37),
            mock.patch.object(
                launcher_module.os,
                "execve",
                side_effect=OSError("exec fixture"),
            ) as execute,
        ):
            self.assertEqual(launcher_module.main(), 2)
        command, argv, environment = execute.call_args.args
        self.assertEqual(command, "/usr/bin/bash")
        self.assertEqual(argv, ["/usr/bin/bash", "/proc/self/fd/37"])
        self.assertEqual(environment["NEXPOLY_DFT_GOVERNED_FD_LAUNCH"], "1")

    def test_production_fd_launch_uses_fixed_checkout_root(self) -> None:
        script = ROOT / "workers/monomer_dft_worker/run_host_worker.sh"
        descriptor = os.open(
            script,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            result = subprocess.run(
                ["/usr/bin/bash", f"/proc/self/fd/{descriptor}"],
                check=False,
                capture_output=True,
                text=True,
                pass_fds=(descriptor,),
                env={
                    "HOME": "/home/devuser",
                    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "MONOMER_DFT_DEPLOYMENT": "prod",
                    "NEXPOLY_DFT_GOVERNED_FD_LAUNCH": "1",
                    # This deliberate early fail occurs only after the script
                    # has accepted its production code/runtime roots.
                    "APP_POSTGRES_DSN": "fd-root-proof",
                },
                timeout=10,
            )
        finally:
            os.close(descriptor)
        self.assertEqual(result.returncode, 2)
        self.assertIn("APP_POSTGRES_DSN must not be present", result.stderr)
        self.assertNotIn("production Worker code root", result.stderr)

    def test_direct_development_checkout_cannot_claim_production(self) -> None:
        script = ROOT / "workers/monomer_dft_worker/run_host_worker.sh"
        result = subprocess.run(
            ["/usr/bin/bash", str(script)],
            check=False,
            capture_output=True,
            text=True,
            env={
                "HOME": "/home/devuser",
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "MONOMER_DFT_DEPLOYMENT": "prod",
            },
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("production Worker code root", result.stderr)


class DftPrepareAbortTests(unittest.TestCase):
    def controller(self, root: Path):
        production = private_directory(root / "production")
        runtime = private_directory(root / "control")
        controller = controller_module.PullDeployController(
            production,
            runtime,
            apply=True,
        )
        private_directory(controller.venv_root / "dft")
        private_directory(controller.prepare_aborts_dir)
        private_directory(controller.prepare_abort_archives_dir)
        controller.lock_path.write_text("", encoding="utf-8")
        controller.lock_path.chmod(0o600)
        return controller

    @staticmethod
    def owner() -> dict[str, object]:
        return {
            "schema_version": 1,
            "operation_id": OPERATION,
            "release_sha": SHA_A,
            "source_tree": TREE_B,
        }

    def journal(self, controller, evidence: dict[str, object]) -> dict[str, object]:
        return {
            "operation_id": OPERATION,
            "target_sha": SHA_A,
            "target_tree": TREE_B,
            "prepare_owner_sha256": DIGEST_C,
            "created_at": "2026-08-14T00:00:00Z",
            "archive_path": str(
                controller.prepare_abort_archives_dir / OPERATION
            ),
            "dft_staging": evidence,
        }

    @staticmethod
    def reconcile_twice(controller, journal: dict[str, object]) -> None:
        for _attempt in range(2):
            with controller.deployment_lock():
                controller._reconcile_prepare_abort_dft_staging(journal)
        controller._assert_prepare_abort_dft_terminal(journal)

    def test_dft_archive_mutation_requires_deployment_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dft-abort-") as raw:
            controller = self.controller(Path(raw))
            source = private_directory(
                controller.venv_root / "dft" / ".lock-required"
            )
            target = (
                controller.prepare_abort_archives_dir
                / OPERATION
                / "monomer-dft-runtime"
                / "staging"
            )
            with self.assertRaisesRegex(
                controller_module.PullDeployError,
                "lacks deploy.lock ownership",
            ):
                controller._archive_prepare_abort_directory(
                    source=source,
                    target=target,
                    expected_inventory_sha256=(
                        controller_module.directory_inventory_digest(source)
                    ),
                    label="monomer DFT runtime staging",
                )
            self.assertTrue(source.is_dir())
            self.assertFalse(target.exists())

    def test_archives_ownerless_mkdir_cache_and_incomplete_release_idempotently(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dft-abort-") as raw:
            controller = self.controller(Path(raw))
            dft_root = controller.venv_root / "dft"
            staging = private_directory(
                dft_root / f".{SHA_A}.preparing-{OPERATION}"
            )
            cache = private_directory(
                dft_root / ".build-cache" / SHA_A / OPERATION
            )
            private_json(
                cache / "owner.json",
                {
                    **self.owner(),
                    "requirements_lock_sha256": DIGEST_D,
                },
            )
            release = private_directory(dft_root / SHA_A)
            private_json(release / ".preparing.json", self.owner())
            (release / "partial").write_bytes(b"partial")
            (release / "partial").chmod(0o600)

            evidence, target_tree = controller._capture_prepare_abort_dft_staging(
                operation_id=OPERATION,
                target_sha=SHA_A,
                expected_target_tree=TREE_B,
            )
            self.assertEqual(target_tree, TREE_B)
            self.assertIsNotNone(evidence["staging_inventory_sha256"])
            self.assertIsNotNone(evidence["cache_inventory_sha256"])
            self.assertIsNotNone(
                evidence["incomplete_release_inventory_sha256"]
            )
            journal = self.journal(controller, evidence)
            self.reconcile_twice(controller, journal)
            self.assertFalse(staging.exists())
            self.assertFalse(cache.exists())
            self.assertFalse(release.exists())

    def test_ready_release_is_retained_while_operation_owner_is_archived(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dft-abort-") as raw:
            controller = self.controller(Path(raw))
            release = private_directory(controller.venv_root / "dft" / SHA_A)
            runtime_identity = {
                "release_sha": SHA_A,
                "source_tree": TREE_B,
                "requirements_lock_sha256": DIGEST_C,
                "aimnet_source_lock_sha256": DIGEST_D,
                "runtime_inventory_sha256": DIGEST_C,
            }
            private_json(
                release / "READY.json",
                {
                    "schema_version": 1,
                    "status": "ready",
                    "release_sha": SHA_A,
                    "source_tree": TREE_B,
                    "requirements_lock_sha256": DIGEST_C,
                    "aimnet_source_lock_sha256": DIGEST_D,
                    "runtime": {"sealed": True},
                    "ready_at": "2026-08-14T00:00:00Z",
                },
            )
            owner_path = release / ".preparing.json"
            private_json(owner_path, self.owner())
            with mock.patch.object(
                controller,
                "_validate_dft_runtime_directory",
                return_value=runtime_identity,
            ):
                evidence, _target_tree = (
                    controller._capture_prepare_abort_dft_staging(
                        operation_id=OPERATION,
                        target_sha=SHA_A,
                        expected_target_tree=TREE_B,
                    )
                )
                journal = self.journal(controller, evidence)
                self.reconcile_twice(controller, journal)
            self.assertTrue((release / "READY.json").is_file())
            self.assertFalse(owner_path.exists())
            self.assertTrue(
                (
                    Path(journal["archive_path"])
                    / "monomer-dft-runtime/ready-owner.json"
                ).is_file()
            )


if __name__ == "__main__":
    unittest.main()
