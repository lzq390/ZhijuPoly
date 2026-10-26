"""Offline runtime integration regressions: exact history, no DB mutation."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from app import literature_contract as contract
from app.auth import isolation_ledger, schema
from app import postgres_preflight as preflight
from scripts import site_helper_contracts as helpers


class LedgerConnection:
    def __init__(self, ledger):
        self.ledger = ledger

    def execute(self, query, params=None):
        assert "governance.schema_migrations" in query
        return deepcopy(self.ledger)


def test_literature_boundary_contains_every_main_expansion():
    ledger = isolation_ledger.expected_isolation_ledger(contract.LITERATURE_MIGRATION)
    assert ledger == contract.registered_literature_ledger()
    assert [row["version"] for row in ledger][-5:] == [
        "0020_structure_library", "0021_usage_metering", "0022_usage_entitlements",
        "0023_polymer_md_jobs", "0024_literature_projection",
    ]


def test_full_literature_runtime_keeps_usage_and_entitlement_validation():
    connection = LedgerConnection(contract.registered_literature_ledger())
    with patch("app.services.usage_schema.validate_usage_schema") as usage, patch(
        "app.services.entitlement_schema.validate_entitlement_schema"
    ) as entitlements:
        assert isolation_ledger.validate_application_migration_ledger(
            connection, require_usage=True, require_entitlements=True
        ) == contract.registered_literature_ledger()
    usage.assert_called_once_with(connection)
    entitlements.assert_called_once_with(connection)


@pytest.mark.parametrize("version", [
    "0020_structure_library", "0021_usage_metering", "0022_usage_entitlements",
    "0023_polymer_md_jobs",
])
def test_literature_cannot_skip_an_intervening_main_expansion(version):
    ledger = [row for row in contract.registered_literature_ledger() if row["version"] != version]
    with pytest.raises(ValueError):
        isolation_ledger.validate_application_migration_ledger(
            LedgerConnection(ledger), require_usage=True, require_entitlements=True
        )


def test_literature_runtime_rejects_unknown_and_drifted_ledgers():
    full = contract.registered_literature_ledger()
    drifted = deepcopy(full)
    drifted[-1]["checksum"] = "0" * 64
    for invalid in [drifted, full + [{"version": "0025_unknown", "checksum": "0" * 64}], full[:-1] + [full[-2], full[-1]]]:
        with pytest.raises(ValueError):
            isolation_ledger.validate_application_migration_ledger(
                LedgerConnection(invalid), require_usage=True, require_entitlements=True
            )


def test_frozen_v2_receipts_do_not_accept_a_literature_shortcut():
    legacy = [{"version": v, "checksum": c} for v, c in helpers.USER_ISOLATION_V2_MIGRATION_LEDGER]
    assert helpers.validate_user_isolation_v2_ledger(legacy) == legacy
    for invalid in [legacy + [contract.registered_literature_ledger()[-1]], contract.registered_literature_ledger()]:
        with pytest.raises(helpers.SiteHelperContractError):
            helpers.validate_user_isolation_v2_ledger(invalid)
    assert helpers.validate_literature_ledger(contract.registered_literature_ledger()) == contract.registered_literature_ledger()


def test_explicit_literature_preflight_requires_md_and_projection_tables():
    target = preflight.SCHEMA_TARGET_LITERATURE
    assert preflight._required_migrations(target) == tuple(row["version"] for row in contract.registered_literature_ledger())
    tables = set(preflight._required_runtime_tables(target))
    assert ("polymer_md", "jobs") in tables
    assert set(contract.LITERATURE_TABLES) <= tables
    assert not any(name[0] == "lab" for name in tables)
    assert preflight.known_checksums_for_target(target)[contract.LITERATURE_MIGRATION] == contract.LITERATURE_MIGRATION_SHA256


def test_new_boundary_keeps_structure_ownership_and_nonowner_role_checks():
    assert schema.owner_tables(None, contract.LITERATURE_MIGRATION)["structure_library.records"] == "id"
    assert "knowledge" in schema.PRIVATE_SCHEMAS
