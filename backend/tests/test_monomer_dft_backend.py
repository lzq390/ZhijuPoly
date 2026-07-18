from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sys
import threading
import time
import zipfile
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from uuid import uuid4

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from app import main as main_module
from app.config import Settings
from app.main import create_app
from app.postgres_database import PostgresUnavailableError
from app.routers.monomer_dft import MONOMER_DFT_STABLE_ERROR_CODES
from app.services.monomer_dft_download_proxy import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_ENTRIES,
    MonomerDftDownloadProxy,
    MonomerDftDownloadProxyError,
    VerifiedMonomerDftFileResponse,
    _verify_zip_members,
)
from app.services import monomer_dft_download_proxy as monomer_dft_download_proxy_module
from app.services.monomer_dft_models import (
    MAX_ARTIFACT_BYTES,
    MonomerDftArtifact,
    MonomerDftArtifactDeleteResponse,
    MonomerDftArtifactsState,
    MonomerDftJobResponse,
    MonomerDftRunRequest,
)
from app.services.monomer_dft_internal_models import (
    InternalScientificResult,
    InternalWorkerArtifactDeletionResponse,
    InternalWorkerRequest,
    InternalWorkerSnapshot,
)
from app.services.monomer_dft_protocol import (
    MonomerDftRequestError,
    calculation_request_sha256,
    prepare_monomer_dft_request,
)
from app.services.monomer_dft_reconciler import (
    MonomerDftReadinessController,
    MonomerDftReconciler,
)
from app.services import monomer_dft_repository as monomer_dft_repository_module
from app.services.monomer_dft_repository import (
    CreateJobResult,
    MonomerDftIdempotencyConflict,
    MonomerDftRepository,
    normalize_artifacts,
    sanitize_error,
    sanitize_public_json,
    sanitize_result,
    sanitize_timings,
)
from app.services.monomer_dft_worker_client import (
    MonomerDftWorkerClient,
    MonomerDftWorkerError,
    MonomerDftWorkerStream,
)
from app.services import monomer_dft_worker_client as monomer_dft_worker_client_module


REQUEST_ADAPTER = TypeAdapter(MonomerDftRunRequest)
WORKER_TIMING_KEYS = (
    "queue_wait_ms",
    "gpu_wait_ms",
    "model_load_ms",
    "structure_prepare_ms",
    "model_compute_ms",
    "optimization_ms",
    "hessian_ms",
    "frequency_ms",
    "artifact_ms",
    "total_ms",
)


def test_hard_off_does_not_create_dft_worker_or_reconciler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_runtime(*_args, **_kwargs):
        raise AssertionError("hard-off must not construct DFT runtime objects")

    monkeypatch.setattr(main_module, "MonomerDftWorkerClient", unexpected_runtime)
    monkeypatch.setattr(main_module, "MonomerDftReconciler", unexpected_runtime)
    monkeypatch.setattr(main_module, "MonomerDftReadinessController", unexpected_runtime)

    app = main_module.create_app(
        Settings(
            monomer_dft_submit_enabled=False,
            monomer_dft_worker_uds="",
            monomer_dft_worker_base_url="http://production-fallback.invalid",
        )
    )

    assert app.state.monomer_dft_runtime_enabled is False
    assert app.state.monomer_dft_worker_client is None
    assert app.state.monomer_dft_reconciler is None
    assert app.state.monomer_dft_readiness_controller is None


class _AsyncBodyStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aiter__(self):
        yield self._body


def _worker_download_stream(
    body: bytes,
    *,
    content_length: int | None = None,
    sha256: str | None = None,
    content_encoding: str | None = None,
) -> MonomerDftWorkerStream:
    headers = {
        "Content-Length": str(len(body) if content_length is None else content_length),
        "ETag": f'"{sha256 or hashlib.sha256(body).hexdigest()}"',
    }
    if content_encoding is not None:
        headers["Content-Encoding"] = content_encoding
    return MonomerDftWorkerStream(
        httpx.Response(
            200,
            headers=headers,
            stream=_AsyncBodyStream(body),
            request=httpx.Request("GET", "http://monomer-dft-worker/artifact"),
        )
    )


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return output.getvalue()


def _single_point_request(**overrides):
    payload = {
        "input": {
            "smiles": "CCO",
            "net_charge": None,
            "multiplicity": 1,
            "psmiles_mode": None,
        },
        "calculation_type": "single_point",
        "model": "aimnet2",
        "conformer": {"seed": 1, "max_iterations": 500},
        "single_point": {"properties": ["forces", "energy", "charges"]},
    }
    payload.update(overrides)
    return payload


def _optimization_request(**overrides):
    payload = {
        "input": {
            "smiles": "CCO",
            "net_charge": None,
            "multiplicity": 1,
            "psmiles_mode": None,
        },
        "calculation_type": "optimization",
        "model": "aimnet2",
        "conformer": {"seed": 1, "max_iterations": 500},
        "optimization": {
            "fmax_eV_per_A": 0.01,
            "max_steps": 50,
            "post_optimization_properties": ["frequencies"],
        },
    }
    payload.update(overrides)
    return payload


def _worker_snapshot(
    submit_payload: dict,
    *,
    status: str = "queued",
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": 2,
        "job_id": submit_payload["job_id"],
        "attempt_token": submit_payload["attempt_token"],
        "request_sha256": submit_payload["request_sha256"],
        "enqueue_sequence": submit_payload["enqueue_sequence"],
        "worker_instance_id": "b" * 32,
        "status": status,
        "artifact_state": "none",
        "queue_position": 1 if status == "queued" else None,
        "stage": "queued",
        "progress_percent": 0,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "request": submit_payload,
        "result": None,
        "error": None,
        "timings": {key: 0.0 for key in WORKER_TIMING_KEYS},
        "artifacts": [],
    }


def _cancelled_worker_snapshot(submit_payload: dict) -> dict:
    snapshot = _worker_snapshot(submit_payload)
    finished = datetime.now(timezone.utc).isoformat()
    snapshot.update(
        {
            "status": "cancelled",
            "queue_position": None,
            "finished_at": finished,
            "updated_at": finished,
        }
    )
    return snapshot


def _completed_worker_snapshot(submit_payload: dict) -> dict:
    snapshot = _worker_snapshot(submit_payload)
    started = datetime.now(timezone.utc).isoformat()
    timings = {key: 0.0 for key in WORKER_TIMING_KEYS}
    timings["model_compute_ms"] = 1.25
    timings["total_ms"] = 2.5
    atomic_numbers = [6, 6, 8, 1, 1, 1, 1, 1, 1]
    symbols = ["C", "C", "O", "H", "H", "H", "H", "H", "H"]
    coordinates = [[float(index), 0.0, 0.0] for index in range(9)]
    snapshot.update(
        {
            "status": "completed",
            "artifact_state": "available",
            "queue_position": None,
            "stage": "artifacts",
            "progress_percent": 100,
            "started_at": started,
            "finished_at": started,
            "updated_at": started,
            "timings": timings,
            "result": {
                "schema_version": 1,
                "calculation_type": "single_point",
                "engine": "aimnet2",
                "model": "aimnet2",
                "input": {
                    "input_type": "smiles",
                    "canonical_smiles": "CCO",
                    "net_charge": 0,
                    "input_formal_charge": 0,
                    "multiplicity": 1,
                    "electron_count": 26,
                },
                "atoms": {
                    "count": 9,
                    "atomic_numbers": atomic_numbers,
                    "symbols": symbols,
                },
                "geometry": {
                    "initial_coordinates_angstrom": coordinates,
                    "final_coordinates_angstrom": coordinates,
                    "units": "angstrom",
                },
                "rdkit": {
                    "seed": 1,
                    "force_field": "MMFF94",
                    "optimization_performed": True,
                    "optimization_status": 0,
                    "optimization_state": "converged",
                },
                "properties": {
                    "energy": {"value_eV": -1.0},
                    "charges": {
                        "values_e": [0.0] * 9,
                        "sum_e": 0.0,
                        "conservation_error_e": 0.0,
                        "conserved": True,
                    },
                    "forces": {
                        "values_eV_per_A": [[0.0, 0.0, 0.0] for _ in range(9)],
                        "fmax_eV_per_A": 0.0,
                    },
                },
                "optimization": None,
                "scientific_status": {
                    "calculation_completed": True,
                    "geometry_status": "not_optimized",
                    "is_stationary": True,
                    "stationary_point": "not_evaluated",
                    "minimum_assessment": "unassessed",
                    "fmax_eV_per_A": 0.0,
                },
                "warnings": [
                    {
                        "code": "single_conformer",
                        "message": "Only one deterministic local conformer was evaluated.",
                    }
                ],
                "timings": timings,
                "provenance": {
                    "worker_version": "0.1.0",
                    "worker_instance_id": "b" * 32,
                    "model_alias": "aimnet2",
                    "model_id": "aimnet2",
                    "model_registry_key": None,
                    "model_family": None,
                    "model_reference": None,
                    "model_sha256": None,
                    "aimnet_version": None,
                    "aimnet_commit": None,
                    "aimnet_wheel_sha256": None,
                    "warp_version": None,
                    "torch_version": None,
                    "cuda_runtime": None,
                    "cuda_version": None,
                    "gpu_name": "RTX 4090",
                    "visible_gpu_count": 1,
                    "logical_device": "cuda:0",
                    "physical_gpu": "3",
                    "gpu_logical_device": "cuda:0",
                    "gpu_physical_device": "3",
                    "conformer_seed": 1,
                    "rdkit_force_field": "MMFF94",
                    "rdkit_optimization_performed": True,
                    "rdkit_optimization_status": 0,
                },
            },
            "artifacts": [
                {
                    "artifact_id": artifact_id,
                    "name": f"{artifact_id}.json",
                    "media_type": "application/json",
                    "size_bytes": 1,
                    "sha256": character * 64,
                }
                for artifact_id, character in (
                    ("initial_structure", "a"),
                    ("final_structure", "b"),
                    ("scientific_result", "c"),
                )
            ],
        }
    )
    return snapshot


def _completed_v2_worker_snapshot(submit_payload: dict, prepared) -> dict:
    snapshot = _completed_worker_snapshot(submit_payload)
    result = snapshot["result"]
    result["schema_version"] = 2
    result["input"].update(
        {
            "input_type": prepared.input_type,
            "canonical_smiles": prepared.canonical_smiles,
            "net_charge": prepared.effective_charge,
            "input_formal_charge": prepared.formal_charge,
            "electron_count": prepared.electron_count,
        }
    )
    result["atoms"]["atomic_numbers"] = list(prepared.atomic_numbers)
    result["atoms"]["isotope_mass_numbers"] = list(prepared.isotope_mass_numbers)
    result["atoms"]["atomic_masses_u"] = list(prepared.atomic_masses_u)
    result["provenance"].update(
        {
            "rdkit_version": "2026.03.3",
            "mass_source": "rdkit_periodic_table_explicit_isotopes",
            "execution_path": "primary",
            "gpu_uuid": "GPU-test",
            "gpu_budget_mib": 4096,
            "broker_instance_id": "broker-test",
            "lease_id": "lease-test",
            "fencing_token": 1,
        }
    )
    return snapshot


def _pending_database_job(prepared) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "job_id": str(uuid4()),
        "calculation_type": prepared.public_request["calculation_type"],
        "status": "pending",
        "request": prepared.public_request,
        "request_sha256": prepared.request_sha256,
        "attempt": 1,
        "queue_position": None,
        "stage": "queued",
        "progress_percent": 0.0,
        "scientific_status": None,
        "warnings": list(prepared.warnings),
        "result": None,
        "timings": {**{key: 0.0 for key in WORKER_TIMING_KEYS}, "end_to_end_ms": 0.0},
        "provenance": {},
        "error": None,
        "artifacts": [],
        "artifacts_state": "none",
        "artifacts_deleted": False,
        "cancel_requested": False,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "_idempotency_key": "fifo-test-0001",
        "_attempt_token": "a" * 64,
        "_enqueue_sequence": 1,
        "_worker_job_id": "",
        "_worker_id": None,
        "_worker_instance_id": None,
    }


