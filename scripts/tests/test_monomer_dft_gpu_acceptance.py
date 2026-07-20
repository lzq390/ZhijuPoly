from __future__ import annotations

import contextlib
from copy import deepcopy
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import socket
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


def gpu3_direct_result() -> dict[str, object]:
    return {
        "status": "ok",
        "preflight": {
            "default_model_path": (
                "/private/aimnet2_wb97m_d3_0.pt"
            )
        },
        "model": "aimnet2",
        "model_sha256": ACCEPTANCE.AIMNET2_MODEL_SHA256,
        "device": "cuda:0",
        "water": {
            "numbers": [8, 1, 1],
            "coordinates_angstrom": deepcopy(
                ACCEPTANCE.WATER_COORDINATES_ANGSTROM
            ),
            "energy_eV": -76.0,
            "charge_sum_e": 0.0,
            "max_force_eV_per_A": 0.01,
            "forces_shape": [3, 3],
            "hessian_shape": [3, 3, 3, 3],
            "hessian_symmetry_max_abs_eV_per_A2": 0.0001,
        },
        "ase": {
            "energy_eV": -76.0,
            "max_force_eV_per_A": 0.01,
        },
        "cco": {
            "smiles": "CCO",
            "canonical_smiles": "CCO",
            "seed": 1,
            "rdkit_max_iters": 500,
            "force_field": "MMFF94",
            "atom_count": 9,
            "energy_eV": ACCEPTANCE.CCO_BASELINE_EV + 0.0005,
            "baseline_eV": ACCEPTANCE.CCO_BASELINE_EV,
            "delta_eV": 0.0005,
            "tolerance_eV": ACCEPTANCE.CCO_TOLERANCE_EV,
        },
        "elapsed_seconds": 1.25,
        "gpu_index": 3,
        "gpu_uuid": ACCEPTANCE.GPU_UUIDS["3"],
    }


