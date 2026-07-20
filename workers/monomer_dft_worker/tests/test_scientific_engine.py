from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from workers.monomer_dft_worker.app.chemistry import (
    ChemistryValidationError,
    parse_smiles_or_psmiles,
    prepare_molecule,
    validate_request_chemistry,
)
from workers.monomer_dft_worker.app.engine import (
    OptimizationOutcome,
    ScientificEngine,
    calculate_projected_frequencies,
    _scientific_status,
)
from workers.monomer_dft_worker.app.schemas import JobSubmitRequest, MolecularInput


GPU_PROVENANCE = {
    "execution_path": "primary",
    "gpu_uuid": "GPU-test",
    "gpu_budget_mib": 4096,
    "broker_instance_id": "broker-test",
    "lease_id": "lease-test",
    "fencing_token": 1,
}


def _request(**changes) -> JobSubmitRequest:
    payload = {
        "schema_version": 2,
        "enqueue_sequence": 1,
        "job_id": "job-1",
        "attempt_token": "a" * 32,
        "input": {"smiles": "O", "net_charge": 0, "multiplicity": 1},
        "calculation_type": "single_point",
        "model": "aimnet2",
        "conformer": {"seed": 7, "max_iterations": 50},
        "single_point": {"properties": ["energy", "charges", "forces"]},
    }
    payload.update(changes)
    return JobSubmitRequest.model_validate(payload)


class HarmonicBackend:
    def evaluate(
        self,
        *,
        atomic_numbers,
        coordinates_angstrom,
        net_charge,
        forces,
        hessian,
        **_kwargs,
    ):
        coordinates = np.asarray(coordinates_angstrom, dtype=np.float64)
        atom_count = len(atomic_numbers)
        result = {
            "energy": np.asarray([0.5 * np.square(coordinates).sum()]),
            "charges": np.full(atom_count, net_charge / atom_count),
        }
        if forces:
            result["forces"] = -coordinates
        if hessian:
            result["hessian"] = np.eye(atom_count * 3)
        return result

    def optimize(
        self,
        *,
        coordinates_angstrom,
        progress,
        cancelled,
        **_kwargs,
    ):
        assert not cancelled()
        coordinates = np.zeros_like(coordinates_angstrom, dtype=np.float64)
        progress(1, 0.0, 0.0)
        return OptimizationOutcome(
            coordinates_angstrom=coordinates,
            converged=True,
            steps=1,
            trace=[
                {
                    "step": 1,
                    "energy_eV": 0.0,
                    "fmax_eV_per_A": 0.0,
                    "coordinates_angstrom": coordinates.tolist(),
                    "charges_e": [0.0] * len(coordinates),
                }
            ],
        )


class SynchronizationRecordingBackend(HarmonicBackend):
    def __init__(self) -> None:
        self.events: list[str] = []

    def synchronize(self) -> None:
        self.events.append("synchronize")

    def evaluate(self, *, hessian, **kwargs):
        self.events.append("evaluate_hessian" if hessian else "evaluate")
        return super().evaluate(hessian=hessian, **kwargs)

    def optimize(self, **kwargs):
        self.events.append("optimize")
        return super().optimize(**kwargs)


def test_request_hash_is_canonical_but_does_not_add_hessian() -> None:
    first = _request(single_point={"properties": ["forces", "energy", "frequencies"]})
    second = _request(single_point={"properties": ["frequencies", "forces", "energy"]})

    assert first.single_point is not None
    assert first.single_point.properties == ("energy", "forces", "frequencies")
    assert "hessian" not in first.single_point.properties
    assert first.properties == ("energy", "forces", "frequencies", "hessian")
    assert first.request_sha256 == second.request_sha256

    different_envelope = JobSubmitRequest.model_validate(
        {
            **first.model_dump(mode="json", exclude={"request_sha256"}),
            "job_id": "another-job",
            "attempt_token": "b" * 32,
        }
    )
    assert different_envelope.request_sha256 == first.request_sha256

    with pytest.raises(Exception):
        JobSubmitRequest.model_validate(
            {**first.model_dump(mode="json"), "request_sha256": "0" * 64}
        )

    with pytest.raises(Exception):
        _request(input={"smiles": "O", "net_charge": "0", "multiplicity": 1})


