from __future__ import annotations

import heapq
import re
from dataclasses import dataclass
from threading import Condition, RLock
from typing import Any

from rdkit import DataStructs

from app.services.fingerprint import generate
from app.utils.exceptions import InvalidSmilesError


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StructureSimilarityIndexUnavailableError(RuntimeError):
    """Raised when a complete, governed in-memory index cannot be used."""


class _StructureSimilaritySourceChanged(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StructureSimilaritySourceSignature:
    import_batch_id: int
    source_file_id: int
    source_sha256: str
    source_updated_at: str
    batch_finished_at: str
    imported_row_count: int
    polymer_count: int
    parseable_count: int
    max_polymer_id: int


@dataclass(frozen=True, slots=True)
class _StructureSimilaritySnapshot:
    signature: StructureSimilaritySourceSignature
    polymer_ids: tuple[int, ...]
    fingerprints: tuple[Any, ...]


_SOURCE_SIGNATURE_SQL = """
WITH latest_core_import AS (
  SELECT
    import_batch_id,
    source_file_id,
    finished_at,
    row_count
  FROM governance.import_batches
  WHERE dataset_key = 'core'
    AND status = 'completed'
    AND finished_at IS NOT NULL
  ORDER BY import_batch_id DESC
  LIMIT 1
),
polymer_counts AS (
  SELECT
    COUNT(*) AS polymer_count,
    COUNT(*) FILTER (WHERE rdkit_parse_ok = true) AS parseable_count,
    COALESCE(MAX(polymer_id), 0) AS max_polymer_id
  FROM core.polymers
)
SELECT
  batch.import_batch_id,
  batch.source_file_id,
  batch.finished_at AS batch_finished_at,
  batch.row_count AS imported_row_count,
  source.sha256 AS source_sha256,
  source.updated_at AS source_updated_at,
  source.status AS source_status,
  counts.polymer_count,
  counts.parseable_count,
  counts.max_polymer_id
FROM latest_core_import AS batch
JOIN governance.source_files AS source
  ON source.source_file_id = batch.source_file_id
CROSS JOIN polymer_counts AS counts
"""


def _source_signature(connection: Any) -> StructureSimilaritySourceSignature:
    row = connection.execute(_SOURCE_SIGNATURE_SQL).fetchone()
    if row is None:
        raise StructureSimilarityIndexUnavailableError(
            "governed core polymer source is unavailable"
        )

    source_sha256 = str(row["source_sha256"] or "")
    if row["source_status"] != "ready" or _SHA256.fullmatch(source_sha256) is None:
        raise StructureSimilarityIndexUnavailableError(
            "governed core polymer source identity is invalid"
        )

    numeric_fields = (
        "import_batch_id",
        "source_file_id",
        "imported_row_count",
        "polymer_count",
        "parseable_count",
        "max_polymer_id",
    )
    values: dict[str, int] = {}
    for field in numeric_fields:
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise StructureSimilarityIndexUnavailableError(
                "governed core polymer source metadata is invalid"
            )
        values[field] = value

    if (
        values["import_batch_id"] < 1
        or values["source_file_id"] < 1
        or values["polymer_count"] < 1
        or values["parseable_count"] < 1
        or values["parseable_count"] > values["polymer_count"]
    ):
        raise StructureSimilarityIndexUnavailableError(
            "governed core polymer source is empty or inconsistent"
        )

    if row["batch_finished_at"] is None or row["source_updated_at"] is None:
        raise StructureSimilarityIndexUnavailableError(
            "governed core polymer source timestamps are invalid"
        )

    return StructureSimilaritySourceSignature(
        import_batch_id=values["import_batch_id"],
        source_file_id=values["source_file_id"],
        source_sha256=source_sha256,
        source_updated_at=str(row["source_updated_at"]),
        batch_finished_at=str(row["batch_finished_at"]),
        imported_row_count=values["imported_row_count"],
        polymer_count=values["polymer_count"],
        parseable_count=values["parseable_count"],
        max_polymer_id=values["max_polymer_id"],
    )


class StructureSimilarityIndex:
    """A process-local, immutable BulkTanimoto index for governed polymers.

    Every caller first reads the governed source signature.  A signature has at
    most one builder, and a snapshot is published only after every parseable
    row produced a fingerprint and the source signature remained unchanged.
    """

    def __init__(self) -> None:
        self._condition = Condition(RLock())
        self._snapshot: _StructureSimilaritySnapshot | None = None
        self._building = False
        self._building_signature: StructureSimilaritySourceSignature | None = None
        self._generation = 0
        self._failed_signature: StructureSimilaritySourceSignature | None = None
        self._failed_message: str | None = None
        self._build_count = 0

    @property
    def build_count(self) -> int:
        with self._condition:
            return self._build_count

    def _build_snapshot(
        self,
        connection: Any,
        expected_signature: StructureSimilaritySourceSignature,
    ) -> _StructureSimilaritySnapshot:
        rows = connection.execute(
            """
            SELECT polymer_id, smiles, canonical_smiles
            FROM core.polymers
            WHERE rdkit_parse_ok = true
            ORDER BY polymer_id
            """
        ).fetchall()
        if len(rows) != expected_signature.parseable_count:
            raise _StructureSimilaritySourceChanged(
                "core polymer row count changed while building the index"
            )

        polymer_ids: list[int] = []
        fingerprints: list[Any] = []
        previous_polymer_id: int | None = None
        for row in rows:
            polymer_id = int(row["polymer_id"])
            if previous_polymer_id is not None and polymer_id <= previous_polymer_id:
                raise StructureSimilarityIndexUnavailableError(
                    "core polymer identifiers are not unique and ordered"
                )
            previous_polymer_id = polymer_id

            source_smiles = str(row["canonical_smiles"] or row["smiles"] or "").strip()
            if not source_smiles:
                raise StructureSimilarityIndexUnavailableError(
                    "a parseable core polymer has no usable SMILES"
                )
            try:
                fingerprint = generate(source_smiles)
            except (TypeError, ValueError) as exc:
                raise StructureSimilarityIndexUnavailableError(
                    "a parseable core polymer could not be fingerprinted"
                ) from exc

            polymer_ids.append(polymer_id)
            fingerprints.append(fingerprint)

        current_signature = _source_signature(connection)
        if current_signature != expected_signature:
            raise _StructureSimilaritySourceChanged(
                "core polymer source changed while building the index"
            )

        return _StructureSimilaritySnapshot(
            signature=expected_signature,
            polymer_ids=tuple(polymer_ids),
            fingerprints=tuple(fingerprints),
        )

    def _snapshot_for_signature(
        self,
        connection: Any,
        signature: StructureSimilaritySourceSignature,
    ) -> _StructureSimilaritySnapshot:
        while True:
            with self._condition:
                if self._snapshot is not None and self._snapshot.signature == signature:
                    return self._snapshot
                if self._failed_signature == signature:
                    raise StructureSimilarityIndexUnavailableError(
                        self._failed_message
                        or "core polymer similarity index is unavailable"
                    )
                if self._building:
                    # A waiter must never become the next builder using the
                    # signature it read before the in-flight build completed.
                    # Force it back through _source_signature() so a refresh
                    # cannot make stale callers rebuild and republish an older
                    # snapshot.
                    while self._building:
                        self._condition.wait()
                    raise _StructureSimilaritySourceChanged(
                        "core polymer index build completed while this caller waited"
                    )
                observed_generation = self._generation

            # Revalidate outside the process lock.  A caller may have read its
            # signature before another thread published a newer snapshot; the
            # second read prevents that stale caller from clearing/rebuilding
            # the newer state.
            if _source_signature(connection) != signature:
                raise _StructureSimilaritySourceChanged(
                    "core polymer source changed before index build admission"
                )

            with self._condition:
                if self._generation != observed_generation:
                    continue
                self._building = True
                self._building_signature = signature
                self._generation += 1
                self._build_count += 1
                break

        try:
            snapshot = self._build_snapshot(connection, signature)
        except _StructureSimilaritySourceChanged:
            with self._condition:
                self._building = False
                self._building_signature = None
                self._generation += 1
                self._condition.notify_all()
            raise
        except StructureSimilarityIndexUnavailableError as exc:
            with self._condition:
                self._building = False
                self._building_signature = None
                self._failed_signature = signature
                self._failed_message = str(exc)
                self._generation += 1
                self._condition.notify_all()
            raise
        except Exception as exc:
            with self._condition:
                self._building = False
                self._building_signature = None
                self._generation += 1
                self._condition.notify_all()
            # Unknown/database failures may be transient.  Fail this request
            # closed, but do not poison an otherwise unchanged governed
            # signature forever; a later request may rebuild after recovery.
            raise StructureSimilarityIndexUnavailableError(
                "core polymer similarity index build failed"
            ) from exc

        with self._condition:
            self._snapshot = snapshot
            self._building = False
            self._building_signature = None
            self._failed_signature = None
            self._failed_message = None
            self._generation += 1
            self._condition.notify_all()
            return snapshot

    def _invalidate_snapshot(
        self,
        signature: StructureSimilaritySourceSignature,
    ) -> None:
        with self._condition:
            if self._snapshot is not None and self._snapshot.signature == signature:
                self._snapshot = None
                self._generation += 1

    def search(
        self,
        connection: Any,
        smiles: str,
        *,
        similarity_threshold: float,
        top_k: int,
    ) -> list[tuple[Any, float]]:
        try:
            query_fingerprint = generate(smiles.strip())
        except (TypeError, ValueError) as exc:
            raise InvalidSmilesError(str(exc)) from exc
        except Exception as exc:
            raise StructureSimilarityIndexUnavailableError(
                "core polymer query fingerprint could not be generated"
            ) from exc

        try:
            return self._search_with_fingerprint(
                connection,
                query_fingerprint,
                similarity_threshold=similarity_threshold,
                top_k=top_k,
            )
        except StructureSimilarityIndexUnavailableError:
            raise
        except Exception as exc:
            raise StructureSimilarityIndexUnavailableError(
                "core polymer similarity index query failed"
            ) from exc

    def _search_with_fingerprint(
        self,
        connection: Any,
        query_fingerprint: Any,
        *,
        similarity_threshold: float,
        top_k: int,
    ) -> list[tuple[Any, float]]:
        for _attempt in range(2):
            signature = _source_signature(connection)
            try:
                snapshot = self._snapshot_for_signature(connection, signature)
            except _StructureSimilaritySourceChanged:
                continue

            scores = DataStructs.BulkTanimotoSimilarity(
                query_fingerprint,
                snapshot.fingerprints,
            )
            selected = heapq.nsmallest(
                top_k,
                (
                    (-float(score), polymer_id)
                    for polymer_id, score in zip(
                        snapshot.polymer_ids,
                        scores,
                        strict=True,
                    )
                    if score >= similarity_threshold
                ),
            )

            if selected:
                selected_ids = [polymer_id for _negative_score, polymer_id in selected]
                rows = connection.execute(
                    """
                    SELECT polymer_id, polymer_name, smiles, canonical_smiles, rdkit_parse_ok
                    FROM core.polymers
                    WHERE polymer_id = ANY(%s)
                      AND rdkit_parse_ok = true
                    """,
                    (selected_ids,),
                ).fetchall()
                rows_by_id = {int(row["polymer_id"]): row for row in rows}
                if len(rows_by_id) != len(selected_ids):
                    self._invalidate_snapshot(snapshot.signature)
                    raise StructureSimilarityIndexUnavailableError(
                        "core polymer rows changed while querying the index"
                    )
            else:
                rows_by_id = {}

            current_signature = _source_signature(connection)
            if current_signature != snapshot.signature:
                self._invalidate_snapshot(snapshot.signature)
                continue

            return [
                (rows_by_id[polymer_id], -negative_score)
                for negative_score, polymer_id in selected
            ]

        raise StructureSimilarityIndexUnavailableError(
            "core polymer source changed while querying the index"
        )
