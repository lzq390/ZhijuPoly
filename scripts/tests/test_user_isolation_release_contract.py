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
