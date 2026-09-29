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


def test_generic_production_deployment_cannot_cross_auth_boundary():
    legacy = [{"version": version, "checksum": checksum} for version, checksum in contracts.CANONICAL_MIGRATION_LEDGER]
    isolated = [{"version": version, "checksum": checksum} for version, checksum in contracts.USER_ISOLATION_MIGRATION_LEDGER]
    with pytest.raises(controller.PullDeployError, match="dedicated app.auth.cutover"):
        controller.canonical_ledger_history(legacy, isolated)
    with pytest.raises(controller.PullDeployError, match="dedicated app.auth.cutover"):
        controller.canonical_ledger_history(isolated, legacy)