def test_request_hash_matches_cross_runtime_golden_vectors() -> None:
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "contracts"
        / "monomer_dft_request_hash_vectors.json"
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["schema_version"] == 1
    for index, vector in enumerate(contract["vectors"], start=1):
        request = JobSubmitRequest.model_validate(
            {
                "job_id": f"fixture-{index}",
                "attempt_token": f"{index:064x}",
                "schema_version": 2,
                "enqueue_sequence": index,
                **vector["request"],
            }
        )
        assert request.request_sha256 == vector["expected_sha256"], vector["name"]
        branch = (
            request.single_point
            if request.calculation_type == "single_point"
            else request.optimization
        )
        assert branch is not None
        serialized = branch.model_dump(mode="json")
        selected = serialized.get(
            "properties", serialized.get("post_optimization_properties", [])
        )
        if "frequencies" in selected:
            assert "hessian" not in selected
            assert "hessian" in request.properties


def test_chemistry_validation_matches_shared_cross_runtime_corpus() -> None:
    contract_path = (
        Path(__file__).resolve().parents[3]
        / "contracts"
        / "monomer_dft_chemistry_corpus.json"
    )
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["schema_version"] == 1

    for index, case in enumerate(contract["cases"], start=1):
        request = JobSubmitRequest.model_validate(
            {
                "job_id": f"chemistry-fixture-{index}",
                "attempt_token": f"{index:064x}",
                "schema_version": 2,
                "enqueue_sequence": index,
                **case["request"],
            }
        )
        expected = case["expected"]
        requires_hessian = any(
            property_name in request.properties
            for property_name in ("hessian", "frequencies")
        )

        if not expected["accepted"]:
            with pytest.raises(ChemistryValidationError) as error:
                validate_request_chemistry(
                    request.input,
                    request.model,
                    requires_hessian=requires_hessian,
                )
            assert error.value.code == expected["error_code"], case["id"]
            continue

        actual = validate_request_chemistry(
            request.input,
            request.model,
            requires_hessian=requires_hessian,
        )
        assert actual == {
            "input_type": expected["input_type"],
            "canonical_smiles": expected["canonical_smiles"],
            "formal_charge": expected["formal_charge"],
            "net_charge": expected["effective_charge"],
            "electron_count": expected["electron_count"],
            "heavy_atom_count": expected["heavy_atom_count"],
            "atom_count": expected["atom_count"],
            "atomic_numbers": expected["atomic_numbers"],
        }, case["id"]


def test_calculation_type_and_matching_branch_are_required() -> None:
    with pytest.raises(Exception):
        JobSubmitRequest.model_validate(
            {
                "job_id": "job-1",
                "attempt_token": "a" * 32,
                "schema_version": 2,
                "enqueue_sequence": 1,
                "input": {"smiles": "O"},
                "single_point": {"properties": ["energy"]},
            }
        )
    optimization = JobSubmitRequest.model_validate(
        {
            "job_id": "job-1",
            "attempt_token": "a" * 32,
            "schema_version": 2,
            "enqueue_sequence": 1,
            "input": {"smiles": "O"},
            "calculation_type": "optimization",
            "optimization": {"max_steps": 10},
        }
    )
    assert optimization.single_point is None


def test_optimization_always_returns_core_properties_and_hashes_original_spec() -> None:
    request = _request(
        calculation_type="optimization",
        single_point=None,
        optimization={"post_optimization_properties": ["frequencies"]},
    )

    assert request.optimization is not None
    assert request.optimization.post_optimization_properties == ("frequencies",)
    assert request.properties == (
        "energy",
        "charges",
        "forces",
        "frequencies",
        "hessian",
    )
    boundary = _request(
        calculation_type="optimization",
        single_point=None,
        optimization={"fmax_eV_per_A": 0.001, "max_steps": 10},
    )
    assert boundary.optimization.fmax_eV_per_A == 0.001
    with pytest.raises(Exception):
        _request(
            calculation_type="optimization",
            single_point=None,
            optimization={"fmax_eV_per_A": 0.0009, "max_steps": 10},
        )