def gpu3_actual_lease(
    *,
    broker_instance_id: str,
    result: dict[str, object],
) -> dict[str, object]:
    lease_id = "a1" * 16
    uid = 1001
    return {
        "lease_id": lease_id,
        "fencing_token": 7,
        "broker_instance_id": broker_instance_id,
        "gpu_index": 3,
        "gpu_uuid": ACCEPTANCE.GPU_UUIDS["3"],
        "request_id": "dft-acceptance-3-" + "f" * 32,
        "workload_pid": 54_321,
        "process_start_ticks": 98_765,
        "workload_cgroup": (
            f"/user.slice/user-{uid}.slice/user@{uid}.service/"
            "nexpoly.slice/nexpoly-gpu.slice/nexpoly-gpu-jobs.slice/"
            f"nexpoly-gpu-job-{lease_id}.scope"
        ),
        "report_sha256": ACCEPTANCE.canonical_json_file_digest(result),
        "model_copy_sha256": ACCEPTANCE.AIMNET2_MODEL_SHA256,
        "model_copy_path_sha256": ACCEPTANCE.canonical_json_digest(
            {"path": result["preflight"]["default_model_path"]}  # type: ignore[index]
        ),
        "model_copy_removed": True,
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
    worker_instance = "1" * 32
    broker_instance = "2" * 32
    gpu3: dict[str, object]
    if gpu3_mode == "actual":
        direct_result = gpu3_direct_result()
        direct_lease = gpu3_actual_lease(
            broker_instance_id=broker_instance,
            result=direct_result,
        )
        gpu3 = {
            "index": 3,
            "uuid": ACCEPTANCE.GPU_UUIDS["3"],
            "mode": "actual",
            "cuda_started": True,
            "fencing_verified": True,
            "result": direct_result,
            "lease": direct_lease,
            "evidence_sha256": ACCEPTANCE.canonical_json_digest(
                {"result": direct_result, "lease": direct_lease}
            ),
        }
    else:
        claim = {
            "kind": "docker",
            "container_id": "f" * 64,
            "container_name": "foreign-gpu3",
            "device_request_sha256": digest("a"),
            "inspection_before_sha256": digest("b"),
            "inspection_after_sha256": digest("b"),
            "observed_before_at": "2026-07-18T00:00:00.250000Z",
            "observed_after_at": "2026-07-18T00:00:00.750000Z",
        }
        broker_status = {
            "schema_version": 1,
            "broker_instance_id": broker_instance,
            "draining": False,
            "gpu3_uuid": ACCEPTANCE.GPU_UUIDS["3"],
            "gpu3_usage_mib": 0,
            "gpu3_lease_ids": [],
            "gpu3_quarantined": False,
            "waiters": 0,
        }
        rejection = {
            "code": "gpu_capacity_unavailable",
            "gpu_index": 3,
            "gpu_uuid": ACCEPTANCE.GPU_UUIDS["3"],
            "placement": "overflow",
            "request_id": "dft-acceptance-gpu3-reject-" + "a" * 32,
            "blocked_reason": ACCEPTANCE.GPU3_BLOCKED_REASON,
            "broker_instance_id": broker_instance,
            "before_status": dict(broker_status),
            "before_status_sha256": ACCEPTANCE.canonical_json_digest(
                broker_status
            ),
            "after_status": dict(broker_status),
            "after_status_sha256": ACCEPTANCE.canonical_json_digest(
                broker_status
            ),
            "claim_sha256": ACCEPTANCE.canonical_json_digest(claim),
        }
        rejection["broker_report_sha256"] = ACCEPTANCE.canonical_json_digest(
            rejection
        )
        gpu3 = {
            "index": 3,
            "uuid": ACCEPTANCE.GPU_UUIDS["3"],
            "mode": "externally_fenced",
            "cuda_started": False,
            "fencing_verified": True,
            "evidence_sha256": ACCEPTANCE.canonical_json_digest(
                {
                    "claim": claim,
                    "rejection": rejection,
                    "blocked_reason": ACCEPTANCE.GPU3_BLOCKED_REASON,
                }
            ),
            "reservations_sha256": ACCEPTANCE.EXTERNAL_RESERVATIONS_SHA256,
            "blocked_reason": ACCEPTANCE.GPU3_BLOCKED_REASON,
            "claim": claim,
            "rejection": rejection,
        }
    process = {
        "pid": 4321,
        "process_start_ticks": 12345,
        "cwd": str(ROOT),
        "command_sha256": digest("4"),
    }
    production = dict(ACCEPTANCE.PRODUCTION_BASELINE_SNAPSHOT)
    science = {
        "status": "passed",
        "gpu_index": 1,
        "gpu_uuid": ACCEPTANCE.GPU_UUIDS["1"],
        "properties": ["energy", "forces", "hessian"],
        "completed_job_id": "11111111-1111-4111-8111-111111111111",
        "worker_instance_id": worker_instance,
        "execution_path": "primary",
        "parent_lease_id": "a" * 32,
        "lease_id": "b" * 32,
        "fencing_token": 7,
        "broker_instance_id": broker_instance,
        "atom_count": 3,
        "atomic_numbers": [8, 1, 1],
        "energy_eV": -76.0,
        "forces_shape": [3, 3],
        "max_force_eV_per_A": 0.01,
        "hessian_shape": [9, 9],
        "hessian_symmetry_max_abs_eV_per_A2": 0.0001,
        "hessian_symmetry_relative_error": 0.00001,
        "hessian_symmetric_within_tolerance": True,
        "scientific_result_sha256": digest("6"),
        "hessian_artifact_sha256": digest("7"),
        "artifact_manifest_sha256": digest("8"),
        "bundle_manifest_sha256": digest("9"),
        "bundle_sha256": digest("0"),
        "completed_journal_sha256": digest("a"),
        "provenance_sha256": digest("b"),
        "aimnet_commit": RUNTIME.RUNTIME_CONTRACT["source"]["commit"],
        "aimnet_wheel_sha256": RUNTIME.RUNTIME_CONTRACT["wheel"]["sha256"],
        "model_sha256": ACCEPTANCE.AIMNET2_MODEL_SHA256,
        "model_registry_key": ACCEPTANCE.AIMNET2_MODEL_REGISTRY_KEY,
    }
    value = {
        "schema_version": 1,
        "status": "passed",
        "run_kind": "final-main",
        "captured_at": "2026-07-18T00:00:02Z",
        "authority": dict(AUTHORITY),
        "bridge": dict(BRIDGE),
        "images": deepcopy(IMAGES),
        "runtime": runtime_evidence(),
        "control_plane": {
            "mode": "fresh_exact_f",
            "image_mode": "published_exact",
            "project_name": "nexpoly_dft_fresh_aaaaaaaa_1234",
            "authority": dict(AUTHORITY),
            "broker": {
                "instance_id": broker_instance,
                "process": deepcopy(process),
                "initial_leases": [],
                "final_leases": [],
                "socket_sha256": digest("d"),
            },
            "worker": {
                "instance_id": worker_instance,
                "process": deepcopy(process),
                "authority_sha": AUTHORITY["sha"],
                "fresh": True,
            },
            "containers": {
                "project_name": "nexpoly_dft_fresh_aaaaaaaa_1234",
                **{
                    role: {
                        "container_id": character * 64,
                        "image_id": IMAGES[role]["image_id"],
                        "digest_ref": IMAGES[role]["digest_ref"],
                        "index_digest": IMAGES[role]["index_digest"],
                        "platform_digest": IMAGES[role]["platform_digest"],
                        "source_revision": IMAGES[role]["revision"],
                        "source": IMAGES[role]["source"],
                        "version": IMAGES[role]["version"],
                        "started_at": "2026-07-18T00:00:00Z",
                    }
                    for role, character in (("backend", "a"), ("web", "b"))
                },
                "postgres": {
                    "container_id": "c" * 64,
                    "image_id": digest("c"),
                    "source_revision": None,
                    "started_at": "2026-07-18T00:00:00Z",
                },
            },
            "cleanup": {
                "worker_stopped": True,
                "containers_removed": True,
                "volume_removed": True,
                "network_removed": True,
                "broker_drained": True,
                    "broker_stopped": True,
                    "mps_indices_stopped": (
                        [1, 3] if gpu3_mode == "actual" else [1]
                    ),
                    "leases_empty": True,
                    "candidate_image_tags": [],
                    "candidate_image_tags_sha256": (
                        ACCEPTANCE.canonical_json_digest([])
                    ),
                    "candidate_images_absent_before": True,
                    "candidate_images_removed": True,
                    "ordinary_dev_images_before_sha256": digest("e"),
                    "ordinary_dev_images_after_sha256": digest("e"),
                    "ordinary_dev_images_unchanged": True,
                },
        },
        "production_cas": {
            "before": deepcopy(production),
            "after": deepcopy(production),
            "unchanged": True,
        },
        "coverage": {
            "broker_science": science,
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
                "fresh_worker_instance_id": worker_instance,
                "cancelled_journal_sha256": digest("e"),
            },
        },
        "gpus": {
            "1": {
                "index": 1,
                "uuid": ACCEPTANCE.GPU_UUIDS["1"],
                "mode": "actual",
                "cuda_started": True,
                "fencing_verified": True,
                "evidence_sha256": ACCEPTANCE.canonical_json_digest(science),
            },
            "2": {
                "index": 2,
                "uuid": ACCEPTANCE.GPU_UUIDS["2"],
                "mode": "unchanged",
                "cuda_started": False,
                "before": deepcopy(snapshot),
                "after": deepcopy(snapshot),
                "audit": {
                    "started_at": "2026-07-18T00:00:00Z",
                    "finished_at": "2026-07-18T00:00:01Z",
                    "interval_ms": 250,
                    "sample_count": 5,
                    "samples": [deepcopy(snapshot) for _ in range(5)],
                    "sampled_at": [
                        "2026-07-18T00:00:00Z",
                        "2026-07-18T00:00:00.250000Z",
                        "2026-07-18T00:00:00.500000Z",
                        "2026-07-18T00:00:00.750000Z",
                        "2026-07-18T00:00:01Z",
                    ],
                    "samples_sha256": ACCEPTANCE.canonical_json_digest(
                        [snapshot for _ in range(5)]
                    ),
                    "drift_detected": False,
                },
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
        observed_at=dt.datetime(2026, 7, 18, 0, 5, tzinfo=dt.UTC),
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

    def test_gpu_evidence_digests_and_actual_lease_are_content_bound(
        self,
    ) -> None:
        value = report()
        value["gpus"]["1"]["evidence_sha256"] = digest("0")  # type: ignore[index]
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "fresh exact-F runtime",
        ):
            validate(ACCEPTANCE.seal_report(value))

    def test_external_rejection_retains_and_binds_recomputable_broker_state(
        self,
    ) -> None:
        def reseal(value: dict[str, object]) -> dict[str, object]:
            gpu3 = value["gpus"]["3"]  # type: ignore[index]
            rejection = gpu3["rejection"]
            rejection["broker_report_sha256"] = (
                ACCEPTANCE.canonical_json_digest(
                    {
                        key: rejection[key]
                        for key in rejection
                        if key != "broker_report_sha256"
                    }
                )
            )
            gpu3["evidence_sha256"] = ACCEPTANCE.canonical_json_digest(
                {
                    "claim": gpu3["claim"],
                    "rejection": rejection,
                    "blocked_reason": gpu3["blocked_reason"],
                }
            )
            return ACCEPTANCE.seal_report(value)

        value = report()
        rejection = value["gpus"]["3"]["rejection"]  # type: ignore[index]
        rejection["before_status"]["waiters"] = 1
        rejection["before_status_sha256"] = ACCEPTANCE.canonical_json_digest(
            rejection["before_status"]
        )
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "unblocked GPU3 projection",
        ):
            validate(reseal(value))

        value = report()
        rejection = value["gpus"]["3"]["rejection"]  # type: ignore[index]
        other_broker = "3" * 32
        rejection["broker_instance_id"] = other_broker
        for side in ("before", "after"):
            rejection[f"{side}_status"]["broker_instance_id"] = other_broker
            rejection[f"{side}_status_sha256"] = (
                ACCEPTANCE.canonical_json_digest(
                    rejection[f"{side}_status"]
                )
            )
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "fresh Broker instance",
        ):
            validate(reseal(value))

        value = report()
        claim = value["gpus"]["3"]["claim"]  # type: ignore[index]
        claim["container_name"] = "tampered-foreign-gpu3"
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "exact Broker rejection",
        ):
            validate(ACCEPTANCE.seal_report(value))

    def test_production_cas_requires_the_complete_inventory_to_match(
        self,
    ) -> None:
        value = report()
        value["production_cas"]["after"]["inventory_sha256"] = digest("0")  # type: ignore[index]
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "complete fixed baseline",
        ):
            validate(ACCEPTANCE.seal_report(value))

        value = report()
        for side in ("before", "after"):
            value["production_cas"][side]["status_sha256"] = digest("0")  # type: ignore[index]
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "complete fixed baseline",
        ):
            validate(ACCEPTANCE.seal_report(value))

        value = report()
        value["gpus"]["3"]["evidence_sha256"] = digest("0")  # type: ignore[index]
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "external-fence digest",
        ):
            validate(ACCEPTANCE.seal_report(value))

        value = report(gpu3_mode="actual")
        lease = value["gpus"]["3"]["lease"]  # type: ignore[index]
        lease["workload_cgroup"] = "/attacker.scope"
        value["gpus"]["3"]["evidence_sha256"] = (  # type: ignore[index]
            ACCEPTANCE.canonical_json_digest(
                {
                    "result": value["gpus"]["3"]["result"],  # type: ignore[index]
                    "lease": lease,
                }
            )
        )
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "Broker lease or transient scope",
        ):
            validate(ACCEPTANCE.seal_report(value))

    def test_stale_or_future_acceptance_is_rejected(self) -> None:
        value = report()
        for observed in (
            dt.datetime(2026, 7, 18, 1, 0, tzinfo=dt.UTC),
            dt.datetime(2026, 7, 17, 23, 0, tzinfo=dt.UTC),
        ):
            with self.subTest(observed=observed), self.assertRaisesRegex(
                ACCEPTANCE.GpuAcceptanceError,
                "stale or from the future",
            ):
                ACCEPTANCE.validate_report(
                    value,
                    authority=AUTHORITY,
                    bridge=BRIDGE,
                    authority_images=IMAGES,
                    runtime_contract=RUNTIME.RUNTIME_CONTRACT,
                    runtime_contract_sha256=RUNTIME.RUNTIME_CONTRACT_SHA256,
                    observed_at=observed,
                )

    def test_candidate_tree_report_is_not_final_acceptance(self) -> None:
        value = report()
        value["status"] = "prevalidated"
        value["run_kind"] = "candidate-tree"
        value["control_plane"]["image_mode"] = "candidate_local"  # type: ignore[index]
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "did not pass",
        ):
            validate(ACCEPTANCE.seal_report(value))

    def test_gpu2_transient_sample_drift_is_rejected(self) -> None:
        value = report()
        audit = value["gpus"]["2"]["audit"]  # type: ignore[index]
        audit["samples"][1]["memory_used_mib"] += 1
        audit["samples_sha256"] = ACCEPTANCE.canonical_json_digest(
            audit["samples"]
        )
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "GPU2 changed during",
        ):
            validate(ACCEPTANCE.seal_report(value))

    def test_gpu2_sampling_gap_and_cross_interval_timestamp_are_rejected(
        self,
    ) -> None:
        value = report()
        audit = value["gpus"]["2"]["audit"]  # type: ignore[index]
        audit["interval_ms"] = 251
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "sampling contract",
        ):
            validate(ACCEPTANCE.seal_report(value))

        value = report()
        audit = value["gpus"]["2"]["audit"]  # type: ignore[index]
        audit["sampled_at"][2] = "2026-07-18T00:00:10Z"
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "sampling gap or time drift",
        ):
            validate(ACCEPTANCE.seal_report(value))

        value = report()
        containers = value["control_plane"]["containers"]  # type: ignore[index]
        containers["backend"]["started_at"] = "2026-07-17T23:59:59Z"
        with self.assertRaisesRegex(
            ACCEPTANCE.GpuAcceptanceError,
            "one fresh audit interval",
        ):
            validate(ACCEPTANCE.seal_report(value))