def test_strict_discriminated_requests_normalize_property_order_without_adding_hessian() -> None:
    single = REQUEST_ADAPTER.validate_python(_single_point_request())
    assert single.single_point.properties == ["energy", "charges", "forces"]

    frequency = REQUEST_ADAPTER.validate_python(
        _single_point_request(single_point={"properties": ["frequencies"]})
    )
    assert frequency.single_point.properties == ["frequencies"]

    optimization = REQUEST_ADAPTER.validate_python(_optimization_request())
    assert optimization.optimization.post_optimization_properties == ["frequencies"]

    with pytest.raises(ValidationError):
        REQUEST_ADAPTER.validate_python(
            _single_point_request(single_point={"properties": ["energy", "energy"]})
        )
    with pytest.raises(ValidationError):
        REQUEST_ADAPTER.validate_python({**_single_point_request(), "spin": 0})
    with pytest.raises(ValidationError):
        REQUEST_ADAPTER.validate_python(
            _optimization_request(
                optimization={
                    "fmax_eV_per_A": 0.0009,
                    "max_steps": 50,
                    "post_optimization_properties": [],
                }
            )
        )


def test_prepared_hash_is_order_stable_and_explicit_charge_warning_is_preserved() -> None:
    first = REQUEST_ADAPTER.validate_python(_single_point_request())
    second = REQUEST_ADAPTER.validate_python(
        _single_point_request(single_point={"properties": ["charges", "forces", "energy"]})
    )
    assert prepare_monomer_dft_request(first).request_sha256 == prepare_monomer_dft_request(second).request_sha256

    charged_payload = _single_point_request()
    charged_payload["input"] = {**charged_payload["input"], "smiles": "CC", "net_charge": 2}
    charged = prepare_monomer_dft_request(REQUEST_ADAPTER.validate_python(charged_payload))
    assert charged.effective_charge == 2
    assert charged.public_request["input"]["net_charge"] == 2
    assert charged.warnings == (
        "Explicit net_charge overrides SMILES charge inference and differs from the encoded formal charge.",
    )

    same_charge_payload = _single_point_request()
    same_charge_payload["input"] = {**same_charge_payload["input"], "net_charge": 0}
    same_charge = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(same_charge_payload)
    )
    assert same_charge.warnings == (
        "Explicit net_charge overrides SMILES charge inference and matches the encoded formal charge.",
    )


@pytest.mark.parametrize(
    "payload",
    [
        _single_point_request(single_point={"properties": ["frequencies"]}),
        _optimization_request(),
    ],
)
def test_backend_and_worker_scientific_request_hashes_match(payload: dict) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from workers.monomer_dft_worker.app.schemas import JobSubmitRequest

    prepared = prepare_monomer_dft_request(REQUEST_ADAPTER.validate_python(payload))
    worker_request = JobSubmitRequest.model_validate(
        {
            "schema_version": 2,
            "job_id": str(uuid4()),
            "attempt_token": "a" * 64,
            "request_sha256": prepared.request_sha256,
            "enqueue_sequence": 1,
            **prepared.worker_request,
        }
    )
    assert worker_request.request_sha256 == prepared.request_sha256
    branch = "single_point" if payload["calculation_type"] == "single_point" else "optimization"
    assert "hessian" not in worker_request.model_dump(mode="json")[branch].get(
        "properties" if branch == "single_point" else "post_optimization_properties",
        [],
    )
    assert "hessian" in worker_request.properties


def test_backend_matches_every_shared_request_hash_vector() -> None:
    fixture = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts" / "monomer_dft_request_hash_vectors.json").read_text(
            encoding="utf-8"
        )
    )
    assert fixture["schema_version"] == 1
    assert fixture["vectors"]
    for vector in fixture["vectors"]:
        prepared = prepare_monomer_dft_request(
            REQUEST_ADAPTER.validate_python(vector["request"])
        )
        assert prepared.request_sha256 == vector["expected_sha256"], vector["name"]


def _backend_chemistry_error_code(message: str) -> str:
    if "multiple molecular fragments" in message or "exactly one connected molecule" in message:
        return "multi_fragment_input"
    if "must be null for ordinary SMILES" in message:
        return "invalid_psmiles_mode"
    if "PSMILES" in message or "attachment point" in message:
        return "invalid_psmiles"
    if "does not support atomic number" in message:
        return "unsupported_element"
    if "supports only net-neutral" in message:
        return "unsupported_charge"
    if "effective net charge must be between" in message:
        return "charge_out_of_range"
    if "Hessian calculations are limited" in message:
        return "hessian_molecule_too_large"
    if "exceeds the supported size limit" in message:
        return "molecule_too_large"
    return "unknown"


def test_backend_matches_shared_chemistry_corpus() -> None:
    fixture = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts" / "monomer_dft_chemistry_corpus.json").read_text(
            encoding="utf-8"
        )
    )
    assert fixture["schema_version"] == 1
    for case in fixture["cases"]:
        request = REQUEST_ADAPTER.validate_python(case["request"])
        expected = case["expected"]
        if not expected["accepted"]:
            with pytest.raises(MonomerDftRequestError) as caught:
                prepare_monomer_dft_request(request)
            assert _backend_chemistry_error_code(str(caught.value)) == expected["error_code"], case["id"]
            continue
        prepared = prepare_monomer_dft_request(request)
        actual = {
            "accepted": True,
            "input_type": prepared.input_type,
            "canonical_smiles": prepared.canonical_smiles,
            "formal_charge": prepared.formal_charge,
            "effective_charge": prepared.effective_charge,
            "electron_count": prepared.electron_count,
            "heavy_atom_count": prepared.heavy_atoms,
            "atom_count": prepared.total_atoms,
            "atomic_numbers": list(prepared.atomic_numbers),
        }
        assert actual == expected, case["id"]


def test_protocol_rejects_psmiles_and_model_domain_errors() -> None:
    ordinary_with_mode = _single_point_request()
    ordinary_with_mode["input"] = {**ordinary_with_mode["input"], "psmiles_mode": "cap"}
    with pytest.raises(MonomerDftRequestError, match="must be null"):
        prepare_monomer_dft_request(REQUEST_ADAPTER.validate_python(ordinary_with_mode))

    psmiles_without_mode = _single_point_request()
    psmiles_without_mode["input"] = {**psmiles_without_mode["input"], "smiles": "*CC*"}
    with pytest.raises(MonomerDftRequestError, match="required"):
        prepare_monomer_dft_request(REQUEST_ADAPTER.validate_python(psmiles_without_mode))

    valid_cap = _single_point_request()
    valid_cap["input"] = {**valid_cap["input"], "smiles": "*CC*", "psmiles_mode": "cap"}
    assert prepare_monomer_dft_request(REQUEST_ADAPTER.validate_python(valid_cap)).canonical_smiles == "CC"

    unsupported = _single_point_request()
    unsupported["input"] = {**unsupported["input"], "smiles": "[Na+]", "net_charge": 1}
    with pytest.raises(MonomerDftRequestError, match="does not support"):
        prepare_monomer_dft_request(REQUEST_ADAPTER.validate_python(unsupported))

    for charged_smiles in (
        "[NH3+]" + "[NH2+]" * 4 + "[NH3+]",
        "[BH3-]" + "[BH2-]" * 4 + "[BH3-]",
    ):
        inferred_charge = _single_point_request()
        inferred_charge["input"] = {
            **inferred_charge["input"],
            "smiles": charged_smiles,
            "net_charge": None,
        }
        with pytest.raises(MonomerDftRequestError) as charge_error:
            prepare_monomer_dft_request(REQUEST_ADAPTER.validate_python(inferred_charge))
        assert charge_error.value.code == "charge_out_of_range"


def test_protocol_preserves_isotope_masses_and_exposes_typed_unsupported_error() -> None:
    isotopic = _single_point_request()
    isotopic["input"] = {**isotopic["input"], "smiles": "[13CH4]"}
    prepared = prepare_monomer_dft_request(REQUEST_ADAPTER.validate_python(isotopic))
    assert len(prepared.atomic_numbers) == len(prepared.isotope_mass_numbers)
    assert len(prepared.atomic_numbers) == len(prepared.atomic_masses_u)
    assert prepared.isotope_mass_numbers[0] == 13
    assert prepared.atomic_masses_u[0] == pytest.approx(13.003354835, rel=1e-7)
    assert all(mass > 0.0 for mass in prepared.atomic_masses_u)

    unsupported = _single_point_request()
    unsupported["input"] = {**unsupported["input"], "smiles": "[999C]"}
    with pytest.raises(MonomerDftRequestError) as caught:
        prepare_monomer_dft_request(REQUEST_ADAPTER.validate_python(unsupported))
    assert caught.value.code == "unsupported_isotope"

    class Repository:
        def find_idempotent_job(self, **_kwargs):
            return None

    app = create_app(Settings())
    app.state.monomer_dft_repository = Repository()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/monomer-dft/jobs",
            json=unsupported,
            headers={"Idempotency-Key": "isotope-test-0001"},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "unsupported_isotope"


def test_isotope_mass_lookup_exception_is_typed_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rdkit import Chem as RealChem

    class PeriodicTable:
        def GetAtomicWeight(self, atomic_number: int):
            return RealChem.GetPeriodicTable().GetAtomicWeight(atomic_number)

        def GetMassForIsotope(self, _atomic_number: int, _isotope: int):
            raise RuntimeError("lookup failed")

    class ChemProxy:
        def __getattr__(self, name: str):
            return getattr(RealChem, name)

        @staticmethod
        def GetPeriodicTable():
            return PeriodicTable()

    monkeypatch.setattr(
        "app.services.monomer_dft_protocol._load_rdkit",
        lambda: ChemProxy(),
    )
    payload = _single_point_request()
    payload["input"] = {**payload["input"], "smiles": "[13C]"}
    with pytest.raises(MonomerDftRequestError) as caught:
        prepare_monomer_dft_request(REQUEST_ADAPTER.validate_python(payload))
    assert caught.value.code == "unsupported_isotope"


def test_worker_payload_sanitizers_project_small_safe_summary() -> None:
    result = sanitize_result(
        {
            "runtime_path": "/secret/runtime",
            "input": {
                "canonical_smiles": r"F/C=C(\F)C",
                "diagnostic": "/secret/input-path",
            },
            "provenance": {
                "execution_path": "primary",
                "lease_id": "lease-test",
                "fencing_token": 1,
                "broker_instance_id": "broker-test",
                "gpu_lease_id": "lease-test",
                "gpu_fencing_token": 1,
                "gpu_broker_instance_id": "broker-test",
            },
            "properties": {
                "hessian": {
                    "shape": [9, 9],
                    "matrix": [[1.0] * 9] * 9,
                    "artifact_id": "hessian",
                }
            },
            "optimization": {
                "trace": [
                    {
                        "step": 1,
                        "energy_eV": -10.5,
                        "fmax_eV_per_A": 0.02,
                        "coordinates_angstrom": [[0.0, 0.0, 0.0]] * 300,
                    },
                    {"step": "bad", "energy_eV": 0.0, "fmax_eV_per_A": 0.0},
                ]
            },
        }
    )
    assert result is not None
    assert "runtime_path" not in result
    assert result["input"]["canonical_smiles"] == r"F/C=C(\F)C"
    assert "/secret" not in result["input"]["diagnostic"]
    assert result["provenance"] == {
        "execution_path": "primary",
        "lease_id": "lease-test",
        "fencing_token": 1,
        "broker_instance_id": "broker-test",
    }
    assert "matrix" not in result["properties"]["hessian"]
    assert result["optimization"]["trace"] == [
        {"step": 1, "energy_eV": -10.5, "fmax_eV_per_A": 0.02}
    ]

    # Generic public JSON cannot bypass path redaction by naming a field after
    # the scientific result key.  Result preservation is a separate, narrowly
    # validated path and rejects path-shaped impostors.
    generic = sanitize_public_json({"canonical_smiles": "/secret/model.pt"})
    assert "/secret" not in generic["canonical_smiles"]
    forged_result = sanitize_result(
        {"input": {"canonical_smiles": "/secret/model.pt"}}
    )
    assert forged_result == {"input": {}}
    for relative_path in ("../secret/model.pt", r"..\secret\model.pt"):
        forged_result = sanitize_result(
            {"input": {"canonical_smiles": relative_path}}
        )
        assert forged_result == {"input": {}}
    stereo_result = sanitize_result(
        {"input": {"canonical_smiles": r"F/C=C(\F)C"}}
    )
    assert stereo_result == {"input": {"canonical_smiles": r"F/C=C(\F)C"}}

    timings = sanitize_timings({"model_compute_ms": 12, "unexpected_ms": 99, "total_ms": float("nan")})
    assert set(timings) == {
        "queue_wait_ms",
        "gpu_wait_ms",
        "model_load_ms",
        "structure_prepare_ms",
        "model_compute_ms",
        "optimization_ms",
        "hessian_ms",
        "frequency_ms",
        "artifact_ms",
        "total_ms",
    }
    assert timings["model_compute_ms"] == 12.0
    assert timings["total_ms"] == 0.0

    error = sanitize_error(
        {"code": "failure", "message": "failed at '(/secret/model.pt)'", "retryable": False, "details": {"path": "/secret"}}
    )
    assert error is not None
    assert "/secret" not in error["message"]
    assert error["details"] == {}
    assert normalize_artifacts(
        [
            {
                "artifact_id": "result",
                "name": "../result.json",
                "media_type": "application/json",
                "size_bytes": 10,
                "sha256": "a" * 64,
            }
        ]
    ) == []


