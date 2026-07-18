from __future__ import annotations

from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ACCEPTANCE = load_module(
    "test_monomer_dft_gpu_acceptance_contract",
    ROOT / "scripts/monomer_dft_gpu_acceptance.py",
)
RUNTIME = load_module(
    "test_monomer_dft_runtime_contract_for_gpu",
    ROOT / "scripts/monomer_dft_runtime_contract.py",
)
HARNESS = load_module(
    "test_run_monomer_dft_gpu_acceptance",
    ROOT / "scripts/run_monomer_dft_gpu_acceptance.py",
)

AUTHORITY = {"sha": "a" * 40, "tree": "b" * 40}
BRIDGE = {"sha": "c" * 40, "tree": "d" * 40}


def digest(character: str) -> str:
    return "sha256:" + character * 64


def image(role: str, character: str) -> dict[str, str]:
    index = digest(character)
    return {
        "role": role,
        "digest_ref": f"ghcr.io/example/{role}@{index}",
        "index_digest": index,
        "platform_digest": digest("7"),
        "image_id": digest("8"),
        "revision": AUTHORITY["sha"],
        "source": "https://github.com/example/repository",
        "version": f"sha-{AUTHORITY['sha']}",
    }


IMAGES = {"backend": image("backend", "1"), "web": image("web", "2")}


def runtime_evidence() -> dict[str, object]:
    contract = RUNTIME.RUNTIME_CONTRACT
    return {
        "contract_sha256": RUNTIME.RUNTIME_CONTRACT_SHA256,
        "python_version": "3.12.3",
        "uv_version": contract["uv_version"],
        "build_lock_sha256": contract["build_lock_sha256"],
        "source": {
            "commit": contract["source"]["commit"],
            "tree": contract["source"]["tree"],
            "archive_sha256": contract["source"]["archive_inventory_sha256"],
        },
        "wheel": {
            key: contract["wheel"][key]
            for key in ("filename", "sha256", "inventory_sha256", "record_sha256")
        },
        "model_registry_sha256": contract["registry_sha256"],
        "models_sha256": contract["models_sha256"],
    }


def report(*, gpu3_mode: str = "externally_fenced") -> dict[str, object]:
    snapshot = {
        "index": 2,
        "uuid": ACCEPTANCE.GPU_UUIDS["2"],
        "memory_used_mib": 128,
        "compute_processes": [
            {
                "pid": 42,
                "process_start_ticks": 123,
                "process_name": "nvidia-cuda-mps-server",
                "used_memory_mib": 64,
            }
        ],
    }
    gpu3: dict[str, object]
    if gpu3_mode == "actual":
        gpu3 = {
            "index": 3,
            "uuid": ACCEPTANCE.GPU_UUIDS["3"],
            "mode": "actual",
            "cuda_started": True,
            "fencing_verified": True,
            "evidence_sha256": digest("9"),
        }
    else:
        gpu3 = {
            "index": 3,
            "uuid": ACCEPTANCE.GPU_UUIDS["3"],
            "mode": "externally_fenced",
            "cuda_started": False,
            "fencing_verified": True,
            "evidence_sha256": digest("9"),
            "reservations_sha256": ACCEPTANCE.EXTERNAL_RESERVATIONS_SHA256,
            "blocked_reason": ACCEPTANCE.GPU3_BLOCKED_REASON,
            "claim": {
                "kind": "docker",
                "container_id": "f" * 64,
                "container_name": "foreign-gpu3",
                "device_request_sha256": digest("a"),
            },
            "rejection": {
                "code": "gpu_capacity_unavailable",
                "gpu_index": 3,
                "gpu_uuid": ACCEPTANCE.GPU_UUIDS["3"],
                "placement": "overflow",
                "broker_report_sha256": digest("b"),
            },
        }
    value = {
        "schema_version": 1,
        "status": "passed",
        "captured_at": "2026-07-18T00:00:00Z",
        "authority": dict(AUTHORITY),
        "bridge": dict(BRIDGE),
        "images": deepcopy(IMAGES),
        "runtime": runtime_evidence(),
        "coverage": {
            "direct_science": {
                "status": "passed",
                "gpu_index": 1,
                "gpu_uuid": ACCEPTANCE.GPU_UUIDS["1"],
                "properties": ["energy", "forces", "hessian"],
                "energy_eV": -76.0,
                "max_force_eV_per_A": 0.01,
                "hessian_symmetry_max_abs_eV_per_A2": 0.0001,
                "report_sha256": digest("c"),
            },
            "broker_uds_backend_e2e": {
                "status": "passed",
                "transport": "broker+uds+backend",
                "gpu_indices": [1],
                "overflow_test_status": (
                    "passed" if gpu3_mode == "actual" else "externally_fenced"
                ),
                "completed_job_id": "11111111-1111-4111-8111-111111111111",
                "cancelled_job_id": "22222222-2222-4222-8222-222222222222",
                "submit": True,
                "poll": True,
                "cancel": True,
                "journal": True,
                "artifact": True,
                "bundle": True,
                "fencing": True,
                "completed_journal_sha256": digest("d"),
                "cancelled_journal_sha256": digest("e"),
                "artifact_sha256": digest("f"),
                "bundle_sha256": digest("1"),
                "provenance_sha256": digest("2"),
            },
        },
        "gpus": {
            "1": {
                "index": 1,
                "uuid": ACCEPTANCE.GPU_UUIDS["1"],
                "mode": "actual",
                "cuda_started": True,
                "fencing_verified": True,
                "evidence_sha256": digest("3"),
            },
            "2": {
                "index": 2,
                "uuid": ACCEPTANCE.GPU_UUIDS["2"],
                "mode": "unchanged",
                "cuda_started": False,
                "before": deepcopy(snapshot),
                "after": deepcopy(snapshot),
                "processes_unchanged": True,
                "memory_unchanged": True,
            },
            "3": gpu3,
        },
    }
    return ACCEPTANCE.seal_report(value)


