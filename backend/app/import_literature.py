"""Independent literature import CLI (never writes to the source database).

Examples, from backend/:
  python -m app.import_literature dry-run --limit 20
  python -m app.import_literature prepare --verify-assets
  python -m app.import_literature publish GENERATION_UUID
  python -m app.import_literature rollback PREVIOUS_GENERATION_UUID

Credentials are read only from LITERATURE_SOURCE_DSN and
LITERATURE_IMPORT_DSN, never from a web request or service-context factory.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.services.literature_normalizer import NORMALIZER_VERSION, canonical_json, select_snapshots
from app.services.literature_source import read_source, verify_extraction_inputs


class ImportStateError(ValueError):
    pass


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ImportStateError(f"{name}_REQUIRED")
    return value


def build_import(source_dsn: str, *, limit: int | None = None, verify_assets: bool = False, asset_workers: int = 8, verify_extraction_input: bool = False) -> tuple[list[dict], dict, dict]:
    if verify_extraction_input and not verify_assets:
        raise ImportStateError("VERIFY_EXTRACTION_INPUT_REQUIRES_VERIFY_ASSETS")
    source = read_source(source_dsn, limit=limit, verify_assets=verify_assets, asset_workers=asset_workers, verify_extraction_input=verify_extraction_input)
    snapshots, report = select_snapshots(source.papers, source.extracts, source.assets_by_doi)
    if verify_extraction_input:
        manifests, verification_report = verify_extraction_inputs(snapshots, source.extraction_candidates, source.assets_by_doi, workers=asset_workers)
        snapshots, report = select_snapshots(source.papers, source.extracts, manifests)
        report["extraction_input_verification"] = verification_report
        report["exact_input_verified_documents"] = sum(r["exact_input_verified"] for r in verification_report)
    report["excluded_unlinked_source_extract_rows"] = source.summary.get("extract_rows_without_paper_id", 0)
    report["excluded_unlinked_source_extract_scope"] = "All source public.dwd_paper_extract_record rows with NULL paper_id; excluded before loading candidate raw JSON. unlinked_extract_rows describes only records supplied to the candidate selector."
    report["excluded_unlinked_source_extract_ids"] = source.summary.get("excluded_unlinked_extract_ids", [])
    asset_counts = Counter(asset.get("status", "unverified") for snapshot in snapshots for asset in snapshot["assets"])
    report["asset_status_counts"] = dict(asset_counts)
    report["capability_document_counts"] = {key: sum(bool(s["capabilities"].get(key)) for s in snapshots) for key in ("has_properties", "has_processes", "has_original", "has_fulltext", "has_pdf", "has_evidence", "has_attachments")}
    report["fact_counts"] = {key: sum(s["counts"][key] for s in snapshots) for key in ("samples", "properties", "processes", "materials", "characterizations")}
    return snapshots, report, source.summary


def _target_connection(dsn: str):
    return psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10, application_name="nexpoly_literature_import")


def _insert_snapshot(connection, snapshot: dict) -> bool:
    columns = ("snapshot_uid", "document_uid", "source_paper_id", "source_extract_id", "source_extract_created_at", "model_version", "normalizer_version", "content_sha256", "raw_json", "normalized_json", "assets", "quality_flags")
    values = tuple(Jsonb(snapshot[k]) if k in {"raw_json", "normalized_json", "assets", "quality_flags"} else snapshot[k] for k in columns)
    inserted = connection.execute("""INSERT INTO knowledge.literature_snapshots
        (snapshot_uid,document_uid,source_paper_id,source_extract_id,source_extract_created_at,
         model_version,normalizer_version,content_sha256,raw_json,normalized_json,assets,quality_flags)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (snapshot_uid) DO NOTHING RETURNING snapshot_uid""", values).fetchone()
    if inserted:
        return True
    # An existing UID may be reused, never updated. Verify the immutable body
    # rather than assuming an ON CONFLICT is an identical content snapshot.
    existing = connection.execute("SELECT " + ",".join(columns) + " FROM knowledge.literature_snapshots WHERE snapshot_uid=%s", (snapshot["snapshot_uid"],)).fetchone()
    if existing is None:
        raise ImportStateError("SNAPSHOT_INSERT_FAILED")
    for key in columns:
        expected = snapshot[key]
        actual = existing[key]
        if key == "source_extract_created_at":
            # Import callers may supply an ISO string; PostgreSQL returns datetime.
            if actual is not None and isinstance(expected, str):
                expected = expected.replace("Z", "+00:00")
                actual = actual.isoformat()
        if actual != expected:
            raise ImportStateError("IMMUTABLE_SNAPSHOT_CONFLICT")
    return False


def store_prepared_generation(connection, generation_id: str, snapshots: list[dict], report: dict, source_summary: dict) -> dict:
    """Caller owns the transaction; a generation becomes ready atomically."""
    new_snapshots = 0
    for snapshot in snapshots:
        new_snapshots += _insert_snapshot(connection, snapshot)
        metadata = snapshot["metadata"]
        connection.execute("""INSERT INTO knowledge.literature_catalog
            (generation_id,document_uid,doi,title,authors,journal,publication_year,
             keywords,keywords_text,abstract,source_paper_ids,capabilities,counts,snapshot_uid)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (
                generation_id, snapshot["document_uid"], metadata["doi"], metadata["title"], Jsonb(metadata["authors"]), metadata["journal"], metadata["year"], Jsonb(metadata["keywords"]), " ".join(metadata["keywords"]), metadata["abstract"] or "", Jsonb(snapshot["source_paper_ids"]), Jsonb(snapshot["capabilities"]), Jsonb(snapshot["counts"]), snapshot["snapshot_uid"],
            ))
    final_report = {**report, "new_snapshots": new_snapshots, "reused_snapshots": len(snapshots) - new_snapshots}
    connection.execute("""UPDATE knowledge.literature_generations SET status='ready',source_summary=%s,report=%s
        WHERE generation_id=%s AND status='building'""", (Jsonb(source_summary), Jsonb(final_report), generation_id))
    return final_report


