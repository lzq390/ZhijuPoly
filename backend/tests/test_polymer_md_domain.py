"""Standalone domain contracts: no API, database, worker, or native engine."""
from copy import deepcopy
from pathlib import Path
from typing import get_args
from uuid import uuid4

import pytest
from pydantic import ValidationError

from polymer_md.capabilities import (
    CATALOG_SOURCE_SHA, get_capability_catalog, get_model_configuration_spec,
    get_post_equilibrium_protocols, get_user_configuration_spec,
)
from polymer_md.contracts import (
    Artifact, Forcefield, ModelConfiguration, NativeConfiguration, NativeProperty,
    ScientificRequest, StageRun, SubmitRequest, WorkerSnapshot, request_sha256,
)
from polymer_md.model_configuration import estimate_model_atoms


def test_legacy_wire_round_trip_preserves_hash_and_absent_options():
    request = ScientificRequest(smiles="*CC*", seed=1729)
    wire = request.model_dump(mode="json")
    assert len(wire) == 18
    assert "model_configuration" not in wire and "native_configuration" not in wire
    assert request_sha256(ScientificRequest.model_validate(wire)) == request_sha256(request)
    assert request_sha256(request.model_copy(update={"seed": 1730})) != request_sha256(request)


def test_native_targets_are_canonical_and_change_execution_identity():
    original = ScientificRequest(smiles="*CC*", seed=1729)
    one = ScientificRequest(smiles="*CC*", seed=1729,
        native_configuration=NativeConfiguration(properties=["Rg", "density"]))
    reordered = ScientificRequest(smiles="*CC*", seed=1729,
        native_configuration=NativeConfiguration(properties=["density", "Rg"]))
    assert request_sha256(one) == request_sha256(reordered) != request_sha256(original)
    assert ScientificRequest.model_validate(one.model_dump(mode="json")) == one
    with pytest.raises(ValidationError):
        NativeConfiguration(properties=["density", "density"])
    with pytest.raises(ValidationError):
        NativeConfiguration(properties=["density"], opt_method="arbitrary-command")


@pytest.mark.parametrize("change", [
    {"forcefield": "arbitrary"}, {"solver_path": "/tmp/unapproved"},
    {"smiles": "*CC*.Cl"}, {"smiles": "*CC*\n"}, {"n_atom": True},
    {"n_atom": 5000, "n_chain": 5}, {"pressure": float("inf")},
])
def test_unapproved_or_unsafe_scientific_inputs_fail_closed(change):
    with pytest.raises(ValidationError):
        ScientificRequest(**{"smiles": "*CC*", **change})


def test_property_specific_chain_and_workflow_requirements():
    with pytest.raises(ValidationError):
        ScientificRequest(smiles="*CC*", n_chain=1,
            native_configuration={"properties": ["sp_ced"]})
    with pytest.raises(ValidationError):
        ScientificRequest(smiles="*CC*",
            native_configuration={"properties": ["thermal_conductivity"]})
    accepted = ScientificRequest(smiles="*CC*", calculation="basic_tc",
        native_configuration={"properties": ["thermal_conductivity"]})
    assert accepted.calculation == "basic_tc"


def test_model_sizing_honors_cycles_caps_and_blend_counts():
    components = [{"id": "A", "smiles": "*CC*"}, {"id": "B", "smiles": "*CO*"}]
    alternating = ModelConfiguration(mode="alternating", components=components,
        chain_length={"mode": "dp", "value": 3})
    stats = {"A": {"atoms": 4, "mass": 20}, "B": {"atoms": 6, "mass": 30}}
    caps = [{"atoms": 1, "mass": 1}, {"atoms": 1, "mass": 1}]
    assert estimate_model_atoms({"n_chain": 2,
        "model_configuration": alternating.model_dump(mode="json")}, stats, caps) == 64
    blend = ModelConfiguration(mode="blend", components=[
        {**components[0], "n_chain": 2, "chain_length": {"mode": "dp", "value": 2}},
        {**components[1], "n_chain": 1, "chain_length": {"mode": "dp", "value": 3}},
    ])
    assert estimate_model_atoms({"n_chain": 3,
        "model_configuration": blend.model_dump(mode="json")}, stats, caps) == 40


