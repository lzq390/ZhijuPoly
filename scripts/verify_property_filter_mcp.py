#!/usr/bin/env python3
"""Read-only acceptance of the fixed development MCP against its real dataset.

Run with the independent MCP virtual environment. Credentials are read only from
its private runtime files; no application configuration or default DSN is loaded.
The report contains timings, counts and checks, never tokens or measurement data.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from property_filter_mcp_dev import (
    BASE_URL, DATABASE, DEV_CONTAINER, DEV_NETWORK, ROLE, TOOLS, DevError,
    docker_field, read_private, verify_dev_target,
)
from app.models import PropertyFilterOption, PropertyFilterSearchRequest
from app.property_filter_mcp_contract import FilterInput, bind_condition, dataset_revision, option_unit, public_option
from app.services.property_filter import (
    get_property_filter_histogram_data, load_property_filter_context,
    search_property_filter_data,
)

GROUP_KEY = "COALESCE(NULLIF(canonical_smiles, ''), NULLIF(smiles, ''), 'record:' || filter_record_id::text)"
EXPECTED_RECORDS = 615159
EXPECTED_OPTIONS = 188


class VerificationError(RuntimeError):
    pass


def require(condition: Any, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def target_credentials() -> tuple[str, str, dict[str, Any]]:
    verify_dev_target()
    ports = docker_field(".NetworkSettings.Ports") or {}
    # Wildcard IPv4 bindings also publish on loopback; never connect to a remote address.
    candidates = [entry for entry in ports.get("5432/tcp", [])
                  if entry.get("HostIp") in {"127.0.0.1", "0.0.0.0"} and entry.get("HostPort") == "15532"]
    require(candidates, "Approved development PostgreSQL port 15532 is not published on IPv4 loopback.")
    private_dsn = read_private("server/dsn").strip()
    values = conninfo_to_dict(private_dsn)
    require(set(values) <= {"user", "password", "host", "port", "dbname"},
            "Development DSN contains unexpected connection overrides.")
    require(values.get("user") == ROLE and values.get("dbname") == DATABASE
            and values.get("host") == DEV_CONTAINER and values.get("port") == "5432",
            "MCP credential DSN does not identify the approved development reader.")
    client = dict(line.split("=", 1) for line in read_private("client.env").splitlines() if line)
    require(client.get("NEXPOLY_MCP_URL") == BASE_URL + "/mcp", "MCP URL must be the fixed loopback development endpoint.")
    token = client.get("NEXPOLY_MCP_TOKEN", "")
    require(32 <= len(token) <= 256, "Development client credential is missing or invalid.")
    dsn = make_conninfo(private_dsn, host="127.0.0.1", port=candidates[0]["HostPort"])
    return dsn, token, {"database": DATABASE, "role": ROLE, "database_container": DEV_CONTAINER,
                        "database_network": DEV_NETWORK, "database_endpoint": "127.0.0.1:15532",
                        "mcp_url": BASE_URL + "/mcp"}


def predicate(option: PropertyFilterOption, bounds: dict[str, Any] | None = None,
              *, alias: str = "") -> tuple[str, list[Any]]:
    prefix = alias + "." if alias else ""
    if option.filter_type == "standardized":
        value_column = prefix + "canonical_value"
        parts = [prefix + "property_key = %s", f"COALESCE({prefix}canonical_unit, '') = %s"]
        parameters: list[Any] = [option.property_key, option_unit(option)]
    else:
        value_column = prefix + "property_value_num"
        parts = [prefix + "property_key IS NULL", prefix + "property_name = %s",
                 f"COALESCE({prefix}property_unit_clean, '') = %s"]
        parameters = [option.property_name, option_unit(option)]
    parts.append(value_column + " IS NOT NULL")
    for bound, operator in (("min_value", ">="), ("max_value", "<=")):
        if bounds is not None and bounds.get(bound) is not None:
            parts.append(value_column + " " + operator + " %s")
            parameters.append(bounds[bound])
    return " AND ".join(parts), parameters


def full_range(option: PropertyFilterOption) -> dict[str, Any]:
    require(option.min_value is not None and option.max_value is not None,
            "Selected property has no usable numeric range.")
    return {"option_key": option.option_key, "min_value": option.min_value, "max_value": option.max_value}


def direct_search(connection: Any, options: list[PropertyFilterOption], filters: list[dict[str, Any]],
                  page_size: int) -> tuple[int, dict[tuple[str, int], int]]:
    statements, parameters = [], []
    for index, (option, bounds) in enumerate(zip(options, filters)):
        where, values = predicate(option, bounds)
        statements.append(f"SELECT %s::integer AS filter_index, {GROUP_KEY} AS group_key FROM core.polymer_property_filter_records WHERE {where}")
        parameters.extend([index, *values])
    rows = connection.execute(f"""
        WITH candidates AS MATERIALIZED ({' UNION ALL '.join(statements)}),
        matched AS MATERIALIZED (
            SELECT group_key FROM candidates GROUP BY group_key HAVING COUNT(DISTINCT filter_index) = %s
        ), page AS (SELECT group_key FROM matched ORDER BY group_key LIMIT %s),
        evidence AS (
            SELECT candidates.group_key, filter_index, COUNT(*) AS evidence_count
            FROM candidates JOIN page USING (group_key) GROUP BY candidates.group_key, filter_index
        )
        SELECT (SELECT COUNT(*) FROM matched) AS matched_count, evidence.*
        FROM (SELECT 1) marker LEFT JOIN evidence ON true
    """, [*parameters, len(filters), page_size]).fetchall()
    return int(rows[0]["matched_count"]), {(row["group_key"], row["filter_index"]): int(row["evidence_count"])
                                        for row in rows if row["group_key"] is not None}


def choose_second_option(connection: Any, first: PropertyFilterOption,
                         options: list[PropertyFilterOption]) -> PropertyFilterOption:
    where, parameters = predicate(first)
    # Distinct property options cannot be satisfied by a single measurement row.
    # Restrict seed groups to keep this discovery query inexpensive on the real dataset.
    row = connection.execute(f"""
        WITH seeds AS MATERIALIZED (
            SELECT DISTINCT {GROUP_KEY} AS group_key
            FROM core.polymer_property_filter_records WHERE {where}
            ORDER BY group_key LIMIT 500
        )
        SELECT records.property_key, records.canonical_unit, records.property_name,
               records.property_unit_clean
        FROM seeds JOIN core.polymer_property_filter_records records
          ON seeds.group_key = COALESCE(NULLIF(records.canonical_smiles, ''), NULLIF(records.smiles, ''), 'record:' || records.filter_record_id::text)
        WHERE ((records.property_key IS NOT NULL AND records.canonical_value IS NOT NULL
                AND (records.property_key <> %s OR COALESCE(records.canonical_unit, '') <> %s))
               OR (records.property_key IS NULL AND records.property_value_num IS NOT NULL))
        ORDER BY records.filter_record_id LIMIT 1
    """, [*parameters, first.property_key, option_unit(first)]).fetchone()
    require(row is not None, "No second property was found for a reproducible cross-measurement AND case.")
    for option in options:
        if row["property_key"] is not None and option.filter_type == "standardized":
            if option.property_key == row["property_key"] and option_unit(option) == (row["canonical_unit"] or ""):
                return option
        if row["property_key"] is None and option.filter_type == "raw":
            if option.property_name == row["property_name"] and option_unit(option) == (row["property_unit_clean"] or ""):
                return option
    raise VerificationError("The second matched property is absent from the validated catalog.")


def direct_histogram(connection: Any, option: PropertyFilterOption, histogram: Any) -> dict[str, Any]:
    where, values = predicate(option)
    value_column = "canonical_value" if option.filter_type == "standardized" else "property_value_num"
    lo, hi, bins = histogram.domain_min, histogram.domain_max, histogram.bin_count
    if lo == hi:
        bucket = "CASE WHEN value < %s THEN 0 WHEN value > %s THEN 2 ELSE 1 END"
        bucket_parameters = [lo, hi]
    else:
        bucket = "CASE WHEN value < %s THEN 0 WHEN value > %s THEN %s ELSE LEAST(%s, FLOOR(((value - %s) / (%s - %s)) * %s)::integer + 1) END"
        bucket_parameters = [lo, hi, bins + 1, bins, lo, hi, lo, bins]
    rows = connection.execute(f"""
        WITH measurements AS (SELECT {value_column} AS value FROM core.polymer_property_filter_records WHERE {where})
        SELECT {bucket} AS bucket, COUNT(*) AS count FROM measurements GROUP BY bucket
    """, [*values, *bucket_parameters]).fetchall()
    counts = {int(row["bucket"]): int(row["count"]) for row in rows}
    return {"counts": [counts.get(index, 0) for index in range(1, bins + 1)],
            "underflow_count": counts.get(0, 0), "overflow_count": counts.get(bins + 1, 0),
            "total_count": sum(counts.values())}


async def verify() -> dict[str, Any]:
    dsn, token, identity = target_credentials()
    calls: list[dict[str, Any]] = []
    wire_sizes: list[int] = []

    async def measure_response(response: Any) -> None:
        body = await response.aread()
        wire_sizes.append(len(body))
        require(len(body) <= 256 * 1024, "MCP response exceeded the 256 KiB contract.")

    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False,
                                  follow_redirects=False, event_hooks={"response": [measure_response]}, timeout=35) as http:
        health = await http.get(BASE_URL + "/healthz")
        require(health.status_code == 200 and health.json().get("status") == "ok", "Development MCP is not healthy.")

        @asynccontextmanager
        async def transport():
            async with streamable_http_client(BASE_URL + "/mcp", http_client=http) as (read, write):
                yield read, write

        async with Client(transport(), read_timeout_seconds=35) as client:
            tools = await client.list_tools()
            require({tool.name for tool in tools.tools} == set(TOOLS), "Tool discovery did not return exactly the three approved read-only tools.")

            async def call(name: str, arguments: dict[str, Any], case: str) -> dict[str, Any]:
                before, started = len(wire_sizes), time.perf_counter()
                result = await client.call_tool(name, arguments)
                elapsed = (time.perf_counter() - started) * 1000
                require(not result.is_error, "MCP tool returned an error during acceptance: " + case)
                payload = result.structured_content
                require(isinstance(payload, dict) and payload.get("ok"), "MCP tool returned an invalid structured result.")
                calls.append({"tool": name, "case": case, "duration_ms": round(elapsed, 2),
                              "response_bytes": sum(wire_sizes[before:]),
                              "result_count": len(payload.get("results", payload.get("options", []))),
                              "matched_material_count": payload.get("matched_material_count"),
                              "histogram_measurement_count": payload.get("histogram", {}).get("total_count")})
                return payload

            with psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=5,
                                 application_name="nexpoly_mcp_readonly_acceptance") as connection:
                with connection.transaction():
                    connection.execute("SET TRANSACTION READ ONLY")
                    connection.execute("SET LOCAL statement_timeout = '20s'")
                    connection.execute("SET LOCAL lock_timeout = '2s'")
                    connection.execute("LOCK TABLE core.polymer_property_filter_records IN ACCESS SHARE MODE")
                    actual = connection.execute("SELECT current_database() AS database, current_user AS role, current_setting('transaction_read_only') AS read_only").fetchone()
                    require(actual["database"] == DATABASE and actual["role"] == ROLE and actual["read_only"] == "on",
                            "Actual baseline connection is not the approved read-only development identity.")
                    context = load_property_filter_context(connection, allow_live_fallback=False)
                    revision = dataset_revision(context.catalog)
                    options = [PropertyFilterOption.model_validate(row) for row in context.options]
                    count = connection.execute("SELECT COUNT(*) AS count FROM core.polymer_property_filter_records").fetchone()["count"]
                    require(count == context.total_records == EXPECTED_RECORDS, "Development measurement count changed from the approved acceptance snapshot.")
                    require(len(options) == EXPECTED_OPTIONS, "Development option count changed from the approved acceptance snapshot.")
                    collected, page = [], 1
                    while True:
                        payload = await call("list_filter_properties", {"page": page, "page_size": 50,
                                              **({"dataset_revision": revision} if page > 1 else {})}, "catalog_page_" + str(page))
                        require(payload["dataset_revision"] == revision and payload["total_measurement_count"] == count,
                                "MCP catalog version/count differs from the read-only baseline.")
                        collected.extend(payload["options"])
                        if not payload["has_more"]:
                            break
                        page += 1
                        require(page <= 20, "Catalog pagination did not terminate.")
                    require(collected == [public_option(option).model_dump(mode="json") for option in options],
                            "Complete MCP catalog differs from the shared service catalog.")
                    standard = next((option for option in options if option.option_key == "std:tg:C"),
                                    next((option for option in options if option.filter_type == "standardized"), None))
                    raw = next((option for option in options if option.filter_type == "raw"), None)
                    require(standard is not None and raw is not None, "Real dataset lacks a standardized or raw acceptance option.")
                    second = choose_second_option(connection, standard, options)
                    selected_cases = [("standardized", [standard]), ("raw", [raw]), ("cross_measurement_and", [standard, second])]
                    for case, selected in selected_cases:
                        filters = [full_range(option) for option in selected]
                        payload = await call("search_polymers", {"dataset_revision": revision, "filters": filters,
                                             "page_size": 3, "measurement_limit_per_filter": 2}, case)
                        request = PropertyFilterSearchRequest(filters=[bind_condition(option, FilterInput(**item))
                                                              for option, item in zip(selected, filters)], page_size=3)
                        baseline = search_property_filter_data(connection, request, measurement_limit_per_filter=2)
                        direct_count, direct_counts = direct_search(connection, selected, filters, 3)
                        require(payload["dataset_revision"] == revision and payload["matched_material_count"] == baseline.response.matched_records == direct_count,
                                "MCP search material count differs from shared-service or independent SQL baseline.")
                        require(direct_count > 0 and len(payload["results"]) == len(baseline.groups), "Acceptance search had no matching material or wrong page size.")
                        for result, group, shared_material in zip(payload["results"], baseline.groups, baseline.response.results):
                            require(result["material_ref"] == revision + ":" + hashlib.sha256(group.group_key.encode()).hexdigest(),
                                    "MCP search grouping/order differs from the baseline.")
                            for index, evidence in enumerate(result["evidence"]):
                                expected_count = group.measurement_counts[index]
                                require(evidence["matching_measurement_count"] == expected_count == direct_counts[(group.group_key, index)],
                                        "MCP evidence count differs from complete SQL measurement counts.")
                                expected_ids = [record.filter_record_id for record in shared_material.records if record.filter_index == index]
                                actual_ids = [record["filter_record_id"] for record in evidence["measurements"]]
                                require(actual_ids == expected_ids and evidence["truncated"] == (expected_count > len(expected_ids)),
                                        "MCP measurement ranking or truncation differs from the shared baseline.")
                        calls[-1]["option_keys"] = [option.option_key for option in selected]
                        calls[-1]["baseline_agreement"] = "shared_service_and_independent_sql"
                    histogram_payload = await call("get_property_histogram", {"dataset_revision": revision,
                                                   "option_key": standard.option_key}, "standardized_histogram")
                    histogram, _ = get_property_filter_histogram_data(connection, context, standard.option_key)
                    require(histogram_payload["histogram"] == histogram.model_dump(mode="json"),
                            "MCP histogram differs from the shared-service baseline.")
                    independent_histogram = direct_histogram(connection, standard, histogram)
                    require(all(histogram_payload["histogram"][key] == value for key, value in independent_histogram.items()),
                            "MCP histogram buckets differ from independent SQL counts.")
                    calls[-1]["baseline_agreement"] = "shared_service_and_independent_sql"
                    return {"status": "passed", "verified_at_utc": datetime.now(timezone.utc).isoformat(),
                            "identity": identity, "read_only": True, "production_changed": False,
                            "dataset_revision": revision, "total_measurement_count": count,
                            "total_option_count": len(options), "catalog_pages": page,
                            "tool_names": sorted(TOOLS), "calls": calls}


def main() -> int:
    try:
        report = asyncio.run(verify())
    except (VerificationError, DevError) as error:
        print(json.dumps({"status": "failed", "reason": str(error)}, ensure_ascii=False))
        return 1
    except Exception as error:
        # Driver/transport exception reprs can contain URLs or connection details.
        print(json.dumps({"status": "failed", "reason": "Acceptance could not complete; inspect development service health and private configuration.",
                          "error_type": type(error).__name__}))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
