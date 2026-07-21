from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from time import sleep

import pytest

from app.postgres_database import postgres_connection
from app.services.similarity import similarity_search_postgres
from app.services.structure_similarity_index import (
    StructureSimilarityIndex,
    StructureSimilarityIndexUnavailableError,
    _StructureSimilaritySourceChanged,
    _source_signature,
)
from app.utils.exceptions import InvalidSmilesError


def test_similarity_search_returns_sorted_matches(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        results = similarity_search_postgres(connection, "CCO", similarity_threshold=0.3, top_k=2)

    assert len(results) == 2
    assert results[0][0]["smiles"] == "CCO"
    assert results[0][1] == 1.0
    assert results[0][1] >= results[1][1]


def test_similarity_search_skips_unparseable_rows(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        results = similarity_search_postgres(connection, "CCO", similarity_threshold=0.0, top_k=10)

    assert all(row["smiles"] != "not-a-smiles" for row, _ in results)


def test_similarity_search_rejects_invalid_smiles(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        with pytest.raises(InvalidSmilesError):
            similarity_search_postgres(connection, "not-a-smiles")


def test_similarity_index_fails_closed_for_parseable_blank_candidate(postgres_dsn: str) -> None:
    index = StructureSimilarityIndex()
    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO core.polymers (polymer_id, polymer_name, smiles, canonical_smiles, rdkit_parse_ok)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (4, "blank", "", "", True),
        )
        with pytest.raises(StructureSimilarityIndexUnavailableError):
            similarity_search_postgres(
                connection,
                "CCO",
                similarity_threshold=0.0,
                top_k=10,
                index=index,
            )
        with pytest.raises(StructureSimilarityIndexUnavailableError):
            similarity_search_postgres(
                connection,
                "CCO",
                similarity_threshold=0.0,
                top_k=10,
                index=index,
            )

    assert index.build_count == 1


def test_similarity_index_retries_transient_build_failure(
    postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = StructureSimilarityIndex()
    original_build = index._build_snapshot
    attempts = 0

    def fail_once(connection, signature):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient database read failure")
        return original_build(connection, signature)

    monkeypatch.setattr(index, "_build_snapshot", fail_once)
    with postgres_connection(postgres_dsn) as connection:
        with pytest.raises(StructureSimilarityIndexUnavailableError):
            similarity_search_postgres(connection, "CCO", index=index)
        results = similarity_search_postgres(connection, "CCO", index=index)

    assert results[0][0]["polymer_id"] == 1
    assert attempts == 2
    assert index.build_count == 2


def test_similarity_index_reuses_process_local_snapshot(postgres_dsn: str) -> None:
    index = StructureSimilarityIndex()

    with postgres_connection(postgres_dsn) as connection:
        first = similarity_search_postgres(connection, "CCO", index=index)
        second = similarity_search_postgres(connection, "CCN", index=index)

    assert first[0][0]["polymer_id"] == 1
    assert second[0][0]["polymer_id"] == 2
    assert index.build_count == 1


def test_similarity_index_rebuilds_after_governance_signature_changes(postgres_dsn: str) -> None:
    index = StructureSimilarityIndex()

    with postgres_connection(postgres_dsn) as connection:
        similarity_search_postgres(connection, "CCO", index=index)

    with postgres_connection(postgres_dsn) as connection:
        source_row = connection.execute(
            """
            UPDATE governance.source_files
            SET sha256 = %s, updated_at = now()
            WHERE logical_name = 'core_property_csv'
            RETURNING source_file_id
            """,
            ("b" * 64,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO governance.import_batches (
              dataset_key, source_file_id, finished_at, status, row_count
            )
            VALUES ('core', %s, now(), 'completed', %s)
            """,
            (int(source_row["source_file_id"]), 6),
        )

    with postgres_connection(postgres_dsn) as connection:
        results = similarity_search_postgres(connection, "CCO", index=index)

    assert results[0][0]["polymer_id"] == 1
    assert index.build_count == 2


def test_similarity_index_never_republishes_a_stale_signature(postgres_dsn: str) -> None:
    index = StructureSimilarityIndex()

    with postgres_connection(postgres_dsn) as connection:
        similarity_search_postgres(connection, "CCO", index=index)
        stale_signature = _source_signature(connection)

        source_row = connection.execute(
            """
            UPDATE governance.source_files
            SET sha256 = %s, updated_at = clock_timestamp()
            WHERE logical_name = 'core_property_csv'
            RETURNING source_file_id
            """,
            ("c" * 64,),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO governance.import_batches (
              dataset_key, source_file_id, finished_at, status, row_count
            )
            VALUES ('core', %s, clock_timestamp(), 'completed', %s)
            """,
            (int(source_row["source_file_id"]), 6),
        )
        similarity_search_postgres(connection, "CCO", index=index)

        with pytest.raises(_StructureSimilaritySourceChanged):
            index._snapshot_for_signature(connection, stale_signature)

    assert index.build_count == 2


def test_similarity_index_has_only_one_concurrent_builder(
    postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = StructureSimilarityIndex()
    original_build = index._build_snapshot
    build_calls = 0
    build_calls_lock = Lock()
    search_barrier = Barrier(4)

    def slow_build(connection, signature):
        nonlocal build_calls
        with build_calls_lock:
            build_calls += 1
        sleep(0.1)
        return original_build(connection, signature)

    monkeypatch.setattr(index, "_build_snapshot", slow_build)

    def search() -> int:
        with postgres_connection(postgres_dsn) as connection:
            search_barrier.wait(timeout=2)
            results = similarity_search_postgres(connection, "CCO", index=index)
            return int(results[0][0]["polymer_id"])

    with ThreadPoolExecutor(max_workers=4) as executor:
        polymer_ids = list(executor.map(lambda _index: search(), range(4)))

    assert polymer_ids == [1, 1, 1, 1]
    assert build_calls == 1
    assert index.build_count == 1
