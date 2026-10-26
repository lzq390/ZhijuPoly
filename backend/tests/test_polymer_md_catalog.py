"""Capability documentation contracts; no database, Worker or scientific engine."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import get_args

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app.routers.polymer_md import _scientific_protocol, router
from app.services.polymer_md_repository import PolymerMdError
from polymer_md import capabilities as catalog_module
from polymer_md.capabilities import CATALOG_SOURCE_SHA, CATALOG_STATUSES, get_capability_catalog, get_user_configuration_spec, get_post_equilibrium_protocols
from polymer_md.contracts import Forcefield, NativeConfiguration, NativeProperty, ScientificRequest, request_sha256


ROOT = Path(__file__).resolve().parents[2]


def test_catalog_import_is_independent_of_scientific_engines():
    code = """
import sys
from polymer_md.capabilities import get_capability_catalog
value = get_capability_catalog()
assert value['catalog_version'] == 1
assert not {'radonpy', 'psi4', 'rdkit', 'numpy', 'scipy', 'mdtraj'} & set(sys.modules)
"""
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True, capture_output=True, text=True)


def test_catalog_has_frozen_provenance_and_cannot_expand_executable_options():
    value = get_capability_catalog()
    assert value["catalog_version"] == 1 and value["source_sha"] == CATALOG_SOURCE_SHA
    assert {item["id"] for item in value["forcefields"] if item["status"] == "connected"} == set(get_args(ScientificRequest.model_fields["forcefield"].annotation))
    assert {item["id"] for item in value["calculations"] if item["status"] == "connected" and "selection" not in item} == set(get_args(ScientificRequest.model_fields["calculation"].annotation))
    assert {item["id"] for item in value["calculations"] if "selection" in item} == {"tg", "solubility_parameter"}
    assert {item["id"] for item in value["forcefields"] if item["status"] != "connected"} >= {"AMBER", "GLYCAM_06j", "TIP3P", "TIP4P", "TIP5P"}
    for section in ("forcefields", "calculations"):
        for item in value[section]:
            assert item["label"] and item["description"] and item["status"] in CATALOG_STATUSES
            if item["status"] != "connected":
                assert item["status"] in {"upstream_only", "experimental"}
    assert "不代表材料科学验收通过" in value["description"]


def test_all_public_request_fields_have_one_group_and_the_real_default():
    params = get_capability_catalog()["parameters"]
    public = {item["key"]: item for item in params if item["group"] in {"basic", "advanced"}}
    assert set(public) == set(ScientificRequest.model_fields)
    assert len(public) == 20
    for key, field in ScientificRequest.model_fields.items():
        item = public[key]
        assert item["status"] == "connected" and item["source"] == "polymer_md/contracts.py"
        if field.is_required():
            assert "default" not in item
        else:
            assert item["default"] == field.get_default(call_default_factory=True)
    assert public["n_atom"]["unit"] == "atom/chain"
    assert "RNEMD固定0.2ps" in public["sample_interval_ps"]["description"]


def test_property_keys_units_and_derivation_limits_match_published_records():
    groups = get_capability_catalog()["property_groups"]
    properties = {item["key"]: item for group in groups for item in group["properties"]}
    expected = {"density", "temperature", "total_energy", "Cp", "Cv", "compressibility", "isentropic_compressibility", "bulk_modulus", "isentropic_bulk_modulus", "volume_expansion", "linear_expansion", "Rg", "r2", "nematic_order_parameter", "self-diffusion", "static_dielectric_const", "refractive_index", "electronic_corrected_dielectric", "thermal_conductivity", "thermal_diffusivity"}
    assert set(properties) == expected
    assert not {"qm_homo", "qm_lumo", "qm_polarizability", "qm_total_energy"} & set(properties)
    assert {item["key"] for item in properties.values() if item["status"] == "derived"} == {"refractive_index", "electronic_corrected_dielectric", "thermal_diffusivity"}
    assert properties["r2"]["unit"] == "nm2" and properties["Rg"]["unit"] == "A"
    assert properties["Cp"]["unit"] == "J/(kg K)"
    assert properties["thermal_conductivity"]["unit"] == "W/(m K)"
    assert properties["thermal_diffusivity"]["unit"] == "m2/s"
    assert properties["thermal_diffusivity"]["dependencies"] == ["thermal_conductivity", "density", "Cp"]
    assert "density" in properties["Cv"]["dependencies"] and "density" in properties["isentropic_compressibility"]["dependencies"]
    assert "全部 passed" in properties["thermal_diffusivity"]["description"]
    assert "warning" in properties["refractive_index"]["description"]
    assert "不是链质心扩散" in properties["self-diffusion"]["description"]
    for item in properties.values():
        assert item["label"] and item["unit"] and item["description"] and item["dependencies"]


def test_workflow_property_coverage_is_explicit_and_does_not_select_new_tasks():
    value = get_capability_catalog()
    groups = {group["id"]: group for group in value["property_groups"]}
    workflows = {item["id"]: item for item in value["calculations"]}
    def keys(workflow):
        return {item["key"] for group in workflow["property_groups"] for item in groups[group]["properties"]}
    assert "thermal_conductivity" not in keys(workflows["basic"])
    assert keys(workflows["basic_tc"]) - keys(workflows["basic"]) == {"thermal_conductivity", "thermal_diffusivity"}
    for key in ("tg", "solubility_parameter"):
        assert workflows[key]["status"] == "connected"
        assert workflows[key]["selection"]["field"] == "native_configuration.properties"
        assert set(workflows[key]["selection"]["properties"]) <= set(get_args(NativeProperty))
        # Targets select an additional branch, not a new top-level workflow enum.
        with pytest.raises(ValidationError):
            ScientificRequest(smiles="*CC*", calculation=key)
    for key in ("dynamic_dielectric", "thermal_langevin", "thermal_green_kubo", "thermal_decomposition", "elongation", "tensile_modulus_dev", "tddft_optical_dispersion"):
        assert workflows[key]["status"] in {"upstream_only", "experimental"}
        with pytest.raises(ValidationError):
            ScientificRequest(smiles="*CC*", calculation=key)


def test_tddft_dispersion_is_upstream_documentation_and_does_not_publish_new_properties():
    catalog = get_capability_catalog()
    item = next(item for item in catalog["calculations"] if item["id"] == "tddft_optical_dispersion")
    assert item["status"] == "upstream_only"
    assert "refractive_index_sos" in item["description"] and "abbe_number_sos" in item["description"]
    assert "单体电子响应不等于已验证体相光学性质" in item["description"]
    assert "不等于完整光谱材料验收" in item["description"]
    assert any("密度" in requirement and "ratio" in requirement for requirement in item["requirements"])
    assert "property_groups" not in item
    parameter = next(item for item in catalog["parameters"] if item["key"] == "upstream.qm.dynamic_polarizability")
    assert parameter["group"] == "upstream" and parameter["status"] == "upstream_only"
    assert "wavelength（nm" in parameter["description"] and "p_state" in parameter["description"]
    assert sum(item["group"] == "upstream" for item in catalog["parameters"]) == 18
    assert sum(len(group["properties"]) for group in catalog["property_groups"]) == 20


def test_protocol_defaults_agree_with_backend_protocol_and_remain_read_only():
    parameters = {item["key"]: item for item in get_capability_catalog()["parameters"]}
    protocol = _scientific_protocol()
    expected = {
        "protocol.qm.conformer_count": protocol["qm"]["conformer_search_count"],
        "protocol.qm.dft_conformer_count": protocol["qm"]["dft_conformer_count"],
        "protocol.build.initial_density": protocol["initial_density_g_cm3"],
        "protocol.packing.target_density": protocol["packing"]["target_density_g_cm3"],
        "protocol.packing.max_temperature": protocol["packing"]["max_temperature_k"],
        "protocol.eq21step.max_temperature": protocol["equilibration"]["max_temperature_k"],
        "protocol.eq21step.max_pressure": protocol["equilibration"]["max_pressure_atm"],
        "protocol.eq_production.time_step": protocol["equilibration"]["time_step_fs"],
        "protocol.rnemd.time_step": protocol["rnemd"]["time_step_fs"],
        "protocol.rnemd.preheat": protocol["rnemd"]["preheat_ps"],
        "protocol.rnemd.rep_other": protocol["rnemd"]["replication_other_axes"],
        "protocol.rnemd.decomp": protocol["rnemd"]["decomposition"],
        "protocol.analysis.eq_burn_in": protocol["equilibrium_analysis"]["burn_in_ps"],
        "protocol.analysis.eq_min_samples": protocol["equilibrium_analysis"]["minimum_production_samples"],
        "protocol.analysis.blocks": protocol["equilibrium_analysis"]["uncertainty_blocks"],
        "protocol.analysis.tc_burn_in": protocol["rnemd"]["burn_in_ps"],
    }
    for key, default in expected.items():
        assert parameters[key]["group"] == "protocol" and parameters[key]["status"] == "fixed"
        assert parameters[key]["default"] == default
    for key, name in {"protocol.qm.optimization": "optimization", "protocol.qm.single_point": "single_point",
                      "protocol.qm.polarizability": "static_polarizability"}.items():
        assert parameters[key]["status"] == "connected"
        assert parameters[key]["legacy_default"] == protocol["qm"]["legacy_defaults"][name]
    assert protocol["qm"]["configuration_source"] == "scientific_request.native_configuration"
    assert protocol["qm"]["legacy_defaults_apply"] == "only_when_native_configuration_is_absent"
    assert protocol["forcefield"] == "scientific_request.forcefield"
    assert parameters["protocol.packing.time_steps"]["default"] == [0.1, 1, 1]
    assert parameters["protocol.rnemd.slabs"]["default"] == 20
    assert parameters["protocol.rnemd.exchange_steps"]["default"] == 1000
    assert parameters["protocol.nonbonded.pppm_accuracy"]["default"] == 1e-6
    for item in parameters.values():
        assert item["source"] and not Path(item["source"]).is_absolute() and ".." not in Path(item["source"]).parts
        if item["group"] in {"protocol", "upstream"}:
            assert item["status"] in {"fixed", "upstream_only", "experimental", "connected"}
    with pytest.raises(ValidationError):
        ScientificRequest(smiles="*CC*", **{"protocol.rnemd.slabs": 40})


def test_catalog_is_detached_and_does_not_change_request_hashes():
    request = ScientificRequest(smiles="*CC*", seed=1729)
    before = request_sha256(request)
    catalog = get_capability_catalog()
    catalog["forcefields"][0]["status"] = "experimental"
    catalog["parameters"][0]["description"] = "caller mutation"
    assert get_capability_catalog()["forcefields"][0]["status"] == "connected"
    assert get_capability_catalog()["parameters"][0]["description"] != "caller mutation"
    assert request_sha256(request) == before


def test_protocol_notes_distinguish_platform_steps_and_property_specific_quality_limits():
    params = {item["key"]: item for item in get_capability_catalog()["parameters"]}
    assert params["upstream.tg.cooling_rate"]["unit"] == "step/interval_temp"
    assert params["upstream.tg.cooling_rate"]["default"] == 1000
    assert "不是K/ns也不是step/K" in params["upstream.tg.cooling_rate"]["description"]
    assert params["protocol.analysis.trajectory_min_frames"]["default"] == 100
    assert params["protocol.analysis.structure_relative_se"]["default"]["Rg"] == 0.01
    assert params["protocol.analysis.response_relative_se"]["default"] == 0.25
    assert "原生Time的常量偏移经验证后记录" in params["protocol.rnemd.time_step"]["description"]


@pytest.mark.parametrize("case", ["version", "source", "duplicate", "status", "active_forcefield", "active_calculation", "property_group", "parameter_group", "nan"])
def test_invalid_catalog_fails_document_validation(tmp_path, monkeypatch, case):
    value = deepcopy(get_capability_catalog())
    if case == "version": value["catalog_version"] = 2
    elif case == "source": value["source_sha"] = "0" * 40
    elif case == "duplicate": value["parameters"].append(value["parameters"][0])
    elif case == "status": value["calculations"][0]["status"] = "enabled"
    elif case == "active_forcefield": value["forcefields"][1]["status"] = "upstream_only"
    elif case == "active_calculation": next(item for item in value["calculations"] if item["id"] == "dynamic_dielectric")["status"] = "connected"
    elif case == "property_group": value["calculations"][0]["property_groups"] = ["absent"]
    elif case == "parameter_group": value["parameters"][0]["group"] = "arbitrary"
    elif case == "nan": value["parameters"][0]["default"] = float("nan")
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(catalog_module, "CATALOG_PATH", path)
        catalog_module._read_catalog.cache_clear()
        try:
            with pytest.raises(ValueError):
                get_capability_catalog()
        finally:
            catalog_module._read_catalog.cache_clear()


@pytest.mark.parametrize("path", ["capabilities", "status"])
@pytest.mark.parametrize("ready", [False, True])
def test_capability_endpoint_returns_the_same_catalog_without_widening_active_options(path, ready):
    app = FastAPI()
    app.state.settings = SimpleNamespace(polymer_md_dev_enabled=ready, polymer_md_submit_enabled=False)
    app.state.polymer_md_repository = SimpleNamespace(schema_ready=lambda: True)
    class Worker:
        async def health(self):
            return {"ready": True, "runtime": {"source_sha": CATALOG_SOURCE_SHA, "verified_files": [{"path": "/private/engine"}], "token": "PRIVATE_CANARY"}}
    app.state.polymer_md_worker_client = Worker() if ready else None
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/polymer-md/{path}")
    assert response.status_code == 200
    value = response.json()
    assert value["capability_catalog"] == get_capability_catalog()
    assert value["supported_forcefields"] == list(get_args(Forcefield))
    assert value["supported_calculations"] == ["basic", "basic_tc"]
    assert value["native_configuration_schema"] == NativeConfiguration.model_json_schema()
    assert value["user_configuration_spec"] == get_user_configuration_spec()
    assert value["user_configuration_spec"]["configuration_version"] == 2
    assert value["parameter_defaults"]["native_configuration"] is None
    assert not value["submit_enabled"]
    assert value["worker_ready"] is ready
    assert "/private/engine" not in response.text and "PRIVATE_CANARY" not in response.text
    assert response.headers["cache-control"] == "private, no-store"


def test_unavailable_worker_does_not_hide_static_documentation_or_become_ready():
    app = FastAPI()
    app.state.settings = SimpleNamespace(polymer_md_dev_enabled=True, polymer_md_submit_enabled=True)
    app.state.polymer_md_repository = SimpleNamespace(schema_ready=lambda: True)
    class Worker:
        async def health(self):
            raise PolymerMdError("worker_unavailable", "unavailable", 503)
    app.state.polymer_md_worker_client = Worker()
    app.include_router(router)
    with TestClient(app) as client:
        value = client.get("/api/v1/polymer-md/capabilities").json()
    assert value["capability_catalog"] == get_capability_catalog()
    assert not value["worker_ready"] and not value["submit_enabled"]


def test_native_menu_matches_the_closed_execution_contract():
    spec = get_user_configuration_spec()
    fields = {field["id"]: field for field in spec["fields"]}
    assert set(fields) == {"forcefield", *NativeConfiguration.model_fields}
    assert {option["value"] for option in fields["forcefield"]["options"]} == set(get_args(Forcefield))
    assert {option["value"] for option in fields["properties"]["options"]} == set(get_args(NativeProperty))
    for name, model_field in NativeConfiguration.model_fields.items():
        if name != "properties":
            assert {option["value"] for option in fields[name]["options"]} == set(get_args(model_field.annotation))
    assert not {"youngs_modulus", "poisson_ratio", "glass_transition_temperature"} & set(get_args(NativeProperty))
    assert "thermal_diffusivity" in get_args(NativeProperty)
    spec["fields"].clear()
    assert get_user_configuration_spec()["fields"]


def test_postequilibrium_protocols_report_fixed_sampling_and_exact_native_keys():
    protocols = get_post_equilibrium_protocols()
    assert protocols == get_user_configuration_spec()["post_equilibrium_protocols"]
    assert protocols["tg"]["method"] == "step_wise"
    assert protocols["tg"]["minimum_temperature_k"] == 100
    assert protocols["tg"]["maximum_temperature_k"] == 800
    assert protocols["tg"]["interval_temperature_k"] == 10
    assert protocols["tg"]["temperature_platforms"] == 71
    assert protocols["tg"]["high_temperature_equilibration_ps"] == 500
    assert protocols["tg"]["temperature_interval_ps"] == 20
    assert protocols["tg"]["temperature_hold_ps"] + protocols["tg"]["cooling_ramp_ps"] == 20
    assert protocols["sp"]["properties"] == ["sp_total", "sp_ced"]
    assert protocols["sp"]["minimum_chain_count"] == 2 and not protocols["sp"]["decomposition"]
    assert protocols["sp"]["sampling_ps"] == 500 and protocols["sp"]["burn_in_ps"] == 100
    assert protocols["sp"]["sample_interval_ps"] == 1
    protocols["tg"]["minimum_temperature_k"] = 50
    assert get_post_equilibrium_protocols()["tg"]["minimum_temperature_k"] == 100


@pytest.mark.parametrize("case", ["unknown_method", "missing_target", "duplicate", "unknown_field", "default", "tg_window", "sp_chain_count"])
def test_native_menu_divergence_fails_closed(tmp_path, monkeypatch, case):
    spec = get_user_configuration_spec()
    fields = {field["id"]: field for field in spec["fields"]}
    if case == "unknown_method": fields["opt_method"]["options"][0]["value"] = "arbitrary"
    elif case == "missing_target": fields["properties"]["options"].pop()
    elif case == "duplicate": fields["charge"]["options"].append(deepcopy(fields["charge"]["options"][0]))
    elif case == "unknown_field": spec["fields"].append({"id": "solver_path"})
    elif case == "default": fields["sp_basis"]["default"] = "6-31G(d)"
    elif case == "tg_window": spec["post_equilibrium_protocols"]["tg"]["minimum_temperature_k"] = 50
    elif case == "sp_chain_count": spec["post_equilibrium_protocols"]["sp"]["minimum_chain_count"] = 1
    path = tmp_path / "configuration.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(catalog_module, "USER_CONFIGURATION_PATH", path)
        catalog_module._read_user_configuration.cache_clear()
        try:
            with pytest.raises(ValueError):
                get_user_configuration_spec()
        finally:
            catalog_module._read_user_configuration.cache_clear()