def validate(value: object) -> dict[str, object]:
    return ACCEPTANCE.validate_report(
        value,
        authority=AUTHORITY,
        bridge=BRIDGE,
        authority_images=IMAGES,
        runtime_contract=RUNTIME.RUNTIME_CONTRACT,
        runtime_contract_sha256=RUNTIME.RUNTIME_CONTRACT_SHA256,
    )


class GpuAcceptanceContractTests(unittest.TestCase):
    def test_accepts_actual_and_exact_external_fence_modes(self) -> None:
        self.assertEqual(validate(report())["status"], "passed")
        self.assertEqual(validate(report(gpu3_mode="actual"))["status"], "passed")

    def test_skipped_gpu3_is_never_success(self) -> None:
        value = report()
        value["gpus"]["3"] = {  # type: ignore[index]
            "index": 3,
            "uuid": ACCEPTANCE.GPU_UUIDS["3"],
            "mode": "skipped",
        }
        value = ACCEPTANCE.seal_report(value)
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "actual or externally_fenced",
        ):
            validate(value)

    def test_gpu2_process_or_memory_drift_is_rejected(self) -> None:
        for field in ("memory_used_mib",):
            value = report()
            value["gpus"]["2"]["after"][field] += 1  # type: ignore[index]
            value = ACCEPTANCE.seal_report(value)
            with self.subTest(field=field), self.assertRaisesRegex(
                ACCEPTANCE.GpuAcceptanceError,
                "GPU2 changed",
            ):
                validate(value)

        value = report()
        value["gpus"]["2"]["after"]["compute_processes"][0][  # type: ignore[index]
            "process_start_ticks"
        ] += 1
        value = ACCEPTANCE.seal_report(value)
        with self.assertRaisesRegex(ACCEPTANCE.GpuAcceptanceError, "GPU2 changed"):
            validate(value)

    def test_missing_e2e_coverage_and_false_fencing_are_rejected(self) -> None:
        for field in ("cancel", "journal", "artifact", "bundle", "fencing"):
            value = report()
            value["coverage"]["broker_uds_backend_e2e"][field] = False  # type: ignore[index]
            value = ACCEPTANCE.seal_report(value)
            with self.subTest(field=field), self.assertRaises(
                ACCEPTANCE.GpuAcceptanceError
            ):
                validate(value)

    def test_backend_e2e_never_claims_direct_gpu3_provenance(self) -> None:
        value = report(gpu3_mode="actual")
        value["coverage"]["broker_uds_backend_e2e"]["gpu_indices"] = [1, 3]  # type: ignore[index]
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "acceptance binding is incomplete",
        ):
            validate(ACCEPTANCE.seal_report(value))

        value = report()
        value["coverage"]["broker_uds_backend_e2e"]["gpu_indices"] = [1, 3]  # type: ignore[index]
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "acceptance binding is incomplete",
        ):
            validate(ACCEPTANCE.seal_report(value))

    def test_runtime_image_claim_and_seal_drift_are_rejected(self) -> None:
        mutations = []
        value = report()
        value["runtime"]["source"]["archive_sha256"] = digest("0")  # type: ignore[index]
        mutations.append(ACCEPTANCE.seal_report(value))
        value = report()
        value["images"]["backend"]["index_digest"] = digest("0")  # type: ignore[index]
        mutations.append(ACCEPTANCE.seal_report(value))
        value = report()
        value["gpus"]["3"]["reservations_sha256"] = digest("0")  # type: ignore[index]
        mutations.append(ACCEPTANCE.seal_report(value))
        value = report()
        value["status"] = "failed"
        mutations.append(value)  # deliberately do not reseal
        for index, mutated in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(
                ACCEPTANCE.GpuAcceptanceError
            ):
                validate(mutated)


