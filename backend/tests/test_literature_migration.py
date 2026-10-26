"""Expand migration under real PG16 auth roles and finite bridge checks."""
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep

import psycopg
from psycopg.rows import dict_row
import pytest

from test_auth_isolation import auth_database
from literature_test_support import install_literature_test_projection, literature_role_guard
from app.auth.backup import validate_backup_schema
from app.auth.cutover import backup_state_seal
from app.auth.isolation_ledger import expected_isolation_ledger, validate_service_migration_ledger
from app.literature_contract import compatible_identity_ledger, registered_literature_ledger, LITERATURE_MIGRATION, LITERATURE_MIGRATION_SHA256
from app.postgres_migrations import apply_postgres_migrations
from scripts.site_helper_contracts import validate_user_isolation_v2_ledger, validate_literature_ledger, SiteHelperContractError


def test_bridge_accepts_only_exact_known_suffix():
    legacy = expected_isolation_ledger()
    current = registered_literature_ledger()
    prefix = current[:-1]
    assert compatible_identity_ledger(legacy, legacy)
    assert compatible_identity_ledger(current, prefix)
    assert validate_user_isolation_v2_ledger(legacy) == legacy
    assert validate_literature_ledger(current) == current
    with pytest.raises(SiteHelperContractError):
        validate_user_isolation_v2_ledger(current)
    for invalid in ([*legacy, current[-1]], current[:-2] + current[-1:],
                    current + [{'version': '0025_unknown', 'checksum': 'b' * 64}],
                    [*prefix, {'version': LITERATURE_MIGRATION, 'checksum': 'b' * 64}]):
        assert not compatible_identity_ledger(invalid, prefix)
        with pytest.raises(SiteHelperContractError):
            validate_literature_ledger(invalid)
        with pytest.raises(SiteHelperContractError):
            validate_user_isolation_v2_ledger(invalid)


def test_bridge_preflight_without_packaged_sql(monkeypatch):
    from app import postgres_preflight as preflight
    monkeypatch.setattr(preflight, '_MIGRATION_CHECKSUMS', {})
    assert preflight.known_checksums_for_target(preflight.SCHEMA_TARGET_ISOLATION) == {
        LITERATURE_MIGRATION: LITERATURE_MIGRATION_SHA256}
    assert preflight.known_checksums_for_target(preflight.SCHEMA_TARGET_STARTUP) == {}


def test_optional_literature_does_not_raise_legacy_preflight_boundary():
    from app import postgres_preflight as preflight
    # The feature's own readiness validates 0024 separately. Merely packaging
    # it must not make an existing 0017 legacy preflight require identity
    # cutovers or this optional projection.
    legacy = preflight._required_migrations(preflight.SCHEMA_TARGET_FINAL)
    assert LITERATURE_MIGRATION not in legacy
    assert '0018_user_isolation_cutover' not in legacy
    assert '0019_service_auth_least_privilege' not in legacy
    isolated = preflight._required_migrations(preflight.SCHEMA_TARGET_ISOLATION)
    assert '0019_service_auth_least_privilege' in isolated
    assert LITERATURE_MIGRATION not in isolated


def test_expand_real_auth_permissions_immutability_and_backup(auth_database):
    dsn = auth_database['admin']
    apply_postgres_migrations(dsn,allowed_kinds={'baseline','expand'})
    install_literature_test_projection(dsn, full_ledger=True)
    with psycopg.connect(dsn,row_factory=dict_row) as connection:
        ledger = validate_service_migration_ledger(connection, through=LITERATURE_MIGRATION)
        assert ledger[-1] == {'version':LITERATURE_MIGRATION,'checksum':LITERATURE_MIGRATION_SHA256}
        assert validate_backup_schema(connection)['current_readiness']
        # The actual pinned SQL must reject an already existing maintenance role.
        with pytest.raises(psycopg.errors.RaiseException, match='explicit role provenance review required'):
            with connection.transaction():
                connection.execute(literature_role_guard(connection))
        seal = backup_state_seal(connection)
        assert any('literature_catalog' in key for key in seal)
        generation = str(uuid4())
        connection.execute("INSERT INTO knowledge.literature_generations(generation_id,status,normalizer_version) VALUES(%s,'building','literature-v1')",(generation,))
        connection.execute("INSERT INTO knowledge.literature_snapshots(snapshot_uid,document_uid,source_paper_id,source_extract_id,normalizer_version,content_sha256,raw_json,normalized_json) VALUES('test:snapshot','literature:test','900001','900000000000000001','literature-v1',%s,'{}','{}')",('a'*64,))
        connection.execute("INSERT INTO knowledge.literature_catalog(generation_id,document_uid,doi,snapshot_uid,title) VALUES(%s,'literature:test','10.1234/test','test:snapshot','Test')",(generation,))
        connection.execute("UPDATE knowledge.literature_generations SET status='published',is_active=true,published_at=now() WHERE generation_id=%s",(generation,))
        connection.commit()
        for statement in ("UPDATE knowledge.literature_snapshots SET raw_json='{}'",
                          "DELETE FROM knowledge.literature_catalog",
                          "UPDATE knowledge.literature_generations SET status='building',is_active=false"):
            with pytest.raises(psycopg.errors.RaiseException):
                with connection.transaction():
                    connection.execute(statement)
    with psycopg.connect(auth_database['api'],row_factory=dict_row) as connection:
        assert connection.execute('SELECT count(*) n FROM knowledge.literature_catalog').fetchone()['n'] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("INSERT INTO knowledge.literature_generations(generation_id,status,normalizer_version) VALUES(%s,'building','literature-v1')",(str(uuid4()),))
    with psycopg.connect(auth_database['audit']) as connection:
        connection.execute('SELECT count(*) FROM knowledge.literature_snapshots')


