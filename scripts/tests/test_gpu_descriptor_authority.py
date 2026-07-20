from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

from gpu_resource import authority as authority_module
from gpu_resource.authority import (
    FormalGpuAuthorityError,
    close_process_gpu_authority,
    load_formal_gpu_authority,
    materialize_formal_gpu_authority,
)
from gpu_resource.client import (
    GpuLease,
    GpuBrokerClientError,
    mps_client_environment,
)
from scripts import preflight_monomer_dft_env as preflight
from workers.monomer_dft_worker.app import config as worker_config


ROOT = Path(__file__).resolve().parents[2]
MPS_CONTROL = ROOT / "scripts/gpu_mps_control.sh"
RESERVATIONS = ROOT / "ops/config/gpu-external-reservations.json"


def _identity(descriptor: int) -> str:
    metadata = os.fstat(descriptor)
    return f"{metadata.st_dev}:{metadata.st_ino}"


class DescriptorAuthorityFixture:
    def __init__(self, root: Path) -> None:
        self.runtime_root = root
        self.runtime_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.runtime_root.chmod(0o700)
        self.gpu_root = root / "gpu-resource"
        self.gpu_root.mkdir(mode=0o700)
        self.slot = self.gpu_root / "mps-1"
        self.pipe = self.slot / "pipe"
        self.log = self.slot / "log"
        self.pipe.mkdir(parents=True, mode=0o700)
        self.log.mkdir(mode=0o700)
        os.mkfifo(self.pipe / "control", mode=0o600)
        self.reservations = self.gpu_root / "external-reservations.json"
        self.reservations.write_bytes(RESERVATIONS.read_bytes())
        self.reservations.chmod(0o600)
        self.broker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.broker.bind(str(self.gpu_root / "broker.sock"))
        self.broker.listen(1)
        self.root_fd = os.open(
            self.gpu_root,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        self.reservations_fd = os.open(
            self.reservations,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        self.slot_fd = os.open(
            self.slot,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        self.pipe_fd = os.open(
            self.pipe,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        self.log_fd = os.open(
            self.log,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )

    def close(self) -> None:
        for descriptor in (
            self.log_fd,
            self.pipe_fd,
            self.slot_fd,
            self.reservations_fd,
            self.root_fd,
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self.broker.close()

    def environment(self) -> dict[str, str]:
        pid = os.getpid()
        from gpu_resource.authority import _process_start_ticks

        return {
            "NEXPOLY_DFT_FORMAL_ACCEPTANCE": "1",
            "NEXPOLY_DFT_PROJECT_NAME": "nexpoly_dft_fresh_authority_test",
            "NEXPOLY_DFT_AUTHORITY_SHA": "a" * 40,
            "NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY": "1",
            "NEXPOLY_DFT_GPU_AUTHORITY_PID": str(pid),
            "NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS": str(
                _process_start_ticks(pid)
            ),
            "NEXPOLY_DFT_GPU_AUTHORITY_ROOT": (
                f"/proc/{pid}/fd/{self.root_fd}"
            ),
            "NEXPOLY_DFT_GPU_AUTHORITY_ROOT_IDENTITY": _identity(
                self.root_fd
            ),
            "NEXPOLY_DFT_GPU_RESERVATIONS_AUTHORITY": (
                f"/proc/{pid}/fd/{self.reservations_fd}"
            ),
            "NEXPOLY_DFT_GPU_RESERVATIONS_IDENTITY": _identity(
                self.reservations_fd
            ),
            "NEXPOLY_DFT_GPU_RESERVATIONS_SHA256": hashlib.sha256(
                RESERVATIONS.read_bytes()
            ).hexdigest(),
            "NEXPOLY_DFT_GPU1_MPS_PIPE_AUTHORITY": (
                f"/proc/{pid}/fd/{self.pipe_fd}"
            ),
            "NEXPOLY_DFT_GPU1_MPS_PIPE_IDENTITY": _identity(self.pipe_fd),
        }


class FormalGpuAuthorityTests(unittest.TestCase):
    def test_normal_gpu_device_is_not_mistaken_for_authority(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"NEXPOLY_DFT_GPU_DEVICE": "1"},
            clear=True,
        ):
            values, authority = preflight.effective_environment(
                ROOT,
                {"NEXPOLY_DFT_GPU_DEVICE": "1"},
            )
        self.assertEqual(values["NEXPOLY_DFT_GPU_DEVICE"], "1")
        self.assertIsNone(authority)

    def test_partial_authority_is_rejected(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"NEXPOLY_DFT_GPU_AUTHORITY_PID": "123"},
                clear=True,
            ),
            self.assertRaisesRegex(
                preflight.PreflightError, "partial"
            ),
        ):
            preflight.effective_environment(ROOT, {})

    def test_exact_parent_descriptor_authority_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DescriptorAuthorityFixture(Path(temporary))
            try:
                authority = load_formal_gpu_authority(
                    fixture.environment(),
                    expected_reservations_file=RESERVATIONS,
                    expected_root=fixture.gpu_root,
                    require=True,
                )
            finally:
                fixture.close()
        assert authority is not None
        self.assertEqual(authority.root.name, str(fixture.root_fd))
        self.assertEqual(dict(authority.pipe_directories)[1].name, str(fixture.pipe_fd))

    def test_pid_reuse_identity_and_closed_descriptor_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DescriptorAuthorityFixture(Path(temporary))
            environment = fixture.environment()
            environment["NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS"] = "1"
            with self.assertRaisesRegex(
                FormalGpuAuthorityError, "process changed"
            ):
                load_formal_gpu_authority(
                    environment,
                    expected_reservations_file=RESERVATIONS,
                    expected_root=fixture.gpu_root,
                    require=True,
                )
            environment = fixture.environment()
            os.close(fixture.pipe_fd)
            fixture.pipe_fd = -1
            with self.assertRaises(FormalGpuAuthorityError):
                load_formal_gpu_authority(
                    environment,
                    expected_reservations_file=RESERVATIONS,
                    expected_root=fixture.gpu_root,
                    require=True,
                )
            fixture.close()

    def test_pipe_descriptor_must_be_exact_child_of_dev_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DescriptorAuthorityFixture(Path(temporary))
            external = Path(temporary) / "external"
            external.mkdir(mode=0o700)
            os.mkfifo(external / "control", mode=0o600)
            external_fd = os.open(
                external,
                os.O_RDONLY
                | os.O_CLOEXEC
                | os.O_DIRECTORY
                | os.O_NOFOLLOW,
            )
            environment = fixture.environment()
            environment["NEXPOLY_DFT_GPU1_MPS_PIPE_AUTHORITY"] = (
                f"/proc/{os.getpid()}/fd/{external_fd}"
            )
            environment["NEXPOLY_DFT_GPU1_MPS_PIPE_IDENTITY"] = _identity(
                external_fd
            )
            try:
                with self.assertRaisesRegex(
                    FormalGpuAuthorityError, "escaped"
                ):
                    load_formal_gpu_authority(
                        environment,
                        expected_reservations_file=RESERVATIONS,
                        expected_root=fixture.gpu_root,
                        require=True,
                    )
            finally:
                os.close(external_fd)
                fixture.close()

    def test_reservation_leaf_replacement_cannot_change_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DescriptorAuthorityFixture(Path(temporary))
            environment = fixture.environment()
            held = fixture.gpu_root / "held-reservations.json"
            fixture.reservations.rename(held)
            fixture.reservations.write_text("malicious", encoding="utf-8")
            fixture.reservations.chmod(0o600)
            try:
                with self.assertRaisesRegex(
                    FormalGpuAuthorityError, "escaped"
                ):
                    load_formal_gpu_authority(
                        environment,
                        expected_reservations_file=RESERVATIONS,
                        expected_root=fixture.gpu_root,
                        require=True,
                    )
                self.assertEqual(
                    os.pread(
                        fixture.reservations_fd,
                        1024 * 1024,
                        0,
                    ),
                    RESERVATIONS.read_bytes(),
                )
                self.assertEqual(
                    fixture.reservations.read_text(encoding="utf-8"),
                    "malicious",
                )
            finally:
                fixture.close()

    def test_authority_cannot_substitute_another_owner_private_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DescriptorAuthorityFixture(Path(temporary) / "dev")
            other = DescriptorAuthorityFixture(Path(temporary) / "other")
            try:
                with self.assertRaisesRegex(
                    FormalGpuAuthorityError, "exact development root"
                ):
                    load_formal_gpu_authority(
                        other.environment(),
                        expected_reservations_file=RESERVATIONS,
                        expected_root=fixture.gpu_root,
                        require=True,
                    )
            finally:
                other.close()
                fixture.close()

    def test_held_root_does_not_follow_replacement_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DescriptorAuthorityFixture(Path(temporary) / "dev")
            environment = fixture.environment()
            original = fixture.runtime_root / "gpu-resource-held"
            external = Path(temporary) / "external"
            external.mkdir(mode=0o700)
            sentinel = external / "sentinel"
            sentinel.write_text("unchanged", encoding="utf-8")
            fixture.gpu_root.rename(original)
            fixture.gpu_root.symlink_to(external, target_is_directory=True)
            try:
                with self.assertRaisesRegex(
                    FormalGpuAuthorityError, "exact development root"
                ):
                    load_formal_gpu_authority(
                        environment,
                        expected_reservations_file=RESERVATIONS,
                        expected_root=fixture.gpu_root,
                        require=True,
                    )
                held = Path(environment["NEXPOLY_DFT_GPU_AUTHORITY_ROOT"])
                self.assertTrue((held / "broker.sock").exists())
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            finally:
                fixture.gpu_root.unlink()
                fixture.gpu_root = original
                fixture.close()

    def test_process_materialization_survives_parent_path_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DescriptorAuthorityFixture(Path(temporary) / "dev")
            environment = fixture.environment()
            original = fixture.runtime_root / "gpu-resource-held"
            external = Path(temporary) / "external"
            external.mkdir(mode=0o700)
            sentinel = external / "sentinel"
            sentinel.write_text("unchanged", encoding="utf-8")
            try:
                with mock.patch.dict(
                    os.environ, environment, clear=True
                ):
                    materialized = materialize_formal_gpu_authority(
                        expected_reservations_file=RESERVATIONS,
                        expected_root=fixture.gpu_root,
                    )
                    assert materialized is not None
                    self.assertRegex(
                        str(materialized.root),
                        rf"^/proc/{os.getpid()}/fd/[0-9]+$",
                    )
                    child_pid = os.getpid() + 1
                    with (
                        mock.patch.object(
                            authority_module.os,
                            "getpid",
                            return_value=child_pid,
                        ),
                        self.assertRaisesRegex(
                            FormalGpuAuthorityError,
                            "cannot cross fork",
                        ),
                    ):
                        load_formal_gpu_authority(
                            expected_reservations_file=RESERVATIONS,
                            expected_root=fixture.gpu_root,
                            require=True,
                        )
                    fixture.gpu_root.rename(original)
                    fixture.gpu_root.symlink_to(
                        external, target_is_directory=True
                    )
                    self.assertTrue(
                        (materialized.root / "broker.sock").exists()
                    )
                    self.assertEqual(
                        os.pread(
                            int(materialized.reservations.name),
                            1024 * 1024,
                            0,
                        ),
                        RESERVATIONS.read_bytes(),
                    )
                    self.assertEqual(
                        sentinel.read_text(encoding="utf-8"),
                        "unchanged",
                    )
            finally:
                close_process_gpu_authority()
                if fixture.gpu_root.is_symlink():
                    fixture.gpu_root.unlink()
                    fixture.gpu_root = original
                fixture.close()

    def test_executor_materializes_its_cuda_pipe_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DescriptorAuthorityFixture(Path(temporary) / "dev")
            environment = fixture.environment()
            environment.update(
                {
                    "MONOMER_DFT_EXECUTOR_PROCESS": "1",
                    "NEXPOLY_DFT_EXECUTOR_GPU_DEVICE": "1",
                    "CUDA_MPS_PIPE_DIRECTORY": environment[
                        "NEXPOLY_DFT_GPU1_MPS_PIPE_AUTHORITY"
                    ],
                }
            )
            try:
                with mock.patch.dict(
                    os.environ, environment, clear=True
                ):
                    materialized = materialize_formal_gpu_authority(
                        expected_reservations_file=RESERVATIONS,
                        expected_root=fixture.gpu_root,
                    )
                    assert materialized is not None
                    self.assertEqual(
                        os.environ["CUDA_MPS_PIPE_DIRECTORY"],
                        str(dict(materialized.pipe_directories)[1]),
                    )
                    self.assertRegex(
                        os.environ["CUDA_MPS_PIPE_DIRECTORY"],
                        rf"^/proc/{os.getpid()}/fd/[0-9]+$",
                    )
            finally:
                close_process_gpu_authority()
                fixture.close()

    def test_materialized_cache_rejects_environment_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DescriptorAuthorityFixture(Path(temporary) / "dev")
            environment = fixture.environment()
            try:
                with mock.patch.dict(
                    os.environ, environment, clear=True
                ):
                    materialize_formal_gpu_authority(
                        expected_reservations_file=RESERVATIONS,
                        expected_root=fixture.gpu_root,
                    )
                    os.environ[
                        "NEXPOLY_DFT_GPU_RESERVATIONS_SHA256"
                    ] = "0" * 64
                    with self.assertRaisesRegex(
                        FormalGpuAuthorityError, "environment changed"
                    ):
                        materialize_formal_gpu_authority(
                            expected_reservations_file=RESERVATIONS,
                            expected_root=fixture.gpu_root,
                        )
            finally:
                close_process_gpu_authority()
                fixture.close()

    def test_executor_rejects_a_different_cuda_pipe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DescriptorAuthorityFixture(Path(temporary) / "dev")
            environment = fixture.environment()
            environment.update(
                {
                    "MONOMER_DFT_EXECUTOR_PROCESS": "1",
                    "NEXPOLY_DFT_EXECUTOR_GPU_DEVICE": "1",
                    "CUDA_MPS_PIPE_DIRECTORY": "/tmp/not-authority",
                }
            )
            try:
                with (
                    mock.patch.dict(
                        os.environ, environment, clear=True
                    ),
                    self.assertRaisesRegex(
                        FormalGpuAuthorityError,
                        "differs from its parent",
                    ),
                ):
                    materialize_formal_gpu_authority(
                        expected_reservations_file=RESERVATIONS,
                        expected_root=fixture.gpu_root,
                    )
            finally:
                close_process_gpu_authority()
                fixture.close()

    def test_direct_mps_client_uses_pipe_descriptor_not_root_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = DescriptorAuthorityFixture(Path(temporary))
            lease = GpuLease(
                lease_id="a" * 32,
                fencing_token=1,
                broker_instance_id="b" * 32,
                kind="execution",
                placement="preferred",
                component="dft",
                environment="dev",
                client_id="test",
                gpu_index=1,
                gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
                memory_mib=4096,
                thread_percent=50,
                preferred=True,
                parent_lease_id="c" * 32,
                status="active",
            )
            pipe_authority = Path(
                f"/proc/{os.getpid()}/fd/{fixture.pipe_fd}"
            )
            try:
                result = mps_client_environment(
                    lease,
                    pipe_root=fixture.gpu_root,
                    pipe_directories={1: pipe_authority},
                )
                self.assertEqual(
                    result["CUDA_MPS_PIPE_DIRECTORY"],
                    str(pipe_authority),
                )
                with self.assertRaisesRegex(
                    GpuBrokerClientError, "lacks"
                ):
                    mps_client_environment(
                        lease,
                        pipe_root=fixture.gpu_root,
                        pipe_directories={},
                    )
            finally:
                fixture.close()

    def test_worker_config_accepts_only_the_same_formal_descriptors(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake_repo = Path(temporary) / "repo"
            runtime = fake_repo / ".runtime"
            fixture = DescriptorAuthorityFixture(runtime)
            config_path = fake_repo / "ops/config"
            config_path.mkdir(parents=True)
            shutil.copy2(
                RESERVATIONS,
                config_path / "gpu-external-reservations.json",
            )
            environment = fixture.environment()
            try:
                with (
                    mock.patch.object(
                        worker_config, "REPO_ROOT", fake_repo
                    ),
                    mock.patch.object(
                        worker_config, "RUNTIME_ROOT", runtime
                    ),
                    mock.patch.dict(
                        os.environ, environment, clear=True
                    ),
                ):
                    self.assertEqual(
                        worker_config.validate_dev_runtime_path(
                            "MONOMER_DFT_GPU_BROKER_UDS",
                            Path(
                                environment[
                                    "NEXPOLY_DFT_GPU_AUTHORITY_ROOT"
                                ]
                            )
                            / "broker.sock",
                            runtime_root=runtime,
                            leaf_kind="socket",
                        ),
                        Path(
                            environment[
                                "NEXPOLY_DFT_GPU_AUTHORITY_ROOT"
                            ]
                        )
                        / "broker.sock",
                    )
                    self.assertEqual(
                        worker_config.validate_dev_runtime_path(
                            "MONOMER_DFT_GPU_MPS_PIPE_ROOT",
                            environment[
                                "NEXPOLY_DFT_GPU_AUTHORITY_ROOT"
                            ],
                            runtime_root=runtime,
                            leaf_kind="directory",
                        ),
                        Path(
                            environment[
                                "NEXPOLY_DFT_GPU_AUTHORITY_ROOT"
                            ]
                        ),
                    )
                    with self.assertRaisesRegex(
                        ValueError, "differs"
                    ):
                        worker_config.validate_dev_runtime_path(
                            "MONOMER_DFT_GPU_MPS_PIPE_ROOT",
                            fixture.gpu_root,
                            runtime_root=runtime,
                            leaf_kind="directory",
                        )
            finally:
                fixture.close()

    def test_preflight_preserves_formal_proc_authority_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake_repo = Path(temporary) / "repo"
            runtime = fake_repo / ".runtime"
            fixture = DescriptorAuthorityFixture(runtime)
            config_path = fake_repo / "ops/config"
            config_path.mkdir(parents=True)
            shutil.copy2(
                RESERVATIONS,
                config_path / "gpu-external-reservations.json",
            )
            private_env = fake_repo / ".env.monomer-dft.dev"
            shutil.copy2(
                ROOT / ".env.monomer-dft.dev.example",
                private_env,
            )
            private_env.chmod(0o600)
            values = preflight.load_env_file(private_env)
            python_path = (
                runtime / "venvs/monomer-dft-worker/bin/python"
            )
            python_path.parent.mkdir(parents=True)
            python_path.write_bytes(b"python")
            for relative in (
                "monomer-dft-worker-socket",
                "monomer-dft-worker-runs",
                "aimnet-cache",
                "warp-cache",
                "uv-cache",
                "aimnet-source-archive",
            ):
                (runtime / relative).mkdir(mode=0o700)
            model_source = fake_repo / "model-source"
            model_source.mkdir()
            values.update(
                {
                    "MONOMER_DFT_PYTHON": str(python_path),
                    "MONOMER_DFT_WORKER_UDS": str(
                        runtime
                        / "monomer-dft-worker-socket/worker.sock"
                    ),
                    "MONOMER_DFT_JOB_ROOT": str(
                        runtime / "monomer-dft-worker-runs"
                    ),
                    "AIMNET_CACHE_DIR": str(runtime / "aimnet-cache"),
                    "WARP_CACHE_PATH": str(runtime / "warp-cache"),
                    "UV_CACHE_DIR": str(runtime / "uv-cache"),
                    "AIMNET_SOURCE_DIR": str(
                        runtime / "aimnet-source-archive"
                    ),
                    "AIMNET_SOURCE_LOCK": str(
                        fake_repo
                        / "workers/monomer_dft_worker/"
                        "aimnet-source.lock.json"
                    ),
                    "AIMNET_MODEL_SOURCE_DIR": str(model_source),
                }
            )
            environment = fixture.environment()
            try:
                with (
                    mock.patch.dict(
                        os.environ, environment, clear=True
                    ),
                    mock.patch.object(
                        preflight.sys, "executable", str(python_path)
                    ),
                    mock.patch.object(
                        preflight.sys, "prefix", str(python_path.parents[1])
                    ),
                    mock.patch.object(
                        preflight.sys, "base_prefix", "/usr"
                    ),
                ):
                    effective, authority = (
                        preflight.effective_environment(
                            fake_repo,
                            values,
                        )
                    )
                    resolved = preflight.validate_environment(
                        fake_repo,
                        effective,
                        authority,
                    )
                expected_root = environment[
                    "NEXPOLY_DFT_GPU_AUTHORITY_ROOT"
                ]
                self.assertEqual(
                    resolved["MONOMER_DFT_GPU_MPS_PIPE_ROOT"],
                    expected_root,
                )
                self.assertEqual(
                    resolved["MONOMER_DFT_GPU_BROKER_UDS"],
                    expected_root + "/broker.sock",
                )
                self.assertEqual(
                    resolved["MONOMER_DFT_GPU_EXTERNAL_RESERVATIONS"],
                    environment[
                        "NEXPOLY_DFT_GPU_RESERVATIONS_AUTHORITY"
                    ],
                )
            finally:
                fixture.close()

    def test_mps_shell_rejects_wrong_hierarchy_before_nvidia(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake_repo = Path(temporary) / "repo"
            fake_scripts = fake_repo / "scripts"
            fake_scripts.mkdir(parents=True)
            fake_control = fake_scripts / "gpu_mps_control.sh"
            shutil.copy2(MPS_CONTROL, fake_control)
            fixture = DescriptorAuthorityFixture(fake_repo / ".runtime")
            external = Path(temporary) / "external"
            external.mkdir(mode=0o700)
            os.mkfifo(external / "control", mode=0o600)
            external_fd = os.open(
                external,
                os.O_RDONLY
                | os.O_CLOEXEC
                | os.O_DIRECTORY
                | os.O_NOFOLLOW,
            )
            marker = Path(temporary) / "nvidia-called"
            fake_bin = Path(temporary) / "bin"
            fake_bin.mkdir()
            fake_nvidia = fake_bin / "nvidia-smi"
            fake_nvidia.write_text(
                f"#!/usr/bin/env bash\n: > {marker}\nexit 99\n",
                encoding="utf-8",
            )
            fake_nvidia.chmod(0o700)
            env = {
                "HOME": os.environ.get("HOME", "/tmp"),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "NEXPOLY_GPU_STATE_ROOT": (
                    f"/proc/{os.getpid()}/fd/{fixture.root_fd}"
                ),
                "NEXPOLY_GPU_EXTERNAL_RESERVATIONS": (
                    f"/proc/{os.getpid()}/fd/{fixture.reservations_fd}"
                ),
                "NEXPOLY_GPU_BROKER_SOCKET": (
                    f"/proc/{os.getpid()}/fd/{fixture.root_fd}/broker.sock"
                ),
                "NEXPOLY_GPU_MPS_SLOT_DIRECTORY": (
                    f"/proc/{os.getpid()}/fd/{fixture.slot_fd}"
                ),
                "NEXPOLY_GPU_MPS_PIPE_DIRECTORY": (
                    f"/proc/{os.getpid()}/fd/{external_fd}"
                ),
                "NEXPOLY_GPU_MPS_LOG_DIRECTORY": (
                    f"/proc/{os.getpid()}/fd/{fixture.log_fd}"
                ),
                "NEXPOLY_GPU_MPS_DESCRIPTOR_AUTHORITY": "1",
                "NEXPOLY_GPU_MPS_AUTHORITY_PID": str(os.getpid()),
                "NEXPOLY_GPU_MPS_AUTHORITY_START_TICKS": (
                    fixture.environment()[
                        "NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS"
                    ]
                ),
                "NEXPOLY_GPU_MPS_EXPECTED_ROOT": str(
                    fixture.gpu_root
                ),
            }
            try:
                completed = subprocess.run(
                    [str(fake_control), "start", "1"],
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertFalse(marker.exists())
            finally:
                os.close(external_fd)
                fixture.close()

    def test_mps_shell_valid_hierarchy_loads_policy_and_starts_mps(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake_repo = Path(temporary) / "repo"
            fake_scripts = fake_repo / "scripts"
            fake_scripts.mkdir(parents=True)
            fake_control = fake_scripts / "gpu_mps_control.sh"
            shutil.copy2(MPS_CONTROL, fake_control)
            fixture = DescriptorAuthorityFixture(fake_repo / ".runtime")
            marker = Path(temporary) / "mps-started"
            fake_bin = Path(temporary) / "bin"
            fake_bin.mkdir()
            fake_nvidia = fake_bin / "nvidia-smi"
            fake_nvidia.write_text(
                (
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    "case \"$*\" in\n"
                    "  *--query-gpu=uuid,compute_mode*)\n"
                    "    printf '%s\\n' "
                    "'GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771, "
                    "Exclusive_Process'\n"
                    "    ;;\n"
                    "  *--query-compute-apps=*) exit 0 ;;\n"
                    "  *) exit 91 ;;\n"
                    "esac\n"
                ),
                encoding="utf-8",
            )
            fake_nvidia.chmod(0o700)
            fake_mps = fake_bin / "nvidia-cuda-mps-control"
            fake_mps.write_text(
                (
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    "if [[ \"${1:-}\" == '-d' ]]; then\n"
                    f"  printf '%s\\n%s\\n' \"$CUDA_MPS_PIPE_DIRECTORY\" "
                    f"\"$CUDA_MPS_LOG_DIRECTORY\" > {marker}\n"
                    "  exit 0\n"
                    "fi\n"
                    "cat >/dev/null\n"
                ),
                encoding="utf-8",
            )
            fake_mps.chmod(0o700)
            for name in ("docker", "systemctl"):
                command = fake_bin / name
                command.write_text(
                    "#!/usr/bin/env bash\nexit 0\n",
                    encoding="utf-8",
                )
                command.chmod(0o700)
            authority_environment = fixture.environment()
            env = {
                "HOME": os.environ.get("HOME", "/tmp"),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "NEXPOLY_GPU_STATE_ROOT": (
                    f"/proc/{os.getpid()}/fd/{fixture.root_fd}"
                ),
                "NEXPOLY_GPU_EXTERNAL_RESERVATIONS": (
                    f"/proc/{os.getpid()}/fd/{fixture.reservations_fd}"
                ),
                "NEXPOLY_GPU_BROKER_SOCKET": (
                    f"/proc/{os.getpid()}/fd/{fixture.root_fd}/broker.sock"
                ),
                "NEXPOLY_GPU_MPS_SLOT_DIRECTORY": (
                    f"/proc/{os.getpid()}/fd/{fixture.slot_fd}"
                ),
                "NEXPOLY_GPU_MPS_PIPE_DIRECTORY": (
                    f"/proc/{os.getpid()}/fd/{fixture.pipe_fd}"
                ),
                "NEXPOLY_GPU_MPS_LOG_DIRECTORY": (
                    f"/proc/{os.getpid()}/fd/{fixture.log_fd}"
                ),
                "NEXPOLY_GPU_MPS_DESCRIPTOR_AUTHORITY": "1",
                "NEXPOLY_GPU_MPS_AUTHORITY_PID": str(os.getpid()),
                "NEXPOLY_GPU_MPS_AUTHORITY_START_TICKS": (
                    authority_environment[
                        "NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS"
                    ]
                ),
                "NEXPOLY_GPU_MPS_EXPECTED_ROOT": str(
                    fixture.gpu_root
                ),
            }
            try:
                completed = subprocess.run(
                    [str(fake_control), "start", "1"],
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                    pass_fds=(
                        fixture.root_fd,
                        fixture.reservations_fd,
                        fixture.slot_fd,
                        fixture.pipe_fd,
                        fixture.log_fd,
                    ),
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    msg=completed.stderr,
                )
                self.assertTrue(marker.exists())
                pipe_path, log_path = marker.read_text(
                    encoding="utf-8"
                ).splitlines()
                self.assertEqual(
                    pipe_path,
                    f"/proc/self/fd/{fixture.pipe_fd}",
                )
                self.assertEqual(
                    log_path,
                    f"/proc/self/fd/{fixture.log_fd}",
                )
            finally:
                fixture.close()

    def test_formal_mps_descriptor_authority_rejects_gpu2_before_nvidia(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake_repo = Path(temporary) / "repo"
            fake_scripts = fake_repo / "scripts"
            fake_scripts.mkdir(parents=True)
            fake_control = fake_scripts / "gpu_mps_control.sh"
            shutil.copy2(MPS_CONTROL, fake_control)
            fixture = DescriptorAuthorityFixture(fake_repo / ".runtime")
            marker = Path(temporary) / "nvidia-called"
            fake_bin = Path(temporary) / "bin"
            fake_bin.mkdir()
            fake_nvidia = fake_bin / "nvidia-smi"
            fake_nvidia.write_text(
                f"#!/usr/bin/env bash\n: > {marker}\nexit 99\n",
                encoding="utf-8",
            )
            fake_nvidia.chmod(0o700)
            authority_environment = fixture.environment()
            env = {
                "HOME": os.environ.get("HOME", "/tmp"),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "NEXPOLY_GPU_STATE_ROOT": (
                    f"/proc/{os.getpid()}/fd/{fixture.root_fd}"
                ),
                "NEXPOLY_GPU_EXTERNAL_RESERVATIONS": (
                    f"/proc/{os.getpid()}/fd/{fixture.reservations_fd}"
                ),
                "NEXPOLY_GPU_BROKER_SOCKET": (
                    f"/proc/{os.getpid()}/fd/{fixture.root_fd}/broker.sock"
                ),
                "NEXPOLY_GPU_MPS_SLOT_DIRECTORY": (
                    f"/proc/{os.getpid()}/fd/{fixture.slot_fd}"
                ),
                "NEXPOLY_GPU_MPS_PIPE_DIRECTORY": (
                    f"/proc/{os.getpid()}/fd/{fixture.pipe_fd}"
                ),
                "NEXPOLY_GPU_MPS_LOG_DIRECTORY": (
                    f"/proc/{os.getpid()}/fd/{fixture.log_fd}"
                ),
                "NEXPOLY_GPU_MPS_DESCRIPTOR_AUTHORITY": "1",
                "NEXPOLY_GPU_MPS_AUTHORITY_PID": str(os.getpid()),
                "NEXPOLY_GPU_MPS_AUTHORITY_START_TICKS": (
                    authority_environment[
                        "NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS"
                    ]
                ),
                "NEXPOLY_GPU_MPS_EXPECTED_ROOT": str(
                    fixture.gpu_root
                ),
            }
            try:
                completed = subprocess.run(
                    [str(fake_control), "start", "2"],
                    env=env,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("forbids production GPU2", completed.stderr)
                self.assertFalse(marker.exists())
            finally:
                fixture.close()


if __name__ == "__main__":
    unittest.main()