@pytest.mark.parametrize(
    ("smiles", "mode", "input_type"),
    (("*CC", "cap", "psmiles_cap"), ("*CCC*", "close", "psmiles_close")),
)
def test_psmiles_modes_accept_degree_one_markers(
    smiles: str, mode: str, input_type: str
) -> None:
    molecule, actual_type = parse_smiles_or_psmiles(smiles, mode)

    assert actual_type == input_type
    assert all(atom.GetAtomicNum() != 0 for atom in molecule.GetAtoms())


def test_regular_smiles_and_invalid_psmiles_modes_are_rejected() -> None:
    with pytest.raises(ChemistryValidationError, match="must be null"):
        parse_smiles_or_psmiles("CCO", "cap")
    with pytest.raises(ChemistryValidationError, match="exactly two"):
        parse_smiles_or_psmiles("*CC", "close")
    with pytest.raises(ChemistryValidationError, match="required"):
        parse_smiles_or_psmiles("*CC", None)


def test_model_domain_charge_multiplicity_and_size_limits() -> None:
    for smiles in ("[C+6]", "[C-6]"):
        with pytest.raises(ChemistryValidationError) as charge_range_error:
            validate_request_chemistry(
                _request(input={"smiles": smiles, "net_charge": None}).input,
                "aimnet2",
            )
        assert charge_range_error.value.code == "charge_out_of_range"
        with pytest.raises(ChemistryValidationError) as prepare_charge_error:
            prepare_molecule(MolecularInput(smiles=smiles), seed=1, max_iters=20)
        assert prepare_charge_error.value.code == "charge_out_of_range"

    with pytest.raises(ChemistryValidationError) as rxn_error:
        validate_request_chemistry(
            _request(input={"smiles": "[NH4+]", "net_charge": 1}).input,
            "aimnet2-rxn",
        )
    assert rxn_error.value.code == "unsupported_charge"

    with pytest.raises(ChemistryValidationError) as multiplicity_error:
        validate_request_chemistry(
            _request(input={"smiles": "[CH3]", "multiplicity": 2}).input,
            "aimnet2",
        )
    assert multiplicity_error.value.code == "unsupported_multiplicity"

    large = "C" * 101
    with pytest.raises(ChemistryValidationError) as size_error:
        validate_request_chemistry(
            _request(input={"smiles": large}).input,
            "aimnet2",
        )
    assert size_error.value.code == "molecule_too_large"

    nse = validate_request_chemistry(
        _request(input={"smiles": "[CH3]", "multiplicity": 2}).input,
        "aimnet2-nse",
    )
    assert nse["electron_count"] == 9
    assert validate_request_chemistry(
        _request(input={"smiles": "CCO"}).input, "aimnet2-rxn"
    )

    with pytest.raises(ChemistryValidationError) as pd_error:
        validate_request_chemistry(_request(input={"smiles": "[Pd]"}).input, "aimnet2")
    assert pd_error.value.code == "unsupported_element"
    assert validate_request_chemistry(
        _request(input={"smiles": "[Pd]"}).input, "aimnet2-pd"
    )

    with pytest.raises(ChemistryValidationError) as arsenic_error:
        validate_request_chemistry(
            _request(input={"smiles": "[AsH3]"}).input, "aimnet2-pd"
        )
    assert arsenic_error.value.code == "unsupported_element"

    with pytest.raises(ChemistryValidationError) as parity_error:
        validate_request_chemistry(
            _request(input={"smiles": "[CH3]", "multiplicity": 1}).input,
            "aimnet2-nse",
        )
    assert parity_error.value.code == "charge_multiplicity_mismatch"

    with pytest.raises(ChemistryValidationError) as total_size_error:
        validate_request_chemistry(
            _request(input={"smiles": "C" * 100}).input,
            "aimnet2",
        )
    assert total_size_error.value.code == "molecule_too_large"
    with pytest.raises(ChemistryValidationError) as hessian_size_error:
        validate_request_chemistry(
            _request(input={"smiles": "C" * 40}).input,
            "aimnet2",
            requires_hessian=True,
        )
    assert hessian_size_error.value.code == "hessian_molecule_too_large"


def test_cap_mode_accepts_more_than_two_attachment_markers() -> None:
    molecule, input_type = parse_smiles_or_psmiles("*C(*)(*)C", "cap")
    assert input_type == "psmiles_cap"
    assert all(atom.GetAtomicNum() != 0 for atom in molecule.GetAtoms())