def test_async_worker_client_uses_exact_payload_and_structured_error_contract() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "runtime_ready": True})
        if request.url.path == "/jobs" and request.method == "POST":
            return httpx.Response(200, json=_worker_snapshot(json.loads(request.content)))
        if request.url.path.endswith("/cancel"):
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": "attempt_conflict",
                        "message": "attempt conflicts with /secret/token",
                        "retryable": False,
                        "details": {
                            "reason": "attempt already exists",
                            "runtime_path": "/secret/token",
                        },
                    }
                },
            )
        raise AssertionError(request.url)

    async def scenario() -> None:
        raw_client = httpx.AsyncClient(
            base_url="http://monomer-dft-worker",
            transport=httpx.MockTransport(handler),
        )
        client = MonomerDftWorkerClient(
            base_url="http://monomer-dft-worker",
            uds_path="/unused/test.sock",
            validation_limiter=anyio.CapacityLimiter(1),
            client=raw_client,
        )
        assert (await client.health())["status"] == "ok"
        job_id = str(uuid4())
        prepared = prepare_monomer_dft_request(
            REQUEST_ADAPTER.validate_python(_single_point_request())
        )
        job = {
            "job_id": job_id,
            "_attempt_token": "a" * 64,
            "_enqueue_sequence": 1,
            "request_sha256": prepared.request_sha256,
            "request": prepared.worker_request,
        }
        queued = await client.submit_job(job)
        assert queued["job_id"] == job_id
        submitted = json.loads(seen[-1].content)
        assert submitted == {
            "schema_version": 2,
            "job_id": job_id,
            "attempt_token": "a" * 64,
            "request_sha256": prepared.request_sha256,
            "enqueue_sequence": 1,
            **prepared.worker_request,
        }
        with pytest.raises(MonomerDftWorkerError) as caught:
            await client.cancel_job(job)
        assert caught.value.status_code == 409
        assert caught.value.code == "attempt_conflict"
        assert "/secret" not in str(caught.value)
        assert caught.value.details == {"reason": "attempt already exists"}
        assert json.loads(seen[-1].content) == submitted
        await raw_client.aclose()

    asyncio.run(scenario())


def test_worker_snapshot_validation_uses_shared_limiter_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )
    job_id = str(uuid4())
    payload = {
        "schema_version": 2,
        "job_id": job_id,
        "attempt_token": "a" * 64,
        "request_sha256": prepared.request_sha256,
        "enqueue_sequence": 1,
        **prepared.worker_request,
    }
    snapshot = _worker_snapshot(payload)
    validation_started = threading.Event()
    validation_threads: list[int] = []
    original_validate = InternalWorkerSnapshot.model_validate

    def slow_validate(value):
        validation_threads.append(threading.get_ident())
        validation_started.set()
        time.sleep(0.05)
        return original_validate(value)

    monkeypatch.setattr(
        InternalWorkerSnapshot,
        "model_validate",
        staticmethod(slow_validate),
    )

    async def scenario() -> None:
        limiter = anyio.CapacityLimiter(1)
        raw_client = httpx.AsyncClient(
            base_url="http://monomer-dft-worker",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=snapshot)
            ),
        )
        client = MonomerDftWorkerClient(
            base_url="http://monomer-dft-worker",
            uds_path="/unused/test.sock",
            validation_limiter=limiter,
            client=raw_client,
        )
        event_loop_thread = threading.get_ident()

        async with limiter:
            pending = asyncio.create_task(client.get_job(job_id))
            await asyncio.sleep(0.02)
            assert validation_started.is_set() is False
            assert pending.done() is False

        for _ in range(100):
            if validation_started.is_set():
                break
            await asyncio.sleep(0.001)
        assert validation_started.is_set() is True
        await asyncio.sleep(0.005)
        assert pending.done() is False
        assert len(validation_threads) == 1
        assert validation_threads[0] != event_loop_thread
        assert (await pending)["job_id"] == job_id
        await raw_client.aclose()

    asyncio.run(scenario())


def test_worker_json_response_body_is_streamed_under_a_hard_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        monomer_dft_worker_client_module,
        "MAX_WORKER_JSON_BYTES",
        32,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_AsyncBodyStream(b'{' + (b'"x"' * 16) + b'}'),
        )

    async def scenario() -> None:
        raw_client = httpx.AsyncClient(
            base_url="http://monomer-dft-worker",
            transport=httpx.MockTransport(handler),
        )
        client = MonomerDftWorkerClient(
            base_url="http://monomer-dft-worker",
            uds_path="/unused/test.sock",
            validation_limiter=anyio.CapacityLimiter(1),
            client=raw_client,
        )
        with pytest.raises(MonomerDftWorkerError) as caught:
            await client.health()
        assert caught.value.status_code == 502
        assert caught.value.code == "invalid_worker_response"
        await raw_client.aclose()

    asyncio.run(scenario())


def test_worker_client_accepts_a_fenced_unknown_job_cancel_tombstone() -> None:
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )
    job = {
        "job_id": str(uuid4()),
        "_attempt_token": "a" * 64,
        "_enqueue_sequence": 17,
        "request_sha256": prepared.request_sha256,
        "request": prepared.worker_request,
    }
    seen_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/jobs/{job['job_id']}/cancel"
        payload = json.loads(request.content)
        seen_payloads.append(payload)
        return httpx.Response(200, json=_cancelled_worker_snapshot(payload))

    async def scenario() -> None:
        raw_client = httpx.AsyncClient(
            base_url="http://monomer-dft-worker",
            transport=httpx.MockTransport(handler),
        )
        client = MonomerDftWorkerClient(
            base_url="http://monomer-dft-worker",
            uds_path="/unused/test.sock",
            validation_limiter=anyio.CapacityLimiter(1),
            client=raw_client,
        )
        snapshot = await client.cancel_job(job)
        assert snapshot["status"] == "cancelled"
        assert snapshot["attempt_token"] == job["_attempt_token"]
        assert snapshot["enqueue_sequence"] == job["_enqueue_sequence"]
        assert seen_payloads == [
            {
                "schema_version": 2,
                "job_id": job["job_id"],
                "attempt_token": job["_attempt_token"],
                "request_sha256": job["request_sha256"],
                "enqueue_sequence": job["_enqueue_sequence"],
                **prepared.worker_request,
            }
        ]
        await raw_client.aclose()

    asyncio.run(scenario())


def test_worker_snapshot_boundary_rejects_terminal_null_and_incomplete_artifact_manifest() -> None:
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )
    submit_payload = {
        "schema_version": 2,
        "job_id": str(uuid4()),
        "attempt_token": "a" * 64,
        "request_sha256": prepared.request_sha256,
        "enqueue_sequence": 1,
        **prepared.worker_request,
    }
    valid = _completed_worker_snapshot(submit_payload)
    validated = InternalWorkerSnapshot.model_validate(valid)
    assert validated.status == "completed"

    legacy_snapshot_timings = deepcopy(valid)
    legacy_snapshot_timings["timings"] = dict(legacy_snapshot_timings["timings"])
    del legacy_snapshot_timings["timings"]["gpu_wait_ms"]
    del legacy_snapshot_timings["timings"]["model_load_ms"]
    with pytest.raises(ValidationError, match="snapshot v2 timings"):
        InternalWorkerSnapshot.model_validate(legacy_snapshot_timings)

    normalized_artifacts = normalize_artifacts(
        validated.model_dump(mode="json")["artifacts"]
    )
    assert [artifact["artifact_id"] for artifact in normalized_artifacts] == [
        artifact.artifact_id for artifact in validated.artifacts
    ]
    assert [artifact["name"] for artifact in normalized_artifacts] == [
        artifact.name for artifact in validated.artifacts
    ]

    completed_without_result = {**valid, "result": None}
    with pytest.raises(ValidationError):
        InternalWorkerSnapshot.model_validate(completed_without_result)

    incomplete_manifest = {**valid, "artifacts": valid["artifacts"][:-1]}
    with pytest.raises(ValidationError):
        InternalWorkerSnapshot.model_validate(incomplete_manifest)

    duplicate_manifest = {
        **valid,
        "artifacts": [*valid["artifacts"], dict(valid["artifacts"][0])],
    }
    with pytest.raises(ValidationError):
        InternalWorkerSnapshot.model_validate(duplicate_manifest)

    for unsafe_name in ("unsafe name.json", "result.", "CON", "com1.txt"):
        unsafe_manifest = deepcopy(valid)
        unsafe_manifest["artifacts"][0]["name"] = unsafe_name
        with pytest.raises(ValidationError):
            InternalWorkerSnapshot.model_validate(unsafe_manifest)
    unsafe_media_type = deepcopy(valid)
    unsafe_media_type["artifacts"][0]["media_type"] = "application/json\r\nX-Unsafe: yes"
    with pytest.raises(ValidationError):
        InternalWorkerSnapshot.model_validate(unsafe_media_type)


@pytest.mark.parametrize("artifact_state", ("none", "deleting", "deleted"))
def test_completed_snapshot_accepts_hidden_manifest_after_artifact_removal(
    artifact_state: str,
) -> None:
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )
    submit_payload = {
        "schema_version": 2,
        "job_id": str(uuid4()),
        "attempt_token": "a" * 64,
        "request_sha256": prepared.request_sha256,
        "enqueue_sequence": 1,
        **prepared.worker_request,
    }
    snapshot = _completed_worker_snapshot(submit_payload)
    snapshot["artifact_state"] = artifact_state
    snapshot["artifacts"] = []

    validated = InternalWorkerSnapshot.model_validate(snapshot)
    assert validated.status == "completed"
    assert validated.artifact_state == artifact_state
    assert validated.artifacts == []

    malformed_available = deepcopy(snapshot)
    malformed_available["artifact_state"] = "available"
    with pytest.raises(ValidationError, match="completed manifest"):
        InternalWorkerSnapshot.model_validate(malformed_available)


