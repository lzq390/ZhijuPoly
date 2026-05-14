from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from app.services.fingerprint import fingerprint_to_bytes, generate
from app.services.reverse_design import (
    ReverseDesignCandidate,
    ReverseDesignSearchResult,
    _build_candidate,
)
from app.utils.exceptions import InvalidSmilesError


DEFAULT_RESULT_LIMIT = 200
DEFAULT_BATCH_SIZE = 5000
DEFAULT_MAX_SCAN_ROWS = 500_000
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ReverseDesignSearchProgress:
    scanned_rows: int
    matched_count: int
    current_tg_radius: float | None
    best_similarity_score: float | None
    exhausted: bool = False
    stopped_by_limit: bool = False
    cancelled: bool = False


POSTGRES_UP_CANDIDATE_SQL = """
SELECT
    t.id AS pi_id,
    t.tg_celsius,
    p.morgan_fp
FROM pi_tg_predictions t
JOIN pi_polymers p ON p.id = t.id
WHERE t.smiles_valid = TRUE
  AND p.morgan_fp IS NOT NULL
  AND t.tg_celsius >= %s
  AND (t.tg_celsius > %s OR (t.tg_celsius = %s AND t.id > %s))
ORDER BY t.tg_celsius ASC, t.id ASC
LIMIT %s
"""


POSTGRES_DOWN_CANDIDATE_SQL = """
SELECT
    t.id AS pi_id,
    t.tg_celsius,
    p.morgan_fp
FROM pi_tg_predictions t
JOIN pi_polymers p ON p.id = t.id
WHERE t.smiles_valid = TRUE
  AND p.morgan_fp IS NOT NULL
  AND t.tg_celsius < %s
  AND (t.tg_celsius < %s OR (t.tg_celsius = %s AND t.id > %s))
ORDER BY t.tg_celsius DESC, t.id ASC
LIMIT %s
"""


POSTGRES_DETAILS_SQL = """
SELECT
    p.id AS pi_id,
    p.mon1,
    p.mon2,
    p.polym,
    NULL AS canonical_polym,
    t.tg_celsius,
    m1.iupac_name AS mon1_iupac_name,
    m2.iupac_name AS mon2_iupac_name
FROM pi_polymers p
JOIN pi_tg_predictions t ON t.id = p.id
LEFT JOIN pi_monomer_iupac m1 ON m1.smiles = p.mon1
LEFT JOIN pi_monomer_iupac m2 ON m2.smiles = p.mon2
WHERE p.id = ANY(%s)
"""


@dataclass(slots=True)
class _ScanState:
    sql: str
    cursor_tg: float
    cursor_id: int = 0
    exhausted: bool = False
    pending: deque[Any] | None = None

    def __post_init__(self) -> None:
        if self.pending is None:
            self.pending = deque()


@dataclass(slots=True)
class _ScoredCandidate:
    pi_id: int
    tg_value: float
    tg_difference: float
    similarity_score: float


def _row_get(row: Any, key: str) -> Any:
    return row[key]


def _fingerprint_bytes(value: Any) -> bytes:
    data = bytes(value)
    if len(data) != 256:
        raise ValueError("Morgan fingerprint must be 256 bytes")
    return data


def _fingerprint_int(value: Any) -> int:
    return int.from_bytes(_fingerprint_bytes(value), byteorder="big", signed=False)


def _tanimoto_fingerprint_int(first: int, second: int) -> float:
    intersection = (first & second).bit_count()
    union = (first | second).bit_count()
    if union == 0:
        return 1.0
    return intersection / union


def tanimoto_fingerprint_bytes(first: bytes | bytearray | memoryview, second: bytes | bytearray | memoryview) -> float:
    return _tanimoto_fingerprint_int(_fingerprint_int(first), _fingerprint_int(second))


def _fetch_scan_batch(
    connection: Any,
    state: _ScanState,
    target_tg: float,
    batch_size: int,
) -> None:
    if state.exhausted:
        return

    params = (target_tg, state.cursor_tg, state.cursor_tg, state.cursor_id, batch_size)
    with connection.cursor() as cursor:
        cursor.execute(state.sql, params)
        rows = cursor.fetchmany(batch_size)

    if not rows:
        state.exhausted = True
        return

    assert state.pending is not None
    state.pending.extend(rows)
    last = rows[-1]
    state.cursor_tg = float(_row_get(last, "tg_celsius"))
    state.cursor_id = int(_row_get(last, "pi_id"))


def _peek_tg_difference(state: _ScanState, target_tg: float) -> float | None:
    assert state.pending is not None
    if not state.pending:
        return None
    return abs(float(_row_get(state.pending[0], "tg_celsius")) - target_tg)