def test_pd_conformer_falls_back_to_etkdg_without_claiming_optimization() -> None:
    molecule = prepare_molecule(
        MolecularInput(smiles="[Pd]"),
        seed=11,
        max_iters=50,
    )

    assert molecule.atomic_numbers.tolist() == [46]
    assert molecule.coordinates_angstrom.shape == (1, 3)
    assert np.isfinite(molecule.coordinates_angstrom).all()
    assert molecule.rdkit_force_field == "ETKDG-only"
    assert molecule.rdkit_optimization_performed is False
    assert molecule.rdkit_optimization_status == -1


def test_explicit_isotopes_are_validated_and_preserved_for_mass_weighting() -> None:
    deuterium = prepare_molecule(
        MolecularInput(smiles="[2H][2H]"), seed=1, max_iters=20
    )
    assert deuterium.isotope_mass_numbers.tolist() == [2, 2]
    assert deuterium.atomic_masses_u.tolist() == pytest.approx(
        [2.014101778, 2.014101778], rel=1.0e-8
    )

    carbon_13 = prepare_molecule(MolecularInput(smiles="[13CH4]"), seed=1, max_iters=20)
    assert carbon_13.isotope_mass_numbers.tolist() == [13, 0, 0, 0, 0]
    assert carbon_13.atomic_masses_u[0] == pytest.approx(13.00335484, rel=1.0e-8)

    with pytest.raises(ChemistryValidationError) as error:
        validate_request_chemistry(MolecularInput(smiles="[999C]"), "aimnet2")
    assert error.value.code == "unsupported_isotope"
    assert error.value.details == {
        "atomic_number": 6,
        "isotope_mass_number": 999,
    }


def test_deuterium_frequency_obeys_inverse_square_root_mass_scaling() -> None:
    coordinates = np.asarray([[-0.37, 0.0, 0.0], [0.37, 0.0, 0.0]])
    hessian = np.eye(6)
    protium = calculate_projected_frequencies(
        hessian, np.asarray([1.007825032, 1.007825032]), coordinates
    )
    deuterium = calculate_projected_frequencies(
        hessian, np.asarray([2.014101778, 2.014101778]), coordinates
    )
    assert protium["mode_count"] == deuterium["mode_count"] == 1
    ratio = deuterium["frequencies_cm-1"][0] / protium["frequencies_cm-1"][0]
    assert ratio == pytest.approx(np.sqrt(1.007825032 / 2.014101778), rel=1.0e-10)

    hydrogen_deuteride = prepare_molecule(
        MolecularInput(smiles="[H][2H]"), seed=1, max_iters=20
    )
    assert hydrogen_deuteride.isotope_mass_numbers.tolist() == [0, 2]
    assert hydrogen_deuteride.atomic_masses_u.tolist() == pytest.approx(
        [1.008, 2.014101778], rel=1.0e-8
    )
    hd_frequencies = calculate_projected_frequencies(
        hessian,
        hydrogen_deuteride.atomic_masses_u,
        hydrogen_deuteride.coordinates_angstrom,
    )
    assert hd_frequencies["mode_count"] == 1
    assert np.isfinite(hd_frequencies["frequencies_cm-1"]).all()


def test_pd_engine_reports_etkdg_only_warning_and_provenance(tmp_path: Path) -> None:
    request = _request(
        input={"smiles": "[Pd]", "multiplicity": 1},
        model="aimnet2-pd",
    )
    result = (
        ScientificEngine(HarmonicBackend())
        .execute(
            request,
            tmp_path / "pd-artifacts",
            progress=lambda *_args: None,
            cancelled=lambda: False,
            provenance=GPU_PROVENANCE,
        )
        .result
    )

    assert result["rdkit"] == {
        "seed": 7,
        "force_field": "ETKDG-only",
        "optimization_performed": False,
        "optimization_status": -1,
        "optimization_state": "not_performed",
    }
    warning = next(
        item
        for item in result["warnings"]
        if item["code"] == "rdkit_force_field_unavailable"
    )
    assert "without force-field optimization" in warning["message"]
    assert all(item["code"] != "net_charge_override" for item in result["warnings"])
    assert result["provenance"]["rdkit_force_field"] == "ETKDG-only"
    assert result["provenance"]["rdkit_optimization_performed"] is False
    assert result["provenance"]["rdkit_optimization_status"] == -1