def test_worker_snapshot_dual_reads_v1_and_requires_v2_mass_provenance() -> None:
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )
    submit_payload = {
        "schema_version": 2,
        "job_id": str(uuid4()),
        "attempt_token": "a" * 64,
        "request_sha256": prepared.request_sha256,
        "enqueue_sequence": 1,
        **prepared.worker_request,
    }
    v1 = _completed_worker_snapshot(submit_payload)
    assert InternalWorkerSnapshot.model_validate(v1).result.schema_version == 1

    legacy_v1 = deepcopy(v1)
    del legacy_v1["result"]["rdkit"]["optimization_performed"]
    del legacy_v1["result"]["rdkit"]["optimization_state"]
    del legacy_v1["result"]["provenance"]["rdkit_optimization_performed"]
    del legacy_v1["result"]["provenance"]["rdkit_optimization_status"]
    validated_legacy = InternalWorkerSnapshot.model_validate(legacy_v1)
    assert validated_legacy.result is not None
    assert validated_legacy.result.schema_version == 1
    assert validated_legacy.result.rdkit.optimization_performed is None
    assert validated_legacy.result.provenance.rdkit_optimization_status is None

    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "monomer_dft_scientific_result_v1_legacy.json"
    )
    fixture_result = InternalScientificResult.model_validate(
        json.loads(fixture_path.read_text(encoding="utf-8"))
    )
    assert fixture_result.schema_version == 1
    assert fixture_result.rdkit.optimization_performed is None
    assert fixture_result.rdkit.optimization_state is None
    assert fixture_result.provenance.rdkit_optimization_performed is None
    assert fixture_result.provenance.rdkit_optimization_status is None

    v2 = deepcopy(v1)
    v2["result"]["schema_version"] = 2
    v2["result"]["atoms"]["isotope_mass_numbers"] = list(
        prepared.isotope_mass_numbers
    )
    v2["result"]["atoms"]["atomic_masses_u"] = list(prepared.atomic_masses_u)
    v2["result"]["provenance"]["rdkit_version"] = "2026.03.3"
    v2["result"]["provenance"][
        "mass_source"
    ] = "rdkit_periodic_table_explicit_isotopes"
    v2["result"]["provenance"].update(
        {
            "execution_path": "primary",
            "gpu_uuid": "GPU-test",
            "gpu_budget_mib": 4096,
            "broker_instance_id": "broker-test",
            "lease_id": "lease-test",
            "fencing_token": 1,
        }
    )
    assert InternalWorkerSnapshot.model_validate(v2).result.schema_version == 2

    missing_gpu_provenance = deepcopy(v2)
    del missing_gpu_provenance["result"]["provenance"]["lease_id"]
    with pytest.raises(ValidationError, match="runtime provenance"):
        InternalWorkerSnapshot.model_validate(missing_gpu_provenance)

    inconsistent_gpu_provenance = deepcopy(v2)
    inconsistent_gpu_provenance["result"]["provenance"]["gpu_physical_device"] = "1"
    with pytest.raises(ValidationError, match="physical GPU provenance"):
        InternalWorkerSnapshot.model_validate(inconsistent_gpu_provenance)

    missing_mass = deepcopy(v2)
    del missing_mass["result"]["atoms"]["atomic_masses_u"]
    with pytest.raises(ValidationError):
        InternalWorkerSnapshot.model_validate(missing_mass)

    v2_missing_runtime_provenance = deepcopy(v2)
    del v2_missing_runtime_provenance["result"]["rdkit"]["optimization_performed"]
    del v2_missing_runtime_provenance["result"]["rdkit"]["optimization_state"]
    with pytest.raises(ValidationError, match="runtime provenance"):
        InternalWorkerSnapshot.model_validate(v2_missing_runtime_provenance)

    forged_isotopes = deepcopy(v2)
    forged_isotopes["result"]["atoms"]["isotope_mass_numbers"] = [999] * len(
        prepared.isotope_mass_numbers
    )
    forged_isotopes["result"]["atoms"]["atomic_masses_u"] = [999.0] * len(
        prepared.atomic_masses_u
    )
    with pytest.raises(ValidationError, match="isotope labels"):
        InternalWorkerSnapshot.model_validate(forged_isotopes)

    forged_masses = deepcopy(v2)
    forged_masses["result"]["atoms"]["atomic_masses_u"] = [999.0] * len(
        prepared.atomic_masses_u
    )
    with pytest.raises(ValidationError, match="RDKit mass table"):
        InternalWorkerSnapshot.model_validate(forged_masses)

    forged_source = deepcopy(v2)
    forged_source["result"]["provenance"]["mass_source"] = "arbitrary_nonempty_source"
    with pytest.raises(ValidationError, match="mass source"):
        InternalWorkerSnapshot.model_validate(forged_source)

    for field, forged_value in (
        ("canonical_smiles", "CO"),
        ("input_type", "psmiles_cap"),
        ("input_formal_charge", 1),
    ):
        forged_identity = deepcopy(v2)
        forged_identity["result"]["input"][field] = forged_value
        with pytest.raises(ValidationError, match="canonical request"):
            InternalWorkerSnapshot.model_validate(forged_identity)

    forged_effective_charge = deepcopy(v2)
    forged_effective_charge["result"]["input"]["net_charge"] = 1
    forged_effective_charge["result"]["input"]["electron_count"] = (
        prepared.electron_count - 1
    )
    forged_effective_charge["result"]["properties"]["charges"].update(
        {
            "values_e": [1.0, *([0.0] * (len(prepared.atomic_numbers) - 1))],
            "sum_e": 1.0,
            "conservation_error_e": 0.0,
            "conserved": True,
        }
    )
    with pytest.raises(ValidationError, match="charge does not match the request"):
        InternalWorkerSnapshot.model_validate(forged_effective_charge)


def test_v2_scientific_status_is_bound_to_forces_convergence_and_frequencies() -> None:
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )
    submit_payload = {
        "schema_version": 2,
        "job_id": str(uuid4()),
        "attempt_token": "a" * 64,
        "request_sha256": prepared.request_sha256,
        "enqueue_sequence": 1,
        **prepared.worker_request,
    }
    snapshot = _completed_v2_worker_snapshot(submit_payload, prepared)
    result = snapshot["result"]
    assert InternalScientificResult.model_validate(result).schema_version == 2

    # V1 remains permissive for historical result reads.
    legacy = deepcopy(_completed_worker_snapshot(submit_payload)["result"])
    legacy["scientific_status"]["minimum_assessment"] = "confirmed_minimum"
    assert InternalScientificResult.model_validate(legacy).schema_version == 1

    forged_single_point = deepcopy(result)
    forged_single_point["scientific_status"]["minimum_assessment"] = "confirmed_minimum"
    with pytest.raises(ValidationError, match="minimum assessment"):
        InternalScientificResult.model_validate(forged_single_point)

    with_frequency = deepcopy(result)
    with_frequency["properties"]["frequencies"] = {
        "artifact_id": "frequencies",
        "values_cm_1": [-50.0, 0.0, 100.0],
        "mode_count": 3,
        "removed_rigid_modes": 6,
        "expected_rigid_modes": 6,
        "linear_molecule": False,
        "imaginary_threshold_cm_1": -10.0,
        "imaginary_mode_count": 1,
        "imaginary_values_cm_1": [-50.0],
        "near_zero_mode_count": 1,
    }
    with_frequency["scientific_status"]["stationary_point"] = "first_order_saddle"
    assert InternalScientificResult.model_validate(with_frequency).schema_version == 2

    forged_threshold = deepcopy(with_frequency)
    forged_threshold["properties"]["frequencies"]["imaginary_threshold_cm_1"] = -20.0
    with pytest.raises(ValidationError, match="imaginary-mode threshold"):
        InternalScientificResult.model_validate(forged_threshold)

    forged_imaginary = deepcopy(with_frequency)
    forged_imaginary["properties"]["frequencies"]["values_cm_1"][0] = -5.0
    with pytest.raises(ValidationError, match="imaginary frequencies"):
        InternalScientificResult.model_validate(forged_imaginary)

    optimization = deepcopy(with_frequency)
    optimization["calculation_type"] = "optimization"
    optimization["optimization"] = {
        "converged": True,
        "steps": 1,
        "fmax_threshold_eV_per_A": 0.01,
        "max_steps": 50,
        "trajectory_artifact_id": "optimization_trajectory",
        "trace": [{"step": 1, "energy_eV": -1.0, "fmax_eV_per_A": 0.0}],
    }
    optimization["scientific_status"].update(
        {
            "geometry_status": "converged",
            "minimum_assessment": "nonminimum_or_saddle",
        }
    )
    assert InternalScientificResult.model_validate(optimization).schema_version == 2

    forged_convergence = deepcopy(optimization)
    forged_convergence["optimization"]["converged"] = False
    with pytest.raises(ValidationError, match="optimization convergence"):
        InternalScientificResult.model_validate(forged_convergence)

    forged_saddle = deepcopy(optimization)
    forged_saddle["scientific_status"]["stationary_point"] = "minimum"
    with pytest.raises(ValidationError, match="stationary-point assessment"):
        InternalScientificResult.model_validate(forged_saddle)


def test_completed_v2_snapshot_preserves_large_input_formal_charge_with_override() -> None:
    nitrogen_count = 21
    smiles = (
        "[NH3+]"
        + "[NH2+]" * (nitrogen_count - 2)
        + "[NH3+]"
    )
    request = REQUEST_ADAPTER.validate_python(
        _single_point_request(
            input={
                "smiles": smiles,
                "net_charge": 1,
                "multiplicity": 1,
                "psmiles_mode": None,
            }
        )
    )
    prepared = prepare_monomer_dft_request(request)
    assert prepared.formal_charge == 21
    assert prepared.effective_charge == 1

    submit_payload = {
        "schema_version": 2,
        "job_id": str(uuid4()),
        "attempt_token": "a" * 64,
        "request_sha256": prepared.request_sha256,
        "enqueue_sequence": 1,
        **prepared.worker_request,
    }
    snapshot = _completed_worker_snapshot(submit_payload)
    result = snapshot["result"]
    atom_count = len(prepared.atomic_numbers)
    coordinates = [[float(index), 0.0, 0.0] for index in range(atom_count)]
    charges = [1.0, *([0.0] * (atom_count - 1))]
    result["schema_version"] = 2
    result["input"] = {
        "input_type": prepared.input_type,
        "canonical_smiles": prepared.canonical_smiles,
        "net_charge": prepared.effective_charge,
        "input_formal_charge": prepared.formal_charge,
        "multiplicity": 1,
        "electron_count": prepared.electron_count,
    }
    result["atoms"] = {
        "count": atom_count,
        "atomic_numbers": list(prepared.atomic_numbers),
        "isotope_mass_numbers": list(prepared.isotope_mass_numbers),
        "atomic_masses_u": list(prepared.atomic_masses_u),
        "symbols": ["N" if number == 7 else "H" for number in prepared.atomic_numbers],
    }
    result["geometry"] = {
        "initial_coordinates_angstrom": coordinates,
        "final_coordinates_angstrom": coordinates,
        "units": "angstrom",
    }
    result["properties"]["charges"] = {
        "values_e": charges,
        "sum_e": 1.0,
        "conservation_error_e": 0.0,
        "conserved": True,
    }
    result["properties"]["forces"] = {
        "values_eV_per_A": [[0.0, 0.0, 0.0] for _ in range(atom_count)],
        "fmax_eV_per_A": 0.0,
    }
    result["warnings"].append(
        {
            "code": "net_charge_override",
            "message": "Explicit net_charge overrides the encoded formal charge.",
        }
    )
    result["provenance"]["rdkit_version"] = "2026.03.3"
    result["provenance"][
        "mass_source"
    ] = "rdkit_periodic_table_explicit_isotopes"
    result["provenance"].update(
        {
            "execution_path": "primary",
            "gpu_uuid": "GPU-test",
            "gpu_budget_mib": 4096,
            "broker_instance_id": "broker-test",
            "lease_id": "lease-test",
            "fencing_token": 1,
        }
    )

    validated = InternalWorkerSnapshot.model_validate(snapshot)
    assert validated.result is not None
    assert validated.result.input.input_formal_charge == 21
    assert validated.result.input.net_charge == 1


def test_public_artifact_uses_the_same_portable_filename_contract() -> None:
    payload = {
        "artifact_id": "scientific_result",
        "name": "scientific_result.json",
        "media_type": "application/json",
        "size_bytes": 1,
        "sha256": "a" * 64,
        "available": True,
    }
    assert MonomerDftArtifact.model_validate(payload).name == "scientific_result.json"
    for unsafe_name in ("unsafe name.json", "result.", "NUL", "lpt9.csv"):
        with pytest.raises(ValidationError):
            MonomerDftArtifact.model_validate({**payload, "name": unsafe_name})


def test_backend_models_match_shared_monomer_dft_api_contract() -> None:
    contract = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "contracts"
            / "monomer_dft_api_contract_v1.json"
        ).read_text(encoding="utf-8")
    )
    for example in contract["delete_examples"]:
        validated = MonomerDftArtifactDeleteResponse.model_validate(example["body"])
        assert validated.model_dump(mode="json") == example["body"]

    states = set(contract["artifacts_states"])
    assert set(get_args(MonomerDftArtifactsState)) == states
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )
    for artifact_state in states:
        job = _pending_database_job(prepared)
        job["artifacts_state"] = artifact_state
        job["artifacts_deleted"] = artifact_state == "deleted"
        public = MonomerDftRepository.public_job(job)
        assert MonomerDftJobResponse.model_validate(public).artifacts_state == artifact_state

    scientific = contract["scientific_results"]
    assert set(get_args(InternalScientificResult.model_fields["schema_version"].annotation)) == set(
        scientific["readable_schema_versions"]
    )
    assert get_args(InternalWorkerRequest.model_fields["schema_version"].annotation) == (
        scientific["produced_schema_version"],
    )
    assert get_args(InternalWorkerSnapshot.model_fields["schema_version"].annotation) == (
        contract["worker_http_protocol_version"],
    )
    assert tuple(scientific["v2_required_timing_fields"]) == WORKER_TIMING_KEYS
    assert set(
        get_args(InternalWorkerSnapshot.model_fields["artifact_state"].annotation)
    ) == set(contract["worker_snapshot_artifact_states"])
    assert contract["completed_delete_count_source"] == "persisted_artifact_manifest"
    assert set(contract["stable_error_codes"]).issubset(MONOMER_DFT_STABLE_ERROR_CODES)
    assert contract["database_schema_gate"] == {
        "migration_version": "0013_monomer_dft_jobs",
        "migration_checksum_sha256": (
            "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc"
        ),
        "readiness_field": "schema_ready",
        "safe_without_schema": ["/status", "/capabilities"],
        "guarded_resource_prefixes": ["/jobs"],
        "not_ready_error": {
            "http_status": 503,
            "code": "schema_not_ready",
            "retry_after_seconds": 5,
        },
    }