class GpuAcceptanceHarnessCpuTests(unittest.TestCase):
    def test_direct_runner_enters_exact_scope_before_registration_and_gate(
        self,
    ) -> None:
        events: list[str] = []
        lease_id = "a1" * 16
        registered = SimpleNamespace(
            lease_id=lease_id,
            fencing_token=7,
            gpu_index=1,
            gpu_uuid=HARNESS.GPU_UUIDS["1"],
        )

        class Managed:
            lease = registered

            def register_workload(self, pid):
                self.assert_pid(pid)
                events.append("register")
                return registered

            def confirm_current(self):
                events.append("confirm")
                return registered

            def close(self):
                events.append("close")

            @staticmethod
            def assert_pid(pid):
                assert pid == 54_321

        managed = Managed()

        class Broker:
            def __init__(self, socket_path):
                self.socket_path = socket_path

            def acquire_managed(self, **kwargs):
                self.kwargs = kwargs
                events.append("acquire")
                return managed

        captured: dict[str, object] = {}
        output_path: Path | None = None

        class Process:
            pid = 54_321
            returncode: int | None = None

            def communicate(self, *, timeout):
                assert timeout == 600.0
                assert output_path is not None
                events.append("communicate")
                HARNESS._write_private_json(output_path, {"status": "ok"})
                self.returncode = 0
                return b"", b""

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                del timeout
                self.returncode = 0
                return 0

        def popen(command, **kwargs):
            captured["command"] = tuple(command)
            captured["kwargs"] = kwargs
            events.append("popen")
            return Process()

        def wait_for_scope(pid, exact_lease_id):
            assert pid == 54_321
            assert exact_lease_id == lease_id
            events.append("scope")
            return 98_765

        def open_gate(descriptor, payload):
            assert descriptor > 0
            assert payload == b"1"
            events.append("gate")
            return 1

        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary)
            output_path = run_directory / "direct-gpu1-result.json"
            with (
                mock.patch("gpu_resource.GpuBrokerClient", Broker),
                mock.patch(
                    "gpu_resource.mps_client_environment",
                    return_value={
                        "CUDA_VISIBLE_DEVICES": HARNESS.GPU_UUIDS["1"],
                    },
                ),
                mock.patch(
                    "gpu_resource.wait_for_scope_membership",
                    side_effect=wait_for_scope,
                ),
                mock.patch.object(HARNESS.subprocess, "Popen", side_effect=popen),
                mock.patch.object(HARNESS.os, "write", side_effect=open_gate),
            ):
                result, evidence = HARNESS.run_leased_direct(
                    resolved={
                        "MONOMER_DFT_PYTHON": os.fspath(Path(os.sys.executable)),
                        "MONOMER_DFT_GPU_BROKER_UDS": "/private/broker.sock",
                        "MONOMER_DFT_GPU_MPS_PIPE_ROOT": "/private/mps",
                    },
                    default_model_path="/private/model.pt",
                    gpu_index="1",
                    placement="preferred",
                    run_directory=run_directory,
                )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(evidence["process_start_ticks"], 98_765)
        self.assertEqual(
            events,
            [
                "acquire",
                "popen",
                "scope",
                "register",
                "gate",
                "communicate",
                "confirm",
                "close",
            ],
        )
        command = captured["command"]
        assert isinstance(command, tuple)
        self.assertEqual(command[0], "/usr/bin/systemd-run")
        self.assertIn(f"--unit=nexpoly-gpu-job-{lease_id}.scope", command)
        self.assertLess(
            command.index(os.fspath(ROOT / "gpu_resource/exec_gate.py")),
            command.index(
                os.fspath(ROOT / "scripts/run_monomer_dft_gpu_acceptance.py")
            ),
        )
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertIn("NEXPOLY_GPU_EXEC_GATE_FD", kwargs["env"])

    def test_registered_cleanup_prepares_mps_before_any_host_signal(self) -> None:
        events: list[str] = []

        class Process:
            pid = 123

            @staticmethod
            def poll():
                return None

            @staticmethod
            def wait(timeout=None):
                events.append(f"wait:{timeout}")
                return 0

        class Managed:
            @staticmethod
            def prepare_process_termination():
                events.append("prepare")

        with mock.patch.object(
            HARNESS.os,
            "killpg",
            side_effect=lambda *_args: events.append("signal"),
        ):
            HARNESS._cleanup_failed_direct_process(
                Process(),
                registered=True,
                managed=Managed(),
            )

        self.assertEqual(events, ["prepare", "wait:10.0"])

    def test_unproven_mps_cleanup_never_sends_a_host_signal(self) -> None:
        class Process:
            pid = 123

            @staticmethod
            def poll():
                return None

        class Managed:
            @staticmethod
            def prepare_process_termination():
                raise RuntimeError("MPS query failed")

        with (
            mock.patch.object(HARNESS.os, "killpg") as kill,
            self.assertRaisesRegex(
                HARNESS.AcceptanceHarnessError,
                "scope remains fail-closed",
            ),
        ):
            HARNESS._cleanup_failed_direct_process(
                Process(),
                registered=True,
                managed=Managed(),
            )
        kill.assert_not_called()

    def test_docker_claim_binds_container_and_device_request(self) -> None:
        inspection = [
            {
                "Id": "a" * 64,
                "Name": "/polyprop-backend-gpu-1",
                "HostConfig": {
                    "DeviceRequests": [
                        {
                            "Driver": "nvidia",
                            "DeviceIDs": ["3"],
                            "Capabilities": [["gpu"]],
                            "Options": {},
                        }
                    ]
                },
            }
        ]

        def fake_run(*command, **_kwargs):
            output = (
                "abc\n"
                if command[:3] == ("docker", "ps", "-q")
                else __import__("json").dumps(inspection)
            )
            return __import__("subprocess").CompletedProcess(command, 0, output, "")

        with mock.patch.object(HARNESS, "_run", side_effect=fake_run):
            claim = HARNESS._docker_gpu3_claim()
        assert claim is not None
        self.assertEqual(claim["container_id"], "a" * 64)
        self.assertEqual(claim["container_name"], "polyprop-backend-gpu-1")
        self.assertTrue(claim["device_request_sha256"].startswith("sha256:"))

    def test_ambiguous_docker_gpu_request_fails_closed(self) -> None:
        inspection = [
            {
                "Id": "a" * 64,
                "Name": "/ambiguous-gpu",
                "HostConfig": {
                    "DeviceRequests": [
                        {
                            "Driver": "nvidia",
                            "Count": 1,
                            "DeviceIDs": [],
                            "Capabilities": [["gpu"]],
                        }
                    ]
                },
            }
        ]

        def fake_run(*command, **_kwargs):
            output = (
                "abc\n"
                if command[:3] == ("docker", "ps", "-q")
                else __import__("json").dumps(inspection)
            )
            return subprocess.CompletedProcess(command, 0, output, "")

        with (
            mock.patch.object(HARNESS, "_run", side_effect=fake_run),
            self.assertRaisesRegex(
                HARNESS.AcceptanceHarnessError,
                "does not identify exact devices",
            ),
        ):
            HARNESS._docker_gpu3_claim()

    def test_cancellation_target_must_reach_running(self) -> None:
        responses = iter(({"status": "queued"}, {"status": "running"}))
        with (
            mock.patch.object(
                HARNESS,
                "_json_response",
                side_effect=lambda *_args, **_kwargs: next(responses),
            ),
            mock.patch.object(HARNESS.time, "sleep"),
        ):
            result = HARNESS._wait_job_running("http://backend", "job-1", 10.0)
        self.assertEqual(result["status"], "running")

        with (
            mock.patch.object(
                HARNESS,
                "_json_response",
                return_value={"status": "completed"},
            ),
            self.assertRaisesRegex(
                HARNESS.AcceptanceHarnessError,
                "before active cancellation",
            ),
        ):
            HARNESS._wait_job_running("http://backend", "job-2", 10.0)

    def test_gpu2_snapshot_is_canonical_and_uses_process_start_identity(self) -> None:
        outputs = iter(
            (
                "2, GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe, 512\n",
                (
                    "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe,"
                    "42,nvidia-cuda-mps-server,128\n"
                ),
            )
        )

        def fake_run(*command, **_kwargs):
            return __import__("subprocess").CompletedProcess(
                command, 0, next(outputs), ""
            )

        with (
            mock.patch.object(HARNESS, "_run", side_effect=fake_run),
            mock.patch.object(HARNESS, "_read_proc_start_ticks", return_value=999),
        ):
            snapshot = HARNESS.snapshot_gpu2()
        self.assertEqual(snapshot["memory_used_mib"], 512)
        self.assertEqual(
            snapshot["compute_processes"][0]["process_start_ticks"],
            999,
        )


if __name__ == "__main__":
    unittest.main()
