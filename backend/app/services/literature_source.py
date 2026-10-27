"""Read-only source extraction and conservative object-storage manifests."""
from __future__ import annotations

import mimetypes
import json
import os
import re
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row

from app.services.literature_normalizer import MAX_JSON_BYTES, json_sha256, normalize_doi

DOI_SQL = r"lower(regexp_replace(btrim(src_source_ref), '^(https?://(dx\.)?doi\.org/|doi:[[:space:]]*)', '', 'i'))"
FILE_DOI_SQL = DOI_SQL.replace("src_source_ref", "source_ref")


@dataclass
class SourceDataset:
    papers: list[dict]
    extracts: list[dict]
    assets_by_doi: dict[str, list[dict]]
    summary: dict
    extraction_candidates: list[dict] = field(default_factory=list)


def _object_path(bucket: Any, value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, None
    path = value.strip()
    if path.startswith("s3://"):
        parsed = urlsplit(path)
        return parsed.netloc, parsed.path.lstrip("/") or None
    if "://" in path:
        # Do not put arbitrary HTTP URLs or signed credentials in a manifest.
        return None, None
    clean_bucket = str(bucket).strip() if bucket else None
    if clean_bucket and path.startswith(clean_bucket + "/"):
        path = path[len(clean_bucket) + 1:]
    return clean_bucket, path.lstrip("/") or None


def _asset(doi: str, *, role: str, bucket: Any, key: Any, size: Any = None, filename: Any = None, checksum: Any = None, version: Any = None) -> dict | None:
    bucket, key = _object_path(bucket, key)
    if not bucket or not key:
        return None
    name = str(filename) if filename else key.rsplit("/", 1)[-1]
    mime = "application/json" if role == "full_text" else "application/pdf" if role == "pdf" else mimetypes.guess_type(name)[0] or "application/octet-stream"
    return {"asset_uid": "literature-asset:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doi}:{role}:{bucket}/{key}")), "role": role, "status": "unverified", "bucket": bucket, "object_key": key, "filename": name, "media_type": mime, "size_bytes": int(size) if size is not None else None, "etag": None, "checksum": str(checksum) if checksum else None, "source_ref": doi, "version": str(version) if version is not None else None, "association": "paper", "exact_input_verified": False}