def prepare_generation(source_dsn: str, target_dsn: str, *, limit: int | None = None, verify_assets: bool = False, asset_workers: int = 8, verify_extraction_input: bool = False) -> dict:
    if verify_extraction_input and not verify_assets:
        raise ImportStateError("VERIFY_EXTRACTION_INPUT_REQUIRES_VERIFY_ASSETS")
    generation_id = str(uuid.uuid4())
    with _target_connection(target_dsn) as connection:
        connection.execute("""INSERT INTO knowledge.literature_generations
            (generation_id,status,is_active,normalizer_version,source_summary,report)
            VALUES (%s,'building',false,%s,'{}'::jsonb,'{}'::jsonb)""", (generation_id, NORMALIZER_VERSION))
        connection.commit()
        try:
            snapshots, report, summary = build_import(source_dsn, limit=limit, verify_assets=verify_assets, asset_workers=asset_workers, verify_extraction_input=verify_extraction_input)
            if not snapshots:
                raise ImportStateError("NO_ELIGIBLE_LITERATURE")
            report = store_prepared_generation(connection, generation_id, snapshots, report, summary)
            connection.commit()
        except Exception as exc:
            connection.rollback()
            code = str(exc) if isinstance(exc, ImportStateError) else type(exc).__name__
            connection.execute("UPDATE knowledge.literature_generations SET status='failed',report=%s WHERE generation_id=%s", (Jsonb({"error_code": code}), generation_id))
            connection.commit()
            raise
    return {"generation_id": generation_id, "status": "ready", "is_active": False, "source_summary": summary, "report": report}


def activate_generation(target_dsn: str, generation_id: str, *, rollback: bool = False, allow_partial: bool = False) -> dict:
    generation_id = str(uuid.UUID(generation_id))
    with _target_connection(target_dsn) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(hashtext('nexpoly_literature_publish'))")
        generation = connection.execute("SELECT * FROM knowledge.literature_generations WHERE generation_id=%s FOR UPDATE", (generation_id,)).fetchone()
        if not generation:
            raise ImportStateError("GENERATION_NOT_FOUND")
        if rollback and generation["status"] != "published":
            raise ImportStateError("ROLLBACK_REQUIRES_PUBLISHED_GENERATION")
        if not rollback and generation["status"] not in {"ready", "published"}:
            raise ImportStateError("GENERATION_NOT_READY")
        if generation["source_summary"].get("partial") and not allow_partial:
            raise ImportStateError("PARTIAL_GENERATION_REQUIRES_ALLOW_PARTIAL")
        count = connection.execute("SELECT count(*) n FROM knowledge.literature_catalog WHERE generation_id=%s", (generation_id,)).fetchone()["n"]
        if not count or count != generation["report"].get("ready_documents"):
            raise ImportStateError("GENERATION_CATALOG_COUNT_MISMATCH")
        connection.execute("UPDATE knowledge.literature_generations SET is_active=false WHERE is_active=true")
        connection.execute("UPDATE knowledge.literature_generations SET is_active=true,status='published',published_at=COALESCE(published_at,now()) WHERE generation_id=%s", (generation_id,))
    return {"generation_id": generation_id, "status": "published", "is_active": True, "documents": count, "action": "rollback" if rollback else "publish"}


def _positive(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("dry-run", "prepare"):
        child = commands.add_parser(command)
        child.add_argument("--limit", type=_positive, help="partial candidate paper subset; publication requires --allow-partial")
        child.add_argument("--verify-assets", action="store_true", help="probe object metadata through the configured MinIO runtime")
        child.add_argument("--verify-extraction-input", action="store_true", help="requires --verify-assets; compare registered result JSON hashes and exact input keys (bounded 2 MiB reads)")
        child.add_argument("--asset-workers", type=_positive, default=8)
        child.add_argument("--report", type=Path, help="optional JSON report; excludes credentials and full extraction content")
    for command in ("publish", "rollback"):
        child = commands.add_parser(command)
        child.add_argument("generation_id", type=uuid.UUID)
        child.add_argument("--allow-partial", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if getattr(args, "verify_extraction_input", False) and not args.verify_assets:
            raise ImportStateError("VERIFY_EXTRACTION_INPUT_REQUIRES_VERIFY_ASSETS")
        if args.command == "dry-run":
            _, report, summary = build_import(_required_env("LITERATURE_SOURCE_DSN"), limit=args.limit, verify_assets=args.verify_assets, asset_workers=args.asset_workers, verify_extraction_input=args.verify_extraction_input)
            result = {"status": "dry_run", "source_summary": summary, "report": report}
        elif args.command == "prepare":
            result = prepare_generation(_required_env("LITERATURE_SOURCE_DSN"), _required_env("LITERATURE_IMPORT_DSN"), limit=args.limit, verify_assets=args.verify_assets, asset_workers=args.asset_workers, verify_extraction_input=args.verify_extraction_input)
        else:
            result = activate_generation(_required_env("LITERATURE_IMPORT_DSN"), str(args.generation_id), rollback=args.command == "rollback", allow_partial=args.allow_partial)
        output = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if getattr(args, "report", None):
            args.report.write_text(output + "\n", encoding="utf-8")
        print(output)
        return 0
    except Exception as exc:
        # Database/network errors may contain DSNs or user names. Never echo
        # their free-form messages from this credential-bearing CLI.
        code = str(exc) if isinstance(exc, ImportStateError) else type(exc).__name__
        print(json.dumps({"status": "failed", "error_code": code}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
