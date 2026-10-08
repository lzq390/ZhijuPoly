from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts import pull_deploy_controller as controller
from scripts import site_helper_contracts as contracts

ROOT = Path(__file__).resolve().parents[2]


def test_isolation_ledger_is_exact_and_legacy_v7_stays_separate():
    assert contracts.CANONICAL_MIGRATION_LEDGER[-1][0] == "0016_monomer_polymerization_batch"
    ledger = [{"version": version, "checksum": checksum} for version, checksum in contracts.USER_ISOLATION_MIGRATION_LEDGER]
    for entry in ledger:
        path = ROOT / "backend/migrations/postgres" / (entry["version"] + ".sql")
        assert entry["checksum"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert contracts.validate_user_isolation_ledger(ledger) == ledger
    ledger[-1]["checksum"] = "a" * 64
    with pytest.raises(contracts.SiteHelperContractError):
        contracts.validate_user_isolation_ledger(ledger)


def test_current_isolation_ledger_is_exact_without_expanding_historical_protocol():
    legacy = [{"version": version, "checksum": checksum}
              for version, checksum in contracts.USER_ISOLATION_MIGRATION_LEDGER]
    current = [{"version": version, "checksum": checksum}
               for version, checksum in contracts.USER_ISOLATION_V2_MIGRATION_LEDGER]
    assert legacy[-1]["version"] == "0018_user_isolation_cutover"
    assert current[-1]["version"] == "0019_service_auth_least_privilege"
    assert current[:-1] == legacy
    for entry in current:
        assert entry["checksum"] == hashlib.sha256(
            (ROOT / "backend/migrations/postgres" / (entry["version"] + ".sql")).read_bytes()).hexdigest()
    assert contracts.validate_user_isolation_v2_ledger(current) == current
    with pytest.raises(contracts.SiteHelperContractError):
        contracts.validate_user_isolation_ledger(current)
    for invalid in (legacy, current + [current[-1]], current[:-1] + [{**current[-1], "checksum":"a"*64}]):
        with pytest.raises(contracts.SiteHelperContractError):
            contracts.validate_user_isolation_v2_ledger(invalid)


def test_generic_production_deployment_cannot_cross_auth_boundary():
    legacy = [{"version": version, "checksum": checksum} for version, checksum in contracts.CANONICAL_MIGRATION_LEDGER]
    isolated = [{"version": version, "checksum": checksum} for version, checksum in contracts.USER_ISOLATION_MIGRATION_LEDGER]
    with pytest.raises(controller.PullDeployError, match="dedicated app.auth.cutover"):
        controller.canonical_ledger_history(legacy, isolated)
    with pytest.raises(controller.PullDeployError, match="dedicated app.auth.cutover"):
        controller.canonical_ledger_history(isolated, legacy)


def test_structure_library_v3_preserves_frozen_historical_ledgers():
    previous = [{"version": v, "checksum": c} for v, c in contracts.USER_ISOLATION_V2_MIGRATION_LEDGER]
    current = [{"version": v, "checksum": c} for v, c in contracts.USER_ISOLATION_V3_MIGRATION_LEDGER]
    assert current[:-1] == previous
    assert current[-1]["version"] == "0020_structure_library"
    assert current[-1]["checksum"] == hashlib.sha256((ROOT / "backend/migrations/postgres/0020_structure_library.sql").read_bytes()).hexdigest()
    assert contracts.validate_user_isolation_v3_ledger(current) == current
    for invalid in (previous, current + [current[-1]], current[:-1] + [{**current[-1], "checksum": "0"*64}]):
        with pytest.raises(contracts.SiteHelperContractError):
            contracts.validate_user_isolation_v3_ledger(invalid)
    with pytest.raises(contracts.SiteHelperContractError):
        contracts.validate_user_isolation_v2_ledger(current)
    assert ("structure_library", "records") in contracts.POST_0020_BUSINESS_MUTABLE_TABLES
    assert ("structure_library", "records") not in contracts.BUSINESS_MUTABLE_TABLES

@pytest.mark.parametrize("version, ledger_name, validator_name, boundary", [
    (4, "USER_ISOLATION_V4_MIGRATION_LEDGER", "validate_user_isolation_v4_ledger", "0021_usage_metering"),
    (5, "USER_ISOLATION_V5_MIGRATION_LEDGER", "validate_user_isolation_v5_ledger", "0022_usage_entitlements"),
])
def test_usage_and_entitlement_receipts_require_exact_bounded_release(version, ledger_name, validator_name, boundary):
    current = [{"version": v, "checksum": c} for v, c in getattr(contracts, ledger_name)]
    validate = getattr(contracts, validator_name)
    assert current[-1]["version"] == boundary
    for entry in current:
        assert entry["checksum"] == hashlib.sha256(
            (ROOT / "backend/migrations/postgres" / (entry["version"] + ".sql")).read_bytes()).hexdigest()
    assert validate(current) == current
    invalid_ledgers = [current[:-1], current + [current[-1]], list(reversed(current)),
        current[:-1] + [{**current[-1], "checksum": "0" * 64}],
        current + [{"version": "0023_unknown_future", "checksum": "0" * 64}]]
    for invalid in invalid_ledgers:
        with pytest.raises(contracts.SiteHelperContractError):
            validate(invalid)
    for historical in (contracts.validate_user_isolation_ledger,
                       contracts.validate_user_isolation_v2_ledger,
                       contracts.validate_user_isolation_v3_ledger):
        with pytest.raises(contracts.SiteHelperContractError):
            historical(current)
    if version == 5:
        with pytest.raises(contracts.SiteHelperContractError):
            contracts.validate_user_isolation_v4_ledger(current)
    # The deployment consumer remains its separate frozen v7 protocol.
    with pytest.raises(contracts.SiteHelperContractError):
        contracts.validate_mutable_data_audit({"schema_version": version, "migration_ledger": current})
    legacy = [{"version": v, "checksum": c} for v, c in contracts.CANONICAL_MIGRATION_LEDGER]
    with pytest.raises(controller.PullDeployError, match="dedicated app.auth.cutover"):
        controller.canonical_ledger_history(legacy, current)