def test_engine_writes_nested_atomic_result_and_npz_hessian(tmp_path: Path) -> None:
    request = _request(
        calculation_type="optimization",
        single_point=None,
        optimization={
            "fmax_eV_per_A": 0.01,
            "max_steps": 10,
            "post_optimization_properties": ["frequencies"],
        },
    )
    execution = ScientificEngine(HarmonicBackend()).execute(
        request,
        tmp_path / "artifacts",
        progress=lambda *_args: None,
        cancelled=lambda: False,
        provenance={
            "worker_version": "test",
            "worker_instance_id": "instance",
            "execution_path": "overflow",
            "gpu_uuid": "GPU-test",
            "gpu_budget_mib": 4096,
            "broker_instance_id": "broker-test",
            "lease_id": "lease-test",
            "fencing_token": 42,
        },
        queue_wait_ms=12.5,
        execution_timings={"gpu_wait_ms": 7.5, "model_load_ms": 9.25},
    )

    result = execution.result
    assert result["schema_version"] == 2
    assert result["atoms"]["count"] == 3
    assert np.asarray(result["geometry"]["final_coordinates_angstrom"]).shape == (3, 3)
    assert set(result["properties"]) >= {
        "energy",
        "charges",
        "forces",
        "hessian",
        "frequencies",
    }
    assert result["scientific_status"]["minimum_assessment"] == "confirmed_minimum"
    assert result["optimization"]["trace"] == [
        {"step": 1, "energy_eV": 0.0, "fmax_eV_per_A": 0.0}
    ]
    assert result["timings"]["queue_wait_ms"] == 12.5
    assert result["timings"]["gpu_wait_ms"] == 7.5
    assert result["timings"]["model_load_ms"] == 9.25
    assert result["provenance"]["execution_path"] == "overflow"
    assert result["provenance"]["gpu_uuid"] == "GPU-test"
    assert result["provenance"]["gpu_budget_mib"] == 4096
    assert result["provenance"]["broker_instance_id"] == "broker-test"
    assert result["provenance"]["lease_id"] == "lease-test"
    assert result["provenance"]["fencing_token"] == 42
    assert set(result["timings"]) >= {
        "structure_prepare_ms",
        "model_compute_ms",
        "optimization_ms",
        "hessian_ms",
        "frequency_ms",
        "artifact_ms",
        "total_ms",
    }
    assert result["timings"]["artifact_ms"] > 0
    measured_total = sum(
        value for key, value in result["timings"].items() if key != "total_ms"
    )
    assert result["timings"]["total_ms"] >= measured_total - 0.1
    descriptors = {
        descriptor.artifact_id: (descriptor, path)
        for descriptor, path in execution.artifacts
    }
    assert descriptors["hessian"][1].suffix == ".npz"
    with np.load(descriptors["hessian"][1], allow_pickle=False) as archive:
        assert archive["hessian_eV_per_A2"].shape == (9, 9)
        assert archive["atomic_numbers"].tolist() == result["atoms"]["atomic_numbers"]
        assert archive["atomic_masses_u"].tolist() == pytest.approx(
            result["atoms"]["atomic_masses_u"]
        )
        assert (
            archive["isotope_mass_numbers"].tolist()
            == result["atoms"]["isotope_mass_numbers"]
        )
        np.testing.assert_allclose(
            archive["coordinates_angstrom"],
            result["geometry"]["final_coordinates_angstrom"],
        )
    trajectory = json.loads(descriptors["optimization_trajectory"][1].read_text())
    assert trajectory["schema_version"] == 1
    assert set(trajectory["frames"][0]) == {
        "step",
        "energy_eV",
        "fmax_eV_per_A",
        "coordinates_angstrom",
        "charges_e",
    }
    serialized = json.loads(descriptors["scientific_result"][1].read_text())
    assert serialized == result