def build_asset_manifests(preprocess_rows: list[dict], file_rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in preprocess_rows:
        doi = normalize_doi(row.get("src_source_ref"))
        if not doi:
            continue
        if (row.get("result_blocks_clean_count") or 0) > 0:
            asset = _asset(doi, role="full_text", bucket=row.get("result_bucket"), key=row.get("result_blocks_clean_object_key"), size=row.get("result_blocks_clean_size"), version=row.get("preprocess_id"))
            if asset:
                groups[doi][asset["asset_uid"]] = asset
        asset = _asset(doi, role="pdf", bucket=row.get("src_bucket"), key=row.get("src_storage_path"), size=row.get("src_file_size"), filename=row.get("src_file_name"), version=row.get("convert_id"))
        if asset:
            groups[doi][asset["asset_uid"]] = asset
    for row in file_rows:
        doi = normalize_doi(row.get("source_ref"))
        if not doi:
            continue
        si = str(row.get("is_si", "")).strip().lower()
        if si == "true":
            role = "attachment"
        elif si == "false" and str(row.get("file_ext", "")).lower() == "pdf":
            role = "pdf"
        else:
            continue
        asset = _asset(doi, role=role, bucket=row.get("storage_bucket"), key=row.get("storage_path"), size=row.get("file_size"), filename=row.get("file_name"), checksum=row.get("checksum"), version=row.get("version"))
        if asset:
            if role == "attachment" and row.get("file_ext"):
                asset["media_type"] = mimetypes.guess_type("file." + str(row["file_ext"]).lower())[0] or asset["media_type"]
            previous = groups[doi].get(asset["asset_uid"])
            if previous:
                # A pipeline artifact carries the more precise version label;
                # retain its selection while filling checksum metadata only.
                if previous.get("checksum") is None:
                    previous["checksum"] = asset.get("checksum")
                if previous.get("size_bytes") is None:
                    previous["size_bytes"] = asset.get("size_bytes")
            else:
                groups[doi][asset["asset_uid"]] = asset
    result = {}
    for doi, mapping in groups.items():
        assets = sorted(mapping.values(), key=lambda a: (a["role"], a["bucket"], a["object_key"]))
        for role in ("pdf", "full_text"):
            members = [a for a in assets if a["role"] == role]
            if len(members) > 1:
                for asset in members:
                    asset["status"] = "ambiguous"
                    asset["error_code"] = "MULTIPLE_PAPER_ASSETS"
        result[doi] = assets
    return result


def verify_asset_manifests(manifests: dict[str, list[dict]], *, workers: int = 8) -> dict[str, list[dict]]:
    from app.services.literature_assets import probe_asset

    def probe(asset: dict) -> dict:
        updated = probe_asset(dict(asset))
        if asset.get("status") == "ambiguous":
            updated["status"] = "ambiguous"
            updated["error_code"] = "MULTIPLE_PAPER_ASSETS"
        # Object existence is not evidence of a DWD extraction input relation.
        updated["exact_input_verified"] = False
        return updated

    locations = [(doi, asset) for doi in sorted(manifests) for asset in manifests[doi]]
    result = {doi: [] for doi in manifests}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as executor:
        for (doi, _), updated in zip(locations, executor.map(probe, [a for _, a in locations])):
            result[doi].append(updated)
    return result


def verify_extraction_inputs(snapshots: list[dict], candidates: list[dict], manifests: dict[str, list[dict]], *, workers: int = 8) -> tuple[dict[str, list[dict]], list[dict]]:
    """Bind only a unique equal-result hash through exact registered input keys.

    Result JSON reads are capped at 2 MiB and use the normal asset client's
    frozen ETag/If-Match path. Failure never invalidates an extracted document.
    """
    from app.services.literature_assets import _get, probe_asset

    by_doi: dict[str, list[dict]] = defaultdict(list)
    for candidate in candidates:
        doi = normalize_doi(candidate.get("src_source_ref"))
        if doi:
            by_doi[doi].append(candidate)

    def read_hash(candidate: dict) -> tuple[str | None, dict | None, str | None]:
        asset = _asset(candidate["normalized_doi"], role="extraction_result", bucket=candidate.get("result_bucket"), key=candidate.get("result_final_object_key"), size=candidate.get("result_final_size"))
        if asset is None:
            return None, None, "INVALID_RESULT_LOCATION"
        response = None
        try:
            asset = probe_asset(asset)
            if asset.get("status") != "available" or not asset.get("etag"):
                return None, asset, asset.get("error_code", "RESULT_UNAVAILABLE")
            if not isinstance(asset.get("size_bytes"), int) or asset["size_bytes"] > MAX_JSON_BYTES:
                return None, asset, "RESULT_JSON_TOO_LARGE"
            response = _get(asset, length=MAX_JSON_BYTES + 1)
            body = response.read(MAX_JSON_BYTES + 1)
            if len(body) > MAX_JSON_BYTES:
                return None, asset, "RESULT_JSON_TOO_LARGE"
            return json_sha256(json.loads(body.decode("utf-8-sig"))), asset, None
        except Exception:
            return None, asset, "RESULT_VERIFICATION_FAILED"
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
                try:
                    response.release_conn()
                except Exception:
                    pass

    def verify(snapshot: dict) -> tuple[str, list[dict], dict]:
        doi = snapshot["metadata"]["doi"]
        assets = [dict(a) for a in manifests.get(doi, [])]
        report = {"doi": doi, "extract_id": snapshot["source_extract_id"], "exact_input_verified": False}
        rows = by_doi.get(doi, [])
        if not rows:
            report["reason"] = "NO_REGISTERED_EXTRACTION_RESULT"
            return doi, assets, report
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for row in rows:
            key = (row.get("result_bucket"), row.get("result_final_object_key"))
            groups[key].append(row)
        matches, errors = [], []
        for related in groups.values():
            candidate = {**related[0], "normalized_doi": doi}
            fingerprint, result_asset, error = read_hash(candidate)
            if error:
                errors.append({"pdee_extract_id": str(candidate.get("pdee_extract_id")), "reason": error})
            if fingerprint == snapshot["content_sha256"]:
                matches.append((related, result_asset))
        report["checked_result_objects"] = len(groups)
        report["matching_result_objects"] = len(matches)
        if errors:
            report["verification_errors"] = errors
        if len(matches) != 1:
            report["reason"] = "NO_MATCHING_RESULT_HASH" if not matches else "AMBIGUOUS_MATCHING_RESULTS"
            return doi, assets, report
        if errors:
            report["reason"] = "INCOMPLETE_RESULT_VERIFICATION"
            return doi, assets, report
        related, result_asset = matches[0]
        complete = [r for r in related if r.get("preprocess_id") is not None and r.get("convert_id") is not None and (r.get("result_blocks_clean_count") or 0) > 0 and normalize_doi(r.get("preprocess_source_ref")) == doi and normalize_doi(r.get("convert_source_ref")) == doi]
        signatures = {(r.get("input_bucket"), r.get("input_blocks_clean_object_key"), r.get("convert_md_object_key"), r.get("src_bucket"), r.get("src_storage_path")) for r in complete}
        if len(signatures) != 1:
            report["reason"] = "INCOMPLETE_OR_AMBIGUOUS_INPUT_CHAIN"
            return doi, assets, report
        row = complete[0]
        text = _asset(doi, role="full_text", bucket=row.get("input_bucket"), key=row.get("input_blocks_clean_object_key"), size=row.get("result_blocks_clean_size"), version=row.get("preprocess_id"))
        pdf = _asset(doi, role="pdf", bucket=row.get("src_bucket"), key=row.get("src_storage_path"), size=row.get("src_file_size"), filename=row.get("src_file_name"), version=row.get("convert_id"))
        if text is None or pdf is None:
            report["reason"] = "INVALID_REGISTERED_INPUT_LOCATION"
            return doi, assets, report
        try:
            text = probe_asset(text)
            if text.get("status") != "available" or not text.get("etag"):
                report["reason"] = "INPUT_BLOCKS_UNAVAILABLE"
                return doi, assets, report
            if row.get("result_blocks_clean_size") is not None and text.get("size_bytes") != row["result_blocks_clean_size"]:
                report["reason"] = "INPUT_BLOCKS_SIZE_CHANGED"
                return doi, assets, report
            proof = {"method": "unique_result_sha256_and_registered_key_chain", "source_extract_id": snapshot["source_extract_id"], "pdee_extract_id": str(row["pdee_extract_id"]), "pdee_result_kind": row.get("result_kind"), "result_bucket": result_asset["bucket"], "result_object_key": result_asset["object_key"], "result_etag": result_asset["etag"], "result_object_version_id": result_asset.get("object_version_id"), "result_content_sha256": snapshot["content_sha256"], "preprocess_id": str(row["preprocess_id"]), "convert_id": str(row["convert_id"])}
            text.update(association="extraction_input", exact_input_verified=True, input_proof=proof)
            # Only the selected exact input may be the default available text.
            for asset in assets:
                if asset["role"] == "full_text" and asset["asset_uid"] != text["asset_uid"]:
                    asset.update(status="ambiguous", error_code="NON_SELECTED_EXTRACTION_INPUT")
                if asset["role"] == "pdf" and asset["asset_uid"] != pdf["asset_uid"]:
                    asset.update(status="ambiguous", error_code="NON_SELECTED_EXTRACTION_INPUT")
            assets = [a for a in assets if a["asset_uid"] not in {text["asset_uid"], pdf["asset_uid"]}]
            assets.append(text)
            pdf = probe_asset(pdf)
            pdf.update(association="extraction_input", exact_input_verified=False, input_proof=proof)
            assets.append(pdf)
            assets.sort(key=lambda a: (a["role"], a["bucket"], a["object_key"]))
            report.update(exact_input_verified=True, reason="VERIFIED", pdee_extract_id=str(row["pdee_extract_id"]), fulltext_asset_uid=text["asset_uid"])
            return doi, assets, report
        except Exception:
            report["reason"] = "INPUT_CHAIN_VERIFICATION_FAILED"
            return doi, assets, report

    result = dict(manifests)
    reports = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as executor:
        for doi, assets, report in executor.map(verify, snapshots):
            result[doi] = assets
            reports.append(report)
    return result, reports


def read_source(dsn: str, *, limit: int | None = None, verify_assets: bool = False, asset_workers: int = 8, verify_extraction_input: bool = False) -> SourceDataset:
    """All source SELECTs share one REPEATABLE READ, READ ONLY snapshot.

    This deliberately does not use the application's service-context factory.
    Explicit connection options enforce read-only mode before the first query.
    """
    raw_timeout = os.environ.get("LITERATURE_SOURCE_STATEMENT_TIMEOUT_MS", "45000")
    if not re.fullmatch(r"[0-9]{1,6}", raw_timeout) or not 1000 <= int(raw_timeout) <= 300000:
        raise ValueError("LITERATURE_SOURCE_STATEMENT_TIMEOUT_MS_INVALID")
    statement_timeout_ms = int(raw_timeout)
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=10, options=f"-c default_transaction_read_only=on -c statement_timeout={statement_timeout_ms} -c lock_timeout=2000", application_name="nexpoly_literature_readonly_import") as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        state = connection.execute("SELECT current_setting('transaction_read_only') read_only, current_setting('TimeZone') source_timezone, transaction_timestamp() captured_at, txid_current_snapshot()::text snapshot").fetchone()
        if state["read_only"] != "on":
            raise RuntimeError("SOURCE_READ_ONLY_REQUIRED")
        paper_sql = "SELECT id::text id,doi,title,journal,year,authors_json,keyword,abstract FROM public.dwd_paper ORDER BY public.dwd_paper.id"
        parameters = ()
        if limit is not None:
            paper_sql += " LIMIT %s"
            parameters = (limit,)
        papers = connection.execute(paper_sql, parameters).fetchall()
        paper_ids = [int(p["id"]) for p in papers]
        extracts = connection.execute("""SELECT id::text id,paper_id::text paper_id,status,model_version,
            created_at AT TIME ZONE current_setting('TimeZone') created_at,raw_json
            FROM public.dwd_paper_extract_record WHERE paper_id = ANY(%s::bigint[]) ORDER BY id""", (paper_ids,)).fetchall() if paper_ids else []
        source_counts = connection.execute("""SELECT count(*) extract_rows_total,
            count(*) FILTER(WHERE paper_id IS NULL) extract_rows_without_paper_id,
            coalesce(jsonb_agg(id::text ORDER BY id) FILTER(WHERE paper_id IS NULL),'[]'::jsonb) excluded_unlinked_extract_ids
            FROM public.dwd_paper_extract_record""").fetchone()
        dois = sorted({d for p in papers if (d := normalize_doi(p.get("doi")))})
        preprocess = connection.execute(f"""SELECT pr.id::text preprocess_id,pr.src_source_ref,pr.result_bucket,
            pr.result_blocks_clean_object_key,pr.result_blocks_clean_size,pr.result_blocks_clean_count,
            cv.id::text convert_id,cv.src_bucket,cv.src_storage_path,cv.src_file_size,cv.src_file_name
            FROM public.ods_pdee_file_record_preprocess pr
            LEFT JOIN public.ods_pdee_file_record_convert cv
              ON cv.result_md_object_key=pr.convert_md_object_key AND cv.status='SUCCESS'
            WHERE pr.is_latest IS TRUE AND pr.status='SUCCESS'
              AND {DOI_SQL.replace('src_source_ref', 'pr.src_source_ref')}=ANY(%s::text[])
            ORDER BY pr.id,cv.id""", (dois,)).fetchall() if dois else []
        files = connection.execute(f"""SELECT id::text id,file_name,file_ext,storage_bucket,storage_path,
            file_size,checksum,version,source_ref,is_si
            FROM public.ods_file_object WHERE storage_bucket='polymer-literature'
              AND status='ACTIVE' AND is_latest=1 AND {FILE_DOI_SQL}=ANY(%s::text[])
            ORDER BY id,file_name""", (dois,)).fetchall() if dois else []
        extraction_candidates = []
        if verify_extraction_input and dois:
            extraction_candidates = connection.execute(f"""WITH er AS (
                SELECT id::text pdee_extract_id,src_source_ref,input_bucket,input_blocks_clean_object_key,
                  result_bucket,result_final_object_key,result_final_size,'single' result_kind
                FROM public.ods_pdee_file_record_extract
                WHERE status='SUCCESS' AND {DOI_SQL}=ANY(%s::text[])
                UNION ALL
                SELECT id::text,src_source_ref,input_bucket,input_blocks_clean_object_key,
                  result_bucket,result_final_object_key,result_final_size,'multi'
                FROM public.ods_pdee_file_record_extract_multi
                WHERE status='SUCCESS' AND {DOI_SQL}=ANY(%s::text[])
              ) SELECT er.*,pr.id::text preprocess_id,pr.src_source_ref preprocess_source_ref,
                pr.result_blocks_clean_size,pr.result_blocks_clean_count,pr.convert_md_object_key,
                cv.id::text convert_id,cv.src_source_ref convert_source_ref,cv.src_bucket,
                cv.src_storage_path,cv.src_file_size,cv.src_file_name
              FROM er LEFT JOIN public.ods_pdee_file_record_preprocess pr
                ON pr.result_bucket=er.input_bucket AND pr.result_blocks_clean_object_key=er.input_blocks_clean_object_key AND pr.status='SUCCESS'
              LEFT JOIN public.ods_pdee_file_record_convert cv
                ON cv.result_bucket=pr.result_bucket AND cv.result_md_object_key=pr.convert_md_object_key AND cv.status='SUCCESS'
              ORDER BY er.pdee_extract_id,pr.id,cv.id""", (dois, dois)).fetchall()
        summary = {"candidate_paper_rows": len(papers), "linked_extract_rows": len(extracts), **source_counts, "source_timezone": state["source_timezone"], "source_transaction_snapshot": state["snapshot"], "source_captured_at": state["captured_at"].isoformat(), "source_read_only": True, "source_isolation": "repeatable read", "source_statement_timeout_ms": statement_timeout_ms, "partial": limit is not None, "limit": limit, "pdee_preprocess_link_rows": len(preprocess), "file_metadata_rows": len(files)}
    manifests = build_asset_manifests(preprocess, files)
    if verify_assets:
        manifests = verify_asset_manifests(manifests, workers=asset_workers)
    summary["asset_verification_requested"] = verify_assets
    summary["extraction_input_verification_requested"] = verify_extraction_input
    summary["extraction_result_chain_rows"] = len(extraction_candidates)
    return SourceDataset(papers, extracts, manifests, summary, extraction_candidates)