def _next_scan_row(
    connection: Any,
    up_state: _ScanState,
    down_state: _ScanState,
    target_tg: float,
    batch_size: int,
) -> Any | None:
    for state in (up_state, down_state):
        assert state.pending is not None
        if not state.pending:
            _fetch_scan_batch(connection, state, target_tg, batch_size)

    assert up_state.pending is not None
    assert down_state.pending is not None
    up_difference = _peek_tg_difference(up_state, target_tg)
    down_difference = _peek_tg_difference(down_state, target_tg)

    if up_difference is None and down_difference is None:
        return None
    if up_difference is None:
        return down_state.pending.popleft()
    if down_difference is None:
        return up_state.pending.popleft()
    if up_difference < down_difference:
        return up_state.pending.popleft()
    if down_difference < up_difference:
        return down_state.pending.popleft()

    up_id = int(_row_get(up_state.pending[0], "pi_id"))
    down_id = int(_row_get(down_state.pending[0], "pi_id"))
    return up_state.pending.popleft() if up_id <= down_id else down_state.pending.popleft()


def _fetch_detail_rows(connection: Any, candidate_ids: list[int]) -> dict[int, Any]:
    if not candidate_ids:
        return {}

    with connection.cursor() as cursor:
        cursor.execute(POSTGRES_DETAILS_SQL, (candidate_ids,))
        rows = cursor.fetchmany(len(candidate_ids))

    return {int(_row_get(row, "pi_id")): row for row in rows}


def search_reverse_design_by_tg_postgres(
    connection: Any,
    smiles: str,
    target_tg: float,
    *,
    similarity_threshold: float = 0.7,
    result_limit: int = DEFAULT_RESULT_LIMIT,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_scan_rows: int | None = DEFAULT_MAX_SCAN_ROWS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    progress_callback: Callable[[ReverseDesignSearchProgress], None] | None = None,
    progress_interval_rows: int = 50_000,
    cancellation_check: Callable[[], bool] | None = None,
) -> ReverseDesignSearchResult:
    try:
        query_fp = generate(smiles.strip())
    except ValueError as exc:
        raise InvalidSmilesError(str(exc)) from exc
    query_fp_bytes = fingerprint_to_bytes(query_fp)
    query_fp_int = int.from_bytes(query_fp_bytes, byteorder="big", signed=False)

    target = float(target_tg)
    limit = max(1, int(result_limit))
    batch = max(1, int(batch_size))
    max_rows = max(1, int(max_scan_rows)) if max_scan_rows is not None else None
    deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
    progress_interval = max(1, int(progress_interval_rows))

    up_state = _ScanState(sql=POSTGRES_UP_CANDIDATE_SQL, cursor_tg=target)
    down_state = _ScanState(sql=POSTGRES_DOWN_CANDIDATE_SQL, cursor_tg=target)
    matches: list[_ScoredCandidate] = []
    scanned_rows = 0
    best_similarity_score: float | None = None
    current_tg_radius: float | None = None
    exhausted = False
    stopped_by_limit = False
    cancelled = False

    def emit_progress() -> None:
        if progress_callback is None:
            return
        progress_callback(
            ReverseDesignSearchProgress(
                scanned_rows=scanned_rows,
                matched_count=len(matches),
                current_tg_radius=current_tg_radius,
                best_similarity_score=best_similarity_score,
                exhausted=exhausted,
                stopped_by_limit=stopped_by_limit,
                cancelled=cancelled,
            )
        )

    while len(matches) < limit:
        if max_rows is not None and scanned_rows >= max_rows:
            stopped_by_limit = True
            break
        if timeout_seconds > 0 and time.monotonic() >= deadline:
            stopped_by_limit = True
            break
        if cancellation_check is not None and cancellation_check():
            cancelled = True
            break

        row = _next_scan_row(connection, up_state, down_state, target, batch)
        if row is None:
            exhausted = True
            break

        scanned_rows += 1
        tg_value = float(_row_get(row, "tg_celsius"))
        current_tg_radius = abs(tg_value - target)
        try:
            similarity_score = _tanimoto_fingerprint_int(query_fp_int, _fingerprint_int(_row_get(row, "morgan_fp")))
        except (KeyError, TypeError, ValueError, RuntimeError):
            continue

        if best_similarity_score is None or similarity_score > best_similarity_score:
            best_similarity_score = similarity_score
        if similarity_score < similarity_threshold:
            if scanned_rows % progress_interval == 0:
                emit_progress()
            continue

        matches.append(
            _ScoredCandidate(
                pi_id=int(_row_get(row, "pi_id")),
                tg_value=tg_value,
                tg_difference=abs(tg_value - target),
                similarity_score=similarity_score,
            )
        )
        if scanned_rows % progress_interval == 0:
            emit_progress()

    matches.sort(key=lambda item: (item.tg_difference, -item.similarity_score, item.pi_id))
    detail_rows = _fetch_detail_rows(connection, [candidate.pi_id for candidate in matches])

    results: list[ReverseDesignCandidate] = []
    for scored in matches:
        detail_row = detail_rows.get(scored.pi_id)
        if detail_row is None:
            continue
        results.append(_build_candidate(detail_row, scored.similarity_score, target))

    emit_progress()
    return ReverseDesignSearchResult(
        candidate_pool_size=len(matches),
        sampled_candidate_count=len(results),
        results=results,
        scanned_rows=scanned_rows,
        best_similarity_score=best_similarity_score,
        current_tg_radius=current_tg_radius,
        exhausted=exhausted,
        stopped_by_limit=stopped_by_limit,
        cancelled=cancelled,
    )