def test_worker_client_maps_malformed_terminal_snapshot_to_stable_502() -> None:
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )
    job_id = str(uuid4())
    payload = {
        "schema_version": 2,
        "job_id": job_id,
        "attempt_token": "a" * 64,
        "request_sha256": prepared.request_sha256,
        "enqueue_sequence": 1,
        **prepared.worker_request,
    }
    malformed = _completed_worker_snapshot(payload)
    malformed["result"] = None

    async def scenario() -> None:
        raw_client = httpx.AsyncClient(
            base_url="http://monomer-dft-worker",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=malformed)),
        )
        client = MonomerDftWorkerClient(
            base_url="http://monomer-dft-worker",
            uds_path="/unused/test.sock",
            validation_limiter=anyio.CapacityLimiter(1),
            client=raw_client,
        )
        with pytest.raises(MonomerDftWorkerError) as caught:
            await client.get_job(job_id)
        assert caught.value.status_code == 502
        assert caught.value.code == "invalid_worker_response"
        assert caught.value.retryable is True
        await raw_client.aclose()

    asyncio.run(scenario())


def test_unsafe_worker_artifact_name_is_stable_502_and_snapshot_is_not_applied() -> None:
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )
    job = _pending_database_job(prepared)
    job["status"] = "running"
    submit_payload = {
        "schema_version": 2,
        "job_id": job["job_id"],
        "attempt_token": job["_attempt_token"],
        "request_sha256": prepared.request_sha256,
        "enqueue_sequence": job["_enqueue_sequence"],
        **prepared.worker_request,
    }
    malformed = _completed_worker_snapshot(submit_payload)
    malformed["artifacts"][0]["name"] = "unsafe artifact.json"

    class Repository:
        def __init__(self) -> None:
            self.applied = False
            self.recorded_code: str | None = None

        def apply_worker_snapshot(self, **_kwargs):
            self.applied = True
            raise AssertionError("unsafe Worker snapshot must not be applied")

        def record_dispatch_error(self, *, code: str, **_kwargs):
            self.recorded_code = code

    async def scenario() -> None:
        raw_client = httpx.AsyncClient(
            base_url="http://monomer-dft-worker",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=malformed)
            ),
        )
        worker = MonomerDftWorkerClient(
            base_url="http://monomer-dft-worker",
            uds_path="/unused/test.sock",
            validation_limiter=anyio.CapacityLimiter(1),
            client=raw_client,
        )
        repository = Repository()
        reconciler = MonomerDftReconciler(
            repository=repository,  # type: ignore[arg-type]
            worker=worker,
            interval_seconds=1,
        )
        assert await reconciler.reconcile_job(job) is None
        assert repository.applied is False
        assert repository.recorded_code == "invalid_worker_response"
        await raw_client.aclose()

    asyncio.run(scenario())


def test_reconciler_skips_when_postgres_leader_lock_is_not_acquired_off_event_loop() -> None:
    class Repository:
        def __init__(self) -> None:
            self.enter_thread: int | None = None
            self.list_called = False

        @contextmanager
        def reconciliation_leader(self):
            self.enter_thread = threading.get_ident()
            yield False

        def list_reconcilable_jobs(self, *, limit: int):
            self.list_called = True
            return []

    async def scenario() -> None:
        repository = Repository()
        event_loop_thread = threading.get_ident()
        reconciler = MonomerDftReconciler(
            repository=repository,  # type: ignore[arg-type]
            worker=object(),  # type: ignore[arg-type]
            interval_seconds=1,
        )
        await reconciler.run_once()
        assert repository.enter_thread is not None
        assert repository.enter_thread != event_loop_thread
        assert repository.list_called is False

    asyncio.run(scenario())


def test_readiness_transition_starts_after_0013_and_regression_fences_all_dft_sql() -> None:
    job = {
        "job_id": str(uuid4()),
        "_attempt_token": "a" * 64,
        "status": "pending",
        "stage": "queued",
        "progress_percent": 0.0,
        "request_sha256": "b" * 64,
        "_enqueue_sequence": 1,
    }

    class Repository:
        def __init__(self) -> None:
            self.ready = False
            self.jobs = [job]
            self.relation_calls: list[str] = []
            self.applied = asyncio.Event()
            self.schema_probes = 0

        def schema_ready(self) -> bool:
            self.schema_probes += 1
            return self.ready

        @contextmanager
        def reconciliation_leader(self):
            self.relation_calls.append("leader")
            yield True

        def list_reconcilable_jobs(self, *, limit: int):
            self.relation_calls.append("list")
            assert limit == 100
            return list(self.jobs)

        def claim_pending_dispatch(self, *, job_id: str, attempt_token: str) -> bool:
            self.relation_calls.append("claim")
            return job_id == job["job_id"] and attempt_token == job["_attempt_token"]

        def apply_worker_snapshot(self, **_kwargs):
            self.relation_calls.append("apply")
            self.jobs.clear()
            self.applied.set()
            return {**job, "status": "queued"}

        def list_expired_artifact_jobs(self, **_kwargs):
            self.relation_calls.append("expired")
            return []

        def list_pending_artifact_deletions(self, **_kwargs):
            self.relation_calls.append("deletions")
            return []

    class Worker:
        async def submit_job(self, submitted):
            assert submitted["job_id"] == job["job_id"]
            return {"status": "queued"}

    async def scenario() -> None:
        repository = Repository()
        reconciler = MonomerDftReconciler(
            repository=repository,  # type: ignore[arg-type]
            worker=Worker(),  # type: ignore[arg-type]
            interval_seconds=60,
        )
        controller = MonomerDftReadinessController(
            repository=repository,  # type: ignore[arg-type]
            reconciler=reconciler,
            interval_seconds=60,
        )

        # The backend may boot while the database is still at 0012. Exercise
        # the real background transition loop rather than manually starting a
        # reconciler after the migration.
        controller.start()
        for _ in range(100):
            if repository.schema_probes >= 1:
                break
            await asyncio.sleep(0.01)
        assert repository.schema_probes >= 1
        assert controller.schema_ready is False
        assert repository.relation_calls == []

        # Applying 0013 online must ensure-start reconciliation and submit the
        # previously durable pending row without a backend restart.
        repository.ready = True
        controller.kick()
        await asyncio.wait_for(repository.applied.wait(), timeout=2)
        assert controller.schema_ready is True
        assert repository.relation_calls[:4] == ["leader", "list", "claim", "apply"]

        # A readiness regression stops the task.  The reconciler's independent
        # per-cycle gate also proves that no monomer_dft relation SQL can run.
        repository.ready = False
        controller.kick()
        for _ in range(100):
            if controller.schema_ready is False:
                break
            await asyncio.sleep(0.01)
        assert controller.schema_ready is False
        assert await controller.refresh() is False
        calls_before_regression_probe = list(repository.relation_calls)
        await reconciler.run_once()
        assert repository.relation_calls == calls_before_regression_probe
        assert await controller.refresh() is False
        assert repository.relation_calls == calls_before_regression_probe
        await controller.stop()

    asyncio.run(scenario())


def test_create_and_cancel_routes_only_write_database_then_kick_leader() -> None:
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )

    class Repository:
        def __init__(self) -> None:
            self.job = _pending_database_job(prepared)
            self.max_active_jobs: int | None = None

        def find_idempotent_job(self, **_kwargs):
            return None

        def create_job(self, _prepared, *, idempotency_key: str, max_active_jobs: int):
            assert idempotency_key == "fifo-test-0001"
            self.max_active_jobs = max_active_jobs
            return CreateJobResult(job=dict(self.job), created=True)

        def request_cancel(self, job_id: str):
            assert job_id == self.job["job_id"]
            self.job = {
                **self.job,
                "status": "cancel_requested",
                "cancel_requested": True,
                "updated_at": datetime.now(timezone.utc),
            }
            return dict(self.job)

        @staticmethod
        def public_job(job: dict, *, idempotent_replay: bool = False):
            return MonomerDftRepository.public_job(
                job,
                idempotent_replay=idempotent_replay,
            )

    class Worker:
        direct_job_calls = 0

        async def health(self):
            return {
                "status": "ok",
                "runtime_ready": True,
                "accepting_jobs": True,
                "draining": False,
                "recovering": False,
                "queued_jobs": 0,
                "max_queued_jobs": 8,
            }

        async def submit_job(self, _job):
            self.direct_job_calls += 1
            raise AssertionError("request thread must not submit to Worker")

        async def cancel_job(self, _job_id):
            self.direct_job_calls += 1
            raise AssertionError("request thread must not cancel at Worker")

    class Reconciler:
        def __init__(self) -> None:
            self.kicks = 0
            self.direct_reconciles = 0

        def kick(self):
            self.kicks += 1

        async def reconcile_job(self, _job):
            self.direct_reconciles += 1
            raise AssertionError("request thread must not reconcile a job")

    settings = Settings(
        monomer_dft_submit_enabled=True,
        monomer_dft_worker_uds="/tmp/monomer-dft-test.sock",
    )
    app = create_app(settings)
    repository = Repository()
    worker = Worker()
    reconciler = Reconciler()
    app.state.monomer_dft_repository = repository
    app.state.monomer_dft_worker_client = worker
    app.state.monomer_dft_reconciler = reconciler
    client = TestClient(app, raise_server_exceptions=False)

    created = client.post(
        "/api/v1/monomer-dft/jobs",
        json=_single_point_request(),
        headers={"Idempotency-Key": "fifo-test-0001"},
    )
    assert created.status_code == 202
    assert created.json()["status"] == "pending"
    assert repository.max_active_jobs == 9
    assert reconciler.kicks == 1
    assert reconciler.direct_reconciles == 0
    assert worker.direct_job_calls == 0

    cancelled = client.post(
        f"/api/v1/monomer-dft/jobs/{repository.job['job_id']}/cancel"
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancel_requested"
    assert cancelled.json()["stage"] == "queued"
    assert reconciler.kicks == 2
    assert reconciler.direct_reconciles == 0
    assert worker.direct_job_calls == 0
    client.close()


def test_delete_route_records_intent_without_calling_worker() -> None:
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )
    job = {
        **_pending_database_job(prepared),
        "status": "completed",
        "stage": "artifacts",
        "progress_percent": 100.0,
        "artifacts_state": "available",
        "artifacts": [
            {
                "artifact_id": "scientific_result",
                "name": "scientific_result.json",
                "media_type": "application/json",
                "size_bytes": 1,
                "sha256": "a" * 64,
                "available": True,
            }
        ],
    }

    class Repository:
        def get_job(self, _job_id: str):
            return dict(job)

        def request_artifact_deletion(self, _job_id: str):
            job["artifacts_state"] = "delete_requested"
            job["artifacts"] = [{**job["artifacts"][0], "available": False}]
            return dict(job)

    class Worker:
        async def delete_artifacts(self, _job_id: str):
            raise AssertionError("public DELETE must not call the Worker")

        async def close(self):
            return None

    class Reconciler:
        def __init__(self) -> None:
            self.kicks = 0

        def kick(self):
            self.kicks += 1

        def start(self):
            return None

        async def stop(self):
            return None

    app = create_app(Settings())
    repository = Repository()
    reconciler = Reconciler()
    app.state.monomer_dft_repository = repository
    app.state.monomer_dft_worker_client = Worker()
    app.state.monomer_dft_reconciler = reconciler
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.delete(
            f"/api/v1/monomer-dft/jobs/{job['job_id']}/artifacts"
        )
    assert response.status_code == 202
    assert response.headers["Retry-After"] == "5"
    assert response.json()["artifacts_state"] == "delete_requested"
    assert response.json()["deleted"] is False
    assert reconciler.kicks == 1