@pytest.mark.parametrize("change", [
    {"version": True}, {"ratios": [True, 0]}, {"ratios": [0.2, 0.2]},
    {"components": [{"id": "A", "smiles": "*CC*"}, {"id": "A", "smiles": "*CO*"}]},
    {"chain_length": {"mode": "dp", "value": 1.5}},
])
def test_ambiguous_model_compositions_are_rejected(change):
    value = {"mode": "random", "components": [
        {"id": "A", "smiles": "*CC*"}, {"id": "B", "smiles": "*CO*"}],
        "chain_length": {"mode": "dp", "value": 5}, "ratios": [0.6, 0.4]}
    with pytest.raises(ValidationError):
        ModelConfiguration.model_validate({**value, **change})


def test_worker_submission_binds_effective_seed_and_scientific_hash():
    request = ScientificRequest(smiles="*CC*", seed=1729)
    value = dict(job_id=str(uuid4()), owner_user_id=str(uuid4()), attempt_token="a" * 64,
        enqueue_sequence=1, scientific_request=request, request_sha256=request_sha256(request))
    assert SubmitRequest(**value).scientific_request.seed == 1729
    with pytest.raises(ValidationError):
        SubmitRequest(**{**value, "request_sha256": "0" * 64})
    with pytest.raises(ValidationError):
        SubmitRequest(**{**value, "scientific_request": ScientificRequest(smiles="*CC*")})


@pytest.mark.parametrize("storage", ["/absolute", "../escape", "a//b", "a/../b", "a\\b"])
def test_artifact_storage_is_relative_and_portable(storage):
    with pytest.raises(ValidationError):
        Artifact(artifact_id="result", stage_run_id="eq3", kind="result",
            filename="result.json", size_bytes=1, sha256="a" * 64, storage_ref=storage)


def test_snapshot_rejects_unconfirmed_cleanup_cross_attempt_and_nonfinite_data():
    job = str(uuid4()); token = "a" * 64
    common = dict(job_id=job, attempt_token=token, request_sha256="b" * 64,
        enqueue_sequence=1, worker_instance_id="c" * 32)
    with pytest.raises(ValidationError):
        WorkerSnapshot(**common, status="completed", cleanup_confirmed=False)
    artifact = Artifact(artifact_id="result", stage_run_id="eq3", kind="result",
        filename="result.json", size_bytes=1, sha256="d" * 64,
        storage_ref=f"{job}/{'e' * 64}/result.json")
    with pytest.raises(ValidationError):
        WorkerSnapshot(**common, status="completed", cleanup_confirmed=True,
            stage_runs=[StageRun(stage_run_id="eq3", stage="eq3")], artifacts=[artifact])
    with pytest.raises(ValidationError):
        StageRun(stage_run_id="eq3", stage="eq3", results={"nested": [float("nan")]})


def test_public_catalog_cannot_widen_execution_or_mutate_cached_source():
    catalog = get_capability_catalog()
    assert catalog["source_sha"] == CATALOG_SOURCE_SHA
    assert {v["id"] for v in catalog["forcefields"] if v["status"] == "connected"} == set(get_args(Forcefield))
    for item in catalog["parameters"]:
        source = Path(item["source"])
        assert not source.is_absolute() and ".." not in source.parts
    catalog["forcefields"][0]["status"] = "experimental"
    assert get_capability_catalog()["forcefields"][0]["status"] == "connected"
    spec = get_user_configuration_spec()
    fields = {field["id"]: field for field in spec["fields"]}
    assert set(fields) == {"forcefield", *NativeConfiguration.model_fields}
    assert {v["value"] for v in fields["properties"]["options"]} == set(get_args(NativeProperty))
    spec["fields"].clear()
    assert get_user_configuration_spec()["fields"]
    assert get_model_configuration_spec()["component_limit"] == 4
    protocols = get_post_equilibrium_protocols()
    assert protocols["sp"]["minimum_chain_count"] == 2
    protocols["tg"]["minimum_temperature_k"] = 0
    assert get_post_equilibrium_protocols()["tg"]["minimum_temperature_k"] == 100
