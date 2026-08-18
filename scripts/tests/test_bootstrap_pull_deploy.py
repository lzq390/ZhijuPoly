from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "bootstrap_pull_deploy.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_pull_deploy", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BOOTSTRAP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOOTSTRAP)
SOURCE_SHA = "1" * 40
SOURCE_TREE = "2" * 40
LIVE_SHA = "0" * 40
LIVE_TREE = "0" * 40
DFT_GPU_UUID = "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe"
TAKEOVER_OPERATION_ID = "takeover-fixture-0001"
ADOPTION_OPERATION_ID = "adopt-fixture-0001"


def takeover_binding(
    operation_id: str = TAKEOVER_OPERATION_ID,
) -> dict[str, object]:
    binding: dict[str, object] = {
        "schema_version": 1,
        "operation_id": operation_id,
        "authority_sha": SOURCE_SHA,
        "authority_tree": SOURCE_TREE,
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
    binding["binding_sha256"] = BOOTSTRAP.digest(
        json.dumps(
            binding,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    )
    return binding


class BootstrapPullDeployTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="nexpoly-pull-bootstrap-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.production = self.root / "production"
        self.runtime = self.root / "runtime"
        self.production.mkdir(mode=0o775)
        subprocess.run(
            ["git", "init", "--initial-branch=main"],
            cwd=self.production,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tracked = self.production / "tracked" / "fixture.txt"
        tracked.parent.mkdir(mode=0o775)
        tracked.write_text("fixture\n", encoding="utf-8")
        os.chmod(tracked, 0o664)
        subprocess.run(["git", "add", "tracked/fixture.txt"], cwd=self.production, check=True)

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        effective_arguments = list(arguments)
        if (
            "--check-source-readiness" not in effective_arguments
            and "--legacy-takeover-operation-id" not in effective_arguments
        ):
            effective_arguments.extend(
                [
                    "--legacy-takeover-operation-id",
                    TAKEOVER_OPERATION_ID,
                ]
            )
        # The pre-takeover installer owns creation of the shared lock.
        # Bootstrap must acquire it before making any runtime write.
        if (
            "--apply" in effective_arguments
            and "--check-source-readiness" not in effective_arguments
            and not (
            self.runtime.exists() or self.runtime.is_symlink()
            )
        ):
            state = self.runtime / "state"
            state.mkdir(parents=True, mode=0o700)
            os.chmod(self.runtime, 0o700)
            os.chmod(state, 0o700)
            lock = state / "deploy.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"NEXPOLY_ALLOW_TEST_ROOT": "1"}),
            mock.patch.object(
                BOOTSTRAP,
                "_completed_legacy_takeover",
                return_value=takeover_binding(),
            ),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = BOOTSTRAP.main(effective_arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def apply_arguments(self) -> list[str]:
        return [
            "--sha",
            SOURCE_SHA,
            "--apply",
            "--production-root",
            str(self.production),
            "--runtime-root",
            str(self.runtime),
            "--confirm-production-root",
            str(self.production.absolute()),
            "--confirm-runtime-root",
            str(self.runtime.absolute()),
            "--legacy-takeover-operation-id",
            TAKEOVER_OPERATION_ID,
            "--confirm-source-tree",
            SOURCE_TREE,
        ]

    def prepare_adoption_fixture(self) -> tuple[str, str]:
        state = self.runtime / "state"
        config = self.runtime / "config"
        state.mkdir(parents=True, mode=0o700)
        config.mkdir(mode=0o700)
        os.chmod(self.runtime, 0o700)
        os.chmod(state, 0o700)
        os.chmod(config, 0o700)
        unit_root = self.root / "systemd/user"
        unit_root.mkdir(parents=True, mode=0o700)
        md = unit_root / "nexpoly-monomer-md-worker.service"
        dft = unit_root / "nexpoly-monomer-dft-worker.service"
        md.write_bytes(b"[Service]\nExecStart=/manual-md\n")
        dft.write_text(
            "\n".join(
                (
                    "[Service]",
                    f"EnvironmentFile={BOOTSTRAP.RUNTIME_ROOT}/config/monomer-dft-runtime.env",
                    'Environment="MONOMER_DFT_GPU_GUARD_STATE='
                    f'{BOOTSTRAP.RUNTIME_ROOT}/state/gpu2-guard.json"',
                    'Environment="NEXPOLY_DFT_GPU_DEVICE=2"',
                    'Environment="NEXPOLY_DFT_OVERFLOW_GPU_DEVICES="',
                    'Environment="MONOMER_DFT_GPU_BROKER_ENABLED=0"',
                    'Environment="MONOMER_DFT_MAX_CONCURRENT_JOBS=1"',
                    'Environment="MONOMER_DFT_MAX_QUEUED_JOBS=8"',
                    f"ExecStartPre=/usr/bin/python3 -I -B {BOOTSTRAP.PRODUCTION_ROOT}/scripts/gpu2_guard.py --require-ready",
                    f"ExecStart={BOOTSTRAP.PRODUCTION_ROOT}/workers/monomer_dft_worker/run_host_worker.sh",
                    "",
                )
            ),
            encoding="utf-8",
        )
        os.chmod(md, 0o600)
        os.chmod(dft, 0o600)
        md_launcher = (
            self.production / "workers/monomer_md_worker/run_host_worker.sh"
        )
        dft_launcher = (
            self.production / "workers/monomer_dft_worker/run_host_worker.sh"
        )
        md_launcher.parent.mkdir(parents=True)
        dft_launcher.parent.mkdir(parents=True)
        md_launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
        dft_launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
        os.chmod(md_launcher, 0o755)
        os.chmod(dft_launcher, 0o755)
        worker_env = config / "worker.env"
        worker_env.write_bytes(b"NEXPOLY_GPU_DEVICE=2\n")
        os.chmod(worker_env, 0o600)
        deploy_env = config / "deploy.env"
        deploy_env.write_bytes(b"sealed deploy configuration\n")
        os.chmod(deploy_env, 0o600)
        app_env = config / "app.env"
        app_env.write_bytes(b"sealed application configuration\n")
        os.chmod(app_env, 0o600)
        slot_root = state / "worker-slots"
        slot_root.mkdir(mode=0o700)
        slot_path = slot_root / "md-a.json"
        slot = {
            "schema_version": 2,
            "status": "ready",
            "component": "monomer-md",
            "slot": "a",
            "source_sha": LIVE_SHA,
            "source_tree": LIVE_TREE,
        }
        BOOTSTRAP._atomic_json(slot_path, slot)
        active = {
            "schema_version": 1,
            "component": "monomer-md",
            "slot": "a",
            "source_sha": LIVE_SHA,
            "source_tree": LIVE_TREE,
            "worker_lock_sha256": "sha256:" + "7" * 64,
            "slot_record_sha256": BOOTSTRAP._canonical_json_digest(slot),
            "operation_id": "manual-md-test-adoption",
            "activated_at": "2026-01-01T00:00:00Z",
        }
        BOOTSTRAP._atomic_json(state / "monomer-md-active-slot.json", active)
        dft_root = self.runtime / "worker-venvs/dft" / LIVE_SHA
        model_root = dft_root / "aimnet-cache"
        model_root.mkdir(parents=True, mode=0o700)
        os.chmod(dft_root, 0o700)
        venv = dft_root / "venv"
        venv_bin = venv / "bin"
        venv_lib = venv / "lib/python3.12/site-packages"
        venv_bin.mkdir(parents=True, mode=0o700)
        venv_lib.mkdir(parents=True, mode=0o700)
        (venv_bin / "python").symlink_to("/usr/bin/python3.12")
        (venv_bin / "python3").symlink_to("python")
        (venv_bin / "python3.12").symlink_to("python")
        (venv / "lib64").symlink_to("lib")
        uv_lock = venv / ".lock"
        uv_lock.write_bytes(b"")
        os.chmod(uv_lock, 0o666)
        hardlink_source = venv_lib / "uv-cache-backed.py"
        hardlink_source.write_bytes(b"legacy hardlink baseline\n")
        os.chmod(hardlink_source, 0o600)
        os.link(hardlink_source, venv_lib / "uv-cache-backed-copy.py")
        warp_cache = dft_root / "warp-cache"
        warp_cache.mkdir(mode=0o700)
        mutable_warp_payload = warp_cache / "kernel.cache"
        mutable_warp_payload.write_bytes(b"mutable and excluded\n")
        os.chmod(mutable_warp_payload, 0o600)
        for name in (
            "aimnet2-pd_0.pt",
            "aimnet2_2025_b973c_d3_0.pt",
            "aimnet2_b973c_d3_0.pt",
            "aimnet2_rxn_0.pt",
            "aimnet2_wb97m_d3_0.pt",
            "aimnet2nse_wb97m_0.pt",
        ):
            checkpoint = model_root / name
            checkpoint.write_bytes((name + "\n").encode("ascii"))
            os.chmod(checkpoint, 0o600)
        BOOTSTRAP._atomic_json(
            dft_root / "runtime.json",
            {
                "schema_version": 1,
                "release": LIVE_SHA,
                "source_tree": LIVE_TREE,
                "requirements_lock_sha256": "sha256:" + "3" * 64,
                "aimnet_source_lock_sha256": "sha256:" + "4" * 64,
            },
        )
        dft_env = config / "monomer-dft-runtime.env"
        dft_env.write_text(
            "\n".join(
                (
                    f"MONOMER_DFT_RELEASE_SHA={LIVE_SHA}",
                    f"MONOMER_DFT_RUNTIME_CONTRACT_SHA256=sha256:{'5' * 64}",
                    f"MONOMER_DFT_PYTHON={dft_root / 'venv/bin/python'}",
                    f"AIMNET_CACHE_DIR={model_root}",
                    f"WARP_CACHE_PATH={dft_root / 'warp-cache'}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        os.chmod(dft_env, 0o600)
        BOOTSTRAP._atomic_json(
            state / "gpu2-guard.json",
            {
                "schema_version": 1,
                "gpu_index": "2",
                "gpu_uuid": DFT_GPU_UUID,
                "status": "ready",
            },
        )
        asset_root = self.root / "asset-release"
        asset_root.mkdir(mode=0o700)
        asset_manifest = asset_root / "ASSET-MANIFEST.json"
        asset_manifest.write_bytes(b"{}\n")
        os.chmod(asset_manifest, 0o600)
        (state / "current-assets").symlink_to(asset_root)
        return BOOTSTRAP.digest(md.read_bytes()), BOOTSTRAP.digest(dft.read_bytes())

    def adoption_base_arguments(self) -> list[str]:
        return [
            "--sha",
            SOURCE_SHA,
            "--live-sha",
            LIVE_SHA,
            "--operation-id",
            ADOPTION_OPERATION_ID,
            "--production-root",
            str(self.production),
            "--runtime-root",
            str(self.runtime),
        ]

    def adoption_apply_arguments(
        self,
        plan: dict[str, object],
        md_sha256: str,
        dft_sha256: str,
    ) -> list[str]:
        return [
            *self.adoption_base_arguments(),
            "--adopt-apply",
            "--confirm-production-root",
            str(self.production.absolute()),
            "--confirm-runtime-root",
            str(self.runtime.absolute()),
            "--confirm-source-tree",
            LIVE_TREE,
            "--confirm-evidence-sha256",
            str(plan["evidence_sha256"]),
            "--confirm-md-unit-sha256",
            md_sha256,
            "--confirm-dft-unit-sha256",
            dft_sha256,
        ]

    @staticmethod
    def adoption_abort_arguments(apply_arguments: list[str]) -> list[str]:
        return [
            value if value != "--adopt-apply" else "--adopt-abort"
            for value in apply_arguments
        ]

    def crash_adoption_before_phase_journal(
        self, phase: str
    ) -> tuple[list[str], dict[str, object]]:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        arguments = self.adoption_apply_arguments(plan, md_sha256, dft_sha256)
        original_advance = BOOTSTRAP._advance_adoption_transaction
        crashed = False

        def crash(*values, **keywords):  # type: ignore[no-untyped-def]
            nonlocal crashed
            if not crashed and keywords.get("phase") == phase:
                crashed = True
                raise BOOTSTRAP.BootstrapError(
                    f"injected pre-journal {phase} abort crash"
                )
            return original_advance(*values, **keywords)

        with mock.patch.object(
            BOOTSTRAP, "_advance_adoption_transaction", side_effect=crash
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertIn(f"pre-journal {phase} abort crash", error)
        transaction = BOOTSTRAP._load_private_json(
            BOOTSTRAP._adoption_transaction_path(
                self.runtime, operation_id=ADOPTION_OPERATION_ID
            )
        )
        return arguments, transaction

    def crash_adoption_after_baseline_link(
        self,
    ) -> tuple[list[str], dict[str, object], Path, Path]:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        arguments = self.adoption_apply_arguments(
            json.loads(output), md_sha256, dft_sha256
        )
        destination = self.runtime / "state/adopted-deployment.json"
        original = BOOTSTRAP._authorized_install_link_identity
        crashed = False

        def crash_link_pair(
            linked_destination: Path,
            temporary: Path,
            **keywords: object,
        ) -> dict[str, object]:
            nonlocal crashed
            if (
                not crashed
                and linked_destination == destination
                and keywords.get("remove_temporary") is True
            ):
                crashed = True
                raise BOOTSTRAP.BootstrapError(
                    "injected crash after hard-link publication"
                )
            return original(linked_destination, temporary, **keywords)

        with mock.patch.object(
            BOOTSTRAP,
            "_authorized_install_link_identity",
            side_effect=crash_link_pair,
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("hard-link publication", error)
        self.assertTrue(crashed)
        transaction = BOOTSTRAP._load_private_json(
            BOOTSTRAP._adoption_transaction_path(
                self.runtime, operation_id=ADOPTION_OPERATION_ID
            )
        )
        temporary = next(
            Path(str(value["path"]))
            for value in transaction["planned_paths"]
            if isinstance(value, dict)
            and value.get("kind") == "install-staging"
            and value.get("destination") == str(destination)
        )
        destination_metadata = destination.lstat()
        temporary_metadata = temporary.lstat()
        self.assertEqual(destination_metadata.st_nlink, 2)
        self.assertEqual(
            (destination_metadata.st_dev, destination_metadata.st_ino),
            (temporary_metadata.st_dev, temporary_metadata.st_ino),
        )
        return arguments, transaction, destination, temporary

    def crash_adoption_with_partial_baseline_staging(
        self,
    ) -> tuple[list[str], dict[str, object], Path, bytes]:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        arguments = self.adoption_apply_arguments(
            json.loads(output), md_sha256, dft_sha256
        )
        destination = self.runtime / "state/adopted-deployment.json"
        original = BOOTSTRAP._write_authorized_staging
        partial = b'{"part'
        temporary: Path | None = None

        def crash_partial(path: Path, payload: bytes, mode: int) -> None:
            nonlocal temporary
            if (
                temporary is None
                and path.parent == destination.parent
                and path.name.startswith(f".{destination.name}.")
                and path.name.endswith(".tmp")
            ):
                temporary = path
                path.write_bytes(partial)
                os.chmod(path, mode)
                raise BOOTSTRAP.BootstrapError(
                    "injected partial install staging crash"
                )
            original(path, payload, mode)

        with mock.patch.object(
            BOOTSTRAP,
            "_write_authorized_staging",
            side_effect=crash_partial,
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("partial install staging crash", error)
        self.assertIsNotNone(temporary)
        assert temporary is not None
        transaction = BOOTSTRAP._load_private_json(
            BOOTSTRAP._adoption_transaction_path(
                self.runtime, operation_id=ADOPTION_OPERATION_ID
            )
        )
        self.assertTrue(temporary.is_file())
        self.assertEqual(temporary.read_bytes(), partial)
        self.assertEqual(temporary.stat().st_nlink, 1)
        return arguments, transaction, temporary, partial

    def assert_adoption_planned_paths_absent(
        self, transaction: dict[str, object]
    ) -> None:
        planned = transaction["planned_paths"]
        self.assertIsInstance(planned, list)
        for ownership in planned:
            self.assertIsInstance(ownership, dict)
            path = Path(str(ownership["path"]))
            self.assertFalse(
                path.exists() or path.is_symlink(),
                f"planned adoption residue remains: {path}",
            )
        self.assertTrue((self.runtime / "state/deploy.lock").is_file())

    def committed_private_repo(self, path: Path) -> Path:
        previous_umask = os.umask(0o077)
        try:
            path.mkdir(mode=0o700)
            subprocess.run(
                ["git", "init", "--initial-branch=main", "--quiet"],
                cwd=path,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Bootstrap Fixture"],
                cwd=path,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "bootstrap@example.invalid"],
                cwd=path,
                check=True,
            )
            (path / "control.txt").write_text("reviewed\n", encoding="utf-8")
            subprocess.run(["git", "add", "control.txt"], cwd=path, check=True)
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "fixture"],
                cwd=path,
                check=True,
            )
        finally:
            os.umask(previous_umask)
        return path

    def ready_private_repo(self, path: Path) -> Path:
        source = self.committed_private_repo(path)
        subprocess.run(
            [
                "git",
                "remote",
                "add",
                "origin",
                BOOTSTRAP.REPOSITORY_SSH_URL,
            ],
            cwd=source,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "update-ref",
                "refs/remotes/origin/main",
                "HEAD",
            ],
            cwd=source,
            check=True,
        )
        return source

    def test_dry_run_is_non_mutating_and_lists_external_layout(self) -> None:
        result, output, error = self.run_main(
            "--sha",
            SOURCE_SHA,
            "--production-root",
            str(self.production),
            "--runtime-root",
            str(self.runtime),
        )
        self.assertEqual(result, 0, error)
        document = json.loads(output)
        self.assertFalse(document["apply"])
        self.assertFalse(self.runtime.exists())
        self.assertIn(str(self.runtime / "state" / "worker-slots"), document["directories"])
        self.assertIn(str(self.runtime / "worker-venvs"), document["directories"])
        self.assertIn(str(self.runtime / "control-releases"), document["directories"])
        self.assertNotIn(str(self.runtime / "worker-venvs" / "md-a"), document["directories"])
        self.assertIn("change Git HEAD or fetch", document["excluded_actions"])
        self.assertEqual(document["delivery_gate"]["remote_main"], SOURCE_SHA)

    def test_adoption_plan_is_strictly_read_only_and_seals_confirmations(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()

        def snapshot() -> list[tuple[str, int, bytes]]:
            result: list[tuple[str, int, bytes]] = []
            for path in sorted(self.runtime.rglob("*")):
                result.append(
                    (
                        path.relative_to(self.runtime).as_posix(),
                        stat.S_IMODE(path.lstat().st_mode),
                        path.read_bytes() if path.is_file() else b"",
                    )
                )
            return result

        before = snapshot()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        self.assertEqual(snapshot(), before)
        self.assertEqual(plan["action"], "manual-runtime-adoption")
        self.assertFalse(plan["apply"])
        self.assertEqual(plan["live_source_sha"], LIVE_SHA)
        self.assertEqual(plan["live_source_tree"], LIVE_TREE)
        self.assertEqual(
            plan["deploy_lock_disposition"], "permanent-control-layout"
        )
        self.assertEqual(
            plan["evidence_sha256"],
            BOOTSTRAP._canonical_json_digest(plan["evidence"]),
        )
        self.assertEqual(
            plan["confirmations"]["md_unit_sha256"], md_sha256
        )
        self.assertEqual(
            plan["confirmations"]["dft_unit_sha256"], dft_sha256
        )
        slot_path = self.runtime / "state/worker-slots/md-a.json"
        self.assertTrue(slot_path.read_bytes().endswith(b"\n"))
        slot_record = json.loads(slot_path.read_bytes())
        slot_identity_sha256 = BOOTSTRAP._canonical_json_digest(slot_record)
        slot_file_sha256 = BOOTSTRAP.digest(slot_path.read_bytes())
        self.assertNotEqual(slot_identity_sha256, slot_file_sha256)
        monomer_md = plan["evidence"]["monomer_md"]
        self.assertEqual(
            monomer_md["active_slot"]["slot_record_sha256"],
            slot_identity_sha256,
        )
        self.assertEqual(
            monomer_md["slot_record_file_sha256"],
            slot_file_sha256,
        )
        self.assertFalse((self.runtime / "state/deploy.lock").exists())
        self.assertFalse((self.runtime / "bin").exists())

    def test_adoption_apply_writes_bootstrap_v3_and_adopted_v1_only(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        md_unit = self.root / "systemd/user/nexpoly-monomer-md-worker.service"
        dft_unit = self.root / "systemd/user/nexpoly-monomer-dft-worker.service"
        unit_before = (md_unit.read_bytes(), dft_unit.read_bytes())
        result, output, error = self.run_main(
            *self.adoption_apply_arguments(plan, md_sha256, dft_sha256)
        )
        self.assertEqual(result, 0, error)
        applied = json.loads(output)
        self.assertEqual(applied["status"], "adopted")
        bootstrap = BOOTSTRAP._load_private_json(
            self.runtime / "state/bootstrap-control.json"
        )
        self.assertEqual(bootstrap["schema_version"], 3)
        self.assertEqual(
            bootstrap["authority_kind"], "manual-runtime-adoption"
        )
        self.assertNotIn("legacy_takeover", bootstrap)
        adopted = BOOTSTRAP._load_private_json(
            self.runtime / "state/adopted-deployment.json"
        )
        self.assertFalse(
            (self.runtime / "state/current-deployment.json").exists()
        )
        self.assertEqual(adopted["schema_version"], 1)
        self.assertEqual(adopted["status"], "adopted")
        self.assertEqual(adopted["source_sha"], LIVE_SHA)
        self.assertEqual(
            adopted["monomer_dft"]["gpu"]["uuid"], DFT_GPU_UUID
        )
        self.assertEqual(
            adopted["monomer_dft"]["runtime"]["runtime_inventory_sha256"],
            BOOTSTRAP._adopted_dft_runtime_inventory(
                self.runtime / "worker-venvs/dft" / LIVE_SHA
            ),
        )
        production_config = adopted["production_config"]
        self.assertEqual(set(production_config), {"deploy_env", "app_env"})
        for name, path in (
            ("deploy_env", self.runtime / "config/deploy.env"),
            ("app_env", self.runtime / "config/app.env"),
        ):
            self.assertEqual(production_config[name]["path"], str(path))
            self.assertEqual(
                production_config[name]["sha256"],
                BOOTSTRAP.digest(path.read_bytes()),
            )
            self.assertEqual(production_config[name]["mode"], "0600")
        deploy_lock = self.runtime / "state/deploy.lock"
        self.assertTrue(deploy_lock.is_file())
        self.assertEqual(stat.S_IMODE(deploy_lock.stat().st_mode), 0o600)
        self.assertEqual(unit_before, (md_unit.read_bytes(), dft_unit.read_bytes()))
        second, _output, error = self.run_main(
            *self.adoption_apply_arguments(plan, md_sha256, dft_sha256)
        )
        self.assertEqual(second, 0, error)

    def test_adoption_seals_existing_and_published_control_bindings(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        observed: list[tuple[Path, bool]] = []
        original = BOOTSTRAP._durability_barrier

        def record(path: Path, **keywords):  # type: ignore[no-untyped-def]
            observed.append((path, bool(keywords.get("directory"))))
            return original(path, **keywords)

        with mock.patch.object(
            BOOTSTRAP, "_durability_barrier", side_effect=record
        ):
            result, output, error = self.run_main(
                *self.adoption_apply_arguments(plan, md_sha256, dft_sha256)
            )
        self.assertEqual(result, 0, error)
        applied = json.loads(output)
        paths = {path for path, _directory in observed}
        self.assertIn(self.runtime / "state/deploy.lock", paths)
        for relative in BOOTSTRAP.DIRECTORIES:
            self.assertIn(self.runtime / relative, paths)
        for name in BOOTSTRAP.IMMUTABLE_FILES:
            self.assertIn(self.runtime / "bin" / name, paths)
        release = (
            self.runtime
            / "control-releases"
            / applied["active_control"]["release_id"]
        )
        self.assertIn((release, True), observed)

    def test_adoption_preflight_rejects_existing_current_state(self) -> None:
        self.prepare_adoption_fixture()
        current = self.runtime / "state/current-deployment.json"
        BOOTSTRAP._atomic_json(current, {"schema_version": 3})
        result, _output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 2)
        self.assertIn("current deployment state to be absent", error)
        self.assertFalse((self.runtime / "state/deploy.lock").exists())
        self.assertFalse((self.runtime / "bin").exists())

    def test_non_test_adoption_abort_rejects_custom_roots_without_writes(self) -> None:
        self.prepare_adoption_fixture()

        def snapshot() -> list[tuple[str, int, bytes]]:
            return [
                (
                    path.relative_to(self.runtime).as_posix(),
                    stat.S_IMODE(path.lstat().st_mode),
                    path.read_bytes() if path.is_file() else b"",
                )
                for path in sorted(self.runtime.rglob("*"))
            ]

        before = snapshot()
        args = BOOTSTRAP.build_parser().parse_args(
            [
                "--sha",
                SOURCE_SHA,
                "--live-sha",
                LIVE_SHA,
                "--operation-id",
                ADOPTION_OPERATION_ID,
                "--adopt-abort",
                "--production-root",
                str(self.production),
                "--runtime-root",
                str(self.runtime),
                "--confirm-production-root",
                str(self.production),
                "--confirm-runtime-root",
                str(self.runtime),
                "--confirm-source-tree",
                LIVE_TREE,
                "--confirm-evidence-sha256",
                "sha256:" + "1" * 64,
                "--confirm-md-unit-sha256",
                "sha256:" + "2" * 64,
                "--confirm-dft-unit-sha256",
                "sha256:" + "3" * 64,
            ]
        )
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "exact production/runtime roots"
        ):
            BOOTSTRAP._manual_adoption_main(args, allow_test=False)
        self.assertEqual(snapshot(), before)
        self.assertFalse((self.runtime / "state/deploy.lock").exists())

    def test_adoption_plan_rejects_a_different_gpu2_uuid(self) -> None:
        self.prepare_adoption_fixture()
        BOOTSTRAP._atomic_json(
            self.runtime / "state/gpu2-guard.json",
            {
                "schema_version": 1,
                "gpu_index": "2",
                "gpu_uuid": "GPU-" + "1" * 32,
                "status": "ready",
            },
        )
        result, _output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 2)
        self.assertIn("GPU2 guard identity is invalid", error)

    def test_adoption_plan_requires_exact_legacy_dft_unit_semantics(self) -> None:
        self.prepare_adoption_fixture()
        unit = self.root / "systemd/user/nexpoly-monomer-dft-worker.service"
        unit.write_text(
            unit.read_text(encoding="utf-8").replace(
                "gpu2_guard.py --require-ready", "gpu2_guard.py"
            ),
            encoding="utf-8",
        )
        os.chmod(unit, 0o600)
        result, _output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 2)
        self.assertIn("exact legacy GPU2 contract", error)

    def test_adoption_rejects_non_enforce_dft_process_environment(self) -> None:
        self.prepare_adoption_fixture()
        unit = self.root / "systemd/user/nexpoly-monomer-dft-worker.service"
        environment = dict(BOOTSTRAP.ADOPTED_DFT_PROCESS_ENVIRONMENT)
        environment["NEXPOLY_DFT_GPU_GUARD_MODE"] = "observe"
        with mock.patch.object(
            BOOTSTRAP,
            "_bounded_process_environment",
            return_value=environment,
        ):
            with self.assertRaisesRegex(
                BOOTSTRAP.BootstrapError, "fail-closed enforce mode"
            ):
                BOOTSTRAP._assert_adopted_dft_unit_semantics(
                    unit.read_bytes(), main_pid=1234, allow_test=False
                )

    def test_adoption_preplant_before_durable_staging_intent_is_retained(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        bin_root = self.runtime / "bin"
        bin_root.mkdir(mode=0o700)
        target = bin_root / "control_runtime_selector.py"
        preplant = BOOTSTRAP._adoption_install_temporary_path(
            target, operation_id=ADOPTION_OPERATION_ID
        )
        preplant.write_bytes(b"same uid preplant\n")
        os.chmod(preplant, 0o700)
        before = (preplant.stat().st_ino, preplant.read_bytes())

        result, _output, error = self.run_main(
            *self.adoption_apply_arguments(plan, md_sha256, dft_sha256)
        )
        self.assertEqual(result, 2, error)
        self.assertIn("pre-existing runtime controls", error)
        self.assertEqual((preplant.stat().st_ino, preplant.read_bytes()), before)

    def test_adoption_fresh_preflight_rejects_foreign_control_destinations(self) -> None:
        self.prepare_adoption_fixture()
        active = self.runtime / "state/active-control.json"
        BOOTSTRAP._atomic_json(active, {"foreign": True})
        before = active.read_bytes()
        result, _output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 2)
        self.assertIn("pre-existing control destination", error)
        self.assertEqual(active.read_bytes(), before)
        self.assertFalse((self.runtime / "state/deploy.lock").exists())

    def test_adoption_fresh_preflight_preserves_foreign_staging(self) -> None:
        self.prepare_adoption_fixture()
        bin_root = self.runtime / "bin"
        bin_root.mkdir(mode=0o700)
        staging = bin_root / ".control_runtime_selector.py.foreign.tmp"
        staging.write_bytes(b"foreign staging\n")
        os.chmod(staging, 0o700)
        before = staging.read_bytes()
        result, _output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 2)
        self.assertIn("pre-existing runtime controls", error)
        self.assertEqual(staging.read_bytes(), before)
        self.assertFalse((self.runtime / "state/deploy.lock").exists())

    def test_selector_routes_adopted_md_and_dft_with_static_cas(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        result, _output, error = self.run_main(
            *self.adoption_apply_arguments(plan, md_sha256, dft_sha256)
        )
        self.assertEqual(result, 0, error)
        selector = BOOTSTRAP._control_runtime(
            source_sha=SOURCE_SHA,
            allow_test=True,
        )
        for role in ("monomer-md", "monomer-dft"):
            manifest, release_root = selector._selected_release(  # type: ignore[attr-defined]
                self.runtime, role, []
            )
            self.assertEqual(manifest["source_sha"], SOURCE_SHA)
            self.assertTrue(release_root.is_dir())

        # The minute-by-minute guard observation is deliberately not a route
        # CAS input; the sealed UUID/index/policy still is.
        BOOTSTRAP._atomic_json(
            self.runtime / "state/gpu2-guard.json",
            {
                "schema_version": 1,
                "gpu_index": "2",
                "gpu_uuid": DFT_GPU_UUID,
                "status": "quarantined",
                "unknown_processes": [{"redacted": True}],
            },
        )
        selector._selected_release(self.runtime, "monomer-dft", [])  # type: ignore[attr-defined]

        # Mutable Warp output is deliberately outside the immutable digest.
        warp_payload = (
            self.runtime
            / "worker-venvs/dft"
            / LIVE_SHA
            / "warp-cache/kernel.cache"
        )
        warp_payload.write_bytes(b"new mutable cache content\n")
        selector._selected_release(self.runtime, "monomer-dft", [])  # type: ignore[attr-defined]

        # Legacy uv hard links are admitted, but their exact link count is CAS.
        hardlink_copy = (
            self.runtime
            / "worker-venvs/dft"
            / LIVE_SHA
            / "venv/lib/python3.12/site-packages/uv-cache-backed-copy.py"
        )
        hardlink_source = hardlink_copy.with_name("uv-cache-backed.py")
        hardlink_copy.unlink()
        with self.assertRaisesRegex(
            selector.ControlRuntimeError,  # type: ignore[attr-defined]
            "governed deployment authority",
        ):
            selector._selected_release(  # type: ignore[attr-defined]
                self.runtime, "monomer-dft", []
            )
        os.link(hardlink_source, hardlink_copy)
        selector._selected_release(self.runtime, "monomer-dft", [])  # type: ignore[attr-defined]

        checkpoint = (
            self.runtime
            / "worker-venvs/dft"
            / LIVE_SHA
            / "aimnet-cache/aimnet2-pd_0.pt"
        )
        checkpoint.write_bytes(b"tampered\n")
        os.chmod(checkpoint, 0o600)
        with self.assertRaisesRegex(
            selector.ControlRuntimeError,  # type: ignore[attr-defined]
            "governed deployment authority",
        ):
            selector._selected_release(  # type: ignore[attr-defined]
                self.runtime, "monomer-dft", []
            )

    def test_adopted_dft_inventory_prunes_warp_cache_before_scandir(self) -> None:
        previous_umask = os.umask(0o022)
        try:
            self.prepare_adoption_fixture()
        finally:
            os.umask(previous_umask)
        runtime = self.runtime / "worker-venvs/dft" / LIVE_SHA
        warp_cache = runtime / "warp-cache"
        original_scandir = os.scandir

        def reject_warp_traversal(path):  # type: ignore[no-untyped-def]
            if Path(path) == warp_cache:
                raise AssertionError("mutable warp-cache must not be traversed")
            return original_scandir(path)

        with mock.patch.object(
            BOOTSTRAP.os, "scandir", side_effect=reject_warp_traversal
        ):
            bootstrap_before = BOOTSTRAP._adopted_dft_runtime_inventory(runtime)

        selector = BOOTSTRAP._control_runtime(
            source_sha=SOURCE_SHA,
            allow_test=True,
        )
        with mock.patch.object(
            selector.os, "scandir", side_effect=reject_warp_traversal
        ):
            selector_before = selector.adopted_dft_runtime_inventory(runtime)
        self.assertEqual(selector_before, bootstrap_before)

        (warp_cache / "kernel.cache").write_bytes(
            b"a completely different mutable warp cache payload\n"
        )
        with mock.patch.object(
            BOOTSTRAP.os, "scandir", side_effect=reject_warp_traversal
        ):
            bootstrap_after = BOOTSTRAP._adopted_dft_runtime_inventory(runtime)
        with mock.patch.object(
            selector.os, "scandir", side_effect=reject_warp_traversal
        ):
            selector_after = selector.adopted_dft_runtime_inventory(runtime)
        self.assertEqual(bootstrap_after, bootstrap_before)
        self.assertEqual(selector_after, selector_before)

    def test_adoption_every_durable_phase_is_crash_resumable(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        arguments = self.adoption_apply_arguments(plan, md_sha256, dft_sha256)
        original_atomic = BOOTSTRAP._atomic_json
        intent_crashed = False

        def crash_intent(path: Path, document: dict[str, object]) -> None:
            nonlocal intent_crashed
            original_atomic(path, document)
            if (
                not intent_crashed
                and path.parent.name == "adoption-transactions"
                and document.get("phase") == "intent"
            ):
                intent_crashed = True
                raise BOOTSTRAP.BootstrapError("injected adoption intent crash")

        with mock.patch.object(
            BOOTSTRAP, "_atomic_json", side_effect=crash_intent
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2)
        self.assertIn("intent crash", error)
        transaction_path = BOOTSTRAP._adoption_transaction_path(
            self.runtime, operation_id=ADOPTION_OPERATION_ID
        )
        self.assertEqual(
            BOOTSTRAP._load_private_json(transaction_path)["phase"], "intent"
        )

        original_advance = BOOTSTRAP._advance_adoption_transaction
        for phase in (
            "layout-ready",
            "controls-ready",
            "baseline-ready",
            "authority-commit-intent",
            "completed",
        ):
            crashed = False

            def crash_phase(*values, **keywords):  # type: ignore[no-untyped-def]
                nonlocal crashed
                transaction = original_advance(*values, **keywords)
                if not crashed and keywords.get("phase") == phase:
                    crashed = True
                    raise BOOTSTRAP.BootstrapError(
                        f"injected adoption {phase} crash"
                    )
                return transaction

            with mock.patch.object(
                BOOTSTRAP,
                "_advance_adoption_transaction",
                side_effect=crash_phase,
            ):
                result, _output, error = self.run_main(*arguments)
            self.assertEqual(result, 2, (phase, error))
            self.assertIn(phase, error)
            self.assertEqual(
                BOOTSTRAP._load_private_json(transaction_path)["phase"], phase
            )
        result, output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "adopted")

    def test_adoption_reseals_visible_layout_plan_before_mutation(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        arguments = self.adoption_apply_arguments(
            json.loads(output), md_sha256, dft_sha256
        )
        transaction_path = BOOTSTRAP._adoption_transaction_path(
            self.runtime, operation_id=ADOPTION_OPERATION_ID
        )
        original_atomic = BOOTSTRAP._atomic_json
        lost_parent_fsync_response = False

        def expose_layout_plan(
            path: Path, document: dict[str, object]
        ) -> None:
            nonlocal lost_parent_fsync_response
            original_atomic(path, document)
            if (
                not lost_parent_fsync_response
                and path == transaction_path
                and document.get("phase") == "intent"
                and "layout" in document.get("step_plans", {})
            ):
                lost_parent_fsync_response = True
                raise BOOTSTRAP.BootstrapError(
                    "injected visible layout-plan journal"
                )

        with mock.patch.object(
            BOOTSTRAP, "_atomic_json", side_effect=expose_layout_plan
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("visible layout-plan journal", error)
        self.assertTrue(lost_parent_fsync_response)

        original_reseal = BOOTSTRAP._reseal_adoption_transaction
        original_layout = BOOTSTRAP._ensure_adoption_layout
        resealed = False
        second_crash = False

        def record_reseal(
            path: Path, transaction: dict[str, object]
        ) -> dict[str, object]:
            nonlocal resealed
            sealed = original_reseal(path, transaction)
            resealed = True
            return sealed

        def crash_after_layout(runtime_root: Path):  # type: ignore[no-untyped-def]
            nonlocal second_crash
            self.assertTrue(
                resealed,
                "layout mutation ran before the recovered journal was durable",
            )
            value = original_layout(runtime_root)
            if not second_crash:
                second_crash = True
                raise BOOTSTRAP.BootstrapError(
                    "injected second crash after layout mutation"
                )
            return value

        with (
            mock.patch.object(
                BOOTSTRAP,
                "_reseal_adoption_transaction",
                side_effect=record_reseal,
            ),
            mock.patch.object(
                BOOTSTRAP,
                "_ensure_adoption_layout",
                side_effect=crash_after_layout,
            ),
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("second crash after layout mutation", error)
        self.assertTrue(resealed)
        self.assertEqual(
            BOOTSTRAP._load_private_json(transaction_path)["phase"],
            "intent",
        )

        result, output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "adopted")

    def test_adoption_reseals_visible_authority_intent_before_publish(
        self,
    ) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        arguments = self.adoption_apply_arguments(
            json.loads(output), md_sha256, dft_sha256
        )
        transaction_path = BOOTSTRAP._adoption_transaction_path(
            self.runtime, operation_id=ADOPTION_OPERATION_ID
        )
        original_atomic = BOOTSTRAP._atomic_json
        lost_parent_fsync_response = False

        def expose_authority_intent(
            path: Path, document: dict[str, object]
        ) -> None:
            nonlocal lost_parent_fsync_response
            original_atomic(path, document)
            if (
                not lost_parent_fsync_response
                and path == transaction_path
                and document.get("phase") == "authority-commit-intent"
            ):
                lost_parent_fsync_response = True
                raise BOOTSTRAP.BootstrapError(
                    "injected visible authority-intent journal"
                )

        with mock.patch.object(
            BOOTSTRAP, "_atomic_json", side_effect=expose_authority_intent
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("visible authority-intent journal", error)
        self.assertFalse(
            (self.runtime / "state/bootstrap-control.json").exists()
        )

        original_reseal = BOOTSTRAP._reseal_adoption_transaction
        original_install = BOOTSTRAP._install_exact
        resealed = False
        second_crash = False

        def record_reseal(
            path: Path, transaction: dict[str, object]
        ) -> dict[str, object]:
            nonlocal resealed
            sealed = original_reseal(path, transaction)
            resealed = True
            return sealed

        def crash_after_bootstrap_publish(
            path: Path,
            payload: bytes,
            mode: int,
            **keywords,
        ):  # type: ignore[no-untyped-def]
            nonlocal second_crash
            if path == self.runtime / "state/bootstrap-control.json":
                self.assertTrue(
                    resealed,
                    "authority publish ran before journal reseal",
                )
            value = original_install(path, payload, mode, **keywords)
            if (
                path == self.runtime / "state/bootstrap-control.json"
                and not second_crash
            ):
                second_crash = True
                raise BOOTSTRAP.BootstrapError(
                    "injected second crash after bootstrap publish"
                )
            return value

        with (
            mock.patch.object(
                BOOTSTRAP,
                "_reseal_adoption_transaction",
                side_effect=record_reseal,
            ),
            mock.patch.object(
                BOOTSTRAP,
                "_install_exact",
                side_effect=crash_after_bootstrap_publish,
            ),
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("second crash after bootstrap publish", error)
        self.assertTrue(resealed)
        self.assertEqual(
            BOOTSTRAP._load_private_json(transaction_path)["phase"],
            "authority-commit-intent",
        )

        result, output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "adopted")
        self.assertEqual(
            BOOTSTRAP._load_private_json(transaction_path)["phase"],
            "completed",
        )

    def test_adoption_abort_reseals_visible_terminal_before_return(self) -> None:
        arguments, transaction = self.crash_adoption_before_phase_journal(
            "layout-ready"
        )
        abort_arguments = self.adoption_abort_arguments(arguments)
        transaction_path = BOOTSTRAP._adoption_transaction_path(
            self.runtime, operation_id=ADOPTION_OPERATION_ID
        )
        original_atomic = BOOTSTRAP._atomic_json
        lost_parent_fsync_response = False

        def expose_aborted_terminal(
            path: Path, document: dict[str, object]
        ) -> None:
            nonlocal lost_parent_fsync_response
            original_atomic(path, document)
            if (
                not lost_parent_fsync_response
                and path == transaction_path
                and document.get("status") == "aborted"
            ):
                lost_parent_fsync_response = True
                raise BOOTSTRAP.BootstrapError(
                    "injected visible aborted journal"
                )

        with mock.patch.object(
            BOOTSTRAP, "_atomic_json", side_effect=expose_aborted_terminal
        ):
            result, _output, error = self.run_main(*abort_arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("visible aborted journal", error)
        self.assertEqual(
            BOOTSTRAP._load_private_json(transaction_path)["status"],
            "aborted",
        )
        self.assert_adoption_planned_paths_absent(transaction)

        original_reseal = BOOTSTRAP._reseal_adoption_transaction
        second_fault = False

        def lose_reseal_response(
            path: Path, value: dict[str, object]
        ) -> dict[str, object]:
            nonlocal second_fault
            sealed = original_reseal(path, value)
            if not second_fault:
                second_fault = True
                raise BOOTSTRAP.BootstrapError(
                    "injected second terminal reseal fault"
                )
            return sealed

        with mock.patch.object(
            BOOTSTRAP,
            "_reseal_adoption_transaction",
            side_effect=lose_reseal_response,
        ):
            result, _output, error = self.run_main(*abort_arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("second terminal reseal fault", error)
        self.assertTrue(second_fault)
        self.assertEqual(
            BOOTSTRAP._load_private_json(transaction_path)["status"],
            "aborted",
        )

        resealed = False

        def record_reseal(
            path: Path, value: dict[str, object]
        ) -> dict[str, object]:
            nonlocal resealed
            result = original_reseal(path, value)
            resealed = True
            return result

        with mock.patch.object(
            BOOTSTRAP,
            "_reseal_adoption_transaction",
            side_effect=record_reseal,
        ):
            result, output, error = self.run_main(*abort_arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "already-aborted")
        self.assertTrue(resealed)

    def test_adoption_ignores_private_random_journal_staging_after_sigkill(
        self,
    ) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        arguments = self.adoption_apply_arguments(
            plan, md_sha256, dft_sha256
        )
        transaction_path = BOOTSTRAP._adoption_transaction_path(
            self.runtime, operation_id=ADOPTION_OPERATION_ID
        )
        original_replace = BOOTSTRAP.os.replace
        staged: dict[str, object] = {}

        def crash_before_journal_replace(
            source: os.PathLike[str] | str,
            destination: os.PathLike[str] | str,
        ) -> None:
            source_path = Path(source)
            destination_path = Path(destination)
            if not staged and destination_path == transaction_path:
                staged.update(
                    path=source_path,
                    payload=source_path.read_bytes(),
                )
                raise BOOTSTRAP.BootstrapError(
                    "injected SIGKILL before adoption journal replace"
                )
            original_replace(source, destination)

        with mock.patch.object(
            BOOTSTRAP.os,
            "replace",
            side_effect=crash_before_journal_replace,
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("before adoption journal replace", error)
        self.assertFalse(transaction_path.exists())
        staging_path = Path(str(staged["path"]))
        payload = bytes(staged["payload"])
        self.assertRegex(
            staging_path.name,
            rf"^\.{ADOPTION_OPERATION_ID}\.json\.[0-9a-f]{{24}}\.tmp$",
        )
        staging_path.write_bytes(payload)
        os.chmod(staging_path, 0o600)
        with staging_path.open("rb") as stream:
            os.fsync(stream.fileno())
        BOOTSTRAP._fsync_directory(staging_path.parent)

        before = (
            staging_path.lstat().st_ino,
            staging_path.read_bytes(),
        )
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["evidence_sha256"], plan["evidence_sha256"])
        self.assertEqual(
            (staging_path.lstat().st_ino, staging_path.read_bytes()),
            before,
        )

        result, output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "adopted")
        self.assertTrue(staging_path.is_file())
        self.assertIn(
            ADOPTION_OPERATION_ID,
            BOOTSTRAP._adoption_transactions(self.runtime),
        )

    def test_adoption_rejects_unsafe_random_journal_staging(self) -> None:
        self.prepare_adoption_fixture()
        directory = (
            self.runtime / BOOTSTRAP.ADOPTION_TRANSACTION_RELATIVE_DIRECTORY
        )
        directory.mkdir(parents=True, mode=0o700)
        os.chmod(directory, 0o700)
        staging = directory / (
            f".{ADOPTION_OPERATION_ID}.json.{'a' * 24}.tmp"
        )
        staging.write_bytes(b"unsafe\n")
        os.chmod(staging, 0o640)

        result, _output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 2)
        self.assertIn("transaction staging is unsafe", error)

    def test_foreign_adoption_operation_cannot_reuse_completed_controls(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        arguments = self.adoption_apply_arguments(plan, md_sha256, dft_sha256)
        original_advance = BOOTSTRAP._advance_adoption_transaction
        crashed = False

        def crash_after_controls(*values, **keywords):  # type: ignore[no-untyped-def]
            nonlocal crashed
            transaction = original_advance(*values, **keywords)
            if not crashed and keywords.get("phase") == "controls-ready":
                crashed = True
                raise BOOTSTRAP.BootstrapError("injected controls-ready crash")
            return transaction

        with mock.patch.object(
            BOOTSTRAP,
            "_advance_adoption_transaction",
            side_effect=crash_after_controls,
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2)
        self.assertIn("controls-ready crash", error)

        def snapshot() -> list[tuple[str, int, bytes]]:
            values: list[tuple[str, int, bytes]] = []
            for path in sorted(self.runtime.rglob("*")):
                values.append(
                    (
                        path.relative_to(self.runtime).as_posix(),
                        stat.S_IMODE(path.lstat().st_mode),
                        path.read_bytes() if path.is_file() else b"",
                    )
                )
            return values

        before = snapshot()
        other_id = "adopt-fixture-0002"
        other_base = [
            other_id if value == ADOPTION_OPERATION_ID else value
            for value in self.adoption_base_arguments()
        ]
        result, _output, error = self.run_main(*other_base, "--adopt-plan")
        self.assertEqual(result, 2)
        self.assertIn("another manual runtime adoption", error)
        self.assertEqual(snapshot(), before)

        other_apply = [
            other_id if value == ADOPTION_OPERATION_ID else value
            for value in arguments
        ]
        result, _output, error = self.run_main(*other_apply)
        self.assertEqual(result, 2)
        self.assertIn("another manual runtime adoption", error)
        self.assertEqual(snapshot(), before)

    def test_adoption_control_release_publish_never_clobbers_raced_destination(
        self,
    ) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        arguments = self.adoption_apply_arguments(plan, md_sha256, dft_sha256)
        original = BOOTSTRAP._rename_noreplace
        raced: Path | None = None

        def race(source: Path, destination: Path) -> None:
            nonlocal raced
            if raced is None:
                destination.mkdir(mode=0o700)
                raced = destination
            original(source, destination)

        with mock.patch.object(BOOTSTRAP, "_rename_noreplace", side_effect=race):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2)
        self.assertIsNotNone(raced)
        assert raced is not None
        self.assertTrue(raced.is_dir())
        self.assertEqual(list(raced.iterdir()), [])
        raced.rmdir()

        result, output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "adopted")

    def test_control_release_publish_response_loss_recovers(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        arguments = self.adoption_apply_arguments(
            json.loads(output), md_sha256, dft_sha256
        )
        original = BOOTSTRAP._rename_noreplace
        lost = False
        published: Path | None = None

        def lose_response(source: Path, destination: Path) -> None:
            nonlocal lost, published
            original(source, destination)
            if not lost:
                lost = True
                published = destination
                raise BOOTSTRAP.BootstrapError(
                    "injected control publication response loss"
                )

        with mock.patch.object(
            BOOTSTRAP, "_rename_noreplace", side_effect=lose_response
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("publication response loss", error)
        self.assertIsNotNone(published)
        assert published is not None
        self.assertTrue(published.is_dir())

        result, output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "adopted")
        self.assertTrue(published.is_dir())

    def test_authority_completion_exchange_preserves_a_raced_destination(
        self,
    ) -> None:
        state = self.runtime / "state"
        state.mkdir(parents=True, mode=0o700)
        os.chmod(self.runtime, 0o700)
        path = state / "bootstrap-control.json"
        operation_id = "adopt-authority-cas-test"
        temporary = state / (
            f".{path.name}.{operation_id}.complete.tmp"
        )
        expected = b'{"status":"prepared"}\n'
        replacement = b'{"status":"completed"}\n'
        foreign = b'{"status":"foreign"}\n'
        authority = BOOTSTRAP._adoption_install_staging_plan(
            temporary,
            path,
            replacement,
            0o600,
            operation_id=operation_id,
            purpose="cas",
        )
        path.write_bytes(expected)
        os.chmod(path, 0o600)
        original = BOOTSTRAP._rename_exchange
        raced = False

        def race(first: Path, second: Path) -> None:
            nonlocal raced
            if not raced:
                raced = True
                BOOTSTRAP._atomic_file(first, foreign, 0o600)
            original(first, second)

        with mock.patch.object(BOOTSTRAP, "_rename_exchange", side_effect=race):
            with self.assertRaisesRegex(
                BOOTSTRAP.BootstrapError,
                "destination changed before exchange",
            ):
                BOOTSTRAP._cas_replace_exact_file(
                    path,
                    expected_payload=expected,
                    replacement_payload=replacement,
                    mode=0o600,
                    temporary_path=temporary,
                    temporary_authority=authority,
                )
        self.assertEqual(path.read_bytes(), foreign)
        self.assertEqual(temporary.read_bytes(), replacement)

        temporary.unlink()
        BOOTSTRAP._atomic_file(path, expected, 0o600)
        BOOTSTRAP._cas_replace_exact_file(
            path,
            expected_payload=expected,
            replacement_payload=replacement,
            mode=0o600,
            temporary_path=temporary,
            temporary_authority=authority,
        )
        self.assertEqual(path.read_bytes(), replacement)
        self.assertFalse(temporary.exists())

    def test_authority_cas_recovers_partial_authorized_staging(self) -> None:
        state = self.runtime / "state"
        state.mkdir(parents=True, mode=0o700)
        os.chmod(self.runtime, 0o700)
        path = state / "bootstrap-control.json"
        operation_id = "adopt-partial-cas-test"
        temporary = state / f".{path.name}.{operation_id}.complete.tmp"
        expected = b'{"status":"prepared"}\n'
        replacement = b'{"status":"completed"}\n'
        path.write_bytes(expected)
        temporary.write_bytes(replacement[:5])
        os.chmod(path, 0o600)
        os.chmod(temporary, 0o600)
        authority = BOOTSTRAP._adoption_install_staging_plan(
            temporary,
            path,
            replacement,
            0o600,
            operation_id=operation_id,
            purpose="cas",
        )

        BOOTSTRAP._cas_replace_exact_file(
            path,
            expected_payload=expected,
            replacement_payload=replacement,
            mode=0o600,
            temporary_path=temporary,
            temporary_authority=authority,
        )

        self.assertEqual(path.read_bytes(), replacement)
        self.assertFalse(temporary.exists())

    def test_completed_cas_reseals_inode_and_parent_after_response_loss(self) -> None:
        state = self.runtime / "state"
        state.mkdir(parents=True, mode=0o700)
        os.chmod(self.runtime, 0o700)
        path = state / "bootstrap-control.json"
        operation_id = "adopt-complete-cas-test"
        temporary = state / f".{path.name}.{operation_id}.complete.tmp"
        expected = b'{"status":"prepared"}\n'
        replacement = b'{"status":"completed"}\n'
        path.write_bytes(replacement)
        temporary.write_bytes(expected)
        os.chmod(path, 0o600)
        os.chmod(temporary, 0o600)
        authority = BOOTSTRAP._adoption_install_staging_plan(
            temporary,
            path,
            replacement,
            0o600,
            operation_id=operation_id,
            purpose="cas",
        )
        original = BOOTSTRAP._fsync_directory
        lost = False

        def lose_parent_response(parent: Path) -> None:
            nonlocal lost
            original(parent)
            if parent == state and not lost:
                lost = True
                raise OSError("injected CAS parent-fsync response loss")

        with mock.patch.object(
            BOOTSTRAP, "_fsync_directory", side_effect=lose_parent_response
        ):
            with self.assertRaisesRegex(OSError, "parent-fsync response loss"):
                BOOTSTRAP._cas_replace_exact_file(
                    path,
                    expected_payload=expected,
                    replacement_payload=replacement,
                    mode=0o600,
                    temporary_path=temporary,
                    temporary_authority=authority,
                )
        self.assertTrue(lost)
        self.assertTrue(temporary.exists())

        BOOTSTRAP._cas_replace_exact_file(
            path,
            expected_payload=expected,
            replacement_payload=replacement,
            mode=0o600,
            temporary_path=temporary,
            temporary_authority=authority,
        )
        self.assertEqual(path.read_bytes(), replacement)
        self.assertFalse(temporary.exists())

    def test_adoption_cas_crash_before_exchange_resumes(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        arguments = self.adoption_apply_arguments(
            json.loads(output), md_sha256, dft_sha256
        )
        crashed = False

        def crash_before_exchange(first: Path, second: Path) -> None:
            nonlocal crashed
            if not crashed:
                crashed = True
                raise BOOTSTRAP.BootstrapError(
                    "injected crash before authority exchange"
                )
            BOOTSTRAP._rename_exchange(first, second)

        with mock.patch.object(
            BOOTSTRAP, "_rename_exchange", side_effect=crash_before_exchange
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("before authority exchange", error)
        bootstrap_path = self.runtime / "state/bootstrap-control.json"
        temporary = self.runtime / "state" / (
            f".{bootstrap_path.name}.{ADOPTION_OPERATION_ID}.complete.tmp"
        )
        self.assertEqual(
            BOOTSTRAP._load_private_json(bootstrap_path)["status"], "prepared"
        )
        self.assertEqual(
            BOOTSTRAP._load_private_json(temporary)["status"], "completed"
        )

        result, output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "adopted")
        self.assertEqual(
            BOOTSTRAP._load_private_json(bootstrap_path)["status"], "completed"
        )
        self.assertFalse(temporary.exists())

    def test_adoption_cas_crash_after_exchange_resumes_and_cleans(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        arguments = self.adoption_apply_arguments(
            json.loads(output), md_sha256, dft_sha256
        )
        original_exchange = BOOTSTRAP._rename_exchange
        crashed = False

        def crash_after_exchange(first: Path, second: Path) -> None:
            nonlocal crashed
            original_exchange(first, second)
            if not crashed:
                crashed = True
                raise BOOTSTRAP.BootstrapError(
                    "injected authority exchange response loss"
                )

        with mock.patch.object(
            BOOTSTRAP, "_rename_exchange", side_effect=crash_after_exchange
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("exchange response loss", error)
        bootstrap_path = self.runtime / "state/bootstrap-control.json"
        temporary = self.runtime / "state" / (
            f".{bootstrap_path.name}.{ADOPTION_OPERATION_ID}.complete.tmp"
        )
        self.assertEqual(
            BOOTSTRAP._load_private_json(bootstrap_path)["status"], "completed"
        )
        self.assertEqual(
            BOOTSTRAP._load_private_json(temporary)["status"], "prepared"
        )

        result, output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "adopted")
        self.assertFalse(temporary.exists())

    def test_adoption_cas_resume_rejects_foreign_sibling(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        arguments = self.adoption_apply_arguments(
            json.loads(output), md_sha256, dft_sha256
        )
        crashed = False

        def crash_before_exchange(_first: Path, _second: Path) -> None:
            nonlocal crashed
            crashed = True
            raise BOOTSTRAP.BootstrapError("injected CAS staging crash")

        with mock.patch.object(
            BOOTSTRAP, "_rename_exchange", side_effect=crash_before_exchange
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertTrue(crashed)
        foreign = self.runtime / "state/.bootstrap-control.json.foreign.tmp"
        foreign.write_bytes(b"foreign\n")
        os.chmod(foreign, 0o600)

        result, _output, error = self.run_main(*arguments)

        self.assertEqual(result, 2)
        self.assertIn("foreign staging files", error)
        self.assertEqual(foreign.read_bytes(), b"foreign\n")

    def test_completed_adoption_cas_rejects_drifted_residue(self) -> None:
        state = self.runtime / "state"
        state.mkdir(parents=True, mode=0o700)
        os.chmod(self.runtime, 0o700)
        path = state / "bootstrap-control.json"
        operation_id = "adopt-drifted-cas-residue"
        temporary = state / f".{path.name}.{operation_id}.complete.tmp"
        expected = b'{"status":"prepared"}\n'
        replacement = b'{"status":"completed"}\n'
        path.write_bytes(replacement)
        temporary.write_bytes(b'{"status":"drifted"}\n')
        os.chmod(path, 0o600)
        os.chmod(temporary, 0o600)
        authority = BOOTSTRAP._adoption_install_staging_plan(
            temporary,
            path,
            replacement,
            0o600,
            operation_id=operation_id,
            purpose="cas",
        )

        with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, "CAS residue"):
            BOOTSTRAP._cas_replace_exact_file(
                path,
                expected_payload=expected,
                replacement_payload=replacement,
                mode=0o600,
                temporary_path=temporary,
                temporary_authority=authority,
            )
        self.assertEqual(path.read_bytes(), replacement)
        self.assertTrue(temporary.exists())

    def test_quarantine_dirfd_cas_restores_a_swapped_source(self) -> None:
        source_parent = self.root / "quarantine-source"
        target_parent = self.root / "quarantine-target"
        source_parent.mkdir(mode=0o700)
        target_parent.mkdir(mode=0o700)
        source = source_parent / "owned"
        target = target_parent / "0000"
        saved = source_parent / "saved-owned"
        foreign = source_parent / "foreign"
        source.write_bytes(b"operation-owned\n")
        foreign.write_bytes(b"foreign\n")
        os.chmod(source, 0o600)
        os.chmod(foreign, 0o600)
        identity = BOOTSTRAP._quarantine_identity(source)
        original_stat = BOOTSTRAP.os.stat
        swapped = False

        def swap_after_bound_stat(path, *values, **keywords):  # type: ignore[no-untyped-def]
            nonlocal swapped
            result = original_stat(path, *values, **keywords)
            if (
                not swapped
                and path == source.name
                and keywords.get("dir_fd") is not None
            ):
                swapped = True
                source.rename(saved)
                foreign.rename(source)
            return result

        with mock.patch.object(
            BOOTSTRAP.os, "stat", side_effect=swap_after_bound_stat
        ):
            with self.assertRaisesRegex(
                BOOTSTRAP.BootstrapError, "target binding changed"
            ):
                BOOTSTRAP._rename_noreplace_between(
                    source,
                    target,
                    expected_identity=identity,
                )

        self.assertTrue(swapped)
        self.assertFalse(target.exists())
        self.assertEqual(source.read_bytes(), b"foreign\n")
        self.assertEqual(saved.read_bytes(), b"operation-owned\n")

    def test_quarantine_failed_restore_retains_forensic_bindings(self) -> None:
        source_parent = self.root / "restore-failure-source"
        target_parent = self.root / "restore-failure-target"
        source_parent.mkdir(mode=0o700)
        target_parent.mkdir(mode=0o700)
        source = source_parent / "owned"
        target = target_parent / "0000"
        saved = source_parent / "saved-owned"
        foreign = source_parent / "foreign"
        source.write_bytes(b"operation-owned\n")
        foreign.write_bytes(b"foreign\n")
        os.chmod(source, 0o600)
        os.chmod(foreign, 0o600)
        identity = BOOTSTRAP._quarantine_identity(source)
        original_stat = BOOTSTRAP.os.stat
        swapped = False
        blocked_restore = False

        def race_restore(path, *values, **keywords):  # type: ignore[no-untyped-def]
            nonlocal swapped, blocked_restore
            result = original_stat(path, *values, **keywords)
            if (
                not swapped
                and path == source.name
                and keywords.get("dir_fd") is not None
            ):
                swapped = True
                source.rename(saved)
                foreign.rename(source)
            elif (
                swapped
                and not blocked_restore
                and path == target.name
                and keywords.get("dir_fd") is not None
            ):
                blocked_restore = True
                source.write_bytes(b"restore blocker\n")
                os.chmod(source, 0o600)
            return result

        with mock.patch.object(BOOTSTRAP.os, "stat", side_effect=race_restore):
            with self.assertRaisesRegex(
                BOOTSTRAP.BootstrapError, "target binding changed"
            ):
                BOOTSTRAP._rename_noreplace_between(
                    source,
                    target,
                    expected_identity=identity,
                )

        self.assertTrue(swapped)
        self.assertTrue(blocked_restore)
        self.assertEqual(saved.read_bytes(), b"operation-owned\n")
        self.assertEqual(source.read_bytes(), b"restore blocker\n")
        self.assertEqual(target.read_bytes(), b"foreign\n")

    def test_adoption_abort_preserves_foreign_authority_and_owned_controls(self) -> None:
        arguments, _transaction = self.crash_adoption_before_phase_journal(
            "baseline-ready"
        )
        control_snapshot = {
            path.relative_to(self.runtime).as_posix(): path.read_bytes()
            for root in (self.runtime / "bin", self.runtime / "control-releases")
            for path in root.rglob("*")
            if path.is_file()
        }
        bootstrap = self.runtime / "state/bootstrap-control.json"
        active = self.runtime / "state/active-control.json"
        BOOTSTRAP._atomic_json(bootstrap, {"operation_id": "adopt-fixture-0002"})
        BOOTSTRAP._atomic_json(active, {"operation_id": "adopt-fixture-0002"})
        result, _output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )
        self.assertEqual(result, 2)
        self.assertIn("foreign deployment authority", error)
        self.assertEqual(
            {
                path.relative_to(self.runtime).as_posix(): path.read_bytes()
                for root in (
                    self.runtime / "bin",
                    self.runtime / "control-releases",
                )
                for path in root.rglob("*")
                if path.is_file()
            },
            control_snapshot,
        )
        self.assertTrue(bootstrap.is_file())
        self.assertTrue(active.is_file())

    def test_adoption_side_effect_before_each_journal_advance_is_resumable(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        arguments = self.adoption_apply_arguments(plan, md_sha256, dft_sha256)
        transaction_path = BOOTSTRAP._adoption_transaction_path(
            self.runtime, operation_id=ADOPTION_OPERATION_ID
        )
        original_advance = BOOTSTRAP._advance_adoption_transaction
        previous_phase = "intent"

        # Every phase prepares its filesystem effects before advancing the
        # journal. Inject the crash in that exact gap, then let the next run
        # reconstruct ownership and resume idempotently from the prior phase.
        for phase in (
            "layout-ready",
            "controls-ready",
            "baseline-ready",
            "authority-commit-intent",
            "completed",
        ):
            crashed = False

            def crash_before_advance(*values, **keywords):  # type: ignore[no-untyped-def]
                nonlocal crashed
                if not crashed and keywords.get("phase") == phase:
                    crashed = True
                    raise BOOTSTRAP.BootstrapError(
                        f"injected pre-journal {phase} crash"
                    )
                return original_advance(*values, **keywords)

            with mock.patch.object(
                BOOTSTRAP,
                "_advance_adoption_transaction",
                side_effect=crash_before_advance,
            ):
                result, _output, error = self.run_main(*arguments)
            self.assertEqual(result, 2, (phase, error))
            self.assertIn(f"pre-journal {phase}", error)
            self.assertEqual(
                BOOTSTRAP._load_private_json(transaction_path)["phase"],
                previous_phase,
            )
            previous_phase = phase

        result, output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "adopted")

    def test_adoption_cas_drift_fails_before_authority(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        md_unit = self.root / "systemd/user/nexpoly-monomer-md-worker.service"
        md_unit.write_bytes(b"[Service]\nExecStart=/drifted\n")
        os.chmod(md_unit, 0o600)
        result, _output, error = self.run_main(
            *self.adoption_apply_arguments(plan, md_sha256, dft_sha256)
        )
        self.assertEqual(result, 2)
        self.assertIn("explicit confirmation", error)
        self.assertFalse((self.runtime / "state/bootstrap-control.json").exists())
        self.assertFalse((self.runtime / "state/active-control.json").exists())

    def test_adoption_rechecks_absent_authority_after_deploy_lock(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        original_preflight = BOOTSTRAP._adoption_preflight
        calls = 0

        def race_preflight(*values, **keywords):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            # manual main and apply both check before the lock. Simulate a
            # governed operation committing current state immediately after
            # those checks but before adoption obtains the shared lock.
            if calls == 3:
                BOOTSTRAP._atomic_json(
                    self.runtime / "state/current-deployment.json",
                    {"schema_version": 3},
                )
            return original_preflight(*values, **keywords)

        with mock.patch.object(
            BOOTSTRAP, "_adoption_preflight", side_effect=race_preflight
        ):
            result, _output, error = self.run_main(
                *self.adoption_apply_arguments(plan, md_sha256, dft_sha256)
            )
        self.assertEqual(result, 2)
        self.assertIn("current deployment state to be absent", error)
        self.assertFalse((self.runtime / "state/bootstrap-control.json").exists())
        self.assertFalse((self.runtime / "state/active-control.json").exists())
        self.assertFalse(
            BOOTSTRAP._adoption_transaction_path(
                self.runtime, operation_id=ADOPTION_OPERATION_ID
            ).exists()
        )
        # deploy.lock is a permanent part of the adopted control layout even
        # when the under-lock preflight refuses to begin a transaction.
        self.assertTrue((self.runtime / "state/deploy.lock").is_file())
        self.assertEqual(
            stat.S_IMODE((self.runtime / "state/deploy.lock").stat().st_mode),
            0o600,
        )

    def test_adoption_abort_cleans_precommit(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        apply_arguments = self.adoption_apply_arguments(
            plan, md_sha256, dft_sha256
        )
        original_advance = BOOTSTRAP._advance_adoption_transaction
        crashed = False

        def crash_baseline(*values, **keywords):  # type: ignore[no-untyped-def]
            nonlocal crashed
            transaction = original_advance(*values, **keywords)
            if not crashed and keywords.get("phase") == "baseline-ready":
                crashed = True
                raise BOOTSTRAP.BootstrapError("injected baseline crash")
            return transaction

        with mock.patch.object(
            BOOTSTRAP,
            "_advance_adoption_transaction",
            side_effect=crash_baseline,
        ):
            result, _output, error = self.run_main(*apply_arguments)
        self.assertEqual(result, 2)
        self.assertIn("baseline crash", error)
        abort_arguments = [
            value if value != "--adopt-apply" else "--adopt-abort"
            for value in apply_arguments
        ]
        result, output, error = self.run_main(*abort_arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "aborted")
        self.assertFalse((self.runtime / "state/current-deployment.json").exists())
        self.assertFalse((self.runtime / "state/adopted-deployment.json").exists())
        self.assertFalse((self.runtime / "state/bootstrap-control.json").exists())
        self.assertFalse((self.runtime / "state/active-control.json").exists())
        self.assertTrue((self.runtime / "state/deploy.lock").is_file())

    def test_adoption_abort_removes_prejournal_layout_mutations(self) -> None:
        arguments, transaction = self.crash_adoption_before_phase_journal(
            "layout-ready"
        )
        result, output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "aborted")
        self.assert_adoption_planned_paths_absent(transaction)

    def test_adoption_abort_removes_prejournal_control_mutations(self) -> None:
        arguments, transaction = self.crash_adoption_before_phase_journal(
            "controls-ready"
        )
        result, output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "aborted")
        self.assert_adoption_planned_paths_absent(transaction)

    def test_adoption_abort_removes_prejournal_baseline_mutations(self) -> None:
        arguments, transaction = self.crash_adoption_before_phase_journal(
            "baseline-ready"
        )
        result, output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "aborted")
        self.assert_adoption_planned_paths_absent(transaction)

    def test_adoption_abort_quarantine_recovers_two_faults(self) -> None:
        arguments, transaction = self.crash_adoption_before_phase_journal(
            "controls-ready"
        )
        abort_arguments = self.adoption_abort_arguments(arguments)
        original_rename = BOOTSTRAP._rename_noreplace_between
        first_fault = False

        def lose_rename_response(
            source: Path, target: Path, **keywords: object
        ) -> None:
            nonlocal first_fault
            original_rename(source, target, **keywords)
            if not first_fault:
                first_fault = True
                raise BOOTSTRAP.BootstrapError(
                    "injected quarantine rename response loss"
                )

        with mock.patch.object(
            BOOTSTRAP,
            "_rename_noreplace_between",
            side_effect=lose_rename_response,
        ):
            result, _output, error = self.run_main(*abort_arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("quarantine rename response loss", error)
        self.assertTrue(first_fault)

        transaction_path = BOOTSTRAP._adoption_transaction_path(
            self.runtime, operation_id=ADOPTION_OPERATION_ID
        )
        partial = BOOTSTRAP._load_private_json(transaction_path)
        quarantine_plan = partial["step_evidence"]["abort_quarantine"]
        quarantine_root = Path(str(quarantine_plan["root"]))
        self.assertTrue(quarantine_root.is_dir())
        self.assertTrue(any(quarantine_root.iterdir()))

        original_resume = BOOTSTRAP._resume_adoption_quarantine
        second_fault = False

        def crash_after_resume(*values, **keywords):  # type: ignore[no-untyped-def]
            nonlocal second_fault
            original_resume(*values, **keywords)
            if not second_fault:
                second_fault = True
                raise BOOTSTRAP.BootstrapError(
                    "injected second quarantine fault"
                )

        with mock.patch.object(
            BOOTSTRAP,
            "_resume_adoption_quarantine",
            side_effect=crash_after_resume,
        ):
            result, _output, error = self.run_main(*abort_arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("second quarantine fault", error)
        self.assertTrue(second_fault)
        self.assert_adoption_planned_paths_absent(transaction)

        result, output, error = self.run_main(*abort_arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "aborted")
        self.assertTrue(quarantine_root.is_dir())
        terminal = BOOTSTRAP._load_private_json(transaction_path)
        self.assertEqual(terminal["status"], "aborted")
        self.assertEqual(
            terminal["step_evidence"]["abort_quarantine"], quarantine_plan
        )

    def test_adoption_abort_refuses_tampered_prejournal_control(self) -> None:
        arguments, transaction = self.crash_adoption_before_phase_journal(
            "controls-ready"
        )
        planned = transaction["planned_paths"]
        self.assertIsInstance(planned, list)
        target = next(
            Path(str(ownership["path"]))
            for ownership in planned
            if isinstance(ownership, dict)
            and ownership.get("kind") == "file"
            and Path(str(ownership.get("path"))).parent
            == self.runtime / "bin"
            and Path(str(ownership.get("path"))).name
            in BOOTSTRAP.IMMUTABLE_FILES
        )
        target.write_bytes(b"tampered after the planned control write\n")
        os.chmod(target, 0o700)

        def snapshot() -> list[tuple[str, int, bytes]]:
            return [
                (
                    path.relative_to(self.runtime).as_posix(),
                    stat.S_IMODE(path.lstat().st_mode),
                    path.read_bytes() if path.is_file() else b"",
                )
                for path in sorted(self.runtime.rglob("*"))
            ]

        before = snapshot()
        result, _output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )
        self.assertEqual(result, 2)
        self.assertIn("changed before abort", error)
        self.assertTrue(target.is_file())
        self.assertEqual(snapshot(), before)

    def test_adoption_abort_removes_complete_atomic_install_temporary(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        arguments = self.adoption_apply_arguments(plan, md_sha256, dft_sha256)
        original_install = BOOTSTRAP._install_exact
        crashed = False

        def crash_with_complete_temporary(
            path: Path,
            payload: bytes,
            mode: int,
            **keywords: object,
        ) -> str:
            nonlocal crashed
            temporary = keywords.get("temporary_path")
            if not crashed and isinstance(temporary, Path):
                crashed = True
                BOOTSTRAP._atomic_file(temporary, payload, mode)
                raise BOOTSTRAP.BootstrapError(
                    "injected complete atomic install temporary crash"
                )
            return original_install(path, payload, mode, **keywords)

        with mock.patch.object(
            BOOTSTRAP,
            "_install_exact",
            side_effect=crash_with_complete_temporary,
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2)
        self.assertIn("atomic install temporary crash", error)
        transaction = BOOTSTRAP._load_private_json(
            BOOTSTRAP._adoption_transaction_path(
                self.runtime, operation_id=ADOPTION_OPERATION_ID
            )
        )
        temporary_paths = [
            Path(str(value["path"]))
            for value in transaction["planned_paths"]
            if isinstance(value, dict)
            and value.get("kind") == "install-staging"
            and str(value.get("path", "")).endswith(
                f".{ADOPTION_OPERATION_ID}.tmp"
            )
        ]
        self.assertTrue(any(path.is_file() for path in temporary_paths))
        result, output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "aborted")
        self.assert_adoption_planned_paths_absent(transaction)

    @staticmethod
    def quarantine_target_for_source(
        transaction: dict[str, object], source: Path
    ) -> Path:
        evidence = transaction["step_evidence"]
        assert isinstance(evidence, dict)
        quarantine = evidence["abort_quarantine"]
        assert isinstance(quarantine, dict)
        entries = quarantine["entries"]
        assert isinstance(entries, list)
        return next(
            Path(str(entry["target"]))
            for entry in entries
            if isinstance(entry, dict) and entry.get("source") == str(source)
        )

    def test_partial_install_staging_abort_retains_payload_in_quarantine(
        self,
    ) -> None:
        arguments, transaction, temporary, partial = (
            self.crash_adoption_with_partial_baseline_staging()
        )

        result, output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )

        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "aborted")
        terminal = BOOTSTRAP._load_private_json(
            BOOTSTRAP._adoption_transaction_path(
                self.runtime, operation_id=ADOPTION_OPERATION_ID
            )
        )
        quarantined = self.quarantine_target_for_source(terminal, temporary)
        self.assertEqual(quarantined.read_bytes(), partial)
        self.assertFalse(temporary.exists())
        self.assert_adoption_planned_paths_absent(transaction)

    def test_partial_install_staging_abort_recovers_move_response_loss(
        self,
    ) -> None:
        arguments, transaction, temporary, partial = (
            self.crash_adoption_with_partial_baseline_staging()
        )
        abort_arguments = self.adoption_abort_arguments(arguments)
        original = BOOTSTRAP._rename_noreplace_between
        lost = False

        def lose_response(
            source: Path, target: Path, **keywords: object
        ) -> None:
            nonlocal lost
            original(source, target, **keywords)
            if source == temporary and not lost:
                lost = True
                raise BOOTSTRAP.BootstrapError(
                    "injected partial staging quarantine response loss"
                )

        with mock.patch.object(
            BOOTSTRAP,
            "_rename_noreplace_between",
            side_effect=lose_response,
        ):
            result, _output, error = self.run_main(*abort_arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("quarantine response loss", error)
        self.assertTrue(lost)

        result, output, error = self.run_main(*abort_arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "aborted")
        terminal = BOOTSTRAP._load_private_json(
            BOOTSTRAP._adoption_transaction_path(
                self.runtime, operation_id=ADOPTION_OPERATION_ID
            )
        )
        quarantined = self.quarantine_target_for_source(terminal, temporary)
        self.assertEqual(quarantined.read_bytes(), partial)
        self.assert_adoption_planned_paths_absent(transaction)

    def test_single_install_staging_nonprefix_is_quarantined_not_adopted(
        self,
    ) -> None:
        arguments, _transaction, temporary, partial = (
            self.crash_adoption_with_partial_baseline_staging()
        )
        foreign = b"X" * len(partial)
        temporary.write_bytes(foreign)

        result, output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )

        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "aborted")
        terminal = BOOTSTRAP._load_private_json(
            BOOTSTRAP._adoption_transaction_path(
                self.runtime, operation_id=ADOPTION_OPERATION_ID
            )
        )
        quarantined = self.quarantine_target_for_source(terminal, temporary)
        self.assertEqual(quarantined.read_bytes(), foreign)
        self.assertFalse(temporary.exists())

    def test_single_install_staging_abort_rejects_symlink(self) -> None:
        arguments, _transaction, temporary, _partial = (
            self.crash_adoption_with_partial_baseline_staging()
        )
        outside = self.root / "foreign-staging-target"
        outside.write_bytes(b"foreign\n")
        os.chmod(outside, 0o600)
        temporary.unlink()
        temporary.symlink_to(outside)

        result, _output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )

        self.assertEqual(result, 2)
        self.assertIn("quarantine source is unsafe", error)
        self.assertEqual(outside.read_bytes(), b"foreign\n")
        self.assertTrue(temporary.is_symlink())

    def test_single_install_staging_abort_rejects_mode_drift(self) -> None:
        arguments, _transaction, temporary, partial = (
            self.crash_adoption_with_partial_baseline_staging()
        )
        os.chmod(temporary, 0o644)

        result, _output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )

        self.assertEqual(result, 2)
        self.assertIn("adoption staging changed before abort", error)
        self.assertEqual(temporary.read_bytes(), partial)
        self.assertEqual(stat.S_IMODE(temporary.stat().st_mode), 0o644)

    def test_single_install_staging_abort_rejects_an_extra_link(self) -> None:
        arguments, _transaction, temporary, partial = (
            self.crash_adoption_with_partial_baseline_staging()
        )
        alias = self.root / "foreign-staging-link"
        os.link(temporary, alias)

        result, _output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )

        self.assertEqual(result, 2)
        self.assertIn("quarantine source is unsafe", error)
        self.assertEqual(temporary.read_bytes(), partial)
        self.assertEqual(alias.read_bytes(), partial)
        self.assertEqual(temporary.stat().st_nlink, 2)

    def test_single_install_staging_abort_rejects_oversize_payload(self) -> None:
        arguments, _transaction, temporary, _partial = (
            self.crash_adoption_with_partial_baseline_staging()
        )
        oversized = 64 * 1024 * 1024 + 1
        with temporary.open("r+b") as stream:
            stream.truncate(oversized)

        result, _output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )

        self.assertEqual(result, 2)
        self.assertIn("durability barrier file is too large", error)
        self.assertEqual(temporary.stat().st_size, oversized)

    def test_linked_install_publication_resumes_forward(self) -> None:
        arguments, _transaction, destination, temporary = (
            self.crash_adoption_after_baseline_link()
        )

        result, output, error = self.run_main(*arguments)

        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "adopted")
        self.assertTrue(destination.is_file())
        self.assertEqual(destination.stat().st_nlink, 1)
        self.assertFalse(temporary.exists())

    def test_linked_install_abort_recovers_two_more_power_losses(self) -> None:
        arguments, transaction, destination, temporary = (
            self.crash_adoption_after_baseline_link()
        )
        abort_arguments = self.adoption_abort_arguments(arguments)
        original_residue = BOOTSTRAP._resume_linked_install_residue
        collapsed = False

        def lose_collapse_response(raw: object) -> None:
            nonlocal collapsed
            original_residue(raw)
            if not collapsed:
                collapsed = True
                raise BOOTSTRAP.BootstrapError(
                    "injected linked-collapse response loss"
                )

        with mock.patch.object(
            BOOTSTRAP,
            "_resume_linked_install_residue",
            side_effect=lose_collapse_response,
        ):
            result, _output, error = self.run_main(*abort_arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("linked-collapse response loss", error)
        self.assertTrue(collapsed)
        self.assertTrue(destination.is_file())
        self.assertEqual(destination.stat().st_nlink, 1)
        self.assertFalse(temporary.exists())

        original_rename = BOOTSTRAP._rename_noreplace_between
        moved = False

        def lose_move_response(
            source: Path, target: Path, **keywords: object
        ) -> None:
            nonlocal moved
            original_rename(source, target, **keywords)
            if not moved:
                moved = True
                raise BOOTSTRAP.BootstrapError(
                    "injected linked-quarantine response loss"
                )

        with mock.patch.object(
            BOOTSTRAP,
            "_rename_noreplace_between",
            side_effect=lose_move_response,
        ):
            result, _output, error = self.run_main(*abort_arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("linked-quarantine response loss", error)
        self.assertTrue(moved)

        result, output, error = self.run_main(*abort_arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "aborted")
        self.assert_adoption_planned_paths_absent(transaction)

    def test_linked_install_abort_rejects_a_third_link(self) -> None:
        arguments, _transaction, destination, temporary = (
            self.crash_adoption_after_baseline_link()
        )
        third = self.root / "third-adoption-link"
        os.link(destination, third)
        before = destination.read_bytes()

        result, _output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )

        self.assertEqual(result, 2)
        self.assertIn("linked install inode differs", error)
        self.assertEqual(destination.read_bytes(), before)
        self.assertTrue(temporary.is_file())
        self.assertTrue(third.is_file())
        self.assertEqual(destination.stat().st_nlink, 3)

    def test_linked_install_abort_rejects_a_different_temporary_inode(self) -> None:
        arguments, _transaction, destination, temporary = (
            self.crash_adoption_after_baseline_link()
        )
        payload = temporary.read_bytes()
        temporary.unlink()
        temporary.write_bytes(payload)
        os.chmod(temporary, 0o600)

        result, _output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )

        self.assertEqual(result, 2)
        self.assertIn("linked install inode differs", error)
        self.assertTrue(destination.is_file())
        self.assertTrue(temporary.is_file())
        self.assertNotEqual(destination.stat().st_ino, temporary.stat().st_ino)

    def test_linked_install_abort_rejects_payload_drift(self) -> None:
        arguments, _transaction, destination, temporary = (
            self.crash_adoption_after_baseline_link()
        )
        destination.write_bytes(b"drifted through both hard-link names\n")

        result, _output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )

        self.assertEqual(result, 2)
        self.assertIn("payload changed", error)
        self.assertEqual(destination.read_bytes(), temporary.read_bytes())
        self.assertEqual(destination.stat().st_nlink, 2)

    def test_adoption_abort_removes_owned_control_staging_tree(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        arguments = self.adoption_apply_arguments(plan, md_sha256, dft_sha256)
        crashed = False

        def crash_with_owned_staging(*values, **keywords):  # type: ignore[no-untyped-def]
            nonlocal crashed
            control_plan = keywords.get("plan")
            if crashed or not isinstance(control_plan, dict):
                raise AssertionError("unexpected repeated control release build")
            crashed = True
            staging = control_plan["staging_path"]
            owner = control_plan["staging_owner"]
            payloads = control_plan["payloads"]
            expected_files = control_plan["expected_files"]
            self.assertIsInstance(staging, Path)
            self.assertIsInstance(owner, dict)
            self.assertIsInstance(payloads, dict)
            self.assertIsInstance(expected_files, dict)
            staging.mkdir(mode=0o700)
            BOOTSTRAP._atomic_json(staging / ".owner.json", owner)
            name, payload = next(iter(payloads.items()))
            record = expected_files[name]
            BOOTSTRAP._atomic_file(
                staging / name,
                payload,
                int(str(record["mode"]), 8),
            )
            raise BOOTSTRAP.BootstrapError(
                "injected owned control staging crash"
            )

        with mock.patch.object(
            BOOTSTRAP,
            "_build_control_release",
            side_effect=crash_with_owned_staging,
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2)
        self.assertIn("owned control staging crash", error)
        transaction = BOOTSTRAP._load_private_json(
            BOOTSTRAP._adoption_transaction_path(
                self.runtime, operation_id=ADOPTION_OPERATION_ID
            )
        )
        staging_paths = [
            Path(str(value["path"]))
            for value in transaction["planned_paths"]
            if isinstance(value, dict)
            and value.get("kind") == "staging-tree"
        ]
        self.assertEqual(len(staging_paths), 1)
        self.assertTrue(staging_paths[0].is_dir())
        result, output, error = self.run_main(
            *self.adoption_abort_arguments(arguments)
        )
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "aborted")
        self.assert_adoption_planned_paths_absent(transaction)

    def test_control_staging_cleanup_recovers_two_faults(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        arguments = self.adoption_apply_arguments(
            json.loads(output), md_sha256, dft_sha256
        )
        staging_holder: dict[str, Path] = {}

        def leave_owned_staging(*values, **keywords):  # type: ignore[no-untyped-def]
            control_plan = keywords["plan"]
            staging = control_plan["staging_path"]
            owner = control_plan["staging_owner"]
            payloads = control_plan["payloads"]
            self.assertIsInstance(staging, Path)
            staging.mkdir(mode=0o700)
            BOOTSTRAP._atomic_json(staging / ".owner.json", owner)
            for name, payload in list(payloads.items())[:2]:
                BOOTSTRAP._atomic_file(staging / name, payload, 0o700)
            staging_holder["path"] = staging
            raise BOOTSTRAP.BootstrapError("injected owned staging residue")

        with mock.patch.object(
            BOOTSTRAP,
            "_build_control_release",
            side_effect=leave_owned_staging,
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("owned staging residue", error)
        staging = staging_holder["path"]
        self.assertGreaterEqual(len(list(staging.iterdir())), 3)

        original_fsync = BOOTSTRAP._fsync_directory
        for fault_number in (1, 2):
            faulted = False

            def fail_cleanup(path: Path) -> None:
                nonlocal faulted
                original_fsync(path)
                if path == staging and not faulted:
                    faulted = True
                    raise BOOTSTRAP.BootstrapError(
                        f"injected staging cleanup fault {fault_number}"
                    )

            with mock.patch.object(
                BOOTSTRAP, "_fsync_directory", side_effect=fail_cleanup
            ):
                result, _output, error = self.run_main(*arguments)
            self.assertEqual(result, 2, error)
            self.assertIn(f"cleanup fault {fault_number}", error)
            self.assertTrue(faulted)

        result, output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "adopted")
        self.assertFalse(staging.exists())

    def test_control_staging_recovers_partial_owner_then_file_fsync_loss(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        arguments = self.adoption_apply_arguments(
            json.loads(output), md_sha256, dft_sha256
        )
        original_write = BOOTSTRAP._write_authorized_staging
        partial_owner: Path | None = None

        def crash_partial_owner(path: Path, payload: bytes, mode: int) -> None:
            nonlocal partial_owner
            if partial_owner is None and path.name == ".owner.json":
                partial_owner = path
                path.write_bytes(payload[:7])
                os.chmod(path, mode)
                raise BOOTSTRAP.BootstrapError(
                    "injected partial staging owner"
                )
            original_write(path, payload, mode)

        with mock.patch.object(
            BOOTSTRAP,
            "_write_authorized_staging",
            side_effect=crash_partial_owner,
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("partial staging owner", error)
        self.assertIsNotNone(partial_owner)
        assert partial_owner is not None
        self.assertEqual(partial_owner.stat().st_size, 7)

        original_fsync = BOOTSTRAP.os.fsync
        lost_file_fsync = False

        def lose_payload_fsync(descriptor: int) -> None:
            nonlocal lost_file_fsync
            original_fsync(descriptor)
            link = Path(f"/proc/self/fd/{descriptor}")
            if not link.exists() or lost_file_fsync:
                return
            opened = Path(os.readlink(link))
            if opened.parent == partial_owner.parent and opened.name != ".owner.json":
                lost_file_fsync = True
                raise OSError("injected staging file-fsync response loss")

        with mock.patch.object(
            BOOTSTRAP.os, "fsync", side_effect=lose_payload_fsync
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("file-fsync response loss", error)
        self.assertTrue(lost_file_fsync)

        result, output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "adopted")
        self.assertFalse(partial_owner.parent.exists())

    def test_random_control_staging_temp_is_retained_and_fails_closed(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        arguments = self.adoption_apply_arguments(
            json.loads(output), md_sha256, dft_sha256
        )
        residue: dict[str, Path] = {}

        def leave_random_temp(*values, **keywords):  # type: ignore[no-untyped-def]
            control_plan = keywords["plan"]
            staging = control_plan["staging_path"]
            owner = control_plan["staging_owner"]
            self.assertIsInstance(staging, Path)
            staging.mkdir(mode=0o700)
            BOOTSTRAP._atomic_json(staging / ".owner.json", owner)
            temporary = staging / ".pull_deploy_controller.py.deadbeef.tmp"
            temporary.write_bytes(b"same uid preplant\n")
            os.chmod(temporary, 0o700)
            residue["path"] = temporary
            raise BOOTSTRAP.BootstrapError("injected random staging temp")

        with mock.patch.object(
            BOOTSTRAP,
            "_build_control_release",
            side_effect=leave_random_temp,
        ):
            result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        temporary = residue["path"]
        before = (temporary.stat().st_ino, temporary.read_bytes())

        result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2, error)
        self.assertIn("unplanned entry", error)
        self.assertEqual((temporary.stat().st_ino, temporary.read_bytes()), before)

    def test_adoption_abort_rejects_post_authority_intent(self) -> None:
        md_sha256, dft_sha256 = self.prepare_adoption_fixture()
        result, output, error = self.run_main(
            *self.adoption_base_arguments(), "--adopt-plan"
        )
        self.assertEqual(result, 0, error)
        plan = json.loads(output)
        apply_arguments = self.adoption_apply_arguments(
            plan, md_sha256, dft_sha256
        )
        original_advance = BOOTSTRAP._advance_adoption_transaction
        crashed = False

        def crash_authority(*values, **keywords):  # type: ignore[no-untyped-def]
            nonlocal crashed
            transaction = original_advance(*values, **keywords)
            if not crashed and keywords.get("phase") == "authority-commit-intent":
                crashed = True
                raise BOOTSTRAP.BootstrapError("injected authority intent crash")
            return transaction

        with mock.patch.object(
            BOOTSTRAP,
            "_advance_adoption_transaction",
            side_effect=crash_authority,
        ):
            result, _output, error = self.run_main(*apply_arguments)
        self.assertEqual(result, 2)
        abort_arguments = [
            value if value != "--adopt-apply" else "--adopt-abort"
            for value in apply_arguments
        ]
        result, _output, error = self.run_main(*abort_arguments)
        self.assertEqual(result, 2)
        self.assertIn("forbidden after authority commit intent", error)

    def test_requested_sha_and_source_tree_confirmation_are_fail_closed(self) -> None:
        result, _output, error = self.run_main(
            *self.apply_arguments()[:-1], "0" * 40
        )
        self.assertEqual(result, 2)
        self.assertIn("matching confirmations", error)
        arguments = self.apply_arguments()
        arguments[1] = "f" * 40
        result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 2)
        self.assertIn("requested bootstrap SHA", error)

    def test_stable_wrappers_ignore_hostile_shell_startup_environment(self) -> None:
        hostile = self.root / "hostile"
        hostile.mkdir(mode=0o700)
        marker = hostile / "bash-env-executed"
        bash_env = hostile / "bash-env"
        bash_env.write_text(f"touch {marker}\n", encoding="utf-8")
        fake_bash = hostile / "bash"
        fake_bash.write_text(f"#!/bin/sh\ntouch {marker}\nexit 99\n", encoding="utf-8")
        os.chmod(fake_bash, 0o700)
        for name in (
            "nexpoly-pull-deploy",
            "nexpoly-production-readiness",
            "nexpoly-pull-contract-0012",
            "nexpoly-reconcile-production-0005-polytao-alias",
        ):
            wrapper = REPOSITORY_ROOT / "scripts" / name
            self.assertEqual(
                wrapper.read_text(encoding="utf-8").splitlines()[0],
                "#!/usr/bin/python3 -I",
            )
            completed = subprocess.run(
                [str(wrapper), "plan"],
                env={
                    "PATH": str(hostile),
                    "BASH_ENV": str(bash_env),
                    "ENV": str(bash_env),
                    "PYTHONPATH": str(hostile),
                    "PYTHONSTARTUP": str(bash_env),
                    "PYTHONUSERBASE": str(hostile),
                },
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(completed.returncode, 99)
            self.assertFalse(marker.exists())
            direct = subprocess.run(
                ["/usr/bin/python3", str(wrapper), "plan"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(direct.returncode, 0)
            self.assertIn("requires isolated Python startup", direct.stderr)

    def test_apply_installs_private_stable_controls_and_no_runtime_state(self) -> None:
        result, output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 0, error)
        document = json.loads(output)
        self.assertEqual(document["status"], "initialized")
        for relative, mode in BOOTSTRAP.DIRECTORIES.items():
            path = self.runtime / relative
            self.assertTrue(path.is_dir(), relative)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
        self.assertEqual(
            {entry.name for entry in (self.runtime / "bin").iterdir()},
            set(BOOTSTRAP.IMMUTABLE_FILES),
        )
        for name in BOOTSTRAP.IMMUTABLE_FILES:
            path = self.runtime / "bin" / name
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        lock = self.runtime / "state" / "deploy.lock"
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        self.assertFalse((self.runtime / "state" / "current-deployment.json").exists())
        self.assertFalse((self.runtime / "state" / "monomer-md-active-slot.json").exists())
        self.assertFalse((self.runtime / "state" / "deploy-in-progress.json").exists())
        active = json.loads(
            (self.runtime / "state/active-control.json").read_text(encoding="utf-8")
        )
        release = self.runtime / "control-releases" / active["release_id"]
        self.assertTrue(release.is_dir())
        self.assertTrue((release / "CONTROL-MANIFEST.json").is_file())
        self.assertTrue((release / "pull_deploy_controller.py").is_file())
        self.assertTrue(
            (release / "reconcile_production_0005_polytao_alias.py").is_file()
        )
        self.assertEqual(stat.S_IMODE(self.production.stat().st_mode) & 0o022, 0)
        self.assertEqual(stat.S_IMODE((self.production / ".git").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.production / ".git/config").stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE((self.production / "tracked/fixture.txt").stat().st_mode) & 0o022,
            0,
        )
        self.assertFalse((self.runtime / "worker-venvs/md-a").exists())
        bootstrap_record = self.runtime / "state/bootstrap-control.json"
        self.assertEqual(stat.S_IMODE(bootstrap_record.stat().st_mode), 0o600)
        self.assertEqual(
            json.loads(bootstrap_record.read_text(encoding="utf-8"))["source_sha"],
            SOURCE_SHA,
        )
        readiness = json.loads(
            bootstrap_record.read_text(encoding="utf-8")
        )["source_readiness"]
        self.assertTrue(readiness["ready"])
        self.assertTrue(readiness["owner_private"])
        self.assertEqual(readiness["source_tree"], SOURCE_TREE)
        authority = json.loads(
            bootstrap_record.read_text(encoding="utf-8")
        )
        self.assertEqual(authority["schema_version"], 2)
        self.assertEqual(
            authority["legacy_takeover"],
            takeover_binding(),
        )

    def test_takeover_binding_requires_exact_f_and_legacy_git_identity(
        self,
    ) -> None:
        observed: dict[str, object] = {}

        def validate_completed(
            runtime_root: Path,
            operation_id: str,
            authority_sha: str,
            authority_tree: str,
            *,
            expected_git_identity: dict[str, str],
        ) -> dict[str, object]:
            observed.update(
                {
                    "runtime_root": runtime_root,
                    "operation_id": operation_id,
                    "authority_sha": authority_sha,
                    "authority_tree": authority_tree,
                    "git_identity": expected_git_identity,
                }
            )
            return takeover_binding(operation_id)

        repository = BOOTSTRAP._production_repository_identity(
            self.production,
            SOURCE_SHA,
            allow_test=True,
        )
        with mock.patch.object(
            BOOTSTRAP,
            "_legacy_takeover_evidence",
            return_value=SimpleNamespace(
                validate_completed=validate_completed
            ),
        ):
            binding = BOOTSTRAP._completed_legacy_takeover(
                self.runtime,
                TAKEOVER_OPERATION_ID,
                source_sha=SOURCE_SHA,
                source_tree=SOURCE_TREE,
                production_repository=repository,
                allow_test=True,
            )
        self.assertEqual(binding, takeover_binding())
        self.assertEqual(
            observed,
            {
                "runtime_root": self.runtime.absolute(),
                "operation_id": TAKEOVER_OPERATION_ID,
                "authority_sha": SOURCE_SHA,
                "authority_tree": SOURCE_TREE,
                "git_identity": {
                    "branch": "refs/heads/main",
                    "head_sha": "0" * 40,
                    "head_tree": "0" * 40,
                    "local_main_sha": "0" * 40,
                },
            },
        )

    def test_shared_deploy_lock_blocks_before_every_runtime_write(self) -> None:
        state = self.runtime / "state"
        state.mkdir(parents=True, mode=0o700)
        os.chmod(self.runtime, 0o700)
        os.chmod(state, 0o700)
        lock = state / "deploy.lock"
        lock.write_bytes(b"pre-takeover-lock\n")
        os.chmod(lock, 0o600)

        def snapshot() -> list[tuple[str, int, bytes]]:
            records: list[tuple[str, int, bytes]] = []
            for path in sorted(self.runtime.rglob("*")):
                relative = path.relative_to(self.runtime).as_posix()
                mode = stat.S_IMODE(path.lstat().st_mode)
                records.append(
                    (
                        relative,
                        mode,
                        path.read_bytes() if path.is_file() else b"",
                    )
                )
            return records

        before = snapshot()
        with lock.open("r+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("another deployment holds deploy.lock", error)
        self.assertEqual(snapshot(), before)

    def test_apply_takes_over_exact_legacy_worker_unit_with_private_backup(self) -> None:
        unit = (
            self.root
            / "systemd/user/nexpoly-monomer-md-worker.service"
        )
        unit.parent.mkdir(parents=True, mode=0o700)
        unit.write_bytes(b"[Service]\nExecStart=/legacy\n")
        os.chmod(unit, 0o664)
        checksum = BOOTSTRAP.digest(unit.read_bytes())
        arguments = [
            *self.apply_arguments(),
            "--confirm-worker-unit-sha256",
            checksum,
        ]
        result, output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)
        self.assertEqual(stat.S_IMODE(unit.stat().st_mode), 0o600)
        takeover = json.loads(output)["worker_unit_takeover"]
        self.assertEqual(takeover["status"], "completed")
        backup = Path(takeover["backup_path"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), unit.read_bytes())
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertTrue(backup.is_relative_to(self.runtime / "backups"))
        result, _output, error = self.run_main(*arguments)
        self.assertEqual(result, 0, error)

    def test_worker_unit_confirmation_and_mode_fail_before_runtime_write(self) -> None:
        unit = self.root / "systemd/user/nexpoly-monomer-md-worker.service"
        unit.parent.mkdir(parents=True, mode=0o700)
        unit.write_bytes(b"legacy unit\n")
        os.chmod(unit, 0o664)
        result, _output, error = self.run_main(
            *self.apply_arguments(),
            "--confirm-worker-unit-sha256",
            "sha256:" + "f" * 64,
        )
        self.assertEqual(result, 2)
        self.assertIn("explicit confirmation", error)
        self.assertFalse((self.runtime / "bin").exists())
        self.assertFalse((self.runtime / "state/bootstrap-control.json").exists())

        os.chmod(unit, 0o644)
        result, _output, error = self.run_main(
            *self.apply_arguments(),
            "--confirm-worker-unit-sha256",
            BOOTSTRAP.digest(unit.read_bytes()),
        )
        self.assertEqual(result, 2)
        self.assertIn("mode is not an allowed", error)
        self.assertFalse((self.runtime / "bin").exists())
        self.assertFalse((self.runtime / "state/bootstrap-control.json").exists())

    def test_worker_unit_takeover_crash_after_atomic_replace_is_resumable(self) -> None:
        unit = self.root / "systemd/user/nexpoly-monomer-md-worker.service"
        unit.parent.mkdir(parents=True, mode=0o700)
        unit.write_bytes(b"legacy unit\n")
        os.chmod(unit, 0o664)
        arguments = [
            *self.apply_arguments(),
            "--confirm-worker-unit-sha256",
            BOOTSTRAP.digest(unit.read_bytes()),
        ]
        reload_pending = False
        reload_calls = 0

        def unit_state(path: Path, *, allow_test: bool) -> dict[str, str]:
            del allow_test
            return {
                "LoadState": "loaded",
                "FragmentPath": str(path),
                "DropInPaths": "",
                "NeedDaemonReload": "yes" if reload_pending else "no",
                "UnitFileState": "enabled",
            }

        def reload_unit(*, allow_test: bool) -> None:
            nonlocal reload_pending, reload_calls
            del allow_test
            reload_calls += 1
            if reload_calls == 1:
                reload_pending = True
                raise BOOTSTRAP.BootstrapError("injected reload crash")
            reload_pending = False

        with (
            mock.patch.object(
                BOOTSTRAP,
                "_worker_unit_state",
                side_effect=unit_state,
            ),
            mock.patch.object(
                BOOTSTRAP,
                "_daemon_reload_worker_unit",
                side_effect=reload_unit,
            ),
        ):
            result, _output, error = self.run_main(*arguments)
            self.assertEqual(result, 2)
            self.assertIn("injected reload crash", error)
            self.assertEqual(stat.S_IMODE(unit.stat().st_mode), 0o600)
            self.assertTrue(
                (
                    self.runtime
                    / "audit/bootstrap-worker-unit/takeover-intent.json"
                ).is_file()
            )
            self.assertFalse(
                (
                    self.runtime
                    / "audit/bootstrap-worker-unit/takeover.json"
                ).exists()
            )
            self.assertTrue(reload_pending)
            result, _output, error = self.run_main(*arguments)
            self.assertEqual(result, 0, error)
            self.assertFalse(reload_pending)

    def test_worker_unit_pre_replace_crash_never_claims_legacy_inode_is_private(
        self,
    ) -> None:
        unit = self.root / "systemd/user/nexpoly-monomer-md-worker.service"
        unit.parent.mkdir(parents=True, mode=0o700)
        unit.write_bytes(b"legacy unit\n")
        os.chmod(unit, 0o664)
        checksum = BOOTSTRAP.digest(unit.read_bytes())
        writer = os.open(unit, os.O_RDWR)
        original_replace = BOOTSTRAP.os.replace

        def fail_unit_replace(source, destination):  # type: ignore[no-untyped-def]
            if Path(destination) == unit:
                raise OSError("injected unit publication crash")
            return original_replace(source, destination)

        try:
            with mock.patch.object(
                BOOTSTRAP.os, "replace", side_effect=fail_unit_replace
            ):
                result, _output, error = self.run_main(
                    *self.apply_arguments(),
                    "--confirm-worker-unit-sha256",
                    checksum,
                )
            self.assertEqual(result, 2)
            self.assertIn("unit publication crash", error)
            self.assertEqual(stat.S_IMODE(unit.stat().st_mode), 0o664)
            self.assertFalse(
                (self.runtime / "audit/bootstrap-worker-unit/takeover.json").exists()
            )
            os.lseek(writer, 0, os.SEEK_SET)
            os.write(writer, b"PWNED\n")
            os.ftruncate(writer, len(b"PWNED\n"))
            os.fsync(writer)
        finally:
            os.close(writer)
        self.assertEqual(unit.read_bytes(), b"PWNED\n")
        result, _output, error = self.run_main(
            *self.apply_arguments(),
            "--confirm-worker-unit-sha256",
            checksum,
        )
        self.assertEqual(result, 2)
        self.assertIn("explicit confirmation", error)

    def test_worker_unit_drift_after_atomic_takeover_fails_closed(self) -> None:
        unit = self.root / "systemd/user/nexpoly-monomer-md-worker.service"
        unit.parent.mkdir(parents=True, mode=0o700)
        unit.write_bytes(b"legacy unit\n")
        os.chmod(unit, 0o664)
        checksum = BOOTSTRAP.digest(unit.read_bytes())

        def mutate_after_reload(*, allow_test: bool) -> None:
            self.assertTrue(allow_test)
            unit.write_bytes(b"mutated after reload\n")
            os.chmod(unit, 0o600)

        with mock.patch.object(
            BOOTSTRAP,
            "_daemon_reload_worker_unit",
            side_effect=mutate_after_reload,
        ):
            result, _output, error = self.run_main(
                *self.apply_arguments(),
                "--confirm-worker-unit-sha256",
                checksum,
            )
        self.assertEqual(result, 2)
        self.assertIn("permission takeover did not verify", error)
        self.assertFalse(
            (self.runtime / "audit/bootstrap-worker-unit/takeover.json").exists()
        )

    def test_apply_is_idempotent_only_for_byte_identical_installed_controller(self) -> None:
        first, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(first, 0, error)
        second, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(second, 0, error)

        installed = self.runtime / "bin" / "control_runtime_selector.py"
        installed.write_text("tampered\n", encoding="utf-8")
        os.chmod(installed, 0o700)
        third, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(third, 2)
        self.assertIn("refusing to overwrite", error)

    def test_immutable_install_crash_never_publishes_a_partial_final_file(self) -> None:
        directory = self.root / "atomic-install"
        directory.mkdir(mode=0o700)
        target = directory / "router"
        with mock.patch.object(
            BOOTSTRAP.os,
            "link",
            side_effect=OSError("injected no-replace publication crash"),
        ):
            with self.assertRaisesRegex(OSError, "publication crash"):
                BOOTSTRAP._install_exact(target, b"reviewed payload\n", 0o700)
        self.assertFalse(target.exists())
        self.assertEqual(list(directory.iterdir()), [])
        self.assertEqual(
            BOOTSTRAP._install_exact(target, b"reviewed payload\n", 0o700),
            BOOTSTRAP.digest(b"reviewed payload\n"),
        )
        self.assertEqual(target.read_bytes(), b"reviewed payload\n")

    def test_unowned_same_uid_install_staging_is_retained(self) -> None:
        directory = self.root / "unowned-install"
        directory.mkdir(mode=0o700)
        target = directory / "router"
        preplant = directory / ".router.same-uid-preplant.tmp"
        preplant.write_bytes(b"attacker-controlled\n")
        os.chmod(preplant, 0o700)
        before = (preplant.stat().st_ino, preplant.read_bytes())

        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "unowned staging"
        ):
            BOOTSTRAP._install_exact(target, b"reviewed\n", 0o700)

        self.assertFalse(target.exists())
        self.assertEqual((preplant.stat().st_ino, preplant.read_bytes()), before)

    def test_authorized_partial_install_staging_recovers_forward(self) -> None:
        directory = self.root / "authorized-install"
        directory.mkdir(mode=0o700)
        target = directory / "router"
        operation_id = "adopt-partial-install-test"
        temporary = directory / f".router.{operation_id}.tmp"
        payload = b"reviewed complete payload\n"
        authority = BOOTSTRAP._adoption_install_staging_plan(
            temporary,
            target,
            payload,
            0o700,
            operation_id=operation_id,
        )
        temporary.write_bytes(payload[:7])
        os.chmod(temporary, 0o700)

        result = BOOTSTRAP._install_exact(
            target,
            payload,
            0o700,
            temporary_path=temporary,
            temporary_authority=authority,
            reject_unowned_staging=True,
        )

        self.assertEqual(result, BOOTSTRAP.digest(payload))
        self.assertEqual(target.read_bytes(), payload)
        self.assertFalse(temporary.exists())

    def test_authorized_install_recovers_after_file_fsync_response_loss(self) -> None:
        directory = self.root / "fsync-lost-install"
        directory.mkdir(mode=0o700)
        target = directory / "router"
        operation_id = "adopt-fsync-install-test"
        temporary = directory / f".router.{operation_id}.tmp"
        payload = b"durable staged payload\n"
        authority = BOOTSTRAP._adoption_install_staging_plan(
            temporary,
            target,
            payload,
            0o700,
            operation_id=operation_id,
        )
        original_fsync = BOOTSTRAP.os.fsync
        lost = False

        def lose_response(descriptor: int) -> None:
            nonlocal lost
            original_fsync(descriptor)
            linked = Path(f"/proc/self/fd/{descriptor}")
            if not lost and linked.exists() and Path(os.readlink(linked)) == temporary:
                lost = True
                raise OSError("injected file-fsync response loss")

        with mock.patch.object(BOOTSTRAP.os, "fsync", side_effect=lose_response):
            with self.assertRaisesRegex(OSError, "file-fsync response loss"):
                BOOTSTRAP._install_exact(
                    target,
                    payload,
                    0o700,
                    temporary_path=temporary,
                    temporary_authority=authority,
                )
        self.assertTrue(lost)
        self.assertEqual(temporary.read_bytes(), payload)
        self.assertFalse(target.exists())

        BOOTSTRAP._install_exact(
            target,
            payload,
            0o700,
            temporary_path=temporary,
            temporary_authority=authority,
        )
        self.assertEqual(target.read_bytes(), payload)
        self.assertFalse(temporary.exists())

    def test_apply_rejects_symlink_inside_production_git(self) -> None:
        target = self.root / "outside"
        target.write_text("outside\n", encoding="utf-8")
        link = self.production / ".git" / "unsafe"
        link.symlink_to(target)
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("unsafe production Git entry", error)

    def test_git_probe_disables_hostile_fsmonitor_and_pins_worktree(self) -> None:
        marker = self.root / "fsmonitor-executed"
        monitor = self.root / "hostile-fsmonitor"
        monitor.write_text(
            f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8"
        )
        os.chmod(monitor, 0o700)
        outside = self.root / "outside-worktree"
        outside.mkdir(mode=0o700)
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.production / ".git"),
                "config",
                "core.fsmonitor",
                str(monitor),
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.production / ".git"),
                "config",
                "core.worktree",
                str(outside),
            ],
            check=True,
        )
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 0, error)
        self.assertFalse(marker.exists())
        self.assertEqual(
            stat.S_IMODE((self.production / "tracked/fixture.txt").stat().st_mode)
            & 0o022,
            0,
        )

    def test_git_probe_rejects_executable_clean_filter_before_git_runs(self) -> None:
        marker = self.root / "clean-filter-executed"
        monitor = self.root / "hostile-clean-filter"
        monitor.write_text(
            f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8"
        )
        os.chmod(monitor, 0o700)
        subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.production / ".git"),
                "config",
                "filter.hostile.clean",
                str(monitor),
            ],
            check=True,
        )
        (self.production / ".gitattributes").write_text(
            "tracked/fixture.txt filter=hostile\n", encoding="utf-8"
        )
        os.chmod(self.production / ".gitattributes", 0o664)
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("unsupported section", error)
        self.assertFalse(marker.exists())
        self.assertFalse(any((self.runtime / "bin").iterdir()))
        self.assertFalse((self.runtime / "state/bootstrap-control.json").exists())

    def test_private_source_rejects_shared_clone_external_object_database(self) -> None:
        source = self.committed_private_repo(self.root / "shared-source")
        private_parent = self.root / "private-bootstrap-parent"
        private_parent.mkdir(mode=0o700)
        clone = private_parent / "clone"
        previous_umask = os.umask(0o077)
        try:
            subprocess.run(
                ["git", "clone", "--shared", "--quiet", str(source), str(clone)],
                check=True,
            )
        finally:
            os.umask(previous_umask)
        alternates = clone / ".git/objects/info/alternates"
        self.assertTrue(alternates.is_file())
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "forbidden external storage"
        ):
            BOOTSTRAP._assert_private_bootstrap_source(clone)

    def test_private_source_rejects_commondir_and_local_hardlink_clones(self) -> None:
        source = self.committed_private_repo(self.root / "local-source")
        private_parent = self.root / "private-clones"
        private_parent.mkdir(mode=0o700)
        previous_umask = os.umask(0o077)
        try:
            independent = private_parent / "independent"
            subprocess.run(
                ["git", "clone", "--no-local", "--quiet", str(source), str(independent)],
                check=True,
            )
            commondir = independent / ".git/commondir"
            commondir.write_text(str(source / ".git") + "\n", encoding="utf-8")
            os.chmod(commondir, 0o600)
            with self.assertRaisesRegex(
                BOOTSTRAP.BootstrapError, "forbidden external storage"
            ):
                BOOTSTRAP._assert_private_bootstrap_source(independent)

            local_clone = private_parent / "local-hardlinks"
            subprocess.run(
                ["git", "clone", "--local", "--quiet", str(source), str(local_clone)],
                check=True,
            )
        finally:
            os.umask(previous_umask)
        with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, "hard-linked"):
            BOOTSTRAP._assert_private_bootstrap_source(local_clone)

    def test_source_readiness_accepts_only_exact_private_canonical_clone(self) -> None:
        source = self.ready_private_repo(self.root / "ready-source")
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        report = BOOTSTRAP.bootstrap_source_readiness(
            source,
            expected_sha=sha,
        )
        self.assertTrue(report["ready"])
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["source_sha"], sha)
        self.assertEqual(report["origin_fetch_urls"], [BOOTSTRAP.REPOSITORY_SSH_URL])
        self.assertEqual(report["origin_push_urls"], [BOOTSTRAP.REPOSITORY_SSH_URL])
        self.assertEqual(report["ignored_entries"], 0)
        self.assertEqual(report["unreachable_objects"], 0)
        self.assertEqual(report["replace_refs"], 0)
        self.assertEqual(report["special_index_entries"], 0)
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "commit identity"
        ):
            BOOTSTRAP.bootstrap_source_readiness(
                source,
                expected_sha="f" * 40,
            )

    def test_source_readiness_rejects_shallow_ignored_and_unreachable_objects(
        self,
    ) -> None:
        shallow = self.ready_private_repo(self.root / "shallow-source")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=shallow,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (shallow / ".git/shallow").write_text(head + "\n", encoding="ascii")
        os.chmod(shallow / ".git/shallow", 0o600)
        with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, "must not be shallow"):
            BOOTSTRAP.bootstrap_source_readiness(shallow)

        ignored = self.ready_private_repo(self.root / "ignored-source")
        (ignored / ".gitignore").write_text("runtime-cache/\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore"], cwd=ignored, check=True)
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "ignore fixture"],
            cwd=ignored,
            check=True,
        )
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
            cwd=ignored,
            check=True,
        )
        cache = ignored / "runtime-cache"
        cache.mkdir(mode=0o700)
        (cache / "value").write_text("ignored\n", encoding="utf-8")
        os.chmod(cache / "value", 0o600)
        with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, "ignored paths"):
            BOOTSTRAP.bootstrap_source_readiness(ignored)

        dangling = self.ready_private_repo(self.root / "dangling-source")
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=dangling,
            input=b"unreviewed object\n",
            check=True,
            stdout=subprocess.PIPE,
        )
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "dangling or unreachable"
        ):
            BOOTSTRAP.bootstrap_source_readiness(dangling)

    def test_source_readiness_rejects_replace_refs_and_hidden_index_bits(
        self,
    ) -> None:
        replacement = self.ready_private_repo(self.root / "replace-source")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=replacement,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=replacement,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        alternate = subprocess.run(
            [
                "git",
                "commit-tree",
                tree,
                "-p",
                head,
                "-m",
                "replacement fixture",
            ],
            cwd=replacement,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "replace", head, alternate],
            cwd=replacement,
            check=True,
        )
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError,
            "replacement refs",
        ):
            BOOTSTRAP.bootstrap_source_readiness(replacement)

        hidden = self.ready_private_repo(self.root / "hidden-index-source")
        subprocess.run(
            ["git", "update-index", "--skip-worktree", "control.txt"],
            cwd=hidden,
            check=True,
        )
        (hidden / "control.txt").write_text("hidden drift\n", encoding="utf-8")
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError,
            "sparse or hidden",
        ):
            BOOTSTRAP.bootstrap_source_readiness(hidden)

        assumed = self.ready_private_repo(self.root / "assume-index-source")
        subprocess.run(
            ["git", "update-index", "--assume-unchanged", "control.txt"],
            cwd=assumed,
            check=True,
        )
        (assumed / "control.txt").write_text("assumed drift\n", encoding="utf-8")
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError,
            "sparse or hidden",
        ):
            BOOTSTRAP.bootstrap_source_readiness(assumed)

    def test_source_readiness_rejects_ambiguous_remote_urls(self) -> None:
        source = self.ready_private_repo(self.root / "multi-url-source")
        subprocess.run(
            [
                "git",
                "remote",
                "set-url",
                "--add",
                "origin",
                BOOTSTRAP.REPOSITORY_SSH_URL,
            ],
            cwd=source,
            check=True,
        )
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError,
            "one canonical",
        ):
            BOOTSTRAP.bootstrap_source_readiness(source)

    def test_source_readiness_rejects_worktree_and_group_writable_clone(self) -> None:
        source = self.ready_private_repo(self.root / "worktree-owner")
        worktree_parent = self.root / "private-worktrees"
        worktree_parent.mkdir(mode=0o700)
        linked = worktree_parent / "linked"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(linked), "HEAD"],
            cwd=source,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "standalone private clone"
        ):
            BOOTSTRAP.bootstrap_source_readiness(linked)

        writable = self.ready_private_repo(self.root / "writable-source")
        os.chmod(writable, 0o770)
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "standalone private clone"
        ):
            BOOTSTRAP.bootstrap_source_readiness(writable)

    def test_source_readiness_cli_is_read_only(self) -> None:
        source = self.ready_private_repo(self.root / "readiness-cli-source")
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        result, output, error = self.run_main(
            "--sha",
            sha,
            "--check-source-readiness",
            "--source-root",
            str(source),
        )
        self.assertEqual(result, 0, error)
        self.assertTrue(json.loads(output)["ready"])
        self.assertFalse(self.runtime.exists())
        result, _output, error = self.run_main(
            "--sha",
            sha,
            "--check-source-readiness",
            "--source-root",
            str(source),
            "--apply",
        )
        self.assertEqual(result, 2)
        self.assertIn("read-only", error)

    def test_strict_object_verification_rejects_hash_path_mismatch(self) -> None:
        source = self.committed_private_repo(self.root / "corrupt-source")
        blob = subprocess.run(
            ["git", "rev-parse", "HEAD:control.txt"],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        object_path = source / ".git/objects" / blob[:2] / blob[2:]
        os.chmod(object_path, 0o600)
        object_path.write_bytes(zlib.compress(b"blob 5\x00evil\n"))
        os.chmod(object_path, 0o400)
        BOOTSTRAP._assert_private_bootstrap_source(source)
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "failed strict verification"
        ):
            BOOTSTRAP._verify_git_object_database(source)

    def test_production_hardening_rejects_external_git_storage_before_git(self) -> None:
        marker = self.production / ".git/objects/info/alternates"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("/tmp/untrusted-objects\n", encoding="utf-8")
        os.chmod(marker, 0o600)
        with self.assertRaisesRegex(
            BOOTSTRAP.BootstrapError, "forbidden external storage"
        ):
            BOOTSTRAP._harden_checkout(self.production)

    def test_github_request_ignores_proxy_keylog_and_rejects_redirects(self) -> None:
        requested_url = f"{BOOTSTRAP.REPOSITORY_API_ROOT}/git/ref/heads/main"
        handlers: list[object] = []

        class Response:
            def __init__(self, url: str) -> None:
                self.url = url

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_args):  # type: ignore[no-untyped-def]
                return False

            def geturl(self) -> str:
                return self.url

            @staticmethod
            def read(_limit: int) -> bytes:
                return b'{"ok":true}'

        class Opener:
            def __init__(self, response_url: str) -> None:
                self.response_url = response_url

            def open(self, _request, timeout):  # type: ignore[no-untyped-def]
                self.assert_timeout(timeout)
                return Response(self.response_url)

            @staticmethod
            def assert_timeout(timeout: int) -> None:
                if timeout != 30:
                    raise AssertionError(timeout)

        def build_opener(*values):  # type: ignore[no-untyped-def]
            handlers.extend(values)
            return Opener(requested_url)

        keylog = self.root / "tls-keys.log"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "HTTPS_PROXY": "http://127.0.0.1:1",
                    "SSLKEYLOGFILE": str(keylog),
                },
            ),
            mock.patch.object(
                BOOTSTRAP.urllib.request,
                "build_opener",
                side_effect=build_opener,
            ),
        ):
            self.assertEqual(
                BOOTSTRAP._request_github_json(requested_url, "token"),
                {"ok": True},
            )
        proxy = next(
            value
            for value in handlers
            if isinstance(value, BOOTSTRAP.urllib.request.ProxyHandler)
        )
        https = next(
            value
            for value in handlers
            if isinstance(value, BOOTSTRAP.urllib.request.HTTPSHandler)
        )
        self.assertEqual(proxy.proxies, {})
        self.assertIsNone(https._context.keylog_filename)
        self.assertFalse(keylog.exists())

        with mock.patch.object(
            BOOTSTRAP.urllib.request,
            "build_opener",
            return_value=Opener("https://example.invalid/redirect"),
        ), self.assertRaisesRegex(BOOTSTRAP.BootstrapError, "redirected"):
            BOOTSTRAP._request_github_json(requested_url, "token")

    def test_sealed_delivery_gate_revalidates_exact_workflow_attempt(self) -> None:
        run_id = 77
        attempt = 1
        required = list(
            BOOTSTRAP._required_ci_jobs(
                source_sha=SOURCE_SHA,
                allow_test=True,
            )
        )
        self.assertEqual(
            required,
            [
                "Publish and smoke immutable main images",
                "bridge-validation",
                "ci-gate",
                "exact-B bridge compatibility",
            ],
        )
        sealed = {
            "remote_main": SOURCE_SHA,
            "ci": {
                "workflow_run_id": run_id,
                "run_attempt": attempt,
                "head_sha": SOURCE_SHA,
                "head_branch": "main",
                "event": "push",
                "path": ".github/workflows/ci.yml",
                "conclusion": "success",
                "required_jobs": required,
            },
        }
        urls: list[str] = []

        def github(url: str, _token: str) -> dict[str, object]:
            urls.append(url)
            if url.endswith("/git/ref/heads/main"):
                return {
                    "ref": "refs/heads/main",
                    "object": {"type": "commit", "sha": SOURCE_SHA},
                }
            if url.endswith(f"/actions/runs/{run_id}/attempts/{attempt}"):
                return {
                    "id": run_id,
                    "run_attempt": attempt,
                    "head_sha": SOURCE_SHA,
                    "head_branch": "main",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                    "path": ".github/workflows/ci.yml",
                }
            if url.endswith(
                f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"
            ):
                return {
                    "jobs": [
                        {"name": name, "conclusion": "success"}
                        for name in required
                    ]
                }
            raise AssertionError(url)

        with (
            mock.patch.object(BOOTSTRAP, "_github_token", return_value="token"),
            mock.patch.object(
                BOOTSTRAP,
                "_required_ci_jobs",
                return_value=tuple(required),
            ),
            mock.patch.object(
                BOOTSTRAP, "_request_github_json", side_effect=github
            ),
        ):
            evidence = BOOTSTRAP._delivery_gate(
                self.production,
                self.runtime,
                SOURCE_SHA,
                allow_test=False,
                sealed=sealed,
            )
        self.assertEqual(evidence, sealed)
        self.assertTrue(any("/attempts/1" in value for value in urls))
        self.assertFalse(any("filter=latest" in value for value in urls))
        for missing in required:
            with self.subTest(missing=missing):
                def incomplete(
                    url: str,
                    token: str,
                    *,
                    omitted: str = missing,
                ) -> dict[str, object]:
                    document = github(url, token)
                    if url.endswith(
                        f"/actions/runs/{run_id}/attempts/{attempt}/jobs"
                        "?per_page=100"
                    ):
                        document = {
                            "jobs": [
                                {"name": name, "conclusion": "success"}
                                for name in required
                                if name != omitted
                            ]
                        }
                    return document

                with (
                    mock.patch.object(
                        BOOTSTRAP,
                        "_github_token",
                        return_value="token",
                    ),
                    mock.patch.object(
                        BOOTSTRAP,
                        "_required_ci_jobs",
                        return_value=tuple(required),
                    ),
                    mock.patch.object(
                        BOOTSTRAP,
                        "_request_github_json",
                        side_effect=incomplete,
                    ),
                    self.assertRaisesRegex(
                        BOOTSTRAP.BootstrapError,
                        "lacks required successful jobs",
                    ),
                ):
                    BOOTSTRAP._delivery_gate(
                        self.production,
                        self.runtime,
                        SOURCE_SHA,
                        allow_test=False,
                        sealed=sealed,
                    )

    def test_apply_rejects_symlink_runtime_root_before_chmod(self) -> None:
        target = self.root / "runtime-target"
        target.mkdir(mode=0o755)
        self.runtime.symlink_to(target, target_is_directory=True)
        before = stat.S_IMODE(target.stat().st_mode)
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("runtime root is unsafe", error)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), before)

    def test_apply_rejects_symlink_deploy_lock(self) -> None:
        state = self.runtime / "state"
        state.mkdir(parents=True, mode=0o700)
        outside = self.root / "outside-lock"
        outside.write_text("unchanged\n", encoding="utf-8")
        os.chmod(outside, 0o600)
        (state / "deploy.lock").symlink_to(outside)
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertTrue(error)
        self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged\n")
        self.assertFalse((self.runtime / "bin").exists())

    def test_content_release_is_never_overwritten_and_tampering_fails_closed(self) -> None:
        first, output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(first, 0, error)
        active = json.loads(output)["active_control"]
        controller = (
            self.runtime
            / "control-releases"
            / active["release_id"]
            / "pull_deploy_controller.py"
        )
        controller.write_text("tampered\n", encoding="utf-8")
        os.chmod(controller, 0o700)
        second, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(second, 2)
        self.assertIn("control release", error)

    def test_complete_prerename_control_staging_is_resumed_exactly(self) -> None:
        first, output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(first, 0, error)
        active = json.loads(output)["active_control"]
        release = self.runtime / "control-releases" / active["release_id"]
        staging = (
            self.runtime
            / "control-releases"
            / f".bootstrap-{active['release_id']}"
        )
        os.rename(release, staging)
        BOOTSTRAP._fsync_directory(staging.parent)

        second, output, error = self.run_main(*self.apply_arguments())

        self.assertEqual(second, 0, error)
        self.assertTrue(release.is_dir())
        self.assertFalse(staging.exists())
        self.assertEqual(
            json.loads(output)["active_control"]["release_id"],
            active["release_id"],
        )

    def test_legacy_control_intent_recovers_partial_deterministic_owner(self) -> None:
        original = BOOTSTRAP._write_authorized_staging
        partial: Path | None = None

        def crash_owner(path: Path, payload: bytes, mode: int) -> None:
            nonlocal partial
            if partial is None and path.name == ".owner.json":
                partial = path
                path.write_bytes(payload[:9])
                os.chmod(path, mode)
                raise BOOTSTRAP.BootstrapError(
                    "injected legacy deterministic owner crash"
                )
            original(path, payload, mode)

        with mock.patch.object(
            BOOTSTRAP,
            "_write_authorized_staging",
            side_effect=crash_owner,
        ):
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2, error)
        self.assertIn("legacy deterministic owner crash", error)
        self.assertIsNotNone(partial)
        assert partial is not None
        self.assertEqual(partial.stat().st_size, 9)

        result, output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 0, error)
        self.assertFalse(partial.parent.exists())
        self.assertEqual(json.loads(output)["status"], "initialized")

    def test_bootstrap_transaction_ancestor_creation_fsyncs_child_and_parent(
        self,
    ) -> None:
        self.runtime.mkdir(mode=0o700)
        original_fsync = BOOTSTRAP.os.fsync
        sealed: set[Path] = set()

        def record_fsync(descriptor: int) -> None:
            try:
                sealed.add(Path(f"/proc/self/fd/{descriptor}").resolve())
            except OSError:
                pass
            original_fsync(descriptor)

        with mock.patch.object(BOOTSTRAP.os, "fsync", side_effect=record_fsync):
            directory = BOOTSTRAP._ensure_bootstrap_transaction_directory(
                self.runtime
            )

        state = self.runtime / "state"
        legacy = state / "legacy-takeover"
        self.assertEqual(directory, legacy / "bootstrap-children")
        self.assertTrue(
            {
                self.runtime,
                state,
                legacy,
                directory,
            }.issubset(sealed)
        )

    def test_visible_bootstrap_intent_is_resealed_before_layout_mutation(
        self,
    ) -> None:
        original_atomic = BOOTSTRAP._atomic_json
        exposed = False

        def expose_intent(path: Path, document: dict[str, object]) -> None:
            nonlocal exposed
            original_atomic(path, document)
            if (
                not exposed
                and path.parent.name == "bootstrap-children"
                and document.get("phase") == "runtime-layout-intent"
            ):
                exposed = True
                raise BOOTSTRAP.BootstrapError(
                    "injected visible bootstrap intent"
                )

        with mock.patch.object(
            BOOTSTRAP, "_atomic_json", side_effect=expose_intent
        ):
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2, error)
        self.assertIn("visible bootstrap intent", error)
        self.assertTrue(exposed)

        original_reseal = BOOTSTRAP._reseal_bootstrap_transaction
        original_initialize = BOOTSTRAP._initialize_runtime_root
        resealed = False
        second_fault = False

        def record_reseal(path: Path) -> dict[str, object]:
            nonlocal resealed
            value = original_reseal(path)
            resealed = True
            return value

        def crash_after_layout(runtime_root: Path) -> None:
            nonlocal second_fault
            self.assertTrue(
                resealed,
                "runtime layout mutation preceded journal durability reseal",
            )
            original_initialize(runtime_root)
            if not second_fault:
                second_fault = True
                raise BOOTSTRAP.BootstrapError(
                    "injected second bootstrap layout fault"
                )

        with (
            mock.patch.object(
                BOOTSTRAP,
                "_reseal_bootstrap_transaction",
                side_effect=record_reseal,
            ),
            mock.patch.object(
                BOOTSTRAP,
                "_initialize_runtime_root",
                side_effect=crash_after_layout,
            ),
        ):
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2, error)
        self.assertIn("second bootstrap layout fault", error)
        self.assertTrue(resealed)

        result, output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "initialized")

    def test_completed_bootstrap_journal_reseals_before_terminal_replay(
        self,
    ) -> None:
        result, output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "initialized")
        original_reseal = BOOTSTRAP._reseal_bootstrap_transaction
        first_fault = False

        def lose_terminal_reseal(path: Path) -> dict[str, object]:
            nonlocal first_fault
            value = original_reseal(path)
            if not first_fault:
                first_fault = True
                raise BOOTSTRAP.BootstrapError(
                    "injected completed journal reseal response loss"
                )
            return value

        with mock.patch.object(
            BOOTSTRAP,
            "_reseal_bootstrap_transaction",
            side_effect=lose_terminal_reseal,
        ):
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2, error)
        self.assertIn("completed journal reseal", error)
        self.assertTrue(first_fault)

        original_initialize = BOOTSTRAP._initialize_runtime_root
        resealed = False
        second_fault = False

        def record_reseal(path: Path) -> dict[str, object]:
            nonlocal resealed
            value = original_reseal(path)
            resealed = True
            return value

        def crash_terminal_layout(runtime_root: Path) -> None:
            nonlocal second_fault
            self.assertTrue(resealed)
            original_initialize(runtime_root)
            if not second_fault:
                second_fault = True
                raise BOOTSTRAP.BootstrapError(
                    "injected second completed replay fault"
                )

        with (
            mock.patch.object(
                BOOTSTRAP,
                "_reseal_bootstrap_transaction",
                side_effect=record_reseal,
            ),
            mock.patch.object(
                BOOTSTRAP,
                "_initialize_runtime_root",
                side_effect=crash_terminal_layout,
            ),
        ):
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2, error)
        self.assertIn("second completed replay fault", error)

        result, output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 0, error)
        self.assertEqual(json.loads(output)["status"], "initialized")

    def test_foreign_control_staging_is_never_silently_removed(self) -> None:
        first, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(first, 0, error)
        foreign = self.runtime / "control-releases/.bootstrap-foreign"
        foreign.mkdir(mode=0o700)
        (foreign / "evidence").write_text("foreign\n", encoding="utf-8")

        second, _output, error = self.run_main(*self.apply_arguments())

        self.assertEqual(second, 2)
        self.assertIn("foreign bootstrap staging", error)
        self.assertEqual(
            (foreign / "evidence").read_text(encoding="utf-8"),
            "foreign\n",
        )

    def test_immutable_router_bytes_must_match_the_reviewed_git_object(self) -> None:
        current = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        with mock.patch.object(BOOTSTRAP, "_safe_source", return_value=b"tampered\n"):
            with self.assertRaisesRegex(BOOTSTRAP.BootstrapError, "reviewed Git object"):
                BOOTSTRAP._read_reviewed_source(
                    "README.md",
                    source_sha=current,
                    allow_test=False,
                )

    def test_installed_reviewed_evidence_can_load_snapshot_dependency(
        self,
    ) -> None:
        installed = self.runtime / "legacy-takeover/bin"
        installed.mkdir(parents=True, mode=0o700)
        for name in (
            "legacy_takeover_evidence.py",
            "legacy_takeover.py",
            "site_helper_contracts.py",
            "git_source_trust.py",
        ):
            target = installed / name
            target.write_bytes(
                (REPOSITORY_ROOT / "scripts" / name).read_bytes()
            )
            os.chmod(target, 0o700)
        validator = BOOTSTRAP._legacy_takeover_evidence(
            source_sha=SOURCE_SHA,
            allow_test=True,
            installed_runtime_root=self.runtime,
        )

        snapshot = validator.snapshot_current_control_layout(self.runtime)

        self.assertRegex(snapshot["sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            [record["relative_path"] for record in snapshot["records"]],
            list(validator.CONTROL_LAYOUT_RELATIVE_PATHS),
        )

    def test_locked_delivery_gate_drift_leaves_no_installed_authority(self) -> None:
        first = {
            "remote_main": SOURCE_SHA,
            "ci": {"head_sha": SOURCE_SHA, "conclusion": "success"},
        }
        second = {
            "remote_main": "f" * 40,
            "ci": {"head_sha": "f" * 40, "conclusion": "success"},
        }
        with mock.patch.object(
            BOOTSTRAP, "_delivery_gate", side_effect=[first, second]
        ):
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("delivery evidence changed", error)
        self.assertFalse((self.runtime / "state/active-control.json").exists())
        self.assertEqual(list((self.runtime / "bin").iterdir()), [])

    def test_late_delivery_drift_has_durable_abort_authority(self) -> None:
        accepted = {
            "remote_main": SOURCE_SHA,
            "ci": {"head_sha": SOURCE_SHA, "conclusion": "success"},
        }
        drifted = {
            "remote_main": "f" * 40,
            "ci": {"head_sha": "f" * 40, "conclusion": "success"},
        }
        with mock.patch.object(
            BOOTSTRAP,
            "_delivery_gate",
            side_effect=[accepted, accepted, drifted],
        ):
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn(
            "authority changed before active-control commit",
            error,
        )
        transaction_path = BOOTSTRAP._bootstrap_transaction_path(
            self.runtime,
            operation_id=TAKEOVER_OPERATION_ID,
            source_sha=SOURCE_SHA,
        )
        transaction = BOOTSTRAP._validate_bootstrap_transaction(
            BOOTSTRAP._load_private_json(transaction_path),
            path=transaction_path,
        )
        self.assertEqual(transaction["status"], "in-progress")
        self.assertEqual(transaction["phase"], "control-release-ready")
        self.assertFalse(
            (self.runtime / "state/bootstrap-control.json").exists()
        )
        self.assertFalse((self.runtime / "state/active-control.json").exists())
        self.assertTrue(any((self.runtime / "bin").iterdir()))
        self.assertEqual(
            stat.S_IMODE((self.production / ".git").stat().st_mode),
            0o700,
        )

        control_digest = "sha256:" + "a" * 64
        permission_digest = "sha256:" + "b" * 64
        terminal_digest = "sha256:" + "c" * 64
        evidence = SimpleNamespace(
            validate_install_manifest=lambda _root, _sha, _tree: {
                "authority_sha": SOURCE_SHA,
                "authority_tree": SOURCE_TREE,
            },
            sha256_file=lambda _path: "sha256:" + "3" * 64,
            snapshot_current_control_layout=lambda _root: {
                "sha256": control_digest
            },
            snapshot_current_checkout_permissions=lambda _root, _operation: {
                "sha256": permission_digest
            },
            validate_status_document=lambda _response, _operation: {
                "active": False,
                "restore_phase": "restored",
                "control_layout_replacement_sha256": control_digest,
                "checkout_permissions_replacement_sha256": permission_digest,
                "restored_terminal_sha256": terminal_digest,
            },
        )
        restore = mock.Mock(
            return_value=subprocess.CompletedProcess(
                ["legacy-restore"],
                0,
                "{}\n",
                "",
            )
        )
        abort_arguments = [
            "--sha",
            SOURCE_SHA,
            "--abort",
            "--production-root",
            str(self.production),
            "--runtime-root",
            str(self.runtime),
            "--confirm-production-root",
            str(self.production.absolute()),
            "--confirm-runtime-root",
            str(self.runtime.absolute()),
            "--legacy-takeover-operation-id",
            TAKEOVER_OPERATION_ID,
            "--confirm-source-tree",
            SOURCE_TREE,
        ]
        with (
            mock.patch.object(
                BOOTSTRAP,
                "_legacy_takeover_evidence",
                return_value=evidence,
            ),
            mock.patch.object(
                BOOTSTRAP,
                "_run_bootstrap_legacy_restore",
                restore,
            ),
        ):
            result, output, error = self.run_main(*abort_arguments)
            self.assertEqual(result, 0, error)
            self.assertEqual(json.loads(output)["status"], "aborted")
            result, output, error = self.run_main(*abort_arguments)
            self.assertEqual(result, 0, error)
            self.assertEqual(json.loads(output)["status"], "already-aborted")
        restore.assert_called_once()
        command = restore.call_args.args[0]
        self.assertIn(control_digest, command)
        self.assertIn(permission_digest, command)
        terminal = BOOTSTRAP._load_private_json(transaction_path)
        self.assertEqual(terminal["status"], "aborted")
        self.assertEqual(
            terminal["restored_terminal_sha256"],
            terminal_digest,
        )

    def test_partial_bootstrap_before_active_pointer_is_safely_resumable(self) -> None:
        first, output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(first, 0, error)
        release_id = json.loads(output)["active_control"]["release_id"]
        (self.runtime / "state/active-control.json").unlink()
        (self.runtime / "state/bootstrap-control.json").unlink()
        second, output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(second, 0, error)
        self.assertEqual(json.loads(output)["active_control"]["release_id"], release_id)

    def test_crash_after_prepared_authority_is_fail_closed_and_resumable(self) -> None:
        original = BOOTSTRAP._atomic_json
        injected = False

        def crash(path: Path, document: dict[str, object]) -> None:
            nonlocal injected
            original(path, document)
            if (
                not injected
                and path.name == "bootstrap-control.json"
                and document.get("status") == "prepared"
            ):
                injected = True
                raise BOOTSTRAP.BootstrapError("injected prepared crash")

        with mock.patch.object(BOOTSTRAP, "_atomic_json", side_effect=crash):
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("injected prepared crash", error)
        self.assertEqual(
            json.loads(
                (self.runtime / "state/bootstrap-control.json").read_text(
                    encoding="utf-8"
                )
            )["status"],
            "prepared",
        )
        self.assertFalse((self.runtime / "state/active-control.json").exists())
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 0, error)

    def test_crash_after_active_pointer_is_fail_closed_and_resumable(self) -> None:
        original = BOOTSTRAP._atomic_json
        injected = False

        def crash(path: Path, document: dict[str, object]) -> None:
            nonlocal injected
            original(path, document)
            if not injected and path.name == "active-control.json":
                injected = True
                raise BOOTSTRAP.BootstrapError("injected active crash")

        with mock.patch.object(BOOTSTRAP, "_atomic_json", side_effect=crash):
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("injected active crash", error)
        self.assertTrue((self.runtime / "state/active-control.json").is_file())
        self.assertEqual(
            json.loads(
                (self.runtime / "state/bootstrap-control.json").read_text(
                    encoding="utf-8"
                )
            )["status"],
            "prepared",
        )
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 0, error)

    def test_crash_after_completed_authority_is_idempotently_verified(self) -> None:
        original = BOOTSTRAP._atomic_json
        injected = False

        def crash(path: Path, document: dict[str, object]) -> None:
            nonlocal injected
            original(path, document)
            if (
                not injected
                and path.name == "bootstrap-control.json"
                and document.get("status") == "completed"
            ):
                injected = True
                raise BOOTSTRAP.BootstrapError("injected completed crash")

        with mock.patch.object(BOOTSTRAP, "_atomic_json", side_effect=crash):
            result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 2)
        self.assertIn("injected completed crash", error)
        self.assertEqual(
            json.loads(
                (self.runtime / "state/bootstrap-control.json").read_text(
                    encoding="utf-8"
                )
            )["status"],
            "completed",
        )
        self.assertTrue((self.runtime / "state/active-control.json").is_file())
        result, _output, error = self.run_main(*self.apply_arguments())
        self.assertEqual(result, 0, error)

    def test_test_mode_cannot_target_real_production_roots(self) -> None:
        result, _output, error = self.run_main(
            "--sha",
            SOURCE_SHA,
            "--apply",
            "--production-root",
            str(BOOTSTRAP.PRODUCTION_ROOT),
            "--runtime-root",
            str(BOOTSTRAP.RUNTIME_ROOT),
            "--confirm-production-root",
            str(BOOTSTRAP.PRODUCTION_ROOT),
            "--confirm-runtime-root",
            str(BOOTSTRAP.RUNTIME_ROOT),
            "--confirm-source-tree",
            SOURCE_TREE,
        )
        self.assertEqual(result, 2)
        self.assertIn("test mode is forbidden", error)

    def test_test_mode_rejects_real_root_subtrees_and_real_unit_derivation(self) -> None:
        result, _output, error = self.run_main(
            "--sha",
            SOURCE_SHA,
            "--production-root",
            str(self.production),
            "--runtime-root",
            str(BOOTSTRAP.RUNTIME_ROOT / "test-child"),
        )
        self.assertEqual(result, 2)
        self.assertIn("test mode is forbidden", error)
        self.assertFalse((BOOTSTRAP.RUNTIME_ROOT / "test-child").exists())

        crafted = BOOTSTRAP.WORKER_UNIT_PATH.parent.parent / "nexpoly-test"
        with mock.patch.object(
            BOOTSTRAP,
            "_worker_unit_path",
            return_value=BOOTSTRAP.WORKER_UNIT_PATH,
        ):
            result, _output, error = self.run_main(
                "--sha",
                SOURCE_SHA,
                "--production-root",
                str(crafted),
                "--runtime-root",
                str(self.runtime),
            )
        self.assertEqual(result, 2)
        self.assertIn("test mode is forbidden", error)

    def test_bootstrap_entrypoint_requires_isolated_fixed_python(self) -> None:
        self.assertEqual(
            SCRIPT.read_text(encoding="utf-8").splitlines()[0],
            "#!/usr/bin/python3 -I",
        )
        hostile = self.root / "hostile-python"
        hostile.mkdir(mode=0o700)
        marker = hostile / "sitecustomize-ran"
        (hostile / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
            encoding="utf-8",
        )
        direct = subprocess.run(
            ["/usr/bin/python3", str(SCRIPT), "--help"],
            env={"PATH": "/usr/bin:/bin"},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(direct.returncode, 2)
        self.assertIn("isolated Python", direct.stderr)
        self.assertFalse(marker.exists())

        isolated = subprocess.run(
            [str(SCRIPT), "--help"],
            env={"PATH": str(hostile), "PYTHONPATH": str(hostile)},
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(isolated.returncode, 0, isolated.stderr)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