def test_gpu_compute_timing_boundaries_synchronize_before_and_after_calls(
    tmp_path: Path,
) -> None:
    backend = SynchronizationRecordingBackend()
    request = _request(
        calculation_type="optimization",
        single_point=None,
        optimization={
            "fmax_eV_per_A": 0.01,
            "max_steps": 10,
            "post_optimization_properties": ["frequencies"],
        },
    )

    ScientificEngine(backend).execute(
        request,
        tmp_path / "synchronization-artifacts",
        progress=lambda *_args: None,
        cancelled=lambda: False,
        provenance=GPU_PROVENANCE,
    )

    assert backend.events == [
        "synchronize",
        "optimize",
        "synchronize",
        "synchronize",
        "evaluate",
        "synchronize",
        "synchronize",
        "evaluate_hessian",
        "synchronize",
    ]


def test_rigid_projection_removes_correct_modes_and_preserves_imaginary_sign() -> None:
    water_masses = np.asarray([15.999, 1.008, 1.008])
    water_coordinates = np.asarray(
        [[0.0, 0.0, 0.0], [0.95, 0.0, 0.0], [-0.24, 0.92, 0.0]]
    )
    water = calculate_projected_frequencies(-np.eye(9), water_masses, water_coordinates)
    assert water["removed_rigid_modes"] == 6
    assert water["mode_count"] == 3
    assert water["imaginary_mode_count"] == 3

    co2_masses = np.asarray([15.999, 12.011, 15.999])
    co2_coordinates = np.asarray([[-1.16, 0.0, 0.0], [0.0, 0.0, 0.0], [1.16, 0.0, 0.0]])
    carbon_dioxide = calculate_projected_frequencies(
        np.eye(9), co2_masses, co2_coordinates
    )
    assert carbon_dioxide["linear_molecule"] is True
    assert carbon_dioxide["removed_rigid_modes"] == 5
    assert carbon_dioxide["mode_count"] == 4

    monatomic = calculate_projected_frequencies(
        np.eye(3), np.asarray([4.0026]), np.asarray([[0.0, 0.0, 0.0]])
    )
    assert monatomic["removed_rigid_modes"] == 3
    assert monatomic["mode_count"] == 0

    diatomic = calculate_projected_frequencies(
        np.eye(6),
        np.asarray([1.008, 1.008]),
        np.asarray([[-0.37, 0.0, 0.0], [0.37, 0.0, 0.0]]),
    )
    assert diatomic["removed_rigid_modes"] == 5
    assert diatomic["mode_count"] == 1


