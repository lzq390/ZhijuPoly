"""Static capability documentation; importing this module never loads an engine.

The catalog describes more upstream features than the executable API accepts.
It must never be used to expand ScientificRequest or replace runtime readiness.
The frontend imports the same JSON for its unauthenticated/offline description.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path
from typing import get_args

from .contracts import Forcefield, ModelConfiguration, NativeConfiguration, NativeProperty, ScientificRequest


CATALOG_SOURCE_SHA = "8834b9071ae93d9af36558a3c24545dd52e5ba1a"
CATALOG_PATH = Path(__file__).with_name("capability_catalog.json")
USER_CONFIGURATION_PATH = Path(__file__).with_name("user_configuration.json")
CATALOG_STATUSES = frozenset({"connected", "upstream_only", "derived", "diagnostic", "fixed", "experimental"})


def _reject_constant(value: str):
    raise ValueError("capability catalog contains non-finite JSON")


def _entries(value: object, key: str) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"capability catalog {key} must be an object list")
    identities = [item.get(key) for item in value]
    if any(not isinstance(item, str) or not item for item in identities) or len(set(identities)) != len(identities):
        raise ValueError("capability catalog identifiers must be unique nonempty strings")
    for item in value:
        if item.get("status") not in CATALOG_STATUSES or not isinstance(item.get("label"), str) or not item["label"]:
            raise ValueError("capability catalog has an invalid label or status")
        if not isinstance(item.get("description"), str) and key != "id":
            raise ValueError("capability catalog property/parameter requires a description")
    return value


@lru_cache(maxsize=1)
def _read_catalog() -> dict:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    # allow_nan=False also catches overflowed numeric literals, such as 1e999.
    json.dumps(catalog, allow_nan=False)
    if catalog.get("catalog_version") != 1 or catalog.get("source_sha") != CATALOG_SOURCE_SHA:
        raise ValueError("capability catalog source/version does not match the frozen protocol")
    fields = _entries(catalog.get("forcefields"), "id")
    calculations = _entries(catalog.get("calculations"), "id")
    groups = _entries(catalog.get("property_groups"), "id")
    parameters = _entries(catalog.get("parameters"), "key")
    if {item["id"] for item in fields if item["status"] == "connected"} != set(get_args(Forcefield)):
        raise ValueError("documented connected forcefields must match the current API")
    if {item["id"] for item in calculations if item["status"] == "connected" and "selection" not in item} != {"basic", "basic_tc"}:
        raise ValueError("documented connected calculations must match the current API")
    selections = {"tg": ["tg"], "solubility_parameter": ["sp_total", "sp_ced"]}
    if {item["id"] for item in calculations if "selection" in item} != set(selections):
        raise ValueError("additional calculation selections must describe the supported Tg and SP routes")
    for item in calculations:
        if "selection" in item:
            if item["id"] not in selections or item["status"] != "connected" or item["selection"] != {
                "field": "native_configuration.properties", "properties": selections[item["id"]], "calculations": ["basic", "basic_tc"]}:
                raise ValueError("additional calculation selection must match supported native property targets")
    ids = {item["id"] for item in groups}
    for item in calculations:
        references = item.get("property_groups", [])
        if not isinstance(references, list) or any(reference not in ids for reference in references):
            raise ValueError("calculation references an unknown property group")
    property_keys = set()
    for group in groups:
        if group.get("calculation") not in {"basic", "basic_tc", None}:
            raise ValueError("property group has an unsupported calculation")
        for item in _entries(group.get("properties"), "key"):
            if item["key"] in property_keys or not isinstance(item.get("unit"), str):
                raise ValueError("property keys must be unique and carry explicit units")
            property_keys.add(item["key"])
    if any(item.get("group") not in {"basic", "advanced", "protocol", "upstream"} for item in parameters):
        raise ValueError("capability catalog has an unknown parameter group")
    return catalog


def get_capability_catalog() -> dict:
    """Return a detached JSON document; callers cannot mutate the cached source."""
    return deepcopy(_read_catalog())


@lru_cache(maxsize=1)
def _read_user_configuration() -> dict:
    """Fail closed if the public menu and the executable literals diverge."""
    value = json.loads(USER_CONFIGURATION_PATH.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    json.dumps(value, allow_nan=False)
    if value.get("configuration_version") != 2 or value.get("source_sha") != CATALOG_SOURCE_SHA:
        raise ValueError("native configuration source/version does not match the frozen protocol")
    fields = value.get("fields")
    if not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields):
        raise ValueError("native configuration fields must be an object list")
    by_id = {field.get("id"): field for field in fields}
    expected = {"forcefield", *NativeConfiguration.model_fields}
    if len(by_id) != len(fields) or set(by_id) != expected:
        raise ValueError("native configuration fields must match the executable schema")
    defaults = {}
    for name, field in by_id.items():
        options = field.get("options")
        if not isinstance(options, list) or any(not isinstance(option, dict) for option in options):
            raise ValueError("native configuration options must be an object list")
        values = [option.get("value") for option in options]
        allowed = set(get_args(Forcefield if name == "forcefield" else NativeProperty if name == "properties"
                               else NativeConfiguration.model_fields[name].annotation))
        if any(not isinstance(item, str) for item in values) or len(set(values)) != len(values) or set(values) != allowed:
            raise ValueError(f"native configuration {name} options diverge from execution")
        if field.get("selection") != ("multiple" if name == "properties" else "single"):
            raise ValueError("native configuration selection has the wrong shape")
        if name == "forcefield":
            if field.get("default") != ScientificRequest.model_fields[name].default:
                raise ValueError("native forcefield default diverges from execution")
        else:
            defaults[name] = field.get("default")
            if name != "properties" and defaults[name] != NativeConfiguration.model_fields[name].default:
                raise ValueError(f"native configuration {name} default diverges from execution")
    NativeConfiguration.model_validate(defaults)
    protocols = value.get("post_equilibrium_protocols", {})
    if protocols.get("tg") != {"method": "step_wise", "minimum_temperature_k": 100,
            "maximum_temperature_k": 800, "interval_temperature_k": 10, "temperature_platforms": 71,
            "high_temperature_equilibration_ps": 500, "time_step_fs": 1, "temperature_interval_ps": 20,
            "temperature_hold_ps": 10, "cooling_ramp_ps": 10, "cooling_ramps": 70,
            "sampling_authority": "server", "requires_equilibrium": True}:
        raise ValueError("Tg protocol must use the supported platform-managed stepwise window")
    if protocols.get("sp") != {"method": "molecular_interaction_rerun", "properties": ["sp_total", "sp_ced"],
            "sampling_ps": 500, "burn_in_ps": 100, "sample_interval_ps": 1, "time_step_fs": 1,
            "minimum_chain_count": 2, "decomposition": False, "sampling_authority": "server", "requires_equilibrium": True}:
        raise ValueError("SP protocol must require a multi-chain molecular rerun without decomposition")
    return value


def get_user_configuration_spec() -> dict:
    return deepcopy(_read_user_configuration())


def get_post_equilibrium_protocols() -> dict:
    return deepcopy(_read_user_configuration()["post_equilibrium_protocols"])


def get_model_configuration_spec() -> dict:
    return {"version": 1, "modes": ["homopolymer", "alternating", "random", "block", "blend"],
            "schema": ModelConfiguration.model_json_schema(),
            "chain_length_modes": {"atoms": {"minimum": 100, "maximum": 5000, "integer": True},
                                   "dp": {"minimum": 1, "maximum": 5000, "integer": True},
                                   "molecular_weight": {"minimum": 100, "maximum": 1_000_000, "unit": "g/mol"}},
            "alternating_dp_semantics": "complete_sequence_cycles;actual_DP=cycles*component_count",
            "component_limit": 4, "block_limit": 16,
            "size_admission": "chemistry_and_terminal_aware_backend_estimate_then_actual_build_count",
            "excluded_modes": ["branched", "crosslinked", "nematic", "crystalline", "cyclic"]}