def test_delete_route_reports_persisted_count_after_async_deletion() -> None:
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )
    job = {
        **_pending_database_job(prepared),
        "status": "completed",
        "stage": "artifacts",
        "progress_percent": 100.0,
        "artifacts_state": "deleted",
        "artifacts_deleted": True,
        "artifacts": [
            {
                "artifact_id": f"artifact_{index}",
                "name": f"artifact_{index}.json",
                "media_type": "application/json",
                "size_bytes": 1,
                "sha256": f"{index + 1:x}" * 64,
                "available": False,
            }
            for index in range(3)
        ],
    }

    class Repository:
        def get_job(self, _job_id: str):
            return dict(job)

    app = create_app(Settings())
    app.state.monomer_dft_repository = Repository()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.delete(
            f"/api/v1/monomer-dft/jobs/{job['job_id']}/artifacts"
        )

    assert response.status_code == 200
    assert response.json() == {
        "job_id": job["job_id"],
        "deleted": True,
        "artifacts_state": "deleted",
        "deleted_artifacts": 3,
        "message": "DFT artifacts were already deleted",
    }


def test_idempotency_hash_precedes_rdkit_and_final_replay_wins_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_model = REQUEST_ADAPTER.validate_python(_single_point_request())
    prepared = prepare_monomer_dft_request(request_model)
    existing = _pending_database_job(prepared)

    class Repository:
        def __init__(self) -> None:
            self.lookups = 0
            self.hashes: list[str] = []

        def find_idempotent_job(self, *, request_sha256: str, **_kwargs):
            self.lookups += 1
            self.hashes.append(request_sha256)
            return None if self.lookups == 1 else dict(existing)

        @staticmethod
        def public_job(job: dict, *, idempotent_replay: bool = False):
            return MonomerDftRepository.public_job(
                job,
                idempotent_replay=idempotent_replay,
            )

    def unavailable(_request):
        raise RuntimeError("RDKit is unavailable")

    monkeypatch.setattr(
        "app.routers.monomer_dft.prepare_monomer_dft_request",
        unavailable,
    )
    app = create_app(Settings())
    repository = Repository()
    app.state.monomer_dft_repository = repository
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/monomer-dft/jobs",
            json=_single_point_request(),
            headers={"Idempotency-Key": "replay-race-0001"},
        )
    assert response.status_code == 202
    assert response.json()["idempotent_replay"] is True
    assert repository.lookups == 2
    assert repository.hashes == [
        calculation_request_sha256(request_model.model_dump(mode="json"))
    ] * 2


def test_successful_validation_rechecks_replay_before_worker_readiness() -> None:
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )
    existing = _pending_database_job(prepared)

    class Repository:
        def __init__(self) -> None:
            self.lookups = 0

        def find_idempotent_job(self, **_kwargs):
            self.lookups += 1
            return None if self.lookups == 1 else dict(existing)

        @staticmethod
        def public_job(job: dict, *, idempotent_replay: bool = False):
            return MonomerDftRepository.public_job(
                job,
                idempotent_replay=idempotent_replay,
            )

    class Worker:
        async def health(self):
            raise AssertionError("readiness must not run after a replay appears")

        async def close(self):
            return None

    class Reconciler:
        def start(self):
            return None

        async def stop(self):
            return None

    app = create_app(
        Settings(
            monomer_dft_submit_enabled=True,
            monomer_dft_worker_uds="/tmp/monomer-dft-test.sock",
        )
    )
    repository = Repository()
    app.state.monomer_dft_repository = repository
    app.state.monomer_dft_worker_client = Worker()
    app.state.monomer_dft_reconciler = Reconciler()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/monomer-dft/jobs",
            json=_single_point_request(),
            headers={"Idempotency-Key": "replay-ready-0001"},
        )
    assert response.status_code == 202
    assert response.json()["idempotent_replay"] is True
    assert repository.lookups == 2


def test_rdkit_validation_uses_dedicated_anyio_limiter_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_prepare = prepare_monomer_dft_request
    activity_lock = threading.Lock()
    active = 0
    maximum_active = 0

    def slow_prepare(request_model):
        nonlocal active, maximum_active
        with activity_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.08)
            return real_prepare(request_model)
        finally:
            with activity_lock:
                active -= 1

    monkeypatch.setattr(
        "app.routers.monomer_dft.prepare_monomer_dft_request",
        slow_prepare,
    )

    class Repository:
        def find_idempotent_job(self, **_kwargs):
            return None

        def create_job(self, prepared, **_kwargs):
            return CreateJobResult(job=_pending_database_job(prepared), created=True)

        @staticmethod
        def public_job(job: dict, *, idempotent_replay: bool = False):
            return MonomerDftRepository.public_job(
                job,
                idempotent_replay=idempotent_replay,
            )

    class Worker:
        async def health(self):
            return {
                "status": "ok",
                "runtime_ready": True,
                "accepting_jobs": True,
                "draining": False,
            }

    class Reconciler:
        def kick(self):
            return None

    app = create_app(
        Settings(
            monomer_dft_submit_enabled=True,
            monomer_dft_worker_uds="/tmp/monomer-dft-test.sock",
            monomer_dft_validation_concurrency=2,
        )
    )
    app.state.monomer_dft_repository = Repository()
    app.state.monomer_dft_worker_client = Worker()
    app.state.monomer_dft_reconciler = Reconciler()

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/api/v1/monomer-dft/jobs",
                        json=_single_point_request(),
                        headers={"Idempotency-Key": f"limiter-test-{index:04d}"},
                    )
                    for index in range(4)
                )
            )
        assert [response.status_code for response in responses] == [202] * 4

    asyncio.run(scenario())
    assert maximum_active == 2


def test_cancel_racing_inflight_submit_is_eventually_cancelled_without_orphan() -> None:
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )
    job = _pending_database_job(prepared)

    class Repository:
        def __init__(self) -> None:
            self.current = dict(job)

        def claim_pending_dispatch(self, *, job_id: str, attempt_token: str) -> bool:
            assert job_id == self.current["job_id"]
            assert attempt_token == self.current["_attempt_token"]
            if self.current["status"] != "pending":
                return False
            self.current = {**self.current, "_dispatch_started": True}
            return True

        def get_job(self, job_id: str):
            assert job_id == self.current["job_id"]
            return dict(self.current)

        def request_cancel(self) -> None:
            self.current = {
                **self.current,
                "status": "cancel_requested",
                "cancel_requested": True,
            }

        def apply_worker_snapshot(self, *, job_id: str, attempt_token: str, snapshot: dict):
            assert job_id == self.current["job_id"]
            assert attempt_token == self.current["_attempt_token"]
            if self.current["status"] == "cancel_requested" and snapshot["status"] in {
                "pending",
                "queued",
                "running",
            }:
                return dict(self.current)
            self.current = {**self.current, "status": snapshot["status"]}
            return dict(self.current)

    class Worker:
        def __init__(self) -> None:
            self.submit_started = asyncio.Event()
            self.release_submit = asyncio.Event()
            self.active_jobs: set[str] = set()
            self.cancel_calls = 0

        async def submit_job(self, submitted: dict):
            self.submit_started.set()
            await self.release_submit.wait()
            self.active_jobs.add(submitted["job_id"])
            return {"status": "queued"}

        async def cancel_job(self, cancelled_job: dict):
            self.cancel_calls += 1
            assert cancelled_job["job_id"] == job["job_id"]
            assert cancelled_job["_attempt_token"] == job["_attempt_token"]
            assert cancelled_job["request_sha256"] == job["request_sha256"]
            assert cancelled_job["_enqueue_sequence"] == job["_enqueue_sequence"]
            self.active_jobs.discard(cancelled_job["job_id"])
            return {"status": "cancelled"}

    async def scenario() -> None:
        repository = Repository()
        worker = Worker()
        reconciler = MonomerDftReconciler(
            repository=repository,  # type: ignore[arg-type]
            worker=worker,  # type: ignore[arg-type]
            interval_seconds=1,
        )
        stale_pending = dict(repository.current)
        inflight_submit = asyncio.create_task(reconciler.reconcile_job(stale_pending))
        await worker.submit_started.wait()
        repository.request_cancel()
        worker.release_submit.set()
        after_submit = await inflight_submit
        assert after_submit is not None
        assert after_submit["status"] == "cancel_requested"
        assert worker.active_jobs == {job["job_id"]}

        terminal = await reconciler.reconcile_job(dict(repository.current))
        assert terminal is not None
        assert terminal["status"] == "cancelled"
        assert worker.cancel_calls == 1
        assert worker.active_jobs == set()

    asyncio.run(scenario())


def test_fifo_reconciler_stops_after_retryable_unknown_outcome() -> None:
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )
    jobs = [_pending_database_job(prepared), _pending_database_job(prepared)]

    class Repository:
        def __init__(self) -> None:
            self.recorded: list[str] = []

        @contextmanager
        def reconciliation_leader(self):
            yield True

        def list_reconcilable_jobs(self, *, limit: int):
            assert limit == 100
            return jobs

        def claim_pending_dispatch(self, *, job_id: str, attempt_token: str) -> bool:
            assert attempt_token
            return any(candidate["job_id"] == job_id for candidate in jobs)

        def record_dispatch_error(self, *, job_id: str, **_kwargs):
            self.recorded.append(job_id)

        def list_expired_artifact_jobs(self, **_kwargs):
            return []

        def list_pending_artifact_deletions(self, **_kwargs):
            return []

    class Worker:
        def __init__(self) -> None:
            self.submitted: list[str] = []

        async def submit_job(self, submitted: dict):
            self.submitted.append(submitted["job_id"])
            raise MonomerDftWorkerError("temporary failure", retryable=True)

    async def scenario() -> None:
        repository = Repository()
        worker = Worker()
        reconciler = MonomerDftReconciler(
            repository=repository,  # type: ignore[arg-type]
            worker=worker,  # type: ignore[arg-type]
            interval_seconds=1,
        )
        await reconciler.run_once()
        assert worker.submitted == [jobs[0]["job_id"]]
        assert repository.recorded == [jobs[0]["job_id"]]

    asyncio.run(scenario())


def test_cancel_404_never_proves_a_dispatched_attempt_stopped() -> None:
    prepared = prepare_monomer_dft_request(
        REQUEST_ADAPTER.validate_python(_single_point_request())
    )
    job = _pending_database_job(prepared)
    job["status"] = "cancel_requested"
    job["cancel_requested"] = True
    job["_dispatch_started"] = True

    class Repository:
        def __init__(self) -> None:
            self.recorded: list[dict] = []
            self.applied = False

        def record_dispatch_error(self, **kwargs):
            self.recorded.append(kwargs)

        def apply_worker_snapshot(self, **_kwargs):
            self.applied = True
            raise AssertionError("an unknown Worker must not produce a terminal snapshot")

    class Worker:
        async def cancel_job(self, cancelled_job: dict):
            assert cancelled_job["job_id"] == job["job_id"]
            raise MonomerDftWorkerError(
                "unknown job",
                status_code=404,
                code="worker_resource_not_found",
                retryable=False,
            )

    async def scenario() -> None:
        repository = Repository()
        reconciler = MonomerDftReconciler(
            repository=repository,  # type: ignore[arg-type]
            worker=Worker(),  # type: ignore[arg-type]
            interval_seconds=1,
        )
        assert await reconciler.reconcile_job(job) is None
        assert repository.applied is False
        assert len(repository.recorded) == 1
        assert repository.recorded[0]["retryable"] is True
        assert repository.recorded[0]["code"] == "worker_resource_not_found"

    asyncio.run(scenario())