def test_near_linear_co2_uses_inertia_rank_and_preserves_bending_modes() -> None:
    masses = np.asarray([15.999, 12.011, 15.999])
    near_linear = np.asarray(
        [[-1.16, 0.0, 0.0], [0.0, 2.0e-4, 0.0], [1.16, 0.0, 0.0]]
    )
    low_imaginary_hessian = -0.02 * np.eye(9)
    baseline = calculate_projected_frequencies(
        low_imaginary_hessian,
        masses,
        near_linear,
    )
    assert baseline["linear_molecule"] is True
    assert baseline["removed_rigid_modes"] == 5
    assert baseline["mode_count"] == 4
    assert baseline["imaginary_mode_count"] == 4

    angle = 0.731
    rotation = np.asarray(
        [
            [np.cos(angle), -np.sin(angle), 0.0],
            [np.sin(angle), np.cos(angle), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    transformed = near_linear @ rotation.T + np.asarray([7.0, -3.0, 2.5])
    rotated_hessian = np.kron(np.eye(3), rotation) @ low_imaginary_hessian
    rotated_hessian = rotated_hessian @ np.kron(np.eye(3), rotation).T
    transformed_result = calculate_projected_frequencies(
        rotated_hessian,
        masses,
        transformed,
    )
    assert transformed_result["linear_molecule"] is True
    assert transformed_result["removed_rigid_modes"] == 5
    assert transformed_result["mode_count"] == 4
    assert np.allclose(
        transformed_result["frequencies_cm-1"],
        baseline["frequencies_cm-1"],
        rtol=1.0e-10,
        atol=1.0e-10,
    )

    physically_bent = near_linear.copy()
    physically_bent[1, 1] = 0.05
    bent_result = calculate_projected_frequencies(
        np.eye(9),
        masses,
        physically_bent,
    )
    assert bent_result["linear_molecule"] is False
    assert bent_result["removed_rigid_modes"] == 6
    assert bent_result["mode_count"] == 3


def test_minimum_assessment_contract() -> None:
    single_point = _scientific_status(
        calculation_type="single_point",
        optimization=None,
        vibration={"imaginary_mode_count": 0},
        is_stationary=True,
        fmax=0.0,
    )
    assert single_point["minimum_assessment"] == "unassessed"
    not_converged = _scientific_status(
        calculation_type="optimization",
        optimization={"converged": False},
        vibration={"imaginary_mode_count": 0},
        is_stationary=False,
        fmax=0.2,
    )
    assert not_converged["minimum_assessment"] == "not_converged"
    not_converged_without_frequency = _scientific_status(
        calculation_type="optimization",
        optimization={"converged": False},
        vibration=None,
        is_stationary=False,
        fmax=0.2,
    )
    assert not_converged_without_frequency["minimum_assessment"] == "not_converged"
    converged_without_frequency = _scientific_status(
        calculation_type="optimization",
        optimization={"converged": True},
        vibration=None,
        is_stationary=True,
        fmax=0.0,
    )
    assert converged_without_frequency["minimum_assessment"] == "unassessed"
    saddle = _scientific_status(
        calculation_type="optimization",
        optimization={"converged": True},
        vibration={"imaginary_mode_count": 1},
        is_stationary=True,
        fmax=0.0,
    )
    assert saddle["minimum_assessment"] == "nonminimum_or_saddle"
    assert saddle["stationary_point"] == "first_order_saddle"


def test_two_negative_projected_modes_are_a_higher_order_saddle() -> None:
    masses = np.asarray([15.999, 1.008, 1.008])
    coordinates = np.asarray([[0.0, 0.0, 0.0], [0.95, 0.0, 0.0], [-0.24, 0.92, 0.0]])
    hessian = np.eye(9)
    hessian[3, 3] = -2.0
    hessian[4, 4] = -2.0
    vibration = calculate_projected_frequencies(hessian, masses, coordinates)
    assert vibration["imaginary_mode_count"] == 2
    assert all(value < -10.0 for value in vibration["imaginary_frequencies_cm-1"])

    status = _scientific_status(
        calculation_type="optimization",
        optimization={"converged": True},
        vibration=vibration,
        is_stationary=True,
        fmax=0.0,
    )
    assert status["stationary_point"] == "higher_order_saddle"
    assert status["minimum_assessment"] == "nonminimum_or_saddle"


def test_warning_contract_for_charge_override_and_rdkit_nonconvergence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workers.monomer_dft_worker.app import engine as engine_module

    original_prepare = engine_module.prepare_molecule

    def nonconverged_prepare(*args, **kwargs):
        return replace(original_prepare(*args, **kwargs), rdkit_optimization_status=1)

    monkeypatch.setattr(engine_module, "prepare_molecule", nonconverged_prepare)
    request = _request(
        input={"smiles": "O", "net_charge": 1, "multiplicity": 2},
        model="aimnet2-nse",
    )
    result = (
        ScientificEngine(HarmonicBackend())
        .execute(
            request,
            tmp_path / "warning-artifacts",
            progress=lambda *_args: None,
            cancelled=lambda: False,
            provenance=GPU_PROVENANCE,
        )
        .result
    )
    warning_codes = {warning["code"] for warning in result["warnings"]}
    assert warning_codes >= {
        "single_conformer",
        "net_charge_override",
        "rdkit_not_converged",
    }
    override = next(
        warning
        for warning in result["warnings"]
        if warning["code"] == "net_charge_override"
    )
    assert "differs" in override["message"]

    matching = (
        ScientificEngine(HarmonicBackend())
        .execute(
            _request(input={"smiles": "O", "net_charge": 0, "multiplicity": 1}),
            tmp_path / "matching-charge-artifacts",
            progress=lambda *_args: None,
            cancelled=lambda: False,
            provenance=GPU_PROVENANCE,
        )
        .result
    )
    matching_override = next(
        warning
        for warning in matching["warnings"]
        if warning["code"] == "net_charge_override"
    )
    assert "matches" in matching_override["message"]