class GpuAcceptanceHarnessCpuTests(unittest.TestCase):
    def bind_runtime_authority(
        self,
        controller,
        *,
        mps_indices: tuple[int, ...] = (),
    ) -> None:
        controller._require_private_gpu_root(create=True)
        if controller.reservations_descriptor < 0:
            try:
                descriptor = os.open(
                    b"external-reservations.json",
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=controller.gpu_root_descriptor,
                )
            except FileNotFoundError:
                write_descriptor = os.open(
                    b"external-reservations.json",
                    (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW
                    ),
                    0o600,
                    dir_fd=controller.gpu_root_descriptor,
                )
                os.write(
                    write_descriptor,
                    (
                        ROOT
                        / "ops/config/gpu-external-reservations.json"
                    ).read_bytes(),
                )
                os.close(write_descriptor)
                descriptor = os.open(
                    b"external-reservations.json",
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=controller.gpu_root_descriptor,
                )
            controller.reservations_descriptor = descriptor
        for index in mps_indices:
            slot = controller._private_directory_descriptor(
                controller.gpu_root_descriptor,
                f"mps-{index}",
                create=False,
            )
            pipe = controller._private_directory_descriptor(
                slot,
                "pipe",
                create=False,
            )
            log = controller._private_directory_descriptor(
                slot,
                "log",
                create=False,
            )
            controller.mps_descriptors[index] = {
                "slot": slot,
                "pipe": pipe,
                "log": log,
            }

    def test_formal_smoke_installs_descriptor_env_before_preflight(
        self,
    ) -> None:
        controller = SimpleNamespace(
            project_name="nexpoly_dft_fresh_smoke_order",
            authority_sha="a" * 40,
            _formal_gpu_authority_environment=lambda: {
                "NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY": "1",
                "NEXPOLY_DFT_GPU_AUTHORITY_PID": "123",
            },
        )

        def prepare(repo_root):
            self.assertEqual(repo_root, ROOT)
            self.assertEqual(
                os.environ["NEXPOLY_DFT_FORMAL_ACCEPTANCE"], "1"
            )
            self.assertEqual(
                os.environ["NEXPOLY_DFT_PROJECT_NAME"],
                controller.project_name,
            )
            self.assertEqual(
                os.environ["NEXPOLY_DFT_AUTHORITY_SHA"],
                controller.authority_sha,
            )
            self.assertEqual(
                os.environ["NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY"],
                "1",
            )
            return {
                "broker_enabled": True,
                "formal_gpu_authority": True,
            }

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                HARNESS.smoke_runtime,
                "prepare_runtime",
                side_effect=prepare,
            ) as smoke,
        ):
            result = HARNESS._prepare_formal_smoke_runtime(controller)
        self.assertTrue(result["formal_gpu_authority"])
        smoke.assert_called_once_with(ROOT)

    def test_gpu1_direct_execution_is_unconditionally_rejected(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(
                HARNESS.AcceptanceHarnessError,
                "only for GPU3 overflow",
            ),
        ):
            HARNESS.run_leased_direct(
                resolved={
                    "MONOMER_DFT_PYTHON": os.sys.executable,
                    "MONOMER_DFT_GPU_BROKER_UDS": "/private/broker.sock",
                    "MONOMER_DFT_GPU_MPS_PIPE_ROOT": "/private/mps",
                },
                default_model_path="/private/model.pt",
                gpu_index="1",
                placement="preferred",
                run_directory=Path(temporary),
            )

    def test_child_environment_strips_loaders_proxies_and_docker_redirects(
        self,
    ) -> None:
        hostile = {
            "HOME": "/private/home",
            "PATH": "/evil/bin",
            "LD_PRELOAD": "/evil.so",
            "PYTHONPATH": "/evil",
            "DOCKER_HOST": "tcp://production:2375",
            "HTTPS_PROXY": "http://proxy",
        }
        with mock.patch.dict(HARNESS.os.environ, hostile, clear=True):
            environment = HARNESS._safe_command_environment(
                extra={"CUDA_VISIBLE_DEVICES": HARNESS.GPU_UUIDS["3"]}
            )
        self.assertEqual(environment["HOME"], "/private/home")
        self.assertEqual(environment["PATH"], HARNESS.SAFE_COMMAND_PATH)
        self.assertEqual(
            environment["CUDA_VISIBLE_DEVICES"], HARNESS.GPU_UUIDS["3"]
        )
        for forbidden in (
            "LD_PRELOAD",
            "PYTHONPATH",
            "DOCKER_HOST",
            "HTTPS_PROXY",
        ):
            self.assertNotIn(forbidden, environment)

    def test_backend_target_is_fixed_loopback_only(self) -> None:
        self.assertEqual(
            HARNESS._require_backend_url(HARNESS.BACKEND_BASE_URL),
            HARNESS.BACKEND_BASE_URL,
        )
        for unsafe in (
            "http://localhost:28000/api/v1/monomer-dft",
            "http://127.0.0.1:28000/api/v1/monomer-dft?next=production",
            "https://example.invalid/api/v1/monomer-dft",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(
                HARNESS.AcceptanceHarnessError
            ):
                HARNESS._require_backend_url(unsafe)

    def test_production_cas_hashes_complete_ignored_boundary_without_following_symlinks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "production"
            repository.mkdir()
            outside = root / "outside-secret"
            outside.write_bytes(b"outside-one")
            (repository / ".gitignore").write_text(
                "ignored/\n",
                encoding="utf-8",
            )
            (repository / "tracked.txt").write_bytes(b"tracked")
            subprocess.run(
                [
                    "git",
                    "init",
                    "--quiet",
                    "--initial-branch=main",
                    str(repository),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "add",
                    ".gitignore",
                    "tracked.txt",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "-c",
                    "user.name=Codex Test",
                    "-c",
                    "user.email=codex@example.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ],
                check=True,
            )
            ignored = repository / "ignored"
            (ignored / "nested").mkdir(parents=True)
            (ignored / "one.bin").write_bytes(b"ignored-one")
            (ignored / "nested" / "two.bin").write_bytes(b"ignored-two")
            (ignored / "outside-link").symlink_to(outside)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/example/fixture.git",
                ],
                check=True,
            )
            fixture_head = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            fixture_tree = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD^{tree}"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            with mock.patch.object(
                HARNESS,
                "PRODUCTION_REPO_ROOT",
                repository,
            ):
                raw_authority = HARNESS._production_git_authority_inventory()
                transient_lock = repository / ".git" / "index.lock"
                transient_lock.write_bytes(b"non-authority transient")
                transient_lock.unlink()
                self.assertEqual(
                    raw_authority,
                    HARNESS._production_git_authority_inventory(),
                )
            wildcard_snapshot = {
                key: mock.ANY
                for key in HARNESS.PRODUCTION_BASELINE_SNAPSHOT
            }

            with (
                mock.patch.object(
                    HARNESS,
                    "PRODUCTION_REPO_ROOT",
                    repository,
                ),
                mock.patch.object(
                    HARNESS,
                    "PRODUCTION_BASELINE_SHA",
                    fixture_head,
                ),
                mock.patch.object(
                    HARNESS,
                    "PRODUCTION_BASELINE_TREE",
                    fixture_tree,
                ),
                mock.patch.object(
                    HARNESS,
                    "PRODUCTION_BASELINE_ORIGIN",
                    "https://github.com/example/fixture.git",
                ),
                mock.patch.object(
                    HARNESS,
                    "PRODUCTION_BASELINE_RAW_GIT_AUTHORITY",
                    raw_authority,
                ),
                mock.patch.object(
                    HARNESS,
                    "PRODUCTION_BASELINE_SNAPSHOT",
                    wildcard_snapshot,
                ),
            ):
                before = HARNESS._production_repo_snapshot()
                outside.write_bytes(b"outside-two")
                after_external_change = HARNESS._production_repo_snapshot()
                self.assertEqual(before, after_external_change)

                nested = ignored / "nested" / "two.bin"
                original_root_mtime = repository.stat().st_mtime_ns
                nested.write_bytes(b"changed-two")
                after_ignored_change = HARNESS._production_repo_snapshot()
                self.assertEqual(
                    after_ignored_change["mtime_ns"],
                    original_root_mtime,
                )
                self.assertNotEqual(
                    before["inventory_sha256"],
                    after_ignored_change["inventory_sha256"],
                )

                (repository / "tracked.txt").write_bytes(b"tracked-drift")
                with self.assertRaisesRegex(
                    HARNESS.AcceptanceHarnessError,
                    "tracked worktree status",
                ):
                    HARNESS._production_repo_snapshot()
                (repository / "tracked.txt").write_bytes(b"tracked")

                untracked = repository / "untracked.txt"
                untracked.write_bytes(b"untracked")
                with self.assertRaisesRegex(
                    HARNESS.AcceptanceHarnessError,
                    "non-ignored untracked",
                ):
                    HARNESS._production_repo_snapshot()
                untracked.unlink()

                with (
                    mock.patch.object(
                        HARNESS,
                        "PRODUCTION_CAS_MAX_TOTAL_BYTES",
                        3,
                    ),
                    self.assertRaisesRegex(
                        HARNESS.AcceptanceHarnessError,
                        "byte budget",
                    ),
                ):
                    HARNESS._production_repo_snapshot()

                global_home = root / "hostile-home"
                global_home.mkdir()
                global_marker = root / "global-fsmonitor-executed"
                global_hook = root / "global-fsmonitor"
                global_hook.write_text(
                    "#!/usr/bin/env bash\n"
                    f"/usr/bin/touch {shlex.quote(str(global_marker))}\n",
                    encoding="utf-8",
                )
                global_hook.chmod(0o700)
                global_include = root / "global-include"
                global_include.write_text(
                    "[core]\n"
                    f"\tfsmonitor = {global_hook}\n",
                    encoding="utf-8",
                )
                (global_home / ".gitconfig").write_text(
                    "[include]\n"
                    f"\tpath = {global_include}\n",
                    encoding="utf-8",
                )
                with mock.patch.dict(
                    os.environ,
                    {
                        "HOME": str(global_home),
                        "XDG_CONFIG_HOME": str(global_home / "xdg"),
                    },
                ):
                    global_snapshot = HARNESS._production_repo_snapshot()
                    self.assertEqual(
                        global_snapshot["head"],
                        fixture_head,
                    )
                self.assertFalse(global_marker.exists())

                local_marker = root / "local-fsmonitor-executed"
                local_hook = root / "local-fsmonitor"
                local_hook.write_text(
                    "#!/usr/bin/env bash\n"
                    f"/usr/bin/touch {shlex.quote(str(local_marker))}\n",
                    encoding="utf-8",
                )
                local_hook.chmod(0o700)
                local_include = root / "local-include"
                local_include.write_text(
                    "[core]\n"
                    f"\tfsmonitor = {local_hook}\n",
                    encoding="utf-8",
                )
                with (repository / ".git/config").open(
                    "a",
                    encoding="utf-8",
                ) as config:
                    config.write(
                        "\n[include]\n"
                        f"\tpath = {local_include}\n"
                    )
                with (
                    mock.patch.object(HARNESS.subprocess, "run") as git_run,
                    self.assertRaisesRegex(
                        HARNESS.AcceptanceHarnessError,
                        "raw Git authority",
                    ),
                ):
                    HARNESS._production_repo_snapshot()
                git_run.assert_not_called()
                self.assertFalse(local_marker.exists())

                authority_after_local = (
                    HARNESS._production_git_authority_inventory()
                )
                info_exclude = repository / ".git/info/exclude"
                info_exclude.write_text(
                    info_exclude.read_text(encoding="utf-8")
                    + "\nreview-sentinel\n",
                    encoding="utf-8",
                )
                authority_after_info = (
                    HARNESS._production_git_authority_inventory()
                )
                self.assertNotEqual(
                    authority_after_local["inventory_sha256"],
                    authority_after_info["inventory_sha256"],
                )

        self.assertEqual(before["tracked_path_count"], 2)
        self.assertEqual(before["ignored_path_count"], 3)
        self.assertEqual(before["untracked_path_count"], 0)
        self.assertGreaterEqual(before["status_boundary_count"], 1)
        self.assertGreaterEqual(before["inventory_entry_count"], 7)
        self.assertGreater(before["content_bytes"], 0)
        self.assertGreater(before["ignored_content_bytes"], 0)
        self.assertEqual(before["untracked_content_bytes"], 0)

    def test_final_image_input_requires_fixed_roots_and_exact_f_labels(
        self,
    ) -> None:
        images = {
            role: {
                "role": role,
                "digest_ref": f"{root}@{digest(character)}",
                "index_digest": digest(character),
                "platform_digest": digest(
                    "3" if role == "backend" else "4"
                ),
                "image_id": digest("5" if role == "backend" else "6"),
                "revision": AUTHORITY["sha"],
                "source": HARNESS.REPOSITORY_SOURCE_URL,
                "version": f"sha-{AUTHORITY['sha']}",
            }
            for role, root, character in (
                ("backend", HARNESS.IMAGE_ROOTS["backend"], "1"),
                ("web", HARNESS.IMAGE_ROOTS["web"], "2"),
            )
        }
        HARNESS._validate_authority_images_input(
            images, authority_sha=AUTHORITY["sha"]
        )
        containerd_images = deepcopy(images)
        for value in containerd_images.values():
            value["image_id"] = value["index_digest"]
        HARNESS._validate_authority_images_input(
            containerd_images, authority_sha=AUTHORITY["sha"]
        )
        forged = deepcopy(images)
        forged["backend"]["digest_ref"] = (
            "ghcr.io/attacker/backend@" + forged["backend"]["index_digest"]
        )
        with self.assertRaisesRegex(
            HARNESS.AcceptanceHarnessError,
            "does not bind exact final main",
        ):
            HARNESS._validate_authority_images_input(
                forged, authority_sha=AUTHORITY["sha"]
            )

    def test_candidate_images_use_unique_project_authority_tags(self) -> None:
        project = "nexpoly_dft_fresh_aaaaaaaa_1234"
        expected = {
            "backend": (
                f"nexpoly-dft-acceptance-backend:{project}-{AUTHORITY['sha']}"
            ),
            "web": (
                f"nexpoly-dft-acceptance-web:{project}-{AUTHORITY['sha']}"
            ),
        }
        self.assertEqual(
            HARNESS._candidate_image_tags(
                project_name=project,
                authority_sha=AUTHORITY["sha"],
            ),
            expected,
        )
        with (
            mock.patch.object(
                HARNESS,
                "_local_docker_environment",
                return_value={},
            ),
            mock.patch.object(
                HARNESS,
                "_run",
                return_value=subprocess.CompletedProcess(
                    ("stack", "start"),
                    0,
                    "",
                    "",
                ),
            ) as command,
        ):
            HARNESS._stack_command(
                "start",
                10.0,
                project_name=project,
                authority_sha=AUTHORITY["sha"],
                run_kind="candidate-tree",
                authority_images=None,
                gpu_authority_environment={
                    "NEXPOLY_DFT_GPU_DESCRIPTOR_AUTHORITY": "1",
                    "NEXPOLY_DFT_GPU_AUTHORITY_PID": "123",
                    "NEXPOLY_DFT_GPU_AUTHORITY_START_TICKS": "456",
                    "NEXPOLY_DFT_GPU_AUTHORITY_ROOT": "/proc/123/fd/10",
                    "NEXPOLY_DFT_GPU_AUTHORITY_ROOT_IDENTITY": "1:10",
                    "NEXPOLY_DFT_GPU_RESERVATIONS_AUTHORITY": (
                        "/proc/123/fd/11"
                    ),
                    "NEXPOLY_DFT_GPU_RESERVATIONS_IDENTITY": "1:11",
                    "NEXPOLY_DFT_GPU_RESERVATIONS_SHA256": "a" * 64,
                    "NEXPOLY_DFT_GPU1_MPS_PIPE_AUTHORITY": (
                        "/proc/123/fd/12"
                    ),
                    "NEXPOLY_DFT_GPU1_MPS_PIPE_IDENTITY": "1:12",
                },
            )
        environment = command.call_args.kwargs["env"]
        self.assertEqual(
            environment["NEXPOLY_DFT_BACKEND_IMAGE_REF"],
            expected["backend"],
        )
        self.assertEqual(
            environment["NEXPOLY_DFT_WEB_IMAGE_REF"],
            expected["web"],
        )
        self.assertNotIn(
            "nexpoly-dft-dev-backend:latest",
            environment.values(),
        )

    def test_candidate_image_cleanup_removes_only_this_run_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "runs"
            run.mkdir(mode=0o700)
            controller = HARNESS.FreshAcceptanceControl(
                runtime_root=root,
                run_directory=run,
                authority_sha=AUTHORITY["sha"],
                authority_tree=AUTHORITY["tree"],
                gpu3_mode="externally_fenced",
                stack_timeout=10.0,
                run_kind="candidate-tree",
                authority_images=None,
            )
            ordinary = {
                HARNESS.ORDINARY_DEV_IMAGE_TAGS[0]: digest("1"),
                HARNESS.ORDINARY_DEV_IMAGE_TAGS[1]: digest("2"),
            }
            images = {
                **ordinary,
                **{
                    tag: digest(character)
                    for tag, character in zip(
                        controller.candidate_image_tags.values(),
                        ("3", "4"),
                        strict=True,
                    )
                },
            }
            controller.ordinary_dev_images_before = dict(ordinary)
            controller.candidate_images_absent_before = True
            removed: list[str] = []

            def snapshot(tags):
                return {tag: images.get(tag) for tag in tags}

            def fake_run(*command, **_kwargs):
                if command[1:3] == ("image", "rm"):
                    tag = command[3]
                    removed.append(tag)
                    images[tag] = None
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise AssertionError(f"unexpected Docker command: {command}")

            with (
                mock.patch.object(
                    HARNESS,
                    "_local_docker_environment",
                    return_value={},
                ),
                mock.patch.object(
                    HARNESS,
                    "_docker_image_tag_snapshot",
                    side_effect=snapshot,
                ),
                mock.patch.object(HARNESS, "_run", side_effect=fake_run),
            ):
                evidence, errors = controller.cleanup()

        self.assertIsNone(evidence)
        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(removed),
            sorted(controller.candidate_image_tags.values()),
        )
        self.assertEqual(
            {tag: images[tag] for tag in ordinary},
            ordinary,
        )
        cleanup = controller.cleanup_evidence
        assert cleanup is not None
        self.assertTrue(cleanup["candidate_images_removed"])
        self.assertTrue(cleanup["ordinary_dev_images_unchanged"])
        self.assertEqual(
            cleanup["candidate_image_tags"],
            sorted(controller.candidate_image_tags.values()),
        )

    def test_candidate_start_rejects_a_preexisting_unique_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "runs"
            run.mkdir(mode=0o700)
            controller = HARNESS.FreshAcceptanceControl(
                runtime_root=root,
                run_directory=run,
                authority_sha=AUTHORITY["sha"],
                authority_tree=AUTHORITY["tree"],
                gpu3_mode="externally_fenced",
                stack_timeout=10.0,
                run_kind="candidate-tree",
                authority_images=None,
            )
            candidate_snapshot = {
                tag: (digest("3") if index == 0 else None)
                for index, tag in enumerate(
                    controller.candidate_image_tags.values()
                )
            }
            with (
                mock.patch.object(HARNESS, "_stack_running", return_value=False),
                mock.patch.object(
                    HARNESS,
                    "_local_docker_environment",
                    return_value={},
                ),
                mock.patch.object(
                    HARNESS,
                    "_docker_image_tag_snapshot",
                    side_effect=[
                        {
                            tag: None
                            for tag in HARNESS.ORDINARY_DEV_IMAGE_TAGS
                        },
                        candidate_snapshot,
                    ],
                ),
                mock.patch.object(HARNESS, "_run") as docker,
                self.assertRaisesRegex(
                    HARNESS.AcceptanceHarnessError,
                    "candidate image tag already exists",
                ),
            ):
                controller._require_absent()
            docker.assert_not_called()

    def test_fresh_lifecycle_rejects_symlinked_gpu_root_before_external_actions(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            tempfile.TemporaryDirectory() as external_temporary,
        ):
            root = Path(temporary)
            root.chmod(0o700)
            run = root / "runs"
            run.mkdir(mode=0o700)
            external = Path(external_temporary)
            sentinel = external / "sentinel"
            sentinel.write_bytes(b"unchanged")
            before = {
                path.name: path.read_bytes()
                for path in external.iterdir()
                if path.is_file()
            }
            (root / "gpu-resource").symlink_to(
                external,
                target_is_directory=True,
            )

            for entrypoint in ("_require_absent", "_start_broker"):
                with self.subTest(entrypoint=entrypoint):
                    controller = HARNESS.FreshAcceptanceControl(
                        runtime_root=root,
                        run_directory=run,
                        authority_sha=AUTHORITY["sha"],
                        authority_tree=AUTHORITY["tree"],
                        gpu3_mode="externally_fenced",
                        stack_timeout=10.0,
                        run_kind="candidate-tree",
                        authority_images=None,
                    )
                    with (
                        mock.patch.object(HARNESS, "_stack_running") as stack,
                        mock.patch.object(
                            HARNESS,
                            "_docker_image_tag_snapshot",
                        ) as images,
                        mock.patch.object(HARNESS, "_run") as command,
                        mock.patch.object(
                            HARNESS.subprocess,
                            "Popen",
                        ) as process,
                        self.assertRaisesRegex(
                            HARNESS.AcceptanceHarnessError,
                            "GPU runtime root",
                        ),
                    ):
                        getattr(controller, entrypoint)()
                    stack.assert_not_called()
                    images.assert_not_called()
                    command.assert_not_called()
                    process.assert_not_called()

            after = {
                path.name: path.read_bytes()
                for path in external.iterdir()
                if path.is_file()
            }
            self.assertEqual(after, before)
            self.assertEqual(sentinel.read_bytes(), b"unchanged")

    def test_final_container_inspection_fails_closed_without_platform_descriptor(
        self,
    ) -> None:
        images = {
            role: {
                "role": role,
                "digest_ref": f"{root}@{digest(character)}",
                "index_digest": digest(character),
                "platform_digest": digest(
                    "3" if role == "backend" else "4"
                ),
                "image_id": digest("5" if role == "backend" else "6"),
                "revision": AUTHORITY["sha"],
                "source": HARNESS.REPOSITORY_SOURCE_URL,
                "version": f"sha-{AUTHORITY['sha']}",
            }
            for role, root, character in (
                ("backend", HARNESS.IMAGE_ROOTS["backend"], "1"),
                ("web", HARNESS.IMAGE_ROOTS["web"], "2"),
            )
        }
        containers = []
        for service, role, character in (
            ("backend", "backend", "a"),
            ("frontend", "web", "b"),
            ("postgres", "postgres", "c"),
        ):
            labels = {
                "com.docker.compose.project": (
                    "nexpoly_dft_fresh_aaaaaaaa_1234"
                ),
                "com.docker.compose.service": service,
            }
            if role != "postgres":
                labels["org.opencontainers.image.revision"] = AUTHORITY["sha"]
            containers.append(
                {
                    "Id": character * 64,
                    "Image": (
                        images[role]["image_id"]
                        if role != "postgres"
                        else digest("7")
                    ),
                    "Config": {"Labels": labels},
                    "State": {
                        "Running": True,
                        "StartedAt": "2026-07-18T00:00:00Z",
                    },
                }
            )

        def fake_run(*command, **_kwargs):
            if command[:2] == ("docker", "ps"):
                output = "one\ntwo\nthree\n"
            elif command[:2] == ("docker", "inspect"):
                output = json.dumps(containers)
            elif command[:4] == (
                "docker",
                "buildx",
                "imagetools",
                "inspect",
            ):
                expected = next(
                    value
                    for value in images.values()
                    if value["digest_ref"] == command[4]
                )
                output = json.dumps(
                    {
                        "mediaType": (
                            "application/vnd.oci.image.index.v1+json"
                        ),
                        "manifests": [
                            {
                                "digest": expected["platform_digest"],
                                "platform": {
                                    "os": "linux",
                                    "architecture": "amd64",
                                },
                            }
                        ],
                    }
                )
            else:
                expected = next(
                    value
                    for value in images.values()
                    if value["digest_ref"] == command[3]
                )
                output = json.dumps(
                    [
                        {
                            "Id": expected["image_id"],
                            "RepoDigests": [expected["digest_ref"]],
                            "Config": {
                                "Labels": {
                                    "org.opencontainers.image.revision": (
                                        expected["revision"]
                                    ),
                                    "org.opencontainers.image.source": (
                                        expected["source"]
                                    ),
                                    "org.opencontainers.image.version": (
                                        expected["version"]
                                    ),
                                }
                            },
                            # Deliberately no Descriptor: older/insufficient
                            # Engine APIs must fail closed.
                        }
                    ]
                )
            return subprocess.CompletedProcess(command, 0, output, "")

        with (
            mock.patch.object(
                HARNESS, "_local_docker_environment", return_value={}
            ),
            mock.patch.object(HARNESS, "_run", side_effect=fake_run),
            self.assertRaisesRegex(
                HARNESS.AcceptanceHarnessError,
                "differs from exact published",
            ),
        ):
            HARNESS._compose_evidence(
                project_name="nexpoly_dft_fresh_aaaaaaaa_1234",
                authority_sha=AUTHORITY["sha"],
                run_kind="final-main",
                authority_images=images,
            )

    def test_unregistered_leased_child_fails_before_scientific_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            spec = directory / "spec.json"
            output = directory / "output.json"
            HARNESS._write_private_json(
                spec,
                {
                    "schema_version": 1,
                    "state": "gated",
                    "default_model_path": "/model.pt",
                    "gpu_index": 3,
                    "gpu_uuid": HARNESS.GPU_UUIDS["3"],
                    "lease_id": "a" * 32,
                    "fencing_token": 1,
                    "broker_instance_id": "broker",
                    "broker_socket": "/broker.sock",
                    "workload_pid": None,
                    "workload_process_start_ticks": None,
                    "workload_cgroup": None,
                    "mps_pipe_directory": "/mps",
                },
            )
            with mock.patch.object(
                HARNESS.smoke_runtime, "run_calculations"
            ) as calculation:
                self.assertEqual(HARNESS._leased_child(spec, output), 2)
            calculation.assert_not_called()
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8"))["status"],
                "error",
            )

    def test_direct_child_receives_self_bound_broker_and_mps_fds(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            pipe = root / "mps-3/pipe"
            pipe.mkdir(parents=True, mode=0o700)
            os.mkfifo(pipe / "control", mode=0o600)
            model = root / "model.pt"
            model.write_bytes(b"model")
            model_fd = os.open(model, os.O_RDONLY | os.O_CLOEXEC)
            managed = SimpleNamespace(
                lease=SimpleNamespace(
                    lease_id="a" * 32,
                    fencing_token=1,
                    broker_instance_id="b" * 32,
                    gpu_index=3,
                    gpu_uuid=HARNESS.GPU_UUIDS["3"],
                    component="dft",
                    thread_percent=50,
                    memory_mib=4096,
                )
            )
            model_copy = SimpleNamespace(
                evidence={"path": str(model)},
                model_descriptor=model_fd,
            )
            descriptors: list[int] = []
            try:
                (
                    spec_path,
                    _output_path,
                    read_fd,
                    write_fd,
                    mps_pipe_fd,
                    broker_root_fd,
                    mps_environment,
                    child_environment,
                    _command,
                ) = HARNESS._prepare_direct_launch(
                    resolved={
                        "MONOMER_DFT_GPU_MPS_PIPE_ROOT": str(root),
                        "MONOMER_DFT_GPU_BROKER_UDS": str(
                            root / "broker.sock"
                        ),
                        "MONOMER_DFT_PYTHON": os.sys.executable,
                        "NEXPOLY_DFT_GPU3_MPS_PIPE_AUTHORITY": str(
                            pipe
                        ),
                    },
                    managed=managed,
                    model_copy=model_copy,
                    gpu_index="3",
                    run_directory=root,
                )
                descriptors.extend(
                    (
                        read_fd,
                        write_fd,
                        mps_pipe_fd,
                        broker_root_fd,
                    )
                )
                expected_pipe = f"/proc/self/fd/{mps_pipe_fd}"
                expected_broker = (
                    f"/proc/self/fd/{broker_root_fd}/broker.sock"
                )
                self.assertEqual(
                    mps_environment["CUDA_MPS_PIPE_DIRECTORY"],
                    expected_pipe,
                )
                self.assertEqual(
                    child_environment["CUDA_MPS_PIPE_DIRECTORY"],
                    expected_pipe,
                )
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    spec["mps_pipe_directory"], expected_pipe
                )
                self.assertEqual(spec["broker_socket"], expected_broker)
            finally:
                for descriptor in descriptors:
                    with contextlib.suppress(OSError):
                        os.close(descriptor)
                os.close(model_fd)

    def test_stable_model_copy_loads_through_read_only_descriptor(self) -> None:
        payload = b"locked-model-through-inherited-descriptor"
        model_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary)
            run_directory.chmod(0o700)
            source_directory = run_directory / "source"
            source_directory.mkdir(mode=0o700)
            source = (
                source_directory
                / HARNESS.acceptance_contract.AIMNET2_MODEL_FILENAME
            )
            source.write_bytes(payload)
            source.chmod(0o400)
            with mock.patch.object(
                HARNESS.acceptance_contract,
                "AIMNET2_MODEL_SHA256",
                model_digest,
            ):
                stable_copy = HARNESS._prepare_stable_model_copy(
                    os.fspath(source),
                    run_directory,
                )
                observed: dict[str, object] = {}

                def calculate(preflight):
                    load_path = preflight["default_model_path"]
                    observed["load_path"] = load_path
                    observed["bytes"] = Path(load_path).read_bytes()
                    return {"preflight": dict(preflight)}

                with mock.patch.object(
                    HARNESS.smoke_runtime,
                    "run_calculations",
                    side_effect=calculate,
                ):
                    result = (
                        HARNESS._run_calculations_from_stable_model_copy(
                            stable_copy.evidence,
                            stable_copy.model_descriptor,
                        )
                    )
                self.assertEqual(observed["bytes"], payload)
                self.assertRegex(
                    str(observed["load_path"]),
                    r"^/proc/self/fd/[1-9][0-9]*$",
                )
                self.assertEqual(
                    result["preflight"]["default_model_path"],
                    stable_copy.evidence["path"],
                )
                self.assertTrue(
                    HARNESS._remove_stable_model_copy(stable_copy)
                )

    def test_source_parent_component_swap_is_rejected_and_cleaned(self) -> None:
        payload = b"trusted-parent-component"
        model_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            run_directory = root / "run"
            run_directory.mkdir(mode=0o700)
            authority = root / "authority"
            source_directory = authority / "models"
            source_directory.mkdir(parents=True, mode=0o700)
            authority.chmod(0o700)
            source = (
                source_directory
                / HARNESS.acceptance_contract.AIMNET2_MODEL_FILENAME
            )
            source.write_bytes(payload)
            source.chmod(0o400)
            real_read = os.read
            swapped = False

            def swap_parent(descriptor, count):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    authority.rename(root / "authority-original")
                    replacement = authority / "models"
                    replacement.mkdir(parents=True, mode=0o700)
                    authority.chmod(0o700)
                    malicious = replacement / source.name
                    malicious.write_bytes(b"replacement-model")
                    malicious.chmod(0o400)
                return real_read(descriptor, count)

            with (
                mock.patch.object(
                    HARNESS.acceptance_contract,
                    "AIMNET2_MODEL_SHA256",
                    model_digest,
                ),
                mock.patch.object(
                    HARNESS.os,
                    "read",
                    side_effect=swap_parent,
                ),
                self.assertRaisesRegex(
                    HARNESS.AcceptanceHarnessError,
                    "source model parent changed",
                ),
            ):
                HARNESS._prepare_stable_model_copy(
                    os.fspath(source),
                    run_directory,
                )
            self.assertTrue(swapped)
            self.assertFalse((run_directory / "direct-gpu3-model").exists())

    def test_path_replacement_cannot_change_descriptor_bound_load(self) -> None:
        payload = b"trusted-descriptor-inode"
        malicious_payload = b"malicious-path-replacement"
        model_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary)
            run_directory.chmod(0o700)
            source_directory = run_directory / "source"
            source_directory.mkdir(mode=0o700)
            source = (
                source_directory
                / HARNESS.acceptance_contract.AIMNET2_MODEL_FILENAME
            )
            source.write_bytes(payload)
            source.chmod(0o400)
            with mock.patch.object(
                HARNESS.acceptance_contract,
                "AIMNET2_MODEL_SHA256",
                model_digest,
            ):
                stable_copy = HARNESS._prepare_stable_model_copy(
                    os.fspath(source),
                    run_directory,
                )
                canonical = Path(stable_copy.evidence["path"])
                displaced = canonical.with_name("displaced-model")
                observed: dict[str, bytes] = {}

                def replace_then_load(preflight):
                    canonical.rename(displaced)
                    canonical.write_bytes(malicious_payload)
                    canonical.chmod(0o400)
                    observed["loaded"] = Path(
                        preflight["default_model_path"]
                    ).read_bytes()
                    return {"preflight": dict(preflight)}

                try:
                    with (
                        mock.patch.object(
                            HARNESS.smoke_runtime,
                            "run_calculations",
                            side_effect=replace_then_load,
                        ),
                        self.assertRaisesRegex(
                            HARNESS.AcceptanceHarnessError,
                            r"stable model (directory|identity)",
                        ),
                    ):
                        HARNESS._run_calculations_from_stable_model_copy(
                            stable_copy.evidence,
                            stable_copy.model_descriptor,
                        )
                    self.assertEqual(observed["loaded"], payload)
                    self.assertNotEqual(observed["loaded"], malicious_payload)
                finally:
                    for descriptor in (
                        stable_copy.model_descriptor,
                        stable_copy.directory_descriptor,
                        stable_copy.run_directory_descriptor,
                    ):
                        if descriptor >= 0:
                            os.close(descriptor)

    def test_gpu2_monitor_detects_transient_drift(self) -> None:
        baseline = {
            "index": 2,
            "uuid": HARNESS.GPU_UUIDS["2"],
            "memory_used_mib": 0,
            "compute_processes": [],
        }
        drift = {**baseline, "memory_used_mib": 1}
        monitor = HARNESS.Gpu2AuditMonitor(
            baseline,
            sampler=mock.Mock(side_effect=[drift, baseline]),
        )
        monitor._sample_once()
        with self.assertRaisesRegex(
            HARNESS.AcceptanceHarnessError,
            "changed during continuous",
        ):
            monitor.stop()

    def test_fresh_lifecycle_cleanup_drains_before_mps_and_broker_stop(
        self,
    ) -> None:
        events: list[str] = []

        class Process:
            pid = 1234
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                events.append("broker-terminate")
                self.returncode = 0

            def wait(self, timeout=None):
                del timeout
                return self.returncode

        class Broker:
            def __init__(self, _socket):
                pass

            def set_draining(self, value):
                self.value = value
                events.append("broker-drain")
                return {"draining": True, "leases": []}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "runs"
            run.mkdir(mode=0o700)
            gpu_root = root / "gpu-resource"
            gpu_root.mkdir(mode=0o700)
            slot = gpu_root / "mps-1"
            slot.mkdir(mode=0o700)
            (slot / "pipe").mkdir(mode=0o700)
            (slot / "log").mkdir(mode=0o700)
            control_path = slot / "pipe/control"
            os.mkfifo(control_path)
            broker_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            broker_socket.bind(str(gpu_root / "broker.sock"))
            broker_socket.close()
            (gpu_root / "broker.sock").chmod(0o600)
            controller = HARNESS.FreshAcceptanceControl(
                runtime_root=root,
                run_directory=run,
                authority_sha=AUTHORITY["sha"],
                authority_tree=AUTHORITY["tree"],
                gpu3_mode="externally_fenced",
                stack_timeout=10.0,
                run_kind="final-main",
                authority_images=None,
            )
            self.bind_runtime_authority(
                controller,
                mps_indices=(1,),
            )
            controller.ordinary_dev_images_before = {
                tag: None for tag in HARNESS.ORDINARY_DEV_IMAGE_TAGS
            }
            controller.candidate_images_absent_before = True
            controller.stack_attempted = True
            controller.broker_process = Process()
            controller.broker_process_evidence = {
                "pid": 1234,
                "process_start_ticks": 1,
                "cwd": str(ROOT),
                "command_sha256": digest("1"),
            }
            controller.broker_instance_id = "broker"
            controller.initial_leases = []
            controller.worker_evidence = {}
            controller.container_evidence = {}
            controller.mps_attempted = [1]
            controller.mps_started = [1]
            volume_present = True

            def fake_run(*command, **_kwargs):
                nonlocal volume_present
                if command[0].endswith("gpu_mps_control.sh"):
                    events.append("mps-stop")
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[1:3] == ("volume", "inspect"):
                    return subprocess.CompletedProcess(
                        command,
                        0 if volume_present else 1,
                        "",
                        "",
                    )
                if command[1:3] == ("volume", "rm"):
                    volume_present = False
                    return subprocess.CompletedProcess(command, 0, "", "")
                if command[1:3] == ("network", "inspect"):
                    return subprocess.CompletedProcess(command, 1, "", "")
                events.append("container-check")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch("gpu_resource.GpuBrokerClient", Broker),
                mock.patch.object(
                    HARNESS,
                    "_stack_command",
                    side_effect=lambda *_args, **_kwargs: events.append(
                        "stack-stop"
                    ),
                ),
                mock.patch.object(HARNESS, "_stack_running", return_value=False),
                mock.patch.object(
                    HARNESS, "_local_docker_environment", return_value={}
                ),
                mock.patch.object(
                    HARNESS,
                    "_docker_image_tag_snapshot",
                    side_effect=lambda tags: {tag: None for tag in tags},
                ),
                mock.patch.object(HARNESS, "_run", side_effect=fake_run),
            ):
                evidence, errors = controller.cleanup()
            self.assertFalse(slot.exists())
            next_controller = HARNESS.FreshAcceptanceControl(
                runtime_root=root,
                run_directory=run,
                authority_sha=AUTHORITY["sha"],
                authority_tree=AUTHORITY["tree"],
                gpu3_mode="externally_fenced",
                stack_timeout=10.0,
                run_kind="final-main",
                authority_images=None,
            )
            self.bind_runtime_authority(next_controller)
            next_controller._gpu_child_absent(
                "mps-1",
                "second lifecycle retained stale GPU1 MPS state",
            )
            next_controller._close_runtime_descriptors()

        self.assertEqual(errors, [])
        assert evidence is not None
        self.assertTrue(evidence["cleanup"]["volume_removed"])
        self.assertTrue(evidence["cleanup"]["network_removed"])
        self.assertLess(events.index("stack-stop"), events.index("broker-drain"))
        self.assertLess(events.index("broker-drain"), events.index("mps-stop"))
        self.assertLess(events.index("mps-stop"), events.index("broker-terminate"))
        roundtrip = report()
        roundtrip["control_plane"]["cleanup"] = deepcopy(  # type: ignore[index]
            evidence["cleanup"]
        )
        self.assertEqual(
            validate(ACCEPTANCE.seal_report(roundtrip))["status"],
            "passed",
        )

    def test_fresh_lifecycle_preserves_broker_when_leases_remain(self) -> None:
        events: list[str] = []

        class Process:
            pid = 1234
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                events.append("terminate")

        class Broker:
            def __init__(self, _socket):
                pass

            def set_draining(self, _value):
                return {"draining": True, "leases": [{"lease_id": "live"}]}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "runs"
            run.mkdir(mode=0o700)
            controller = HARNESS.FreshAcceptanceControl(
                runtime_root=root,
                run_directory=run,
                authority_sha=AUTHORITY["sha"],
                authority_tree=AUTHORITY["tree"],
                gpu3_mode="externally_fenced",
                stack_timeout=10.0,
                run_kind="candidate-tree",
                authority_images=None,
            )
            self.bind_runtime_authority(controller)
            controller.broker_process = Process()
            controller.mps_attempted = [1]
            controller.ordinary_dev_images_before = {
                tag: None for tag in HARNESS.ORDINARY_DEV_IMAGE_TAGS
            }
            controller.candidate_images_absent_before = True
            with (
                mock.patch("gpu_resource.GpuBrokerClient", Broker),
                mock.patch.object(
                    HARNESS,
                    "_local_docker_environment",
                    return_value={},
                ),
                mock.patch.object(
                    HARNESS,
                    "_docker_image_tag_snapshot",
                    side_effect=lambda tags: {tag: None for tag in tags},
                ),
                mock.patch.object(HARNESS, "_run") as command,
            ):
                _evidence, errors = controller.cleanup()
        self.assertTrue(any("still has leases" in error for error in errors))
        self.assertTrue(any("intentionally preserved" in error for error in errors))
        self.assertNotIn("terminate", events)
        command.assert_not_called()

    def test_fresh_lifecycle_preserves_broker_when_mps_stop_fails(self) -> None:
        events: list[str] = []

        class Process:
            pid = 1234
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                events.append("terminate")

        class Broker:
            def __init__(self, _socket):
                pass

            def set_draining(self, _value):
                return {"draining": True, "leases": []}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "runs"
            run.mkdir(mode=0o700)
            gpu_root = root / "gpu-resource"
            gpu_root.mkdir(mode=0o700)
            slot = gpu_root / "mps-1"
            slot.mkdir(mode=0o700)
            (slot / "pipe").mkdir(mode=0o700)
            (slot / "log").mkdir(mode=0o700)
            control_path = slot / "pipe/control"
            os.mkfifo(control_path)
            controller = HARNESS.FreshAcceptanceControl(
                runtime_root=root,
                run_directory=run,
                authority_sha=AUTHORITY["sha"],
                authority_tree=AUTHORITY["tree"],
                gpu3_mode="externally_fenced",
                stack_timeout=10.0,
                run_kind="candidate-tree",
                authority_images=None,
            )
            self.bind_runtime_authority(
                controller,
                mps_indices=(1,),
            )
            controller.broker_process = Process()
            controller.mps_attempted = [1]
            controller.ordinary_dev_images_before = {
                tag: None for tag in HARNESS.ORDINARY_DEV_IMAGE_TAGS
            }
            controller.candidate_images_absent_before = True
            with (
                mock.patch("gpu_resource.GpuBrokerClient", Broker),
                mock.patch.object(
                    HARNESS,
                    "_local_docker_environment",
                    return_value={},
                ),
                mock.patch.object(
                    HARNESS,
                    "_docker_image_tag_snapshot",
                    side_effect=lambda tags: {tag: None for tag in tags},
                ),
                mock.patch.object(
                    HARNESS,
                    "_run",
                    return_value=subprocess.CompletedProcess(
                        ("gpu_mps_control.sh",), 2, "", "failed"
                    ),
                ),
            ):
                _evidence, errors = controller.cleanup()
        self.assertTrue(any("MPS stop failed" in error for error in errors))
        self.assertTrue(any("intentionally preserved" in error for error in errors))
        self.assertNotIn("terminate", events)

    def test_gpu3_overflow_runner_enters_exact_scope_before_registration_and_gate(
        self,
    ) -> None:
        events: list[str] = []
        lease_id = "a1" * 16
        registered = SimpleNamespace(
            lease_id=lease_id,
            fencing_token=7,
            broker_instance_id="2" * 32,
            gpu_index=3,
            gpu_uuid=HARNESS.GPU_UUIDS["3"],
            workload_cgroup=(
                "/user.slice/user-1001.slice/user@1001.service/"
                "nexpoly.slice/nexpoly-gpu.slice/"
                "nexpoly-gpu-jobs.slice/"
                f"nexpoly-gpu-job-{lease_id}.scope"
            ),
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
        stable_model_path: Path | None = None
        model_digest = ""

        class Process:
            pid = 54_321
            returncode: int | None = None

            def communicate(self, *, timeout):
                assert timeout == 600.0
                assert output_path is not None
                assert stable_model_path is not None
                events.append("communicate")
                direct_result = gpu3_direct_result()
                direct_result["model_sha256"] = model_digest
                direct_result["preflight"] = {
                    "default_model_path": os.fspath(stable_model_path)
                }
                HARNESS._write_private_json(
                    output_path,
                    direct_result,
                )
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

        real_os_write = os.write

        def open_gate(descriptor, payload):
            if payload == b"1":
                assert descriptor > 0
                events.append("gate")
                return 1
            return real_os_write(descriptor, payload)

        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary)
            run_directory.chmod(0o700)
            source_directory = run_directory / "source"
            source_directory.mkdir(mode=0o700)
            source_model = (
                source_directory / HARNESS.acceptance_contract.AIMNET2_MODEL_FILENAME
            )
            model_bytes = b"locked-aimnet2-test-model"
            source_model.write_bytes(model_bytes)
            source_model.chmod(0o400)
            model_digest = "sha256:" + hashlib.sha256(model_bytes).hexdigest()
            stable_model_path = (
                run_directory
                / "direct-gpu3-model"
                / HARNESS.acceptance_contract.AIMNET2_MODEL_FILENAME
            )
            output_path = run_directory / "direct-gpu3-result.json"
            gpu_root = run_directory / "gpu-resource"
            pipe_directory = gpu_root / "mps-3/pipe"
            pipe_directory.mkdir(parents=True, mode=0o700)
            with (
                mock.patch.object(
                    HARNESS.acceptance_contract,
                    "AIMNET2_MODEL_SHA256",
                    model_digest,
                ),
                mock.patch("gpu_resource.GpuBrokerClient", Broker),
                mock.patch(
                    "gpu_resource.mps_client_environment",
                    return_value={
                        "CUDA_VISIBLE_DEVICES": HARNESS.GPU_UUIDS["3"],
                        "CUDA_MPS_PIPE_DIRECTORY": "/private/mps/mps-3/pipe",
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
                        "MONOMER_DFT_GPU_BROKER_UDS": os.fspath(
                            gpu_root / "broker.sock"
                        ),
                        "MONOMER_DFT_GPU_MPS_PIPE_ROOT": os.fspath(
                            gpu_root
                        ),
                    },
                    default_model_path=os.fspath(source_model),
                    gpu_index="3",
                    placement="overflow",
                    run_directory=run_directory,
                )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(evidence["process_start_ticks"], 98_765)
        self.assertEqual(evidence["workload_pid"], 54_321)
        self.assertEqual(
            evidence["broker_instance_id"],
            registered.broker_instance_id,
        )
        self.assertEqual(evidence["model_copy_sha256"], model_digest)
        assert stable_model_path is not None
        self.assertFalse(stable_model_path.parent.exists())
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
        self.assertEqual(len(kwargs["pass_fds"]), 4)

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

        with (
            mock.patch.object(HARNESS, "_run", side_effect=fake_run),
            mock.patch.object(
                HARNESS, "_local_docker_environment", return_value={}
            ),
        ):
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
            mock.patch.object(
                HARNESS, "_local_docker_environment", return_value={}
            ),
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