def test_worker_artifact_deletion_response_is_strict_and_404_stays_pending(
    caplog: pytest.LogCaptureFixture,
) -> None:
    job_id = str(uuid4())
    valid = {
        "job_id": job_id,
        "deleted": True,
        "artifact_state": "deleted",
        "deleted_artifacts": 3,
        "message": "deleted",
    }
    assert InternalWorkerArtifactDeletionResponse.model_validate(valid).deleted is True
    invalid_responses = [
        {**valid, "deleted": False},
        {key: value for key, value in valid.items() if key != "deleted"},
        {**valid, "job_id": str(uuid4())},
        {**valid, "artifact_state": "available"},
    ]

    async def client_scenario() -> None:
        payloads = [valid, *invalid_responses]

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payloads.pop(0))

        raw_client = httpx.AsyncClient(
            base_url="http://monomer-dft-worker",
            transport=httpx.MockTransport(handler),
        )
        client = MonomerDftWorkerClient(
            base_url="http://monomer-dft-worker",
            uds_path="/unused/test.sock",
            validation_limiter=anyio.CapacityLimiter(1),
            client=raw_client,
        )
        assert (await client.delete_artifacts(job_id))["artifact_state"] == "deleted"
        for _invalid in invalid_responses:
            with pytest.raises(MonomerDftWorkerError) as caught:
                await client.delete_artifacts(job_id)
            assert caught.value.code == "invalid_worker_response"
        await raw_client.aclose()

    class Repository:
        def __init__(self) -> None:
            self.finalized = 0

        def list_pending_artifact_deletions(self, **_kwargs):
            return [{"job_id": job_id}]

        def mark_artifacts_deleted(self, _job_id: str):
            self.finalized += 1

    class Worker:
        async def delete_artifacts(self, _job_id: str):
            raise MonomerDftWorkerError(
                "not found",
                status_code=404,
                code="worker_resource_not_found",
                retryable=False,
            )

    async def reconcile_scenario() -> None:
        repository = Repository()
        reconciler = MonomerDftReconciler(
            repository=repository,  # type: ignore[arg-type]
            worker=Worker(),  # type: ignore[arg-type]
            interval_seconds=1,
        )
        await reconciler._reconcile_artifact_deletions()
        assert repository.finalized == 0

    asyncio.run(client_scenario())
    with caplog.at_level("WARNING"):
        asyncio.run(reconcile_scenario())
    assert job_id in caplog.text
    assert "worker_resource_not_found" in caplog.text


def test_artifact_stream_requires_upstream_length_and_quoted_etag_handshake() -> None:
    job_id = str(uuid4())

    class Repository:
        def get_artifact(self, *, job_id: str, artifact_id: str):
            assert artifact_id == "scientific_result"
            return {
                "artifact_id": artifact_id,
                "name": "scientific_result.json",
                "media_type": "application/json",
                "size_bytes": 4,
                "sha256": "a" * 64,
                "available": True,
            }

    class Stream:
        def __init__(self) -> None:
            self.response = SimpleNamespace(
                headers={"content-length": "5", "etag": f'"{"b" * 64}"'}
            )
            self.closed = False

        async def close(self):
            self.closed = True

    class Worker:
        def __init__(self) -> None:
            self.stream = Stream()

        async def stream_artifact(self, _job_id: str, _artifact_id: str):
            return self.stream

    app = create_app(Settings())
    worker = Worker()
    app.state.monomer_dft_repository = Repository()
    app.state.monomer_dft_worker_client = worker
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(
        f"/api/v1/monomer-dft/jobs/{job_id}/artifacts/scientific_result"
    )
    assert response.status_code == 502
    assert response.json() == {
        "code": "artifact_integrity_mismatch",
        "message": "DFT worker artifact size does not match the persisted manifest",
        "retryable": True,
        "details": {},
    }
    assert worker.stream.closed is True
    client.close()


def test_download_slots_fail_fast_and_release_when_file_send_fails(tmp_path: Path) -> None:
    body = b"verified artifact"
    digest = hashlib.sha256(body).hexdigest()
    artifact = {
        "name": "result.json",
        "size_bytes": len(body),
        "sha256": digest,
    }

    async def open_stream():
        return _worker_download_stream(body)

    async def scenario() -> None:
        proxy = MonomerDftDownloadProxy(spool_root=str(tmp_path), max_concurrent=2)
        first = await proxy.verify_artifact(open_stream=open_stream, artifact=artifact)
        second = await proxy.verify_artifact(open_stream=open_stream, artifact=artifact)
        started = time.monotonic()
        with pytest.raises(MonomerDftDownloadProxyError) as caught:
            await proxy.verify_artifact(open_stream=open_stream, artifact=artifact)
        assert caught.value.code == "download_capacity_full"
        assert caught.value.status_code == 503
        assert time.monotonic() - started < 0.05

        response = VerifiedMonomerDftFileResponse(
            verified=first,
            media_type="application/json",
            filename="result.json",
        )

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                raise ConnectionError("client disconnected")

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/artifact",
            "raw_path": b"/artifact",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
        }
        with pytest.raises(ConnectionError):
            await response(scope, receive, send)
        assert not first.path.exists()

        third = await proxy.verify_artifact(open_stream=open_stream, artifact=artifact)
        await second.cleanup()
        await third.cleanup()
        assert list(tmp_path.iterdir()) == []

    asyncio.run(scenario())


def test_download_proxy_rejects_encoded_body_and_verifies_zip_manifest(tmp_path: Path) -> None:
    member_body = b'{"energy_eV":-1.0}'
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        member = zipfile.ZipInfo("scientific_result.json")
        member.compress_type = zipfile.ZIP_DEFLATED
        member.extra = b"\xfe\xca\x03\x00xyz"
        member.comment = b"unbound member comment"
        archive.writestr(member, member_body)
        archive.comment = b"unbound archive comment"
    bundle = output.getvalue()
    artifacts = [
        {
            "artifact_id": "scientific_result",
            "name": "scientific_result.json",
            "media_type": "application/json",
            "size_bytes": len(member_body),
            "sha256": hashlib.sha256(member_body).hexdigest(),
            "available": True,
        }
    ]

    async def encoded_stream():
        return _worker_download_stream(member_body, content_encoding="gzip")

    async def bundle_stream():
        return _worker_download_stream(bundle)

    async def scenario() -> None:
        proxy = MonomerDftDownloadProxy(spool_root=str(tmp_path), max_concurrent=2)
        with pytest.raises(MonomerDftDownloadProxyError) as encoded:
            await proxy.verify_artifact(
                open_stream=encoded_stream,
                artifact={
                    "name": "scientific_result.json",
                    "size_bytes": len(member_body),
                    "sha256": hashlib.sha256(member_body).hexdigest(),
                },
            )
        assert encoded.value.code == "artifact_integrity_mismatch"

        verified = await proxy.verify_bundle(
            open_stream=bundle_stream,
            artifacts=artifacts,
        )
        canonical_bundle = verified.path.read_bytes()
        assert canonical_bundle != bundle
        assert verified.size_bytes == len(canonical_bundle)
        assert verified.sha256 == hashlib.sha256(canonical_bundle).hexdigest()
        assert verified.path.stat().st_mode & 0o777 == 0o600
        with zipfile.ZipFile(io.BytesIO(canonical_bundle)) as archive:
            assert archive.comment == b""
            assert archive.namelist() == ["scientific_result.json"]
            canonical_member = archive.infolist()[0]
            assert canonical_member.comment == b""
            assert canonical_member.extra == b""
            assert canonical_member.date_time == (1980, 1, 1, 0, 0, 0)
            assert archive.read(canonical_member) == member_body
        await verified.cleanup()

        repeated = await proxy.verify_bundle(
            open_stream=bundle_stream,
            artifacts=artifacts,
        )
        assert repeated.path.read_bytes() == canonical_bundle
        assert repeated.sha256 == hashlib.sha256(canonical_bundle).hexdigest()
        await repeated.cleanup()

        wrong_manifest = deepcopy(artifacts)
        wrong_manifest[0]["sha256"] = "f" * 64
        with pytest.raises(MonomerDftDownloadProxyError) as invalid:
            await proxy.verify_bundle(
                open_stream=bundle_stream,
                artifacts=wrong_manifest,
            )
        assert invalid.value.code == "artifact_bundle_invalid"

    asyncio.run(scenario())


def test_download_spool_suffix_is_bounded_for_portable_long_names() -> None:
    assert monomer_dft_download_proxy_module._safe_spool_suffix("result.json") == ".json"
    assert (
        monomer_dft_download_proxy_module._safe_spool_suffix(
            "artifact." + "x" * 245
        )
        == ".bin"
    )


def test_download_spool_is_process_private_and_recovers_dead_process_files(
    tmp_path: Path,
) -> None:
    stale = tmp_path / "process-999999999-1"
    stale.mkdir(mode=0o700)
    stale_file = stale / ".download-crashed.json"
    stale_file.write_bytes(b"incomplete")
    stale_file.chmod(0o600)
    body = b"verified"
    artifact = {
        "name": "result.json",
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }

    async def open_stream():
        return _worker_download_stream(body)

    async def scenario() -> None:
        proxy = MonomerDftDownloadProxy(spool_root=str(tmp_path), max_concurrent=2)
        verified = await proxy.verify_artifact(
            open_stream=open_stream,
            artifact=artifact,
        )
        assert stale.exists() is False
        assert verified.path.parent.name.startswith(f"process-{os.getpid()}-")
        assert verified.path.parent.stat().st_mode & 0o777 == 0o700
        assert verified.path.parent.stat().st_uid == os.geteuid()
        assert verified.path.stat().st_mode & 0o777 == 0o600
        await verified.cleanup()
        assert list(tmp_path.iterdir()) == []

    asyncio.run(scenario())


