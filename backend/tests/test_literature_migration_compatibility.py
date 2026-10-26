"""Source-only exact ledger compatibility; no SQL or admission expansion."""
from copy import deepcopy
import pytest
from app.auth.isolation_ledger import expected_isolation_ledger, validate_application_migration_ledger
from app.literature_contract import compatible_identity_ledger, registered_literature_ledger, LITERATURE_MIGRATION, LITERATURE_MIGRATION_SHA256
from scripts.site_helper_contracts import validate_user_isolation_v2_ledger, validate_literature_ledger, SiteHelperContractError

def test_bridge_accepts_only_exact_known_suffix():
    legacy = expected_isolation_ledger()
    current = registered_literature_ledger()
    prefix = current[:-1]
    assert prefix[-1]["version"] == "0023_polymer_md_jobs"
    assert compatible_identity_ledger(legacy, legacy)
    assert compatible_identity_ledger(prefix, prefix)
    assert compatible_identity_ledger(current, prefix)
    assert validate_user_isolation_v2_ledger(legacy) == legacy
    assert validate_literature_ledger(current) == current
    with pytest.raises(SiteHelperContractError): validate_user_isolation_v2_ledger(current)
    assert not compatible_identity_ledger([*legacy, current[-1]], legacy)
    for invalid in (current[:-2]+current[-1:], current+[{"version":"0025_unknown","checksum":"b"*64}],
                    [*prefix, {"version":LITERATURE_MIGRATION,"checksum":"b"*64}]):
        assert not compatible_identity_ledger(invalid, prefix)
        with pytest.raises(SiteHelperContractError): validate_literature_ledger(invalid)

def test_bridge_preflight_with_empty_checksum_cache(monkeypatch):
    from app import postgres_preflight as preflight
    monkeypatch.setattr(preflight, "_MIGRATION_CHECKSUMS", {})
    assert preflight.known_checksums_for_target(preflight.SCHEMA_TARGET_ISOLATION) == {LITERATURE_MIGRATION: LITERATURE_MIGRATION_SHA256}
    assert preflight.known_checksums_for_target(preflight.SCHEMA_TARGET_STARTUP) == {}

@pytest.mark.parametrize("missing", ["0020_structure_library", "0021_usage_metering", "0022_usage_entitlements", "0023_polymer_md_jobs"])
def test_no_intervening_main_migration_can_be_omitted(missing):
    current = registered_literature_ledger(); invalid = [row for row in current if row["version"] != missing]
    assert not compatible_identity_ledger(invalid, current[:-1])
    with pytest.raises(SiteHelperContractError): validate_literature_ledger(invalid)

@pytest.mark.parametrize("mutation", ["reordered", "duplicate", "wrong-checksum", "short-prefix"])
def test_whole_ledger_stays_immutable_and_ordered(mutation):
    current = registered_literature_ledger(); invalid = deepcopy(current)
    if mutation == "reordered": invalid[1], invalid[2] = invalid[2], invalid[1]
    elif mutation == "duplicate": invalid.insert(2, dict(invalid[1]))
    elif mutation == "wrong-checksum": invalid[3]["checksum"] = "b" * 64
    else: invalid = [*expected_isolation_ledger(), current[-1]]
    assert not compatible_identity_ledger(invalid, current[:-1])
    with pytest.raises(SiteHelperContractError): validate_literature_ledger(invalid)

def test_checksum_facts_do_not_enable_future_runtime_or_automatic_migrations():
    from app import postgres_preflight as preflight
    current = registered_literature_ledger()
    class LedgerOnly:
        def execute(self, *_): return iter(deepcopy(current))
    assert validate_application_migration_ledger(
        LedgerOnly(), require_usage=False, require_entitlements=False) == current
    class FutureLedger:
        def execute(self, *_):
            return iter([*deepcopy(current), {"version": "0025_unknown", "checksum": "b" * 64}])
    with pytest.raises(ValueError, match="exact canonical"):
        validate_application_migration_ledger(FutureLedger(), require_usage=False, require_entitlements=False)
    assert LITERATURE_MIGRATION not in preflight._EXPAND_MIGRATIONS
    for target in preflight.SCHEMA_TARGETS:
        required = preflight._required_migrations(target)
        assert (LITERATURE_MIGRATION in required) == (target == preflight.SCHEMA_TARGET_LITERATURE)

def test_wrong_cached_checksum_and_unknown_target_fail_closed(monkeypatch):
    from app import postgres_preflight as preflight
    monkeypatch.setattr(preflight, "_MIGRATION_CHECKSUMS", {LITERATURE_MIGRATION: "b" * 64})
    with pytest.raises(ValueError, match="registered release"): preflight.known_checksums_for_target(preflight.SCHEMA_TARGET_ISOLATION)
    with pytest.raises(ValueError, match="unsupported"): preflight.known_checksums_for_target("unknown-target")

def test_callers_cannot_mutate_pinned_or_cached_facts():
    from app import postgres_preflight as preflight
    value = validate_literature_ledger(registered_literature_ledger()); value[-1]["checksum"] = "b" * 64
    assert validate_literature_ledger(registered_literature_ledger())[-1]["checksum"] == LITERATURE_MIGRATION_SHA256
    known = preflight.known_checksums_for_target(preflight.SCHEMA_TARGET_ISOLATION); known.clear()
    assert preflight.known_checksums_for_target(preflight.SCHEMA_TARGET_ISOLATION)[LITERATURE_MIGRATION] == LITERATURE_MIGRATION_SHA256
