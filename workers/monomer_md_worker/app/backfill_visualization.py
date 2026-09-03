from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import stat
from typing import Any, Sequence

from .config import WorkerSettings, load_settings
from .formal_result_visualization import (
    VISUALIZATION_SCHEMA_VERSION,
    build_formal_visualization,
)
from .repository import _qualified_identifier
from .storage_lock import job_storage_lock

try:
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - validated by the Worker runtime.
    psycopg = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    Jsonb = None  # type: ignore[assignment]


@dataclass(frozen=True)
class BackfillOutcome:
    job_id: str
    outcome: str
    visualization_status: str | None = None
    payload_bytes: int = 0
    would_update: bool = False
    next_cursor: str | None = None
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "outcome": self.outcome,
            "visualization_status": self.visualization_status,
            "payload_bytes": self.payload_bytes,
            "would_update": self.would_update,
            **({"warnings": list(self.warnings)} if self.warnings else {}),
            **({"next_cursor": self.next_cursor} if self.next_cursor else {}),
        }


class VisualizationBackfillRepository:
    def __init__(self, settings: WorkerSettings) -> None:
        if not settings.app_postgres_dsn:
            raise RuntimeError("APP_POSTGRES_DSN is required for visualization backfill")
        if psycopg is None or sql is None or dict_row is None or Jsonb is None:
            raise RuntimeError("psycopg is required for visualization backfill")
        self._settings = settings

    def list_candidates(
        self,
        *,
        limit: int,
        after_job_id: str | None,
    ) -> list[dict[str, Any]]:
        settings = self._settings
        cursor_sql = sql.SQL("")
        params: list[Any] = [str(VISUALIZATION_SCHEMA_VERSION)]
        if after_job_id:
            cursor_sql = sql.SQL("AND {} > %s").format(
                sql.Identifier(settings.job_id_column)
            )
            params.append(after_job_id)
        query = sql.SQL(
            """
            SELECT {job_id} AS job_id,
                   {protocol} AS protocol,
                   {artifact_root} AS artifact_root,
                   {status} AS status,
                   {run_mode} AS run_mode,
                   {result_data} AS result_data,
                   artifact_deleted_at,
                   {finished_at} AS finished_at,
                   {updated_at} AS updated_at
            FROM {table}
            WHERE {status} = 'completed'
              AND {run_mode} = 'formal'
              AND artifact_deleted_at IS NULL
              AND COALESCE({result_data}, '{{}}'::jsonb)
                    #>> '{{visualization,schema_version}}' IS DISTINCT FROM %s
              {cursor_sql}
            ORDER BY {job_id}
            LIMIT %s
            """
        ).format(
            job_id=sql.Identifier(settings.job_id_column),
            protocol=sql.Identifier(settings.protocol_column),
            artifact_root=sql.Identifier(settings.output_dir_column),
            status=sql.Identifier(settings.status_column),
            run_mode=sql.Identifier(settings.run_mode_column),
            result_data=sql.Identifier(settings.result_column),
            finished_at=sql.Identifier(settings.finished_at_column),
            updated_at=sql.Identifier(settings.updated_at_column),
            table=_qualified_identifier(settings.job_table),
            cursor_sql=cursor_sql,
        )
        params.append(limit)
        with psycopg.connect(
            settings.app_postgres_dsn,
            row_factory=dict_row,
        ) as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        settings = self._settings
        query = sql.SQL(
            """
            SELECT {job_id} AS job_id,
                   {protocol} AS protocol,
                   {artifact_root} AS artifact_root,
                   {status} AS status,
                   {run_mode} AS run_mode,
                   {result_data} AS result_data,
                   artifact_deleted_at,
                   {finished_at} AS finished_at,
                   {updated_at} AS updated_at
            FROM {table}
            WHERE {job_id} = %s
            """
        ).format(
            job_id=sql.Identifier(settings.job_id_column),
            protocol=sql.Identifier(settings.protocol_column),
            artifact_root=sql.Identifier(settings.output_dir_column),
            status=sql.Identifier(settings.status_column),
            run_mode=sql.Identifier(settings.run_mode_column),
            result_data=sql.Identifier(settings.result_column),
            finished_at=sql.Identifier(settings.finished_at_column),
            updated_at=sql.Identifier(settings.updated_at_column),
            table=_qualified_identifier(settings.job_table),
        )
        with psycopg.connect(
            settings.app_postgres_dsn,
            row_factory=dict_row,
        ) as connection:
            row = connection.execute(query, (job_id,)).fetchone()
        return dict(row) if row is not None else None

    def update_visualization_cas(
        self,
        snapshot: dict[str, Any],
        merged_result: dict[str, Any],
    ) -> bool:
        settings = self._settings
        query = sql.SQL(
            """
            UPDATE {table}
            SET {result_data} = %s,
                {updated_at} = now()
            WHERE {job_id} = %s
              AND {status} = 'completed'
              AND {run_mode} = 'formal'
              AND {protocol} IS NOT DISTINCT FROM %s
              AND {artifact_root} IS NOT DISTINCT FROM %s
              AND {finished_at} IS NOT DISTINCT FROM %s
              AND {updated_at} IS NOT DISTINCT FROM %s
              AND artifact_deleted_at IS NULL
              AND {result_data} IS NOT DISTINCT FROM %s
            RETURNING {job_id}
            """
        ).format(
            table=_qualified_identifier(settings.job_table),
            result_data=sql.Identifier(settings.result_column),
            updated_at=sql.Identifier(settings.updated_at_column),
            job_id=sql.Identifier(settings.job_id_column),
            status=sql.Identifier(settings.status_column),
            run_mode=sql.Identifier(settings.run_mode_column),
            protocol=sql.Identifier(settings.protocol_column),
            artifact_root=sql.Identifier(settings.output_dir_column),
            finished_at=sql.Identifier(settings.finished_at_column),
        )
        expected_result = snapshot.get("result_data")
        expected_parameter = Jsonb(expected_result) if expected_result is not None else None
        params = (
            Jsonb(merged_result),
            snapshot["job_id"],
            snapshot.get("protocol"),
            snapshot.get("artifact_root"),
            snapshot.get("finished_at"),
            snapshot.get("updated_at"),
            expected_parameter,
        )
        with psycopg.connect(settings.app_postgres_dsn) as connection:
            changed = connection.execute(query, params).fetchone() is not None
        return changed