def test_download_spool_rejects_symlinked_process_root_without_touching_target(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    symlink = tmp_path / "process-999999999-1"
    symlink.symlink_to(outside, target_is_directory=True)
    body = b"verified"
    artifact = {
        "name": "result.json",
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }

    async def open_stream():
        return _worker_download_stream(body)

    async def scenario() -> None:
        proxy = MonomerDftDownloadProxy(spool_root=str(tmp_path), max_concurrent=2)
        with pytest.raises(MonomerDftDownloadProxyError) as caught:
            await proxy.verify_artifact(
                open_stream=open_stream,
                artifact=artifact,
            )
        assert caught.value.code == "download_staging_unavailable"
        assert outside.is_dir()

    asyncio.run(scenario())


def test_bundle_cancellation_waits_for_verifier_before_cleanup_and_slot_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    member_body = b'{"energy_eV":-1.0}'
    bundle = _zip_bytes({"scientific_result.json": member_body})
    artifacts = [
        {
            "artifact_id": "scientific_result",
            "name": "scientific_result.json",
            "media_type": "application/json",
            "size_bytes": len(member_body),
            "sha256": hashlib.sha256(member_body).hexdigest(),
            "available": True,
        }
    ]
    verification_started = threading.Event()
    release_verification = threading.Event()
    original = monomer_dft_download_proxy_module._verify_and_canonicalize_zip

    def blocked_verification(path, manifest):
        verification_started.set()
        assert release_verification.wait(timeout=5.0)
        return original(path, manifest)

    monkeypatch.setattr(
        monomer_dft_download_proxy_module,
        "_verify_and_canonicalize_zip",
        blocked_verification,
    )

    async def open_stream():
        return _worker_download_stream(bundle)

    async def scenario() -> None:
        proxy = MonomerDftDownloadProxy(spool_root=str(tmp_path), max_concurrent=2)
        pending = asyncio.create_task(
            proxy.verify_bundle(open_stream=open_stream, artifacts=artifacts)
        )
        assert await asyncio.to_thread(verification_started.wait, 2.0)
        pending.cancel()
        await asyncio.sleep(0.02)
        assert pending.done() is False
        assert any(tmp_path.iterdir())

        # A second cancellation (for example request disconnect followed by
        # application shutdown) must not abandon the live verifier thread.
        pending.cancel()
        await asyncio.sleep(0.02)
        assert pending.done() is False
        assert any(tmp_path.iterdir())

        release_verification.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert list(tmp_path.iterdir()) == []

        # Cancellation released the capacity slot only after the thread and
        # spool cleanup completed.
        verified = await proxy.verify_bundle(open_stream=open_stream, artifacts=artifacts)
        await verified.cleanup()
        assert list(tmp_path.iterdir()) == []

    asyncio.run(scenario())


def test_zip_manifest_expanded_size_is_bounded_before_archive_open(tmp_path: Path) -> None:
    artifacts = [
        {
            "artifact_id": f"artifact_{index}",
            "name": f"artifact_{index}.bin",
            "media_type": "application/octet-stream",
            "size_bytes": MAX_ARTIFACT_BYTES,
            "sha256": f"{index + 1:x}" * 64,
            "available": True,
        }
        for index in range(MAX_BUNDLE_BYTES // MAX_ARTIFACT_BYTES + 1)
    ]
    with pytest.raises(MonomerDftDownloadProxyError) as caught:
        _verify_zip_members(tmp_path / "does-not-exist.zip", artifacts)
    assert caught.value.code == "artifact_size_out_of_contract"


def test_zip_manifest_entry_count_is_bounded_before_archive_open(tmp_path: Path) -> None:
    artifacts = [
        {
            "artifact_id": f"artifact_{index}",
            "name": f"artifact_{index}.bin",
            "media_type": "application/octet-stream",
            "size_bytes": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
            "available": True,
        }
        for index in range(MAX_BUNDLE_ENTRIES + 1)
    ]
    with pytest.raises(MonomerDftDownloadProxyError) as caught:
        _verify_zip_members(tmp_path / "does-not-exist.zip", artifacts)
    assert caught.value.code == "artifact_manifest_invalid"


def test_artifact_body_is_verified_before_success_response(tmp_path: Path) -> None:
    job_id = str(uuid4())
    expected = b"good"
    actual = b"evil"

    class Repository:
        def get_artifact(self, **_kwargs):
            return {
                "artifact_id": "scientific_result",
                "name": "scientific_result.json",
                "media_type": "application/json",
                "size_bytes": len(expected),
                "sha256": hashlib.sha256(expected).hexdigest(),
                "available": True,
            }

    class Worker:
        async def stream_artifact(self, _job_id: str, _artifact_id: str):
            return _worker_download_stream(
                actual,
                sha256=hashlib.sha256(expected).hexdigest(),
            )

        async def close(self):
            return None

    class Reconciler:
        def start(self):
            return None

        async def stop(self):
            return None

    app = create_app(
        Settings(monomer_dft_download_spool_root=str(tmp_path))
    )
    app.state.monomer_dft_repository = Repository()
    app.state.monomer_dft_worker_client = Worker()
    app.state.monomer_dft_reconciler = Reconciler()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            f"/api/v1/monomer-dft/jobs/{job_id}/artifacts/scientific_result"
        )
    assert response.status_code == 502
    assert response.json()["code"] == "artifact_integrity_mismatch"
    assert list(tmp_path.iterdir()) == []


def test_monomer_dft_capacity_configuration_is_fail_closed_at_nine() -> None:
    settings = Settings()
    assert settings.monomer_dft_max_active_jobs == 9
    assert settings.monomer_dft_validation_concurrency == 2
    assert settings.monomer_dft_download_max_concurrent == 2
    assert settings.monomer_dft_download_spool_root == "/tmp/monomer-dft-downloads"
    with pytest.raises(ValueError, match="must be exactly 9"):
        Settings(monomer_dft_max_active_jobs=8)
    with pytest.raises(ValueError, match="must be exactly 9"):
        Settings(monomer_dft_max_active_jobs=10)
    with pytest.raises(ValueError, match="between 1 and 4"):
        Settings(monomer_dft_validation_concurrency=5)
    with pytest.raises(ValueError, match="must be exactly 2"):
        Settings(monomer_dft_download_max_concurrent=3)


def test_capabilities_publish_fixed_capacity_and_scientific_model_meanings() -> None:
    class Repository:
        def count_active_jobs(self):
            return 0

    class Worker:
        async def health(self):
            return {
                "status": "ok",
                "runtime_ready": True,
                "accepting_jobs": True,
                "draining": False,
            }

        async def capabilities(self):
            return {
                "schema_version": 1,
                "models": [
                    {
                        "alias": "aimnet2-pd",
                        "label": "Worker label",
                        "description": "untrusted generic description",
                        "loaded": True,
                    }
                ],
                "queue": {"max_queued_jobs": 99},
            }

    app = create_app(
        Settings(
            monomer_dft_submit_enabled=True,
            monomer_dft_worker_uds="/tmp/monomer-dft-test.sock",
        )
    )
    app.state.monomer_dft_repository = Repository()
    app.state.monomer_dft_worker_client = Worker()
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/v1/monomer-dft/capabilities")
    assert response.status_code == 200
    payload = response.json()
    assert payload["limits"] == {
        "max_atoms": 300,
        "max_heavy_atoms": 100,
        "max_hessian_atoms": 100,
        "min_optimization_steps": 10,
        "max_optimization_steps": 50,
        "max_concurrent_jobs": 1,
        "max_queued_jobs": 8,
        "max_active_jobs": 9,
    }
    descriptions = {model["id"]: model["description"] for model in payload["models"]}
    assert "B97-3c" in descriptions["aimnet2-2025"]
    assert "reproducing" in descriptions["aimnet2-b973c"]
    assert "multiplicity" in descriptions["aimnet2-nse"]
    assert "excluding As" in descriptions["aimnet2-pd"]
    assert "CPCM(THF)" in descriptions["aimnet2-pd"]
    assert "reaction paths" in descriptions["aimnet2-rxn"]
    assert "SCF" not in " ".join(descriptions.values())
    client.close()


def test_schema_boundary_keeps_status_safe_and_blocks_database_routes_before_sql() -> None:
    class Repository:
        database_route_called = False

        def schema_ready(self):
            return False

        def count_active_jobs(self):
            raise AssertionError("status must not query DFT tables before 0013")

        def list_jobs(self, **_kwargs):
            self.database_route_called = True
            raise AssertionError("jobs route must be gated before DFT SQL")

    class Worker:
        async def health(self):
            raise AssertionError("worker must not be contacted before the DFT schema is ready")

        async def capabilities(self):
            raise AssertionError("worker must not be contacted before the DFT schema is ready")

    app = create_app(
        Settings(
            monomer_dft_submit_enabled=True,
            monomer_dft_worker_uds="/tmp/monomer-dft-test.sock",
        )
    )
    repository = Repository()
    app.state.monomer_dft_repository = repository
    app.state.monomer_dft_worker_client = Worker()
    client = TestClient(app, raise_server_exceptions=False)

    service_status = client.get("/api/v1/monomer-dft/status")
    assert service_status.status_code == 200
    assert service_status.json()["schema_ready"] is False
    assert service_status.json()["available"] is False
    assert service_status.json()["active_jobs"] == 0

    capabilities = client.get("/api/v1/monomer-dft/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["schema_ready"] is False
    assert capabilities.json()["available"] is False

    jobs = client.get("/api/v1/monomer-dft/jobs")
    assert jobs.status_code == 503
    assert jobs.headers["Retry-After"] == "5"
    assert jobs.json() == {
        "code": "schema_not_ready",
        "message": "monomer DFT schema is not ready",
        "retryable": True,
        "details": {},
    }
    assert repository.database_route_called is False

    # The legacy read-only DFT API is a separate router and must not inherit
    # the monomer-DFT schema gate.
    legacy = client.get("/api/v1/dft/pca-sample?limit=1")
    assert legacy.json().get("code") != "schema_not_ready"
    client.close()


def test_backend_startup_uses_through_0012_schema_profile(monkeypatch) -> None:
    observed: dict[str, object] = {}

    def fake_run_preflight(
        settings,
        *,
        mode,
        strict,
        schema_target,
    ):
        observed.update(
            settings=settings,
            mode=mode,
            strict=strict,
            schema_target=schema_target,
        )
        return {"status": "ok"}

    monkeypatch.setattr(main_module, "run_preflight", fake_run_preflight)
    monkeypatch.setattr(main_module, "preflight_blockers", lambda report: [])
    settings = object()
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))

    main_module._run_database_startup_preflight(app, required=True)

    assert observed == {
        "settings": settings,
        "mode": "runtime",
        "strict": True,
        "schema_target": main_module.SCHEMA_TARGET_STARTUP,
    }
    assert app.state.database_preflight_errors == ()


@pytest.mark.parametrize(
    ("probe_ready", "expected"),
    [(False, False), (True, True)],
)
def test_repository_schema_readiness_uses_shared_exact_probe(
    monkeypatch,
    probe_ready: bool,
    expected: bool,
) -> None:
    connection = object()
    observed: list[object] = []

    @contextmanager
    def factory(_dsn: str):
        yield connection

    def probe(candidate):
        observed.append(candidate)
        return SimpleNamespace(ready=probe_ready)

    monkeypatch.setattr(
        monomer_dft_repository_module,
        "probe_monomer_dft_schema",
        probe,
    )
    repository = MonomerDftRepository(
        "unused",
        connection_factory=factory,
    )

    assert repository.schema_ready() is expected
    assert observed == [connection]


def test_router_public_errors_are_always_top_level_and_path_free() -> None:
    class UnavailableRepository:
        def list_jobs(self, **_kwargs):
            raise PostgresUnavailableError("database failed at /private/socket")

    app = create_app(Settings())
    app.state.monomer_dft_repository = UnavailableRepository()
    client = TestClient(app, raise_server_exceptions=False)

    invalid = client.post("/api/v1/monomer-dft/jobs", json=_single_point_request())
    assert invalid.status_code == 422
    assert set(invalid.json()) == {"code", "message", "retryable", "details"}
    assert invalid.json()["code"] == "invalid_request"

    missing = client.get("/api/v1/monomer-dft/jobs/not-a-uuid")
    assert missing.status_code == 404
    assert missing.json() == {
        "code": "job_not_found",
        "message": "DFT job not found",
        "retryable": False,
        "details": {},
    }

    unavailable = client.get("/api/v1/monomer-dft/jobs")
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "database_unavailable"
    assert "/private" not in unavailable.text

    class BrokenRepository:
        def list_jobs(self, **_kwargs):
            raise RuntimeError("unexpected at /private/source.py")

    app.state.monomer_dft_repository = BrokenRepository()
    internal = client.get("/api/v1/monomer-dft/jobs")
    assert internal.status_code == 500
    assert internal.json() == {
        "code": "internal_error",
        "message": "monomer DFT request failed",
        "retryable": False,
        "details": {},
    }
    assert "/private" not in internal.text
    client.close()


def test_exact_dft_prefix_404_405_pagination_and_cross_site_envelopes() -> None:
    app = create_app(Settings())
    with TestClient(app, raise_server_exceptions=False) as client:
        missing = client.get("/api/v1/monomer-dft/not-a-route")
        assert missing.status_code == 404
        assert missing.json() == {
            "code": "route_not_found",
            "message": "monomer DFT route not found",
            "retryable": False,
            "details": {},
        }

        method = client.put("/api/v1/monomer-dft/status")
        assert method.status_code == 405
        assert method.json()["code"] == "method_not_allowed"
        assert "GET" in method.headers["Allow"]

        page = client.get("/api/v1/monomer-dft/jobs?page=10001")
        assert page.status_code == 422
        assert page.json()["code"] == "invalid_request"

        blocked = client.get(
            "/api/v1/monomer-dft/status",
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert blocked.status_code == 403
        assert blocked.json() == {
            "code": "cross_site_request_blocked",
            "message": "cross-site browser requests are not allowed",
            "retryable": False,
            "details": {},
        }

        other = client.get(
            "/api/v1/not-a-route",
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert other.status_code == 403
        assert other.json() == {"detail": "cross-site browser requests are not allowed"}


def test_submit_maps_scientific_idempotency_capacity_and_recovery_errors() -> None:
    class Repository:
        conflict = False

        def find_idempotent_job(self, **_kwargs):
            if self.conflict:
                raise MonomerDftIdempotencyConflict(
                    "Idempotency-Key was already used for a different DFT request"
                )
            return None

    class Worker:
        health_payload = {
            "status": "ok",
            "runtime_ready": True,
            "accepting_jobs": False,
            "draining": False,
            "recovering": False,
            "queued_jobs": 8,
            "max_queued_jobs": 8,
        }

        async def health(self):
            return dict(self.health_payload)

    settings = Settings(
        monomer_dft_submit_enabled=True,
        monomer_dft_worker_uds="/tmp/monomer-dft-test.sock",
    )
    app = create_app(settings)
    repository = Repository()
    worker = Worker()
    app.state.monomer_dft_repository = repository
    app.state.monomer_dft_worker_client = worker
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Idempotency-Key": "mapping-test-0001"}

    invalid_payload = _single_point_request()
    invalid_payload["input"] = {**invalid_payload["input"], "smiles": "not-a-smiles"}
    invalid = client.post("/api/v1/monomer-dft/jobs", json=invalid_payload, headers=headers)
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "invalid_scientific_request"

    repository.conflict = True
    conflict = client.post("/api/v1/monomer-dft/jobs", json=_single_point_request(), headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"
    repository.conflict = False

    capacity = client.post("/api/v1/monomer-dft/jobs", json=_single_point_request(), headers=headers)
    assert capacity.status_code == 429
    assert capacity.json()["code"] == "worker_capacity_full"
    assert capacity.headers["Retry-After"] == "5"

    worker.health_payload["recovering"] = True
    unavailable = client.post("/api/v1/monomer-dft/jobs", json=_single_point_request(), headers=headers)
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "worker_unavailable"
    for response in (invalid, conflict, capacity, unavailable):
        assert set(response.json()) == {"code", "message", "retryable", "details"}
    client.close()