def test_catalog_insert_rechecks_status_after_publication(auth_database):
    """A writer waiting on publication must not reuse its old ready status."""
    dsn = auth_database['admin']
    apply_postgres_migrations(dsn, allowed_kinds={'baseline', 'expand'})
    install_literature_test_projection(dsn, full_ledger=True)
    generation, suffix = str(uuid4()), uuid4().hex
    with psycopg.connect(dsn) as seed:
        seed.execute("INSERT INTO knowledge.literature_generations(generation_id,status,normalizer_version) VALUES(%s,'ready','literature-v1')", (generation,))
        seed.execute("INSERT INTO knowledge.literature_snapshots(snapshot_uid,document_uid,source_paper_id,source_extract_id,normalizer_version,content_sha256,raw_json,normalized_json) VALUES(%s,%s,'1','1','literature-v1',%s,'{}','{}')", (suffix, suffix, 'a'*64))
    with psycopg.connect(dsn) as writer, psycopg.connect(dsn) as publisher, psycopg.connect(dsn) as observer:
        # activate_generation takes this row lock before counting the catalog.
        publisher.execute("SELECT generation_id FROM knowledge.literature_generations WHERE generation_id=%s FOR UPDATE", (generation,))
        def insert():
            writer.execute("INSERT INTO knowledge.literature_catalog(generation_id,document_uid,doi,snapshot_uid,title) VALUES(%s,%s,%s,%s,'Concurrent record')", (generation, suffix, '10.1234/'+suffix, suffix))
            writer.commit()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(insert)
            try:
                deadline = monotonic() + 5
                blocked = False
                while monotonic() < deadline and not future.done():
                    blocked = publisher.info.backend_pid in observer.execute("SELECT pg_blocking_pids(%s)", (writer.info.backend_pid,)).fetchone()[0]
                    if blocked:
                        break
                    sleep(0.01)
                assert blocked, 'Catalog writer should wait for the publication lock'
                publisher.execute("UPDATE knowledge.literature_generations SET status='published',published_at=now() WHERE generation_id=%s", (generation,))
                publisher.commit()
                with pytest.raises(psycopg.errors.RaiseException, match='Published literature catalogs are immutable'):
                    future.result(timeout=5)
            finally:
                publisher.rollback()
                try:
                    future.result(timeout=5)
                except psycopg.errors.RaiseException:
                    pass
                writer.rollback()


def test_combined_optional_schema_passes_current_service_and_api_contracts(auth_database):
    from app.config import Settings
    from app.postgres_preflight import (run_preflight, SCHEMA_TARGET_CURRENT_ISOLATION,
                                       SCHEMA_TARGET_LITERATURE)
    dsn = auth_database["admin"]
    apply_postgres_migrations(dsn, allowed_kinds={"baseline", "expand"})
    install_literature_test_projection(dsn, full_ledger=True)
    settings = Settings(app_postgres_dsn=auth_database["api"],
                        pi_postgres_dsn=auth_database["api"],
                        literature_enabled=True, polymer_md_dev_enabled=True,
                        polymer_md_environment="dev")
    report = run_preflight(settings, dsn=auth_database["service"], mode="schema",
                           strict=True, schema_target=SCHEMA_TARGET_CURRENT_ISOLATION)
    assert "isolation_contract_error" not in report, report.get("isolation_contract_error")
    assert report["schema_target"] == SCHEMA_TARGET_LITERATURE
    assert report["literature_schema_ready"] is True
    assert report["postgres"]["reachable"] is True
    assert report["migrations"]["missing"] == []
    assert report["migrations"]["checksum_mismatches"] == []