def backfill_candidate(
    candidate: dict[str, Any],
    *,
    repository: VisualizationBackfillRepository,
    allowed_roots: Sequence[Path],
    apply: bool,
) -> BackfillOutcome:
    job_id = str(candidate.get("job_id") or "")
    if not job_id:
        return BackfillOutcome("unknown", "invalid_job")
    initial_eligibility = _eligibility_outcome(candidate)
    if initial_eligibility is not None:
        return BackfillOutcome(job_id, initial_eligibility)
    root, root_outcome = _resolve_artifact_root(candidate, allowed_roots)
    if root is None:
        return BackfillOutcome(job_id, root_outcome)

    try:
        with job_storage_lock(root.parent, job_id):
            return _backfill_locked_candidate(
                job_id,
                root,
                repository=repository,
                allowed_roots=allowed_roots,
                apply=apply,
            )
    except Exception:
        return BackfillOutcome(job_id, "processing_error")


def _backfill_locked_candidate(
    job_id: str,
    root: Path,
    *,
    repository: VisualizationBackfillRepository,
    allowed_roots: Sequence[Path],
    apply: bool,
) -> BackfillOutcome:
    current = repository.get_job(job_id)
    if current is None:
        return BackfillOutcome(job_id, "missing_job")
    eligibility = _eligibility_outcome(current)
    if eligibility is not None:
        return BackfillOutcome(job_id, eligibility)
    current_root, current_root_outcome = _resolve_artifact_root(current, allowed_roots)
    if current_root is None:
        return BackfillOutcome(job_id, current_root_outcome)
    if current_root != root:
        return BackfillOutcome(job_id, "snapshot_changed")
    protocol = str(current.get("protocol") or "")
    try:
        visualization = build_formal_visualization(protocol, current_root)
    except Exception:
        return BackfillOutcome(job_id, "extract_error")
    visualization_status = str(visualization.get("status") or "unavailable")
    visualization_warnings = _visualization_warnings(visualization)
    result_data = current.get("result_data")
    if not isinstance(result_data, dict):
        result_data = {}
    merged_result = {**result_data, "visualization": visualization}
    payload_bytes = len(
        json.dumps(merged_result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if visualization_status == "unavailable":
        return BackfillOutcome(
            job_id,
            "unavailable",
            visualization_status,
            payload_bytes,
            False,
            warnings=visualization_warnings,
        )
    if not apply:
        return BackfillOutcome(
            job_id,
            "partial" if visualization_status == "partial" else "extractable",
            visualization_status,
            payload_bytes,
            True,
            warnings=visualization_warnings,
        )
    changed = repository.update_visualization_cas(current, merged_result)
    return BackfillOutcome(
        job_id,
        "updated" if changed else "cas_conflict",
        visualization_status,
        payload_bytes,
        changed,
        warnings=visualization_warnings,
    )


def _eligibility_outcome(snapshot: dict[str, Any]) -> str | None:
    if snapshot.get("artifact_deleted_at") is not None:
        return "skip_deleted"
    if snapshot.get("status") != "completed" or snapshot.get("run_mode") != "formal":
        return "skip_ineligible"
    if _visualization_version(snapshot.get("result_data")) >= VISUALIZATION_SCHEMA_VERSION:
        return f"already_v{VISUALIZATION_SCHEMA_VERSION}"
    return None


def _visualization_version(result_data: Any) -> int:
    if not isinstance(result_data, dict):
        return 0
    visualization = result_data.get("visualization")
    if not isinstance(visualization, dict):
        return 0
    version = visualization.get("schema_version")
    return version if isinstance(version, int) and not isinstance(version, bool) else 0


def _visualization_warnings(visualization: dict[str, Any]) -> tuple[str, ...]:
    warnings: list[str] = []
    global_warnings = visualization.get("warnings")
    if isinstance(global_warnings, list):
        warnings.extend(item for item in global_warnings if isinstance(item, str))
    return tuple(dict.fromkeys(warnings))


def _is_missing_outcome(outcome: BackfillOutcome) -> bool:
    if outcome.outcome in {"missing_job", "missing_root"}:
        return True
    if outcome.outcome == "unavailable" and not outcome.warnings:
        return True
    return any(
        marker in warning
        for warning in outcome.warnings
        for marker in ("MISSING", "UNAVAILABLE", "EMPTY")
    )


def _is_corrupt_outcome(outcome: BackfillOutcome) -> bool:
    if outcome.outcome in {"extract_error", "processing_error"}:
        return True
    return any(
        marker in warning
        for warning in outcome.warnings
        for marker in ("UNSAFE", "UNREADABLE", "MISMATCH", "NONFINITE", "EXTRACTION_FAILED")
    )


def _resolve_artifact_root(
    snapshot: dict[str, Any],
    allowed_roots: Sequence[Path],
) -> tuple[Path | None, str]:
    job_id = str(snapshot.get("job_id") or "")
    raw_root = snapshot.get("artifact_root")
    if not isinstance(raw_root, str) or not raw_root:
        return None, "missing_root"
    candidate = Path(raw_root)
    if not candidate.is_absolute() or candidate.name != job_id:
        return None, "unsafe_root"
    try:
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return None, "unsafe_root"
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None, "missing_root"
    approved: list[Path] = []
    for root in allowed_roots:
        try:
            root_metadata = root.lstat()
            if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
                continue
            approved_root = root.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        approved.append(approved_root)
    if resolved.parent not in approved:
        return None, "unsafe_root"
    return resolved, "ok"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize versioned formal monomer-MD visualization data.",
    )
    parser.add_argument("--job-id")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--cursor", help="Exclusive job-id keyset cursor.")
    parser.add_argument(
        "--allowed-root",
        action="append",
        type=Path,
        default=[],
        help="Additional approved legacy artifact root; may be repeated.",
    )
    parser.add_argument("--apply", action="store_true", help="Persist CAS updates; default is dry-run.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.batch_size < 1 or args.batch_size > 10_000:
        raise SystemExit("--batch-size must be between 1 and 10000")
    settings = load_settings()
    repository = VisualizationBackfillRepository(settings)
    allowed_roots = [settings.job_root, *args.allowed_root]
    if args.job_id:
        selected = repository.get_job(args.job_id)
        candidates = [selected] if selected is not None else []
    else:
        candidates = repository.list_candidates(
            limit=args.batch_size,
            after_job_id=args.cursor,
        )

    counts: Counter[str] = Counter()
    outcomes: list[BackfillOutcome] = []
    for candidate in candidates:
        if candidate is None:
            continue
        outcome = backfill_candidate(
            candidate,
            repository=repository,
            allowed_roots=allowed_roots,
            apply=args.apply,
        )
        outcomes.append(outcome)
        counts[outcome.outcome] += 1
        print(json.dumps(outcome.as_dict(), ensure_ascii=False, sort_keys=True))

    if args.job_id and not candidates:
        outcome = BackfillOutcome(args.job_id, "missing_job")
        outcomes.append(outcome)
        counts[outcome.outcome] += 1
        print(json.dumps(outcome.as_dict(), ensure_ascii=False, sort_keys=True))
    next_cursor = outcomes[-1].job_id if outcomes else args.cursor
    summary = {
        "mode": "apply" if args.apply else "dry-run",
        "scanned": len(outcomes),
        "counts": dict(sorted(counts.items())),
        f"already_v{VISUALIZATION_SCHEMA_VERSION}": counts.get(
            f"already_v{VISUALIZATION_SCHEMA_VERSION}",
            0,
        ),
        "extractable": sum(
            1 for item in outcomes if item.visualization_status == "complete"
        ),
        "partial": sum(
            1 for item in outcomes if item.visualization_status == "partial"
        ),
        "missing": sum(1 for item in outcomes if _is_missing_outcome(item)),
        "corrupt": sum(1 for item in outcomes if _is_corrupt_outcome(item)),
        "would_update": sum(1 for item in outcomes if item.would_update),
        "payload_bytes": sum(item.payload_bytes for item in outcomes),
        "next_cursor": next_cursor,
        "generated_at": datetime.now().astimezone().isoformat(),
    }
    print(json.dumps({"summary": summary}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
