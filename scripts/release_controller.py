#!/usr/bin/env python3
"""Build, verify, and safely apply immutable NexPoly releases.

Mutating commands are dry-run by default.  A real production change requires
both ``--apply`` and the exact production root.  This makes the same CLI useful
in CI policy tests without giving a typo permission to touch a running stack.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, BinaryIO, Iterable
import urllib.parse
import urllib.request


PRODUCTION_ROOT = Path("/data/lzq/gith/nexpoly")
ASSET_RELEASES_ROOT = Path("/data/lzq/nexpoly-assets/releases")
MAIN_REPOSITORY_URL = "https://github.com/lzq390/ZhijuPoly.git"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
SAFE_DATASET_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SAFE_MIGRATION_RE = re.compile(r"^[0-9a-z][0-9a-z_-]{0,127}$")
DOCKER_COMPOSE_VERSION_RE = re.compile(
    r"^v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?$"
)
MINIMUM_DOCKER_COMPOSE_VERSION = (2, 24, 4)
SCHEMA_VERSION = 2
SUPPORTED_RELEASE_SCHEMA_VERSIONS = frozenset({1, 2})
MIGRATION_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")
OPERATION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
POLYTAO_CONTRACT_PREVIOUS_VERSION = "0011_monomer_md_demo_steps"
POLYTAO_SCHEMA_COMPATIBILITY_FLOOR = "0012_drop_polytao_jobs"
POLYTAO_CONTRACT_CHECKSUM = "c59b6f1efe9f926ad135379bd1a7141a7920730fa93c0e802646b1b913511728"
CONTRACT_0012_EXTERNAL_AUDIT_COMMAND = (
    "NEXPOLY_CONTRACT_0012_EXTERNAL_DATABASE_AUDIT_COMMAND"
)
CONTRACT_0012_EXTERNAL_AUDIT_USERS = {
    "nexpoly_dev": "NEXPOLY_CONTRACT_0012_DEV_AUDIT_USER",
    "nexpoly_md_health_opt": "NEXPOLY_CONTRACT_0012_MD_HEALTH_AUDIT_USER",
}
ACTIVE_JOB_CATEGORIES_V1 = (
    "monomer_md",
    "polytao",
    "online_knowledge",
    "conditional_generation",
    "reverse_design",
    "gpu_inference",
    "gpu_waiting",
    "inflight_api_writes",
)
ACTIVE_JOB_CATEGORIES_V2 = (*ACTIVE_JOB_CATEGORIES_V1, "monomer_dft")
# Compatibility for existing operational tooling and tests. New consumers
# should select a category set through the payload schema version.
ACTIVE_JOB_CATEGORIES = ACTIVE_JOB_CATEGORIES_V1
FORBIDDEN_DEPLOY_HOOKS = (
    "NEXPOLY_DRAIN_ENABLE_COMMAND",
    "NEXPOLY_DRAIN_DISABLE_COMMAND",
    "NEXPOLY_ACTIVE_JOBS_COMMAND",
    "NEXPOLY_NON_MONOMER_ACTIVE_JOBS_COMMAND",
)
FORBIDDEN_DEPLOY_ENV_OVERRIDES = {
    "PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONUSERBASE",
    "VIRTUAL_ENV",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "BASH_ENV",
    "ENV",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "COMPOSE_FILE",
    "COMPOSE_PROJECT_NAME",
    # The reviewed release manifest is the sole target-asset identity.
    "NEXPOLY_ASSET_MANIFEST_DIGEST",
}
FORBIDDEN_WORKER_ENV_OVERRIDES = {
    "PATH",
    "PYTHONHOME",
    "PYTHONUSERBASE",
    "VIRTUAL_ENV",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "BASH_ENV",
    "ENV",
}
WORKER_BASE_IDENTITY_FIELDS = {
    "schema_version",
    "configured_path",
    "resolved_path",
    "executable_sha256",
    "executable_size",
    "implementation",
    "python_version",
    "python_abi",
    "prefix",
    "base_prefix",
    "distribution_count",
    "distribution_metadata_sha256",
    "conda_package_count",
    "conda_metadata_sha256",
}
WORKER_TOOLCHAIN_IDENTITY_FIELDS = {
    "schema_version",
    "conda_executable",
    "conda_executable_sha256",
    "conda_explicit_sha256",
    "gmx_executable",
    "gmx_executable_sha256",
    "gmx_version_sha256",
}
WORKER_BASE_IDENTITY_PROGRAM = r'''
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys


def digest_bytes(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def canonical_name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


distributions = []
for distribution in importlib.metadata.distributions():
    name = distribution.metadata.get("Name")
    if not name:
        continue
    distributions.append(
        {
            "name": canonical_name(name),
            "version": distribution.version,
            "metadata_sha256": digest_bytes(
                (distribution.read_text("METADATA") or "").encode("utf-8", "surrogateescape")
            ),
            "record_sha256": digest_bytes(
                (distribution.read_text("RECORD") or "").encode("utf-8", "surrogateescape")
            ),
            "direct_url_sha256": digest_bytes(
                (distribution.read_text("direct_url.json") or "").encode("utf-8", "surrogateescape")
            ),
        }
    )
distributions.sort(key=lambda item: tuple(item.values()))
distribution_bytes = json.dumps(
    distributions, sort_keys=True, separators=(",", ":")
).encode("utf-8")

prefix = Path(sys.prefix).resolve()
conda_records = []
conda_meta = prefix / "conda-meta"
if conda_meta.is_dir():
    for path in sorted(conda_meta.glob("*.json")):
        if path.is_file() and not path.is_symlink():
            conda_records.append([path.name, digest_bytes(path.read_bytes())])
conda_bytes = json.dumps(conda_records, separators=(",", ":")).encode("utf-8")

print(
    json.dumps(
        {
            "implementation": sys.implementation.name,
            "python_version": sys.version,
            "python_abi": sys.implementation.cache_tag,
            "reported_executable": os.path.realpath(sys.executable),
            "prefix": str(prefix),
            "base_prefix": str(Path(sys.base_prefix).resolve()),
            "distribution_count": len(distributions),
            "distribution_metadata_sha256": digest_bytes(distribution_bytes),
            "conda_package_count": len(conda_records),
            "conda_metadata_sha256": digest_bytes(conda_bytes),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
'''
WORKER_VENV_VERIFY_PROGRAM = r'''
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import sysconfig


def canonical_name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


venv = Path(sys.argv[1]).resolve(strict=True)
document = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
if set(document) != {"schema_version", "requirements"} or document["schema_version"] != 1:
    raise SystemExit("invalid Worker lock expectation document")
requirements = document["requirements"]
if not isinstance(requirements, list):
    raise SystemExit("invalid Worker lock expectation list")

site_roots = []
for scheme in ("purelib", "platlib"):
    value = sysconfig.get_path(scheme)
    if value:
        root = Path(value).resolve(strict=True)
        if not root.is_relative_to(venv):
            raise SystemExit(f"Worker venv {scheme} escapes the release venv: {root}")
        if root not in site_roots:
            site_roots.append(root)

local = {}
for root in site_roots:
    for distribution in importlib.metadata.distributions(path=[str(root)]):
        name = distribution.metadata.get("Name")
        if not name:
            continue
        files = distribution.files or ()
        metadata_files = [
            distribution.locate_file(item).resolve()
            for item in files
            if str(item).endswith(".dist-info/METADATA")
            or str(item).endswith(".egg-info/PKG-INFO")
        ]
        if not metadata_files or any(
            not path.is_file()
            or path.is_symlink()
            or not any(path.is_relative_to(site_root) for site_root in site_roots)
            for path in metadata_files
        ):
            raise SystemExit(f"Worker distribution has unsafe/non-local metadata: {name}")
        local.setdefault(canonical_name(name), []).append(distribution.version)

for requirement in requirements:
    if not isinstance(requirement, dict) or set(requirement) != {"name", "version"}:
        raise SystemExit("invalid Worker requirement record")
    name = requirement["name"]
    version = requirement["version"]
    versions = local.get(name, [])
    if versions != [version]:
        raise SystemExit(
            f"locked Worker distribution is not installed exactly once in release venv: "
            f"{name}=={version} (local versions: {versions})"
        )
'''
CONTRACT_GPU_API_SMOKE_PROGRAM = r'''
import json
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT_SECONDS = int(sys.argv[1])


def request_json(method, path, payload=None, timeout=30):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        BASE_URL + path,
        data=body,
        headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        detail = error.read(4096).decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path} returned HTTP {error.code}: {detail}") from error


def submit(path, payload):
    status, response = request_json("POST", path, payload)
    if status != 202 or not isinstance(response.get("job_id"), str):
        raise RuntimeError(f"{path} did not return HTTP 202 with a job_id")
    return response["job_id"]


def poll(path, job_id):
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while True:
        status, response = request_json("GET", f"{path}/{job_id}")
        if status != 200:
            raise RuntimeError(f"{path}/{job_id} returned HTTP {status}")
        phase = response.get("status")
        if phase == "completed":
            return response
        if phase in {"failed", "cancelled"}:
            detail = response.get("error") or response.get("error_message") or phase
            raise RuntimeError(f"{path}/{job_id} ended as {phase}: {detail}")
        if time.monotonic() >= deadline:
            raise RuntimeError(f"{path}/{job_id} did not complete within {TIMEOUT_SECONDS}s")
        time.sleep(1)


conditional_path = "/api/v1/conditional-generation/tg/jobs"
conditional_job_id = submit(
    conditional_path,
    {
        "smiles": "*CC*",
        "delta_tg": 30,
        "candidate_count": 1,
        "top_k": 5,
        "temperature": 1.0,
    },
)
conditional = poll(conditional_path, conditional_job_id)
if not isinstance(conditional.get("result"), dict):
    raise RuntimeError("conditional-generation completed without a result object")

polytao_path = "/api/v1/conditional-generation/polytao/jobs"
polytao_job_id = submit(
    polytao_path,
    {
        "descriptors": {
            "MolWt": 264,
            "HeavyAtomCount": 19,
            "NHOHCount": 0,
            "NOCount": 4,
            "NumAliphaticCarbocycles": 1,
            "NumAliphaticHeterocycles": 0,
            "NumAliphaticRings": 1,
            "NumAromaticCarbocycles": 0,
            "NumAromaticHeterocycles": 0,
            "NumAromaticRings": 0,
            "NumHAcceptors": 4,
            "NumHDonors": 0,
            "NumHeteroatoms": 6,
            "NumRotatableBonds": 5,
            "RingCount": 1,
        },
        "input_smiles": None,
        "candidate_count": 1,
        "temperature": 1.0,
        "top_k": 100,
        "top_p": 0.999,
        "max_length": 300,
    },
)
polytao = poll(polytao_path, polytao_job_id)
results = (polytao.get("result") or {}).get("results")
if not isinstance(results, list) or not results:
    raise RuntimeError("PolyTAO completed without a candidate")
candidate = results[0]
svg = candidate.get("structure_svg")
if (
    not candidate.get("generated_smiles")
    or not isinstance(svg, str)
    or "<svg" not in svg[:512]
    or not svg.rstrip().endswith("</svg>")
):
    raise RuntimeError("PolyTAO candidate is missing generated_smiles or a complete structure_svg")
print(json.dumps({"conditional_generation": "completed", "polytao": "completed"}, sort_keys=True))
'''
CONTRACT_0012_AUDIT_PROGRAM = r'''
import hashlib
import json

from app.config import Settings
from app.postgres_database import postgres_connection


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")


with postgres_connection(Settings().app_postgres_dsn) as connection:
    rows = [
        row["payload"]
        for row in connection.execute(
            "SELECT to_jsonb(jobs) AS payload "
            "FROM generation.polytao_jobs AS jobs ORDER BY job_id::text"
        ).fetchall()
    ]
    statuses = {
        str(row["status"]): int(row["count"])
        for row in connection.execute(
            "SELECT status, COUNT(*) AS count "
            "FROM generation.polytao_jobs GROUP BY status ORDER BY status"
        ).fetchall()
    }
    columns = [
        dict(row)
        for row in connection.execute(
            "SELECT column_name, ordinal_position, data_type, udt_schema, udt_name, "
            "is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'generation' AND table_name = 'polytao_jobs' "
            "ORDER BY ordinal_position"
        ).fetchall()
    ]
    indexes = [
        dict(row)
        for row in connection.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'generation' AND tablename = 'polytao_jobs' "
            "ORDER BY indexname"
        ).fetchall()
    ]
    constraints = [
        dict(row)
        for row in connection.execute(
            "SELECT constraint_row.conname AS name, constraint_row.contype AS type, "
            "constraint_row.condeferrable AS deferrable, "
            "constraint_row.condeferred AS initially_deferred, "
            "constraint_row.convalidated AS validated, "
            "pg_get_constraintdef(constraint_row.oid, true) AS definition "
            "FROM pg_constraint AS constraint_row "
            "JOIN pg_class AS relation ON relation.oid = constraint_row.conrelid "
            "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = 'generation' "
            "AND relation.relname = 'polytao_jobs' "
            "ORDER BY constraint_row.conname"
        ).fetchall()
    ]
    triggers = [
        dict(row)
        for row in connection.execute(
            "SELECT trigger_row.tgname AS name, trigger_row.tgenabled AS enabled, "
            "pg_get_triggerdef(trigger_row.oid, true) AS definition "
            "FROM pg_trigger AS trigger_row "
            "JOIN pg_class AS relation ON relation.oid = trigger_row.tgrelid "
            "JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace "
            "WHERE namespace.nspname = 'generation' "
            "AND relation.relname = 'polytao_jobs' "
            "AND NOT trigger_row.tgisinternal ORDER BY trigger_row.tgname"
        ).fetchall()
    ]
    structure = {
        "columns": columns,
        "indexes": indexes,
        "constraints": constraints,
        "triggers": triggers,
    }

print(
    json.dumps(
        {
            "schema_version": 2,
            "row_count": len(rows),
            "status_counts": statuses,
            "rows_sha256": hashlib.sha256(canonical(rows)).hexdigest(),
            "schema_sha256": hashlib.sha256(canonical(structure)).hexdigest(),
            "structure_counts": {
                name: len(records) for name, records in structure.items()
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
'''
CONTRACT_0012_INVENTORY_PROGRAM = r'''
import json

from app.config import Settings
from app.postgres_database import postgres_connection


with postgres_connection(Settings().app_postgres_dsn) as connection:
    connection.execute("SET TRANSACTION READ ONLY")
    identity = connection.execute(
        "SELECT current_database() AS database, current_user AS user, "
        "current_setting('transaction_read_only') AS transaction_read_only"
    ).fetchone()
    databases = [
        {
            "name": str(row["name"]),
            "owner": str(row["owner"]),
            "is_template": bool(row["is_template"]),
            "allow_connections": bool(row["allow_connections"]),
        }
        for row in connection.execute(
            "SELECT datname AS name, pg_get_userbyid(datdba) AS owner, "
            "datistemplate AS is_template, datallowconn AS allow_connections "
            "FROM pg_database ORDER BY datname"
        ).fetchall()
    ]
    ledger_relation = connection.execute(
        "SELECT to_regclass('governance.schema_migrations') AS relation"
    ).fetchone()["relation"]
    if ledger_relation is None:
        ledger = []
    else:
        ledger = [
            {"version": str(row["version"]), "checksum": str(row["checksum"])}
            for row in connection.execute(
                "SELECT version, checksum FROM governance.schema_migrations "
                "ORDER BY version"
            ).fetchall()
        ]
    legacy_relation = connection.execute(
        "SELECT to_regclass('generation.polytao_jobs') AS relation"
    ).fetchone()["relation"]

print(
    json.dumps(
        {
            "schema_version": 1,
            "target_database": str(identity["database"]),
            "current_user": str(identity["user"]),
            "databases": databases,
            "ledger": ledger,
            "legacy_relation_present": legacy_relation is not None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
'''
CONTRACT_0012_DATABASE_AUDIT_PROGRAM = r'''
import json

from app.config import Settings
from app.postgres_database import postgres_connection


with postgres_connection(Settings().app_postgres_dsn) as connection:
    connection.execute("SET TRANSACTION READ ONLY")
    identity = connection.execute(
        "SELECT current_database() AS database, current_user AS user, "
        "current_setting('transaction_read_only') AS transaction_read_only"
    ).fetchone()
    ledger_relation = connection.execute(
        "SELECT to_regclass('governance.schema_migrations') AS relation"
    ).fetchone()["relation"]
    if ledger_relation is None:
        ledger = []
    else:
        ledger = [
            {"version": str(row["version"]), "checksum": str(row["checksum"])}
            for row in connection.execute(
                "SELECT version, checksum FROM governance.schema_migrations "
                "ORDER BY version"
            ).fetchall()
        ]
    legacy_relation = connection.execute(
        "SELECT to_regclass('generation.polytao_jobs') AS relation"
    ).fetchone()["relation"]

print(
    json.dumps(
        {
            "schema_version": 1,
            "database": str(identity["database"]),
            "current_user": str(identity["user"]),
            "transaction_read_only": identity["transaction_read_only"] == "on",
            "ledger": ledger,
            "legacy_relation_present": legacy_relation is not None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
'''
CONTRACT_0012_VERIFY_PROGRAM = r'''
import json

from app.config import Settings
from app.postgres_database import postgres_connection

expected = "c59b6f1efe9f926ad135379bd1a7141a7920730fa93c0e802646b1b913511728"
with postgres_connection(Settings().app_postgres_dsn) as connection:
    row = connection.execute(
        "SELECT checksum FROM governance.schema_migrations "
        "WHERE version = '0012_drop_polytao_jobs'"
    ).fetchone()
    table = connection.execute(
        "SELECT to_regclass('generation.polytao_jobs') AS relation"
    ).fetchone()["relation"]
    schema = connection.execute(
        "SELECT to_regnamespace('generation') AS namespace"
    ).fetchone()["namespace"]
if row is None or str(row["checksum"]) != expected:
    raise SystemExit("0012 ledger checksum is absent or incorrect")
if table is not None or schema is not None:
    raise SystemExit("0012 did not remove the governed PolyTAO table/schema")
print(json.dumps({"schema_version": 1, "verified": True}, sort_keys=True))
'''


class ReleaseError(RuntimeError):
    """A release contract or operation failed safely."""


class DeploymentDeferred(ReleaseError):
    """Active work prevented a safe deployment within the drain window."""


class DeploymentSuperseded(ReleaseError):
    """The automatic release is no longer the repository's main target."""


def require_docker_compose_version(
    environment: dict[str, str] | None = None,
) -> tuple[int, int, int]:
    """Require the Compose release that introduced production override tags."""

    minimum = ".".join(str(part) for part in MINIMUM_DOCKER_COMPOSE_VERSION)
    try:
        completed = subprocess.run(
            ["docker", "compose", "version", "--short"],
            env=environment,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseError(
            f"Docker Compose >= {minimum} is required for "
            "docker-compose.prod.yml !reset/!override tags"
        ) from exc
    raw_version = completed.stdout.strip()
    match = DOCKER_COMPOSE_VERSION_RE.fullmatch(raw_version)
    if completed.returncode != 0 or match is None:
        raise ReleaseError(
            f"Docker Compose >= {minimum} is required for "
            "docker-compose.prod.yml !reset/!override tags; "
            "the installed version could not be verified"
        )
    version = tuple(int(part) for part in match.groups())
    if version < MINIMUM_DOCKER_COMPOSE_VERSION:
        raise ReleaseError(
            f"Docker Compose >= {minimum} is required for "
            "docker-compose.prod.yml !reset/!override tags; "
            f"found {raw_version}"
        )
    return version


def release_uses_worker(document: dict[str, Any]) -> bool:
    """Return whether the release carries the Monomer-MD runtime payload."""

    return document.get("release_bundle") is not None


def failure_status(error: Exception) -> str:
    if isinstance(error, DeploymentDeferred):
        return "deferred"
    if isinstance(error, DeploymentSuperseded):
        return "superseded"
    return "failed"


def validated_active_total(
    payload: object,
    expected_categories: set[str],
    *,
    ignore_monomer_md: bool = False,
) -> int:
    if not isinstance(payload, dict):
        raise ReleaseError("deployment status is not a JSON object")
    if "schema_version" in payload:
        raise ReleaseError(
            "deployment status uses the forbidden legacy schema_version field"
        )
    if "active_jobs_schema_version" not in payload:
        required_categories = set(expected_categories)
    else:
        schema_version = payload["active_jobs_schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ReleaseError("deployment status has an unsupported schema version")
        if schema_version == 1:
            required_categories = set(ACTIVE_JOB_CATEGORIES_V1)
        elif schema_version == 2:
            required_categories = set(ACTIVE_JOB_CATEGORIES_V2)
        else:
            raise ReleaseError("deployment status has an unsupported schema version")
    jobs = payload.get("active_jobs")
    total = payload.get("active_total")
    if not isinstance(jobs, dict) or set(jobs) != required_categories:
        raise ReleaseError("deployment status does not contain the exact required job categories")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ReleaseError("deployment status active_total is not a non-negative integer")
    normalized: dict[str, int] = {}
    for category, value in jobs.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReleaseError(
                f"deployment status category {category} is not a non-negative integer"
            )
        normalized[category] = value
    if sum(normalized.values()) != total:
        raise ReleaseError("deployment status active_total does not match its category counts")
    if ignore_monomer_md:
        if "monomer_md" not in normalized:
            raise ReleaseError("deployment status cannot exclude an absent monomer_md category")
        total -= normalized["monomer_md"]
    return total


def rollback_allows_resume(database_changed: bool, rollback: str | None) -> bool:
    return not database_changed or rollback == "success"


def release_migration_records(
    document: dict[str, Any],
    *,
    enforce_sequence: bool = True,
) -> list[dict[str, Any]]:
    """Normalize release-manifest V1/V2 migrations without weakening either schema."""

    schema_version = document.get("schema_version", 1)
    migrations = document.get("migrations")
    if (
        isinstance(schema_version, bool)
        or schema_version not in SUPPORTED_RELEASE_SCHEMA_VERSIONS
        or not isinstance(migrations, list)
    ):
        raise ReleaseError("unsupported release migration manifest schema")

    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, migration in enumerate(migrations):
        if not isinstance(migration, dict):
            raise ReleaseError(f"migration record {index} must be an object")
        if schema_version == 1:
            if set(migration) != {"name", "type"}:
                raise ReleaseError("V1 migration records must contain exactly name and type")
            version = migration.get("name")
            kind = migration.get("type")
            epoch = 1
            checksum = None
            requirements: list[dict[str, str]] = []
        else:
            if set(migration) != {
                "version",
                "kind",
                "epoch",
                "checksum",
                "requires_contracts",
            }:
                raise ReleaseError(
                    "V2 migration records must contain exactly version, kind, epoch, "
                    "checksum, and requires_contracts"
                )
            version = migration.get("version")
            kind = migration.get("kind")
            epoch = migration.get("epoch")
            checksum = migration.get("checksum")
            raw_requirements = migration.get("requires_contracts")
            if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
                raise ReleaseError(f"migration {version} has an invalid epoch")
            if not isinstance(checksum, str) or MIGRATION_CHECKSUM_RE.fullmatch(checksum) is None:
                raise ReleaseError(f"migration {version} has an invalid canonical checksum")
            if not isinstance(raw_requirements, list):
                raise ReleaseError(f"migration {version} requires_contracts must be a list")
            requirements = []
            requirement_versions: set[str] = set()
            for requirement in raw_requirements:
                if not isinstance(requirement, dict) or set(requirement) != {
                    "version",
                    "checksum",
                }:
                    raise ReleaseError(
                        f"migration {version} contains an invalid contract requirement"
                    )
                required_version = requirement.get("version")
                required_checksum = requirement.get("checksum")
                if (
                    not isinstance(required_version, str)
                    or SAFE_MIGRATION_RE.fullmatch(required_version) is None
                    or required_version in requirement_versions
                    or not isinstance(required_checksum, str)
                    or MIGRATION_CHECKSUM_RE.fullmatch(required_checksum) is None
                ):
                    raise ReleaseError(
                        f"migration {version} contains an invalid or duplicate contract requirement"
                    )
                requirement_versions.add(required_version)
                requirements.append(
                    {"version": required_version, "checksum": required_checksum}
                )
        if (
            not isinstance(version, str)
            or SAFE_MIGRATION_RE.fullmatch(version) is None
            or version in seen
        ):
            raise ReleaseError("migration versions must be unique safe identifiers")
        if kind not in {"baseline", "expand", "contract"}:
            raise ReleaseError(f"invalid migration kind: {kind}")
        seen.add(version)
        normalized.append(
            {
                "version": version,
                "kind": kind,
                "epoch": epoch,
                "checksum": checksum,
                "requires_contracts": requirements,
            }
        )

    if schema_version == 1:
        if not enforce_sequence:
            return normalized
        first_contract = next(
            (index for index, record in enumerate(normalized) if record["kind"] == "contract"),
            None,
        )
        if first_contract is not None and any(
            record["kind"] != "contract" for record in normalized[first_contract:]
        ):
            raise ReleaseError("V1 contract migrations must form the trailing migration suffix")
        return normalized

    if not enforce_sequence:
        return normalized
    if normalized:
        epochs = [record["epoch"] for record in normalized]
        if epochs[0] != 1:
            raise ReleaseError("release migration epoch numbering must start at 1")
        if any(
            current < previous or current > previous + 1
            for previous, current in zip(epochs, epochs[1:])
        ):
            raise ReleaseError("release migration epochs must be ordered and contiguous")
    prior_contracts: list[dict[str, str]] = []
    for epoch in sorted({record["epoch"] for record in normalized}):
        epoch_records = [record for record in normalized if record["epoch"] == epoch]
        first_contract = next(
            (index for index, record in enumerate(epoch_records) if record["kind"] == "contract"),
            None,
        )
        if first_contract is not None and any(
            record["kind"] != "contract" for record in epoch_records[first_contract:]
        ):
            raise ReleaseError(
                f"contract migrations must form the trailing suffix of epoch {epoch}"
            )
        for record in epoch_records:
            if record["requires_contracts"] != prior_contracts:
                raise ReleaseError(
                    f"migration {record['version']} must checksum-bind every earlier-epoch contract"
                )
        prior_contracts.extend(
            {"version": record["version"], "checksum": record["checksum"]}
            for record in epoch_records
            if record["kind"] == "contract"
        )
    return normalized


def assert_release_supports_schema_floor(
    manifest: dict[str, Any],
    floor: object,
) -> None:
    if floor is None:
        return
    if isinstance(floor, str):
        if not SAFE_MIGRATION_RE.fullmatch(floor):
            raise ReleaseError("current release state contains an invalid schema compatibility floor")
        version = floor
        checksum = None
    elif isinstance(floor, dict) and set(floor) == {"version", "checksum"}:
        version = floor.get("version")
        checksum = floor.get("checksum")
        if (
            not isinstance(version, str)
            or SAFE_MIGRATION_RE.fullmatch(version) is None
            or not isinstance(checksum, str)
            or MIGRATION_CHECKSUM_RE.fullmatch(checksum) is None
        ):
            raise ReleaseError("current release state contains an invalid schema compatibility floor")
    else:
        raise ReleaseError("current release state contains an invalid schema compatibility floor")
    supported = {
        record["version"]: record["checksum"]
        for record in release_migration_records(manifest)
    }
    if version not in supported or (
        checksum is not None and supported.get(version) != checksum
    ):
        raise ReleaseError(
            f"rollback release does not support the active schema compatibility floor {version}"
        )


def schema_compatibility_floor_after(
    previous_floor: object,
    applied_migrations: Iterable[str],
    migration_records: Iterable[dict[str, Any]] | None = None,
) -> object:
    if previous_floor is not None:
        # Validate both the historical name-only record and the checksum-bound
        # V2 record. Existing state remains readable, while all new floors are
        # written from V2 migration evidence below.
        assert_release_supports_schema_floor(
            {"schema_version": 1, "migrations": [{"name": previous_floor, "type": "contract"}]}
            if isinstance(previous_floor, str)
            else {
                "schema_version": 2,
                "migrations": [
                    {
                        "version": previous_floor.get("version") if isinstance(previous_floor, dict) else None,
                        "kind": "contract",
                        "epoch": 1,
                        "checksum": previous_floor.get("checksum") if isinstance(previous_floor, dict) else None,
                        "requires_contracts": [],
                    }
                ],
            },
            previous_floor,
        )
    if POLYTAO_SCHEMA_COMPATIBILITY_FLOOR in applied_migrations:
        if migration_records is None:
            raise ReleaseError(
                "cannot create a schema floor from a migration name without canonical records"
            )
        records = {
            record.get("version"): record
            for record in migration_records
            if isinstance(record, dict)
        }
        record = records.get(POLYTAO_SCHEMA_COMPATIBILITY_FLOOR)
        checksum = record.get("checksum") if isinstance(record, dict) else None
        if not isinstance(checksum, str) or MIGRATION_CHECKSUM_RE.fullmatch(checksum) is None:
            raise ReleaseError("cannot create a schema floor without canonical contract checksum")
        return {
            "version": POLYTAO_SCHEMA_COMPATIBILITY_FLOOR,
            "checksum": checksum,
        }
    return previous_floor


def merge_applied_migrations(previous: object, newly_applied: Iterable[str]) -> list[str]:
    """Return a validated, ordered union of the successful migration history."""

    if previous is None:
        previous = []
    if not isinstance(previous, list):
        raise ReleaseError("current release state contains an invalid migration history")
    merged: list[str] = []
    seen: set[str] = set()
    for source, label in ((previous, "current"), (list(newly_applied), "new")):
        for name in source:
            if not isinstance(name, str) or not SAFE_MIGRATION_RE.fullmatch(name):
                raise ReleaseError(f"{label} migration history contains an invalid name")
            if name in seen:
                if label == "current":
                    raise ReleaseError("current release state contains duplicate migrations")
                continue
            seen.add(name)
            merged.append(name)
    return merged


def previous_release_for_deploy(previous_state: dict[str, Any], target_sha: str) -> str:
    """Preserve the last distinct rollback target across same-SHA redeploys."""

    if not previous_state:
        return "bootstrap"
    current_sha = require_sha(str(previous_state.get("source_sha", "")), "current release SHA")
    if current_sha != target_sha:
        return current_sha
    if "previous_release" not in previous_state:
        raise ReleaseError("current release state is missing previous_release")
    previous = previous_state["previous_release"]
    if previous == "bootstrap":
        return previous
    previous_sha = require_sha(str(previous), "previous release SHA")
    if previous_sha == current_sha:
        raise ReleaseError("current release state has no distinct rollback target")
    return previous_sha


def _is_canonical_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return False
    return (
        parsed.tzinfo is not None
        and parsed.utcoffset() == dt.timedelta(0)
        and parsed.microsecond == 0
        and parsed.isoformat(timespec="seconds") == value
        and value.endswith("+00:00")
    )


def _validated_approved_contract_records(
    current_state: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, str]]:
    approved_records = current_state.get("approved_contracts")
    explicit = current_state.get("approved_contract_migrations")
    if explicit not in (None, []):
        raise ReleaseError(
            "name-only contract approvals are not valid approval authority"
        )
    if approved_records is None:
        return [], {}
    if not isinstance(approved_records, list):
        raise ReleaseError("current release state contains invalid approved contracts")
    approved: dict[str, str] = {}
    normalized: list[dict[str, str]] = []
    for record in approved_records:
        if not isinstance(record, dict) or set(record) != {
            "version",
            "checksum",
            "operation_id",
            "approved_at",
        }:
            raise ReleaseError("current release state contains invalid approved contracts")
        version = record.get("version")
        checksum = record.get("checksum")
        operation_id = record.get("operation_id")
        approved_at = record.get("approved_at")
        if (
            not isinstance(version, str)
            or SAFE_MIGRATION_RE.fullmatch(version) is None
            or version != POLYTAO_SCHEMA_COMPATIBILITY_FLOOR
            or version in approved
            or not isinstance(checksum, str)
            or MIGRATION_CHECKSUM_RE.fullmatch(checksum) is None
            or checksum != POLYTAO_CONTRACT_CHECKSUM
            or not isinstance(operation_id, str)
            or OPERATION_ID_RE.fullmatch(operation_id) is None
            or not _is_canonical_utc_timestamp(approved_at)
        ):
            raise ReleaseError("current release state contains invalid approved contracts")
        approved[version] = checksum
        normalized.append(
            {
                "version": version,
                "checksum": checksum,
                "operation_id": operation_id,
                "approved_at": approved_at,
            }
        )
    return normalized, approved


def approved_contract_migrations(current_state: dict[str, Any]) -> dict[str, str]:
    """Return only checksum-bound approvals backed by the atomic epoch barrier.

    Historic migration history, candidate manifests, compatibility floors, and
    name-only approval lists are intentionally not authority to approve a
    destructive contract.
    """

    _records, approved = _validated_approved_contract_records(current_state)
    barrier = validated_migration_epoch_barrier(current_state)
    if approved and barrier is None:
        raise ReleaseError(
            "checksum-bound contract approval is missing its migration epoch barrier"
        )
    return approved


def validated_migration_epoch_barrier(
    current_state: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate the atomic approval/floor/barrier record written by maintenance."""

    barrier = current_state.get("migration_epoch_barrier")
    last_operation = current_state.get("last_contract_operation")
    approved_records, _approved = _validated_approved_contract_records(current_state)
    has_checksum_approvals = bool(approved_records)
    has_checksum_floor = isinstance(
        current_state.get("schema_compatibility_floor"),
        dict,
    )
    if barrier is None:
        if last_operation is not None or has_checksum_approvals or has_checksum_floor:
            raise ReleaseError(
                "checksum-bound contract approval is missing its migration epoch barrier"
            )
        return None
    if len(approved_records) != 1:
        raise ReleaseError(
            "migration epoch barrier must bind the only checksum-approved contract"
        )
    if not isinstance(barrier, dict) or set(barrier) != {
        "epoch",
        "contract",
        "operation_id",
        "approved_at",
    }:
        raise ReleaseError("current release state contains an invalid migration epoch barrier")
    epoch = barrier.get("epoch")
    contract = barrier.get("contract")
    operation_id = barrier.get("operation_id")
    approved_at = barrier.get("approved_at")
    if (
        isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch != 1
        or not isinstance(contract, dict)
        or set(contract) != {"version", "checksum"}
        or not isinstance(operation_id, str)
        or OPERATION_ID_RE.fullmatch(operation_id) is None
        or not _is_canonical_utc_timestamp(approved_at)
        or last_operation != operation_id
    ):
        raise ReleaseError("current release state contains an invalid migration epoch barrier")
    version = contract.get("version")
    checksum = contract.get("checksum")
    if (
        not isinstance(version, str)
        or SAFE_MIGRATION_RE.fullmatch(version) is None
        or version != POLYTAO_SCHEMA_COMPATIBILITY_FLOOR
        or not isinstance(checksum, str)
        or MIGRATION_CHECKSUM_RE.fullmatch(checksum) is None
        or checksum != POLYTAO_CONTRACT_CHECKSUM
    ):
        raise ReleaseError("current release state contains an invalid migration epoch barrier")
    floor = current_state.get("schema_compatibility_floor")
    if floor != {"version": version, "checksum": checksum}:
        raise ReleaseError("migration epoch barrier differs from the schema compatibility floor")
    matching = [
        record
        for record in approved_records
        if record.get("version") == version
    ]
    if matching != [
        {
            "version": version,
            "checksum": checksum,
            "operation_id": operation_id,
            "approved_at": approved_at,
        }
    ]:
        raise ReleaseError("migration epoch barrier differs from its contract approval")
    return dict(barrier)


def pending_contract_migrations(
    current_state: dict[str, Any],
    candidate_manifest: dict[str, Any],
) -> list[str]:
    """Return contract migrations introduced by a not-yet-published candidate.

    A candidate artifact exists for every successfully built main SHA, so the
    artifact name alone is not approval evidence.  Promotion is allowed only
    when its manifest introduces a contract migration that is absent from the
    current successful release manifest.
    """

    candidate_contracts = {
        record["version"]: record["checksum"]
        for record in release_migration_records(
            candidate_manifest,
            enforce_sequence=False,
        )
        if record["kind"] == "contract"
    }
    approved = approved_contract_migrations(current_state)
    return sorted(
        version
        for version, checksum in candidate_contracts.items()
        if version not in approved or approved[version] != checksum
    )


def code_deploy_migration_mode(
    current_state: dict[str, Any],
    candidate_manifest: dict[str, Any],
    *,
    deployment_mode: str,
    target_sha: str,
) -> str:
    """Select expand-only deployment while allowing a trailing contract suffix."""

    del deployment_mode, target_sha
    records = release_migration_records(candidate_manifest)
    validated_migration_epoch_barrier(current_state)
    approved = approved_contract_migrations(current_state)
    for record in records:
        if record["kind"] not in {"baseline", "expand"}:
            continue
        missing = [
            requirement["version"]
            for requirement in record["requires_contracts"]
            if approved.get(requirement["version"]) != requirement["checksum"]
        ]
        if missing:
            raise ReleaseError(
                f"migration {record['version']} requires checksum-approved contracts: "
                + ", ".join(missing)
            )
    return "expand"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def canonical_json_digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def canonical_distribution_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise ReleaseError(f"invalid Worker distribution name in lock: {value}")
    return re.sub(r"[-_.]+", "-", value).lower()


def worker_lock_requirements(lock: Path, bundle_root: Path) -> list[dict[str, str]]:
    """Resolve safe ``-r`` includes and return every exact Worker lock pin."""

    boundary = bundle_root.resolve(strict=True)
    requirements: dict[str, str] = {}
    visited: set[Path] = set()

    def visit(candidate: Path) -> None:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ReleaseError(f"Worker requirements lock is missing: {candidate}") from exc
        if not resolved.is_relative_to(boundary) or candidate.is_symlink() or not resolved.is_file():
            raise ReleaseError(f"Worker requirements lock is unsafe: {candidate}")
        if resolved in visited:
            raise ReleaseError(f"Worker requirements lock contains a recursive include: {candidate}")
        visited.add(resolved)
        try:
            lines = resolved.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ReleaseError(f"Worker requirements lock is not UTF-8: {candidate}") from exc
        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#") or raw_line[:1].isspace():
                continue
            if stripped == "--only-binary :all:":
                continue
            include = re.fullmatch(r"(?:-r|--requirement)\s+([^\s]+)", stripped)
            if include:
                relative = PurePosixPath(include.group(1))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ReleaseError("Worker requirements lock include escapes the bundle")
                visit(boundary.joinpath(*relative.parts))
                continue
            match = re.fullmatch(
                r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)(?:\s+\\)?",
                stripped,
            )
            if not match:
                raise ReleaseError(f"unsupported Worker requirements lock entry: {stripped}")
            name = canonical_distribution_name(match.group(1))
            version = match.group(2)
            previous = requirements.get(name)
            if previous is not None and previous != version:
                raise ReleaseError(f"Worker requirements locks disagree for distribution: {name}")
            requirements[name] = version
        visited.remove(resolved)

    visit(lock)
    if not requirements:
        raise ReleaseError("Worker requirements lock does not contain any exact distribution pins")
    return [{"name": name, "version": requirements[name]} for name in sorted(requirements)]


def validate_worker_base_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != WORKER_BASE_IDENTITY_FIELDS | {"identity_sha256"}:
        raise ReleaseError("Worker base Python identity record is missing or invalid")
    if value.get("schema_version") != 1:
        raise ReleaseError("Worker base Python identity schema is unsupported")
    for key in ("configured_path", "resolved_path", "prefix", "base_prefix"):
        item = value.get(key)
        if not isinstance(item, str) or not Path(item).is_absolute() or ".." in Path(item).parts:
            raise ReleaseError(f"Worker base Python identity contains an invalid {key}")
    for key in ("executable_sha256", "distribution_metadata_sha256", "conda_metadata_sha256"):
        if not isinstance(value.get(key), str) or not DIGEST_RE.fullmatch(value[key]):
            raise ReleaseError(f"Worker base Python identity contains an invalid {key}")
    for key in ("implementation", "python_version", "python_abi"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ReleaseError(f"Worker base Python identity contains an invalid {key}")
    for key in ("executable_size", "distribution_count", "conda_package_count"):
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ReleaseError(f"Worker base Python identity contains an invalid {key}")
    material = {key: value[key] for key in WORKER_BASE_IDENTITY_FIELDS}
    expected = canonical_json_digest(material)
    if value.get("identity_sha256") != expected:
        raise ReleaseError("Worker base Python identity fingerprint does not match its record")
    return dict(value)


def inspect_worker_base_python(
    configured_value: str,
    expected_identity: str | None,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fingerprint a frozen base runtime without rejecting normal Conda symlinks."""

    configured = Path(configured_value)
    if not configured_value or not configured.is_absolute() or ".." in configured.parts:
        raise ReleaseError("NEXPOLY_WORKER_BASE_PYTHON must be an absolute safe path")
    try:
        configured_metadata = configured.lstat()
        resolved = configured.resolve(strict=True)
        before = resolved.stat()
    except OSError as exc:
        raise ReleaseError("NEXPOLY_WORKER_BASE_PYTHON cannot be resolved safely") from exc
    if not (stat.S_ISREG(configured_metadata.st_mode) or stat.S_ISLNK(configured_metadata.st_mode)):
        raise ReleaseError("NEXPOLY_WORKER_BASE_PYTHON must name a file or a file symlink")
    if not stat.S_ISREG(before.st_mode) or not os.access(resolved, os.X_OK):
        raise ReleaseError("NEXPOLY_WORKER_BASE_PYTHON must resolve to an executable regular file")
    if before.st_mode & 0o022:
        raise ReleaseError("frozen Worker base Python must not be group/world writable")

    executable_digest = sha256_file(resolved)
    clean_environment = (environment or os.environ).copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        clean_environment.pop(key, None)
    clean_environment["PYTHONNOUSERSITE"] = "1"
    result = subprocess.run(
        [str(resolved), "-I", "-c", WORKER_BASE_IDENTITY_PROGRAM],
        cwd="/",
        env=clean_environment,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    try:
        runtime = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseError("Worker base Python did not return a valid identity document") from exc
    runtime_fields = {
        "implementation",
        "python_version",
        "python_abi",
        "reported_executable",
        "prefix",
        "base_prefix",
        "distribution_count",
        "distribution_metadata_sha256",
        "conda_package_count",
        "conda_metadata_sha256",
    }
    if not isinstance(runtime, dict) or set(runtime) != runtime_fields:
        raise ReleaseError("Worker base Python returned an incomplete identity document")
    if runtime.get("reported_executable") != str(resolved):
        raise ReleaseError("Worker base Python reported a different executable identity")
    try:
        after = resolved.stat()
        resolved_after = configured.resolve(strict=True)
    except OSError as exc:
        raise ReleaseError("Worker base Python changed while it was fingerprinted") from exc
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if resolved_after != resolved or any(getattr(before, key) != getattr(after, key) for key in stable_fields):
        raise ReleaseError("Worker base Python changed while it was fingerprinted")

    material = {
        "schema_version": 1,
        "configured_path": str(configured),
        "resolved_path": str(resolved),
        "executable_sha256": executable_digest,
        "executable_size": before.st_size,
        **{key: runtime[key] for key in runtime_fields if key != "reported_executable"},
    }
    identity = validate_worker_base_identity(
        {**material, "identity_sha256": canonical_json_digest(material)}
    )
    if expected_identity is not None:
        if not DIGEST_RE.fullmatch(expected_identity):
            raise ReleaseError("NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256 must be a sha256 digest")
        if identity["identity_sha256"] != expected_identity:
            raise ReleaseError("frozen Worker base Python identity differs from deploy.env")
    return identity


def validate_worker_toolchain_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != WORKER_TOOLCHAIN_IDENTITY_FIELDS | {
        "identity_sha256"
    }:
        raise ReleaseError("Worker Conda/GROMACS identity record is missing or invalid")
    if value.get("schema_version") != 1:
        raise ReleaseError("Worker Conda/GROMACS identity schema is unsupported")
    for key in ("conda_executable", "gmx_executable"):
        item = value.get(key)
        if not isinstance(item, str) or not Path(item).is_absolute() or ".." in Path(item).parts:
            raise ReleaseError(f"Worker toolchain identity contains an invalid {key}")
    for key in (
        "conda_executable_sha256",
        "conda_explicit_sha256",
        "gmx_executable_sha256",
        "gmx_version_sha256",
    ):
        if not isinstance(value.get(key), str) or not DIGEST_RE.fullmatch(value[key]):
            raise ReleaseError(f"Worker toolchain identity contains an invalid {key}")
    material = {key: value[key] for key in WORKER_TOOLCHAIN_IDENTITY_FIELDS}
    if value.get("identity_sha256") != canonical_json_digest(material):
        raise ReleaseError("Worker Conda/GROMACS identity fingerprint does not match its record")
    return dict(value)


def inspect_worker_toolchain(
    base_identity: dict[str, Any],
    environment: dict[str, str],
) -> dict[str, Any]:
    """Fingerprint `conda list --explicit` and the exact GROMACS runtime."""

    configured_python_bin = Path(str(base_identity["configured_path"])).parent
    paths: dict[str, Path] = {}
    for key, label in (
        ("NEXPOLY_WORKER_CONDA_EXE", "Conda executable"),
        ("NEXPOLY_WORKER_GMX", "GROMACS executable"),
    ):
        raw = environment.get(key, "")
        configured = Path(raw)
        if not raw or not configured.is_absolute() or ".." in configured.parts:
            raise ReleaseError(f"{key} must be an absolute safe path")
        try:
            resolved = configured.resolve(strict=True)
            metadata = resolved.stat()
        except OSError as exc:
            raise ReleaseError(f"{label} cannot be resolved safely") from exc
        if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
            raise ReleaseError(f"{label} must resolve to an executable regular file")
        paths[key] = resolved
    if Path(environment["NEXPOLY_WORKER_GMX"]).parent != configured_python_bin:
        raise ReleaseError("NEXPOLY_WORKER_GMX must come from the frozen Worker environment bin")

    before = {
        key: (path.stat(), sha256_file(path))
        for key, path in paths.items()
    }
    conda = subprocess.run(
        [
            str(paths["NEXPOLY_WORKER_CONDA_EXE"]),
            "list",
            "--explicit",
            "--prefix",
            str(base_identity["prefix"]),
        ],
        cwd="/",
        env=environment,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ).stdout
    if b"@EXPLICIT" not in conda:
        raise ReleaseError("conda list --explicit returned an invalid environment identity")
    gmx = subprocess.run(
        [str(paths["NEXPOLY_WORKER_GMX"]), "--version"],
        cwd="/",
        env=environment,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ).stdout
    if b"GROMACS version" not in gmx:
        raise ReleaseError("gmx --version returned an invalid runtime identity")
    for key, path in paths.items():
        after = path.stat()
        initial, digest = before[key]
        if (
            (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or sha256_file(path) != digest
        ):
            raise ReleaseError("Worker Conda/GROMACS executable changed while fingerprinting")
    material = {
        "schema_version": 1,
        "conda_executable": str(paths["NEXPOLY_WORKER_CONDA_EXE"]),
        "conda_executable_sha256": before["NEXPOLY_WORKER_CONDA_EXE"][1],
        "conda_explicit_sha256": sha256_bytes(conda),
        "gmx_executable": str(paths["NEXPOLY_WORKER_GMX"]),
        "gmx_executable_sha256": before["NEXPOLY_WORKER_GMX"][1],
        "gmx_version_sha256": sha256_bytes(gmx),
    }
    return validate_worker_toolchain_identity(
        {**material, "identity_sha256": canonical_json_digest(material)}
    )


def inspect_asset_release(path: Path) -> tuple[Path, str, str]:
    asset_root = path.resolve()
    if not asset_root.is_dir():
        raise ReleaseError(f"pinned asset release is missing: {asset_root}")
    asset_manifest = asset_root / "ASSET-MANIFEST.json"
    if not asset_manifest.is_file() or asset_manifest.is_symlink():
        raise ReleaseError(f"pinned asset manifest is missing: {asset_manifest}")
    if asset_root.stat().st_mode & 0o222 or asset_manifest.stat().st_mode & 0o222:
        raise ReleaseError("pinned production asset release and manifest must be read-only")
    try:
        document = json.loads(asset_manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("pinned asset manifest is not valid JSON") from exc
    expected_trees = {"model", "database", "backend-data", "byteff2"}
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or set(document) != {
            "schema_version",
            "byteff2_commit",
            "byteff2_submodules",
            "assets",
        }
        or not isinstance(document.get("assets"), dict)
        or set(document["assets"]) != expected_trees
    ):
        raise ReleaseError("pinned asset manifest has an unsupported schema")
    manifest_commit = require_sha(str(document.get("byteff2_commit", "")), "asset ByteFF2 commit")
    submodules = document.get("byteff2_submodules")
    if not isinstance(submodules, dict) or any(
        not isinstance(name, str)
        or not name
        or PurePosixPath(name).is_absolute()
        or ".." in PurePosixPath(name).parts
        or str(PurePosixPath(name)) != name
        or not isinstance(commit, str)
        or not SHA_RE.fullmatch(commit)
        for name, commit in submodules.items()
    ):
        raise ReleaseError("pinned asset manifest has invalid ByteFF2 submodule metadata")
    root_entries = {entry.name for entry in asset_root.iterdir()}
    if root_entries != expected_trees | {"ASSET-MANIFEST.json"}:
        raise ReleaseError("pinned asset release contains unmanifested root entries")
    for tree_name in sorted(expected_trees):
        tree = asset_root / tree_name
        if not tree.is_dir() or tree.is_symlink():
            raise ReleaseError(f"pinned asset tree is missing or unsafe: {tree_name}")
        records = document["assets"][tree_name]
        if not isinstance(records, list):
            raise ReleaseError(f"pinned asset manifest tree is not a list: {tree_name}")
        expected_files: dict[str, tuple[int, str]] = {}
        for record in records:
            if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
                raise ReleaseError(f"pinned asset record has an invalid shape: {tree_name}")
            relative = record.get("path")
            size = record.get("size")
            checksum = record.get("sha256")
            pure = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath(".")
            if (
                not isinstance(relative, str)
                or not relative
                or pure.is_absolute()
                or ".." in pure.parts
                or str(pure) != relative
                or relative in expected_files
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or not isinstance(checksum, str)
                or not re.fullmatch(r"[0-9a-f]{64}", checksum)
            ):
                raise ReleaseError(f"pinned asset record is unsafe: {tree_name}/{relative}")
            expected_files[relative] = (size, checksum)

        actual_files: set[str] = set()
        for current, directories, files in os.walk(tree, followlinks=False):
            current_path = Path(current)
            if current_path.stat().st_mode & 0o222:
                raise ReleaseError(f"pinned asset directory is writable: {current_path}")
            for directory_name in directories:
                directory = current_path / directory_name
                if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o222:
                    raise ReleaseError(f"pinned asset directory is unsafe: {directory}")
            for filename in files:
                file_path = current_path / filename
                relative = file_path.relative_to(tree).as_posix()
                if file_path.is_symlink() or not file_path.is_file() or file_path.stat().st_mode & 0o222:
                    raise ReleaseError(f"pinned asset file is unsafe: {tree_name}/{relative}")
                actual_files.add(relative)
        if actual_files != set(expected_files):
            raise ReleaseError(f"pinned asset file inventory differs from manifest: {tree_name}")
        for relative, (expected_size, expected_checksum) in expected_files.items():
            file_path = tree.joinpath(*PurePosixPath(relative).parts)
            before = file_path.stat()
            if before.st_size != expected_size:
                raise ReleaseError(f"pinned asset file size differs from manifest: {tree_name}/{relative}")
            checksum = sha256_file(file_path).removeprefix("sha256:")
            after = file_path.stat()
            if (
                checksum != expected_checksum
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise ReleaseError(f"pinned asset file digest differs from manifest: {tree_name}/{relative}")
    digest = sha256_file(asset_manifest)
    byteff2_commit_file = asset_root / "byteff2" / "BYTEFF2-COMMIT"
    if not byteff2_commit_file.is_file() or byteff2_commit_file.is_symlink():
        raise ReleaseError(f"pinned asset release is missing BYTEFF2-COMMIT: {byteff2_commit_file}")
    byteff2_commit = require_sha(byteff2_commit_file.read_text(encoding="ascii").strip(), "ByteFF2 commit")
    if byteff2_commit != manifest_commit:
        raise ReleaseError("BYTEFF2-COMMIT differs from the pinned asset manifest")
    return asset_root, digest, byteff2_commit


def inspect_managed_asset_pointer(
    pointer: Path,
    configured_digest: str,
) -> tuple[Path, str, str]:
    """Resolve the sole production asset pointer to one content-addressed release."""

    expected_digest = require_digest(configured_digest, "configured asset digest")
    try:
        store_status = ASSET_RELEASES_ROOT.lstat()
    except OSError as exc:
        raise ReleaseError(f"managed asset store is unavailable: {ASSET_RELEASES_ROOT}") from exc
    if (
        not stat.S_ISDIR(store_status.st_mode)
        or ASSET_RELEASES_ROOT.is_symlink()
        or store_status.st_mode & 0o022
    ):
        raise ReleaseError("managed asset store must be a real, non-writable directory")
    try:
        before = pointer.lstat()
        raw_target = os.readlink(pointer)
        after = pointer.lstat()
    except OSError as exc:
        raise ReleaseError(f"managed production asset pointer is unavailable: {pointer}") from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    )
    if not stat.S_ISLNK(before.st_mode) or before_identity != after_identity:
        raise ReleaseError("NEXPOLY_ASSET_ROOT must be a stable managed symlink")
    target = Path(raw_target)
    if not target.is_absolute():
        raise ReleaseError("managed production asset pointer must use an absolute target")
    expected_name = expected_digest.removeprefix("sha256:")
    if target.parent != ASSET_RELEASES_ROOT or target.name != expected_name:
        raise ReleaseError(
            "managed production asset pointer must target "
            f"{ASSET_RELEASES_ROOT}/<configured digest>"
        )
    try:
        target_status = target.lstat()
    except OSError as exc:
        raise ReleaseError(f"managed asset release is unavailable: {target}") from exc
    if not stat.S_ISDIR(target_status.st_mode) or target.is_symlink():
        raise ReleaseError("managed asset pointer target must be a real release directory")
    asset_root, actual_digest, byteff2_commit = inspect_asset_release(target)
    if asset_root != target or actual_digest != expected_digest:
        raise ReleaseError("managed asset release directory and manifest digest disagree")
    try:
        final = pointer.lstat()
        final_target = os.readlink(pointer)
    except OSError as exc:
        raise ReleaseError("managed production asset pointer changed during validation") from exc
    final_identity = (
        final.st_dev,
        final.st_ino,
        final.st_mode,
        final.st_size,
        final.st_mtime_ns,
    )
    if final_identity != before_identity or final_target != raw_target:
        raise ReleaseError("managed production asset pointer changed during validation")
    return asset_root, actual_digest, byteff2_commit


def inspect_managed_asset_release(configured_digest: str) -> tuple[Path, str, str]:
    """Resolve and verify a content-addressed candidate without moving the live pointer."""

    expected_digest = require_digest(configured_digest, "candidate asset digest")
    try:
        store_status = ASSET_RELEASES_ROOT.lstat()
    except OSError as exc:
        raise ReleaseError(f"managed asset store is unavailable: {ASSET_RELEASES_ROOT}") from exc
    if (
        not stat.S_ISDIR(store_status.st_mode)
        or ASSET_RELEASES_ROOT.is_symlink()
        or store_status.st_mode & 0o022
    ):
        raise ReleaseError("managed asset store must be a real, non-writable directory")
    target = ASSET_RELEASES_ROOT / expected_digest.removeprefix("sha256:")
    try:
        target_status = target.lstat()
    except OSError as exc:
        raise ReleaseError(f"candidate asset release is unavailable: {target}") from exc
    if not stat.S_ISDIR(target_status.st_mode) or target.is_symlink():
        raise ReleaseError("candidate asset release must be a real directory")
    resolved, actual_digest, byteff2_commit = inspect_asset_release(target)
    if resolved != target or actual_digest != expected_digest:
        raise ReleaseError("candidate asset release directory and manifest digest disagree")
    return resolved, actual_digest, byteff2_commit


def require_sha(value: str, name: str = "SHA") -> str:
    normalized = value.strip().lower()
    if not SHA_RE.fullmatch(normalized):
        raise ReleaseError(f"{name} must be a full 40-character lowercase commit SHA")
    return normalized


def require_digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if not DIGEST_RE.fullmatch(normalized):
        raise ReleaseError(f"{name} must be sha256 followed by 64 lowercase hex characters")
    return normalized


def require_image(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if not IMAGE_RE.fullmatch(normalized):
        raise ReleaseError(f"{name} must be an immutable image@sha256 digest reference")
    return normalized


def artifact_record(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ReleaseError(f"release artifact is not a regular file: {path}")
    return {"name": path.name, "size": path.stat().st_size, "sha256": sha256_file(path)}


def parse_migrations(values: Iterable[str]) -> list[dict[str, str]]:
    migrations: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        name, separator, kind = value.rpartition(":")
        if not separator or not SAFE_MIGRATION_RE.fullmatch(name) or kind not in {"expand", "contract", "baseline"}:
            raise ReleaseError(f"invalid migration descriptor {value!r}; expected name:expand|contract|baseline")
        if name in seen:
            raise ReleaseError(f"duplicate migration name: {name}")
        seen.add(name)
        migrations.append({"name": name, "type": kind})
    return migrations


def migration_sql_checksum(path: Path) -> str:
    normalized = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def release_migrations_from_policy_manifest(
    path: Path,
    *,
    include_baseline: bool = False,
) -> list[dict[str, Any]]:
    """Load a V1/V2 repository policy and emit checksum-complete V2 records."""

    policy = load_manifest(path)
    if set(policy) != {"schema_version", "migrations"}:
        raise ReleaseError("migration policy manifest has an invalid shape")
    schema_version = policy.get("schema_version")
    raw_migrations = policy.get("migrations")
    if (
        isinstance(schema_version, bool)
        or schema_version not in {1, 2}
        or not isinstance(raw_migrations, list)
    ):
        raise ReleaseError("migration policy manifest must use schema version 1 or 2")
    sql_paths = sorted(path.parent.glob("*.sql"))
    sql_by_version = {sql_path.stem: sql_path for sql_path in sql_paths}
    records: list[dict[str, Any]] = []
    for raw in raw_migrations:
        if not isinstance(raw, dict):
            raise ReleaseError("migration policy contains a non-object record")
        if schema_version == 1:
            if set(raw) != {"version", "kind"}:
                raise ReleaseError("migration policy V1 record has an invalid shape")
            version = raw.get("version")
            kind = raw.get("kind")
            epoch = 1
            requirements: list[dict[str, str]] = []
            declared_checksum = None
        else:
            if set(raw) != {
                "version",
                "kind",
                "epoch",
                "checksum",
                "requires_contracts",
            }:
                raise ReleaseError("migration policy V2 record has an invalid shape")
            version = raw.get("version")
            kind = raw.get("kind")
            epoch = raw.get("epoch")
            declared_checksum = raw.get("checksum")
            requirements = raw.get("requires_contracts")
        if not isinstance(version, str) or version not in sql_by_version:
            raise ReleaseError(f"migration policy SQL is missing for {version}")
        actual_checksum = migration_sql_checksum(sql_by_version[version])
        if declared_checksum is not None and declared_checksum != actual_checksum:
            raise ReleaseError(
                f"migration policy checksum differs from canonical SQL for {version}"
            )
        records.append(
            {
                "version": version,
                "kind": kind,
                "epoch": epoch,
                "checksum": actual_checksum,
                "requires_contracts": requirements,
            }
        )
    if {record["version"] for record in records} != set(sql_by_version):
        raise ReleaseError("migration policy and SQL file sets differ")
    if [record["version"] for record in records] != list(sql_by_version):
        raise ReleaseError("migration policy and SQL files are not in the same lexical order")
    release_migration_records({"schema_version": 2, "migrations": records})
    baselines = [record for record in records if record["kind"] == "baseline"]
    if len(baselines) != 1 or records[0]["kind"] != "baseline":
        raise ReleaseError("migration policy must contain one leading baseline")
    if include_baseline:
        return records
    return [record for record in records if record["kind"] != "baseline"]


def load_release_input(path: Path) -> dict[str, Any]:
    """Load the small, reviewed asset/data input committed with a release."""

    document = load_manifest(path)
    if set(document) != {
        "schema_version",
        "asset_manifest_digest",
        "datasets_on_asset_change",
    } or document.get("schema_version") != 1:
        raise ReleaseError("release input must use the supported three-field schema")
    asset_digest = require_digest(
        str(document.get("asset_manifest_digest", "")),
        "release input asset manifest digest",
    )
    datasets = document.get("datasets_on_asset_change")
    if (
        not isinstance(datasets, list)
        or not datasets
        or any(
            not isinstance(dataset, str)
            or not SAFE_DATASET_RE.fullmatch(dataset)
            or dataset in {"all", "none"}
            for dataset in datasets
        )
        or len(set(datasets)) != len(datasets)
    ):
        raise ReleaseError(
            "datasets_on_asset_change must be a non-empty duplicate-free list of explicit datasets"
        )
    return {
        "asset_manifest_digest": asset_digest,
        "datasets_on_asset_change": datasets,
    }


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    source_sha = require_sha(args.sha, "source SHA")
    release_bundle_value = getattr(args, "release_bundle", None)
    if not release_bundle_value:
        raise ReleaseError("build-manifest requires --release-bundle")
    release_bundle = artifact_record(Path(release_bundle_value))
    release_input = load_release_input(Path(args.release_input))
    policy_manifest = getattr(args, "migration_manifest", None)
    legacy_descriptors = list(getattr(args, "migration", None) or [])
    if policy_manifest and legacy_descriptors:
        raise ReleaseError("build-manifest cannot combine --migration-manifest and --migration")
    if policy_manifest:
        schema_version = 2
        migrations: list[dict[str, Any]] = release_migrations_from_policy_manifest(
            Path(policy_manifest)
        )
    else:
        # Compatibility for detached V1 builders during the bridge window.
        # The production workflow always supplies the repository policy and
        # therefore emits checksum-bound schema V2.
        schema_version = 1
        migrations = parse_migrations(legacy_descriptors)
    document: dict[str, Any] = {
        "schema_version": schema_version,
        "release_type": "code",
        "source_sha": source_sha,
        "ci_run_id": str(args.ci_run_id),
        "created_at": utc_now(),
        "images": {
            "backend": require_image(args.backend_image, "backend image"),
            "web": require_image(args.web_image, "web image"),
        },
        "release_bundle": release_bundle,
        "asset_manifest_digest": release_input["asset_manifest_digest"],
        "datasets_on_asset_change": release_input["datasets_on_asset_change"],
        "migrations": migrations,
    }
    validate_manifest(document, deployment_mode="auto")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(output)
    fsync_directory(output.parent)
    return document


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"cannot read release manifest {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ReleaseError("release manifest must contain a JSON object")
    return document


def validate_artifact_record(record: Any, name: str, *, optional: bool = False) -> None:
    if record is None and optional:
        return
    if not isinstance(record, dict) or set(record) != {"name", "size", "sha256"}:
        raise ReleaseError(f"{name} record has an invalid shape")
    filename = record["name"]
    if not isinstance(filename, str) or Path(filename).name != filename or filename in {"", ".", ".."}:
        raise ReleaseError(f"{name} filename must be a basename")
    if not isinstance(record["size"], int) or isinstance(record["size"], bool) or record["size"] < 1:
        raise ReleaseError(f"{name} size must be a positive integer")
    if not isinstance(record["sha256"], str):
        raise ReleaseError(f"{name} sha256 is missing")
    require_digest(record["sha256"], f"{name} sha256")


def validate_manifest(document: dict[str, Any], deployment_mode: str = "auto") -> dict[str, Any]:
    if deployment_mode not in {"auto", "bootstrap"}:
        raise ReleaseError("deployment mode must be auto or bootstrap")
    expected_fields = {
        "schema_version",
        "release_type",
        "source_sha",
        "ci_run_id",
        "created_at",
        "images",
        "release_bundle",
        "asset_manifest_digest",
        "datasets_on_asset_change",
        "migrations",
    }
    if set(document) != expected_fields:
        raise ReleaseError("release manifest must contain exactly the single-bundle schema fields")
    if (
        isinstance(document.get("schema_version"), bool)
        or document.get("schema_version") not in SUPPORTED_RELEASE_SCHEMA_VERSIONS
        or document.get("release_type") != "code"
    ):
        raise ReleaseError("unsupported release manifest schema or type")
    require_sha(str(document.get("source_sha", "")), "source SHA")
    if not str(document.get("ci_run_id", "")).strip():
        raise ReleaseError("ci_run_id is required")
    images = document.get("images")
    if not isinstance(images, dict) or set(images) != {"backend", "web"}:
        raise ReleaseError("images must contain exactly backend and web")
    require_image(str(images["backend"]), "backend image")
    require_image(str(images["web"]), "web image")
    validate_artifact_record(document.get("release_bundle"), "release bundle")
    require_digest(str(document["asset_manifest_digest"]), "asset manifest digest")
    datasets = document.get("datasets_on_asset_change", [])
    if (
        not isinstance(datasets, list)
        or any(
            not isinstance(dataset, str)
            or not SAFE_DATASET_RE.fullmatch(dataset)
            or dataset in {"all", "none"}
            for dataset in datasets
        )
        or len(set(datasets)) != len(datasets)
    ):
        raise ReleaseError("datasets_on_asset_change must contain explicit safe dataset names")
    if not datasets:
        raise ReleaseError("single-bundle releases require explicit datasets_on_asset_change")
    migrations = release_migration_records(document)
    for migration in migrations:
        if deployment_mode == "auto" and migration["kind"] == "baseline":
            raise ReleaseError("automatic code deployment manifests must exclude baseline migrations")
    return document


def verify_artifact(directory: Path, record: dict[str, Any], name: str) -> Path:
    path = directory / record["name"]
    if not path.is_file() or path.is_symlink():
        raise ReleaseError(f"{name} is missing or not a regular file: {path}")
    if path.stat().st_size != record["size"]:
        raise ReleaseError(f"{name} size differs from the manifest")
    if sha256_file(path) != record["sha256"]:
        raise ReleaseError(f"{name} digest differs from the manifest")
    return path


def verify_manifest_command(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = Path(args.manifest).resolve()
    document = validate_manifest(load_manifest(manifest_path), deployment_mode="auto")
    if args.sha and document["source_sha"] != require_sha(args.sha, "expected SHA"):
        raise ReleaseError("manifest source SHA does not match the expected SHA")
    verify_artifact(manifest_path.parent, document["release_bundle"], "release bundle")
    return document


def load_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        raise ReleaseError(f"deployment environment file is missing or unsafe: {path}")
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ReleaseError(f"invalid deployment environment entry on line {number}")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ReleaseError(f"invalid deployment environment name on line {number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _decode_dsn_component(value: str | None, field: str) -> str:
    if value is None or re.search(r"%(?![0-9A-Fa-f]{2})", value):
        raise ReleaseError(f"{field} is not a valid production PostgreSQL DSN")
    try:
        return urllib.parse.unquote(value, encoding="utf-8", errors="strict")
    except UnicodeError as exc:
        raise ReleaseError(f"{field} is not a valid production PostgreSQL DSN") from exc


def validate_postgres_dsn(
    value: str,
    field: str,
    *,
    expected_user: str,
    expected_password: str,
    expected_host: str,
    expected_port: int,
    expected_database: str,
) -> None:
    """Validate one exact production DSN without ever echoing its credentials."""

    try:
        parsed = urllib.parse.urlsplit(value)
        username = _decode_dsn_component(parsed.username, field)
        password = _decode_dsn_component(parsed.password, field)
        hostname = parsed.hostname
        port = parsed.port
        database = _decode_dsn_component(
            parsed.path[1:] if parsed.path.startswith("/") else None,
            field,
        )
    except ValueError as exc:
        raise ReleaseError(f"{field} is not a valid production PostgreSQL DSN") from exc
    if (
        parsed.scheme != "postgresql"
        or hostname != expected_host
        or port != expected_port
        or parsed.query
        or parsed.fragment
        or parsed.path.count("/") != 1
        or not parsed.path.startswith("/")
        or username != expected_user
        or password != expected_password
        or database != expected_database
    ):
        raise ReleaseError(f"{field} does not match the pinned production PostgreSQL identity")


def fsync_directory(path: Path) -> None:
    """Durably persist directory-entry changes for state files."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_durable_directory(path: Path, mode: int = 0o700) -> None:
    """Create a real directory and durably persist every new parent entry."""

    if path.exists() or path.is_symlink():
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ReleaseError(f"cannot inspect directory {path}") from exc
        if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
            raise ReleaseError(f"required directory is unsafe: {path}")
        return
    parent = path.parent
    if parent == path:
        raise ReleaseError(f"cannot create directory root {path}")
    ensure_durable_directory(parent, mode)
    try:
        path.mkdir(mode=mode)
    except FileExistsError:
        if not path.is_dir() or path.is_symlink():
            raise ReleaseError(f"required directory is unsafe: {path}")
        return
    os.chmod(path, mode)
    fsync_directory(path)
    fsync_directory(parent)


def fsync_regular_file(path: Path) -> None:
    """Persist one non-symlink regular file and its directory entry."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseError(f"cannot open durable file {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ReleaseError(f"durable file is not a regular file: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_directory(path.parent)


def durable_unlink(path: Path, *, missing_ok: bool = False) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    fsync_directory(path.parent)


def atomic_json(path: Path, document: dict[str, Any], mode: int = 0o600) -> None:
    ensure_durable_directory(path.parent)
    fd, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        fsync_directory(path.parent)
    finally:
        durable_unlink(temporary, missing_ok=True)


def atomic_text(path: Path, value: str, *, mode: int = 0o600) -> None:
    ensure_durable_directory(path.parent)
    fd, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        fsync_directory(path.parent)
    finally:
        durable_unlink(temporary, missing_ok=True)


def safe_extract_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:*") as source:
        members = source.getmembers()
        for member in members:
            relative = PurePosixPath(member.name)
            if not relative.parts and member.isdir() and member.name in {".", "./"}:
                continue
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ReleaseError(f"unsafe archive path: {member.name}")
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                raise ReleaseError(f"archive contains an unsupported entry: {member.name}")
        for member in members:
            relative = PurePosixPath(member.name)
            if not relative.parts and member.isdir() and member.name in {".", "./"}:
                continue
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                os.chmod(target, 0o755)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source_file = source.extractfile(member)
            if source_file is None:
                raise ReleaseError(f"cannot read archive entry: {member.name}")
            with source_file, target.open("wb") as output:
                shutil.copyfileobj(source_file, output)
            os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)


class ReleaseController:
    def __init__(
        self,
        root: Path,
        manifest_path: Path,
        mode: str,
        apply: bool,
    ) -> None:
        self.root = root.resolve()
        self.manifest_path = manifest_path.resolve()
        self.mode = mode
        self.apply = apply
        self.document = validate_manifest(
            load_manifest(self.manifest_path),
            deployment_mode=mode,
        )
        self.sha = self.document["source_sha"]
        self.ops = self.root / "ops"
        self.config_dir = self.ops / "config"
        self.release_dir = self.ops / "releases" / self.sha
        self.staging = self.ops / "releases" / f"{self.sha}.staging"
        self.candidate_dir = self.staging
        self.state_path = self.ops / "state" / "release-state.json"
        self.in_progress_path = self.ops / "state" / "deploy-in-progress.json"
        self.env_file = self.config_dir / "deploy.env"
        self.previous_state: dict[str, Any] = {}
        self.backup_path: Path | None = None
        self.database_changed = False
        self.worker_restart_deferred = False
        self.worker_drain_info: dict[str, Any] | None = None
        self.worker_previous_instance: str | None = None
        self.worker_base_python_identity: dict[str, Any] | None = None
        self.worker_toolchain_identity: dict[str, Any] | None = None
        self.bootstrap = False
        self.attempt_path: Path | None = None

    def ensure_root(self) -> None:
        if self.apply and self.root != PRODUCTION_ROOT:
            if os.environ.get("NEXPOLY_ALLOW_TEST_ROOT") != "1":
                raise ReleaseError(f"--apply is allowed only for {PRODUCTION_ROOT}")
        if self.apply and not self.root.is_dir():
            raise ReleaseError(f"production root does not exist: {self.root}")
        if self.apply:
            require_docker_compose_version(os.environ.copy())

    def plan(self) -> dict[str, Any]:
        return {
            "action": "deploy",
            "apply": self.apply,
            "mode": self.mode,
            "production_root": str(self.root),
            "source_sha": self.sha,
            "ci_run_id": self.document["ci_run_id"],
            "backend_image": self.document["images"]["backend"],
            "web_image": self.document["images"]["web"],
            "release_manifest_sha256": sha256_file(self.manifest_path),
            "release_bundle_sha256": (
                self.document["release_bundle"]["sha256"]
                if self.document.get("release_bundle")
                else None
            ),
            "staging": str(self.staging),
        }

    def create_attempt_path(self) -> Path:
        self.attempt_path = self.in_progress_path
        return self.in_progress_path

    def write_attempt(self, state: dict[str, Any]) -> None:
        if self.attempt_path is None:
            raise ReleaseError("deployment attempt path has not been initialized")
        atomic_json(self.attempt_path, state)

    @contextlib.contextmanager
    def deployment_lock(self):
        lock_path = self.ops / "state" / "deploy.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as stream:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ReleaseError("another production deployment holds deploy.lock") from exc
            yield

    def environment(self) -> dict[str, str]:
        for directory in (self.config_dir, self.ops / "state", self.ops / "releases", self.root / "backups"):
            if not directory.is_dir() or directory.is_symlink():
                raise ReleaseError(f"required production directory is missing or unsafe: {directory}")
            if directory.stat().st_mode & 0o077:
                raise ReleaseError(f"production directory must not grant group/other access: {directory}")
        private_files = [self.env_file, self.config_dir / "app.env"]
        if release_uses_worker(self.document):
            private_files.append(self.config_dir / "worker.env")
        for path in private_files:
            if not path.is_file() or path.is_symlink():
                raise ReleaseError(f"required private configuration file is missing or unsafe: {path}")
            metadata = path.stat()
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
                raise ReleaseError(f"private configuration must be owned by the deploy user and mode 0600: {path}")
        values = load_dotenv(self.env_file)
        forbidden_values = sorted(FORBIDDEN_DEPLOY_ENV_OVERRIDES.intersection(values))
        if forbidden_values:
            raise ReleaseError(
                "deploy.env contains forbidden process/runtime overrides: "
                + ", ".join(forbidden_values)
            )
        required = {
            "NEXPOLY_POSTGRES_USER",
            "NEXPOLY_POSTGRES_PASSWORD",
            "NEXPOLY_POSTGRES_DB",
            "APP_POSTGRES_DSN",
            "PI_POSTGRES_DSN",
            "LAB_DATA_POSTGRES_DSN",
            "NEXPOLY_ASSET_ROOT",
            "POLYTAO_ENABLED",
        }
        missing = sorted(key for key in required if not values.get(key))
        if missing:
            raise ReleaseError(f"deploy.env is missing required non-empty values: {', '.join(missing)}")
        if release_uses_worker(self.document):
            worker_base_keys = {
                "NEXPOLY_WORKER_BASE_PYTHON",
                "NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256",
                "NEXPOLY_WORKER_CONDA_EXE",
                "NEXPOLY_WORKER_GMX",
            }
            missing_worker_base = sorted(key for key in worker_base_keys if not values.get(key))
            if missing_worker_base:
                raise ReleaseError(
                    "deploy.env is missing frozen Worker base identity values: "
                    + ", ".join(missing_worker_base)
                )
        if values["NEXPOLY_POSTGRES_PASSWORD"] in {"polyprop", "nexpoly", "password"}:
            raise ReleaseError("production Postgres password is a known fixed default")
        if values["NEXPOLY_POSTGRES_DB"] != "nexpoly":
            raise ReleaseError(
                "production maintenance and release operations are hard-locked to database nexpoly"
            )
        postgres_identity = {
            "expected_user": values["NEXPOLY_POSTGRES_USER"],
            "expected_password": values["NEXPOLY_POSTGRES_PASSWORD"],
            "expected_host": "lab-postgres",
            "expected_port": 5432,
            "expected_database": values["NEXPOLY_POSTGRES_DB"],
        }
        for key in ("APP_POSTGRES_DSN", "PI_POSTGRES_DSN", "LAB_DATA_POSTGRES_DSN"):
            validate_postgres_dsn(values[key], key, **postgres_identity)
        if values.get("NEXPOLY_POSTGRES_PORT", "55432") != "55432":
            raise ReleaseError("production PostgreSQL host port must remain 55432")
        if values["POLYTAO_ENABLED"].strip().lower() != "true":
            raise ReleaseError("POLYTAO_ENABLED must be true for a full production release")
        configured_hooks = sorted(
            key for key in FORBIDDEN_DEPLOY_HOOKS if values.get(key) or os.environ.get(key)
        )
        if configured_hooks:
            raise ReleaseError(
                "custom production drain/job hooks are forbidden: " + ", ".join(configured_hooks)
            )
        if release_uses_worker(self.document):
            worker_values = load_dotenv(self.config_dir / "worker.env")
            forbidden_worker_values = sorted(
                FORBIDDEN_WORKER_ENV_OVERRIDES.intersection(worker_values)
            )
            if forbidden_worker_values:
                raise ReleaseError(
                    "worker.env contains forbidden process/runtime overrides: "
                    + ", ".join(forbidden_worker_values)
                )
            worker_dsn = worker_values.get("APP_POSTGRES_DSN")
            if not worker_dsn:
                raise ReleaseError("worker.env is missing required non-empty value: APP_POSTGRES_DSN")
            validate_postgres_dsn(
                worker_dsn,
                "worker.env APP_POSTGRES_DSN",
                expected_user=values["NEXPOLY_POSTGRES_USER"],
                expected_password=values["NEXPOLY_POSTGRES_PASSWORD"],
                expected_host="127.0.0.1",
                expected_port=55432,
                expected_database=values["NEXPOLY_POSTGRES_DB"],
            )
            expected_worker_values = {
                "MONOMER_MD_DEFAULT_STEPS": "300",
                "MONOMER_MD_MAX_STEPS": "300",
                "MONOMER_MD_MAX_ACTIVE_JOBS": "1",
                "MONOMER_MD_MAX_CONCURRENT_JOBS": "1",
                "BYTEFF2_ROOT": str(self.ops / "current-assets" / "byteff2"),
                "BYTEFF2_PYTHON": "/home/devuser/miniconda3/envs/byteff2-repro/bin/python",
                "PYTHONPATH": (
                    f"{self.ops / 'current'}:"
                    f"{self.ops / 'current-assets' / 'byteff2'}:"
                    f"{self.ops / 'current-assets' / 'byteff2' / 'submodules' / 'bytemol'}"
                ),
                "MONOMER_MD_PYTHON": str(self.ops / "current" / "worker-venv" / "bin" / "python"),
                "MONOMER_MD_JOB_ROOT": str(self.ops / "state" / "monomer-md-worker-runs"),
                "MONOMER_MD_WORKER_UDS": str(
                    self.ops / "state" / "monomer-md-worker-socket" / "worker.sock"
                ),
                "MONOMER_MD_WORKER_MODE": "real",
                "MONOMER_MD_GPU_BROKER_ENABLED": "false",
                "MONOMER_MD_GPU_BROKER_ENVIRONMENT": "prod",
                "MONOMER_MD_GPU_BROKER_SOCKET_PATH": str(
                    self.ops / "state" / "gpu-resource" / "broker.sock"
                ),
                "MONOMER_MD_GPU_MPS_PIPE_ROOT": str(
                    self.ops / "state" / "gpu-resource"
                ),
                "MONOMER_MD_GPU_BROKER_WAIT_TIMEOUT_SECONDS": "600",
                "MONOMER_MD_GPU_BROKER_HEARTBEAT_INTERVAL_SECONDS": "5",
            }
            for key, expected in expected_worker_values.items():
                if worker_values.get(key) != expected:
                    raise ReleaseError(
                        f"{key} must equal the pinned production Worker value {expected}"
                    )
        configured_asset_root = Path(values["NEXPOLY_ASSET_ROOT"])
        managed_asset_pointer = self.ops / "current-assets"
        if configured_asset_root != managed_asset_pointer:
            raise ReleaseError(
                f"NEXPOLY_ASSET_ROOT must be the managed production pointer {managed_asset_pointer}"
            )
        try:
            raw_current_target = Path(os.readlink(managed_asset_pointer))
        except OSError as exc:
            raise ReleaseError("managed production asset pointer is unavailable") from exc
        current_digest = require_digest(
            f"sha256:{raw_current_target.name}",
            "current managed asset digest",
        )
        current_asset_root, actual_asset_digest, current_byteff2_commit = (
            inspect_managed_asset_pointer(managed_asset_pointer, current_digest)
        )
        expected_digest = self.document.get("asset_manifest_digest") or actual_asset_digest
        target_asset_root, target_digest, target_byteff2_commit = inspect_managed_asset_release(
            expected_digest
        )
        self.document["current_asset_manifest_digest"] = actual_asset_digest
        self.document["current_byteff2_commit"] = current_byteff2_commit
        self.document["current_asset_root"] = str(current_asset_root)
        self.document["resolved_asset_manifest_digest"] = target_digest
        self.document["resolved_byteff2_commit"] = target_byteff2_commit
        self.document["resolved_asset_root"] = str(target_asset_root)
        environment = os.environ.copy()
        environment.update(values)
        environment.update(
            {
                "COMPOSE_PROJECT_NAME": "nexpoly",
                "NEXPOLY_BACKEND_IMAGE": self.document["images"]["backend"],
                "NEXPOLY_WEB_IMAGE": self.document["images"]["web"],
                "NEXPOLY_APP_ENV_FILE": str(self.config_dir / "app.env"),
                "NEXPOLY_ASSET_MANIFEST_DIGEST": target_digest,
                "NEXPOLY_POSTGRES_PORT": "55432",
                "DEPLOYMENT_DRAIN_ENABLED": "true",
            }
        )
        raw_minimum = values.get("NEXPOLY_MIN_FREE_BYTES", str(10 * 1024**3))
        try:
            minimum_free = int(raw_minimum)
        except ValueError as exc:
            raise ReleaseError("NEXPOLY_MIN_FREE_BYTES must be an integer") from exc
        if minimum_free < 1024**3 or minimum_free > 1024**4:
            raise ReleaseError("NEXPOLY_MIN_FREE_BYTES must be between 1 GiB and 1 TiB")
        free = shutil.disk_usage(self.root).free
        if free < minimum_free:
            raise ReleaseError(f"insufficient free space for deployment: {free} bytes available")
        environment["NEXPOLY_RESOLVED_FREE_BYTES"] = str(free)
        environment["NEXPOLY_CONFIGURED_ASSET_ROOT"] = str(managed_asset_pointer)
        return environment

    def run(self, command: list[str], *, env: dict[str, str], stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> None:
        display = shlex.join(command)
        print(f"[release-controller] {display}")
        subprocess.run(command, cwd=self.root, env=env, stdin=stdin, stdout=stdout, check=True)

    def bootstrap_hook_command(
        self,
        environment: dict[str, str],
        key: str,
    ) -> list[str]:
        command = shlex.split(environment[key])
        if len(command) != 1:
            raise ReleaseError(f"{key} must name exactly one audited executable")
        executable = Path(command[0])
        try:
            metadata = executable.lstat()
        except OSError as exc:
            raise ReleaseError(f"{key} executable is missing") from exc
        if (
            not executable.is_absolute()
            or not stat.S_ISREG(metadata.st_mode)
            or executable.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ReleaseError(
                f"{key} must be an absolute deploy-user-owned mode-0700 regular file"
            )
        return command

    def run_bootstrap_quiesce(self, environment: dict[str, str]) -> dict[str, Any]:
        """Run the audited legacy hook and require complete zero-work evidence."""

        command = self.bootstrap_hook_command(
            environment,
            "NEXPOLY_BOOTSTRAP_QUIESCE_COMMAND",
        )
        print(f"[release-controller] {shlex.join(command)}")
        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                env=environment,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            raise ReleaseError("bootstrap quiesce hook failed") from exc
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ReleaseError("bootstrap quiesce hook must print exactly one JSON object") from exc
        required_fields = {"ingress_isolated", "active_jobs", "active_total"}
        if not isinstance(payload, dict) or frozenset(payload) not in {
            frozenset(required_fields),
            frozenset((*required_fields, "active_jobs_schema_version")),
        }:
            raise ReleaseError("bootstrap quiesce evidence has an invalid shape")
        if payload["ingress_isolated"] is not True:
            raise ReleaseError("bootstrap quiesce did not prove isolated ingress")
        try:
            total = validated_active_total(payload, set(ACTIVE_JOB_CATEGORIES_V1))
        except ReleaseError as exc:
            detail = str(exc)
            if "does not match" in detail:
                raise ReleaseError(
                    "bootstrap quiesce active_total does not match category counts"
                ) from exc
            if "job categories" in detail:
                raise ReleaseError(
                    "bootstrap quiesce evidence does not cover every active-job category"
                ) from exc
            if "category" in detail:
                raise ReleaseError("bootstrap quiesce count is invalid") from exc
            raise
        if total != 0:
            raise ReleaseError("bootstrap quiesce found active work; refusing to create a backup")
        schema_version = payload.get("active_jobs_schema_version", 1)
        categories = (
            ACTIVE_JOB_CATEGORIES_V2 if schema_version == 2 else ACTIVE_JOB_CATEGORIES_V1
        )
        return {
            "active_jobs_schema_version": schema_version,
            "ingress_isolated": True,
            "active_jobs": {
                category: payload["active_jobs"][category] for category in categories
            },
            "active_total": 0,
        }

    def run_bootstrap_rollback(self, environment: dict[str, str]) -> dict[str, Any]:
        """Require audited proof that the complete legacy runtime was restored."""

        expected_identity = require_digest(
            environment.get("NEXPOLY_BOOTSTRAP_LEGACY_RUNTIME_SHA256", ""),
            "bootstrap legacy runtime evidence digest",
        )
        command = self.bootstrap_hook_command(
            environment,
            "NEXPOLY_BOOTSTRAP_ROLLBACK_COMMAND",
        )
        print(f"[release-controller] {shlex.join(command)}")
        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                env=environment,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            raise ReleaseError("bootstrap legacy-runtime rollback hook failed") from exc
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ReleaseError(
                "bootstrap rollback hook must print exactly one JSON object"
            ) from exc
        expected_fields = {
            "schema_version",
            "legacy_runtime_restored",
            "backend_image_id",
            "web_image_id",
            "worker_unit_sha256",
            "backend_healthy",
            "web_healthy",
            "worker_healthy",
            "ingress_restored",
        }
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            raise ReleaseError("bootstrap rollback evidence has an invalid shape")
        if payload.get("schema_version") != 1:
            raise ReleaseError("bootstrap rollback evidence schema is unsupported")
        identity_material: dict[str, str] = {}
        for key in ("backend_image_id", "web_image_id", "worker_unit_sha256"):
            value = payload.get(key)
            if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
                raise ReleaseError(f"bootstrap rollback evidence has an invalid {key}")
            identity_material[key] = value
        if canonical_json_digest(identity_material) != expected_identity:
            raise ReleaseError("bootstrap rollback restored a different legacy runtime identity")
        for key in (
            "legacy_runtime_restored",
            "backend_healthy",
            "web_healthy",
            "worker_healthy",
            "ingress_restored",
        ):
            if payload.get(key) is not True:
                raise ReleaseError(f"bootstrap rollback did not prove {key}")
        return payload

    def run_migrations(self, environment: dict[str, str], *, mode: str = "expand") -> list[str]:
        if mode not in {"bootstrap", "bootstrap-expand", "expand", "contract-0012"}:
            raise ReleaseError(
                "migration mode must be bootstrap, bootstrap-expand, expand, or contract-0012"
            )
        policy_path = (
            self.candidate_dir
            / "backend"
            / "migrations"
            / "postgres"
            / "manifest.json"
        )
        canonical_records = release_migrations_from_policy_manifest(
            policy_path,
            include_baseline=True,
        )
        release_records = release_migration_records(self.document)
        canonical_release_records = [
            record for record in canonical_records if record["kind"] != "baseline"
        ]
        if self.document.get("schema_version", 1) == 2:
            if release_records != canonical_release_records:
                raise ReleaseError(
                    "release manifest migrations differ from the candidate canonical policy"
                )
        else:
            release_projection = [
                (record["version"], record["kind"]) for record in release_records
            ]
            canonical_projection = [
                (record["version"], record["kind"])
                for record in canonical_release_records
            ]
            if release_projection != canonical_projection:
                raise ReleaseError(
                    "release manifest migrations differ from the candidate canonical policy"
                )
        previous_history = self.previous_state.get("migrations", [])
        if (
            not isinstance(previous_history, list)
            or any(
                not isinstance(version, str)
                or SAFE_MIGRATION_RE.fullmatch(version) is None
                for version in previous_history
            )
            or len(set(previous_history)) != len(previous_history)
        ):
            raise ReleaseError("current release state contains an invalid migration history")
        previous_versions = set(previous_history)

        # Bind the immutable release manifest to the candidate SQL before the
        # migration container receives an opportunity to execute any DDL.
        command = self.compose(
            self.candidate_dir,
            "run", "--rm", "--no-deps", "postgres-init",
            "python", "-m", "app.postgres_migrations", "--mode", mode,
        )
        print(f"[release-controller] {shlex.join(command)}")
        result = subprocess.run(
            command,
            cwd=self.root,
            env=environment,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )

        applied: list[str] = []
        output_records: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for line in result.stdout.splitlines():
            if not line:
                raise ReleaseError("migration runner emitted a malformed output record")
            fields = line.split("\t")
            if (
                len(fields) != 3
                or fields[1] not in {"applied", "skipped"}
                or SAFE_MIGRATION_RE.fullmatch(fields[0]) is None
                or MIGRATION_CHECKSUM_RE.fullmatch(fields[2]) is None
            ):
                raise ReleaseError("migration runner emitted a malformed output record")
            version, status, checksum = fields
            if version in seen:
                raise ReleaseError(
                    f"migration runner emitted duplicate output for {version}"
                )
            seen.add(version)
            output_records.append((version, status, checksum))

        expected_versions = [record["version"] for record in canonical_records]
        if [record[0] for record in output_records] != expected_versions:
            raise ReleaseError(
                "migration output does not exactly match the canonical migration set and order"
            )
        canonical_by_version = {
            record["version"]: record for record in canonical_records
        }
        for version, status, checksum in output_records:
            expected_record = canonical_by_version[version]
            if checksum != expected_record["checksum"]:
                raise ReleaseError(
                    f"migration runner checksum differs from canonical SQL for {version}"
                )
            if expected_record["kind"] == "baseline" and status != "skipped":
                raise ReleaseError("the canonical baseline may only be reported as skipped")
            if mode == "contract-0012":
                if (
                    version != POLYTAO_SCHEMA_COMPATIBILITY_FLOOR
                    and status != "skipped"
                ):
                    raise ReleaseError(
                        "restricted 0012 migration output applied a non-target migration"
                    )
            elif expected_record["kind"] == "contract" and status != "skipped":
                raise ReleaseError(
                    f"migration output applied contract {version} outside maintenance"
                )
            if status == "applied":
                if version in previous_versions:
                    raise ReleaseError(
                        f"migration output re-applied release-state migration {version}"
                    )
                applied.append(version)
        return applied

    def assert_still_current_main(self, environment: dict[str, str]) -> None:
        """Fail closed inside deploy.lock if an auto/bootstrap release was superseded."""
        probe_environment = environment.copy()
        probe_environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    "credential.helper=",
                    "ls-remote",
                    "--exit-code",
                    MAIN_REPOSITORY_URL,
                    "refs/heads/main",
                ],
                cwd=self.root,
                env=probe_environment,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseError("cannot verify the current main SHA inside the deployment lock") from exc
        lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
        if (
            result.returncode != 0
            or len(lines) != 1
            or len(lines[0]) != 2
            or lines[0][1] != "refs/heads/main"
            or not SHA_RE.fullmatch(lines[0][0])
        ):
            raise ReleaseError("repository main SHA probe returned an invalid result")
        current_main = lines[0][0]
        if current_main != self.sha:
            raise DeploymentSuperseded(
                f"release {self.sha} was superseded by main {current_main}; production was not switched"
            )

    def refresh_analytics_snapshot(
        self,
        environment: dict[str, str],
        *,
        release: Path | None = None,
        source_sha: str | None = None,
    ) -> None:
        """Persist the governed analytics snapshot before strict runtime health."""

        snapshot_release = release or self.candidate_dir
        snapshot_sha = require_sha(source_sha or self.sha, "analytics snapshot source SHA")
        self.run(
            self.compose(
                snapshot_release,
                "run",
                "--rm",
                "--no-deps",
                "postgres-init",
                "python",
                "-m",
                "app.generate_database_analytics_snapshot",
                "--source-sha",
                snapshot_sha,
            ),
            env=environment,
        )

    def compose(self, release: Path, *arguments: str) -> list[str]:
        return [
            "docker", "compose", "-p", "nexpoly",
            "-f", str(release / "docker-compose.yml"),
            "-f", str(release / "docker-compose.prod.yml"),
            "--env-file", str(self.env_file),
            *arguments,
        ]

    def prepare_staging(self, environment: dict[str, str]) -> None:
        if self.release_dir.exists():
            raise ReleaseError(f"release directory already exists for {self.sha}")
        if self.staging.exists():
            raise ReleaseError(f"staging directory already exists for {self.sha}")
        archive = verify_artifact(
            self.manifest_path.parent,
            self.document["release_bundle"],
            "release bundle",
        )
        safe_extract_tar(archive, self.staging)
        for required in ("docker-compose.yml", "docker-compose.prod.yml"):
            if not (self.staging / required).is_file():
                raise ReleaseError(f"control archive is missing {required}")
        shutil.copy2(self.manifest_path, self.staging / "release-manifest.json")
        os.chmod(self.staging / "release-manifest.json", 0o600)
        self.prepare_worker(environment)
        rendered = subprocess.run(
            self.compose(self.staging, "config", "--images"), cwd=self.root, env=environment,
            check=True, text=True, stdout=subprocess.PIPE,
        ).stdout.splitlines()
        application_images = {self.document["images"]["backend"], self.document["images"]["web"]}
        if not application_images.issubset(set(rendered)):
            raise ReleaseError("rendered production Compose does not contain both manifest image digests")
        if any(image.startswith(("nexpoly-backend:", "nexpoly-nginx:")) or image.endswith(":latest") for image in rendered):
            raise ReleaseError("rendered production Compose contains a mutable application image")
        config_result = subprocess.run(
            self.compose(self.staging, "config", "--format", "json"),
            cwd=self.root,
            env=environment,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        try:
            config = json.loads(config_result.stdout)
            services = config["services"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ReleaseError("production Compose did not render a valid service document") from exc
        expected_services = {"postgres-init", "backend", "nginx", "lab-postgres"}
        if not isinstance(services, dict) or set(services) != expected_services:
            raise ReleaseError("production Compose service set differs from the approved four services")
        expected_images = {
            "postgres-init": self.document["images"]["backend"],
            "backend": self.document["images"]["backend"],
            "nginx": self.document["images"]["web"],
        }
        for service, definition in services.items():
            if not isinstance(definition, dict) or definition.get("build") is not None:
                raise ReleaseError(f"production Compose service contains a build context: {service}")
            image = definition.get("image")
            if service in expected_images and image != expected_images[service]:
                raise ReleaseError(f"production Compose image differs from the manifest: {service}")
            if not isinstance(image, str) or "@sha256:" not in image or image.endswith(":latest"):
                raise ReleaseError(f"production Compose image is not digest-pinned: {service}")
        postgres_ports = services["lab-postgres"].get("ports")
        if not isinstance(postgres_ports, list) or len(postgres_ports) != 1:
            raise ReleaseError("production PostgreSQL must publish exactly one loopback port")
        port = postgres_ports[0]
        if not isinstance(port, dict) or port.get("host_ip") != "127.0.0.1" or port.get("target") != 5432:
            raise ReleaseError("production PostgreSQL must bind target 5432 only on 127.0.0.1")

    @staticmethod
    def worker_bundle_dir(release: Path) -> Path:
        nested = release / "worker-bundle"
        return nested if nested.is_dir() else release

    def worker_requirement_document(self, bundle: Path) -> dict[str, Any]:
        root_lock = bundle / "requirements.lock"
        lock = (
            root_lock
            if root_lock.is_file()
            else bundle / "workers" / "monomer_md_worker" / "requirements.lock"
        )
        if not lock.is_file():
            raise ReleaseError("worker archive must contain a Monomer-MD requirements lock")
        return {
            "schema_version": 1,
            "requirements": worker_lock_requirements(lock, bundle),
        }

    def verify_worker_venv(
        self,
        venv: Path,
        expectation_path: Path,
        environment: dict[str, str],
    ) -> None:
        self.run(
            [
                str(venv / "bin" / "python"),
                "-I",
                "-c",
                WORKER_VENV_VERIFY_PROGRAM,
                str(venv),
                str(expectation_path),
            ],
            env=environment,
        )

    def prepare_worker(self, environment: dict[str, str]) -> None:
        nested_bundle = self.staging / "worker-bundle"
        bundle = nested_bundle if nested_bundle.is_dir() else self.staging
        wheelhouse = bundle / "wheelhouse"
        root_lock = bundle / "requirements.lock"
        locks = [root_lock] if root_lock.is_file() else [
            bundle / "workers" / "monomer_md_worker" / "requirements.lock",
        ]
        if not wheelhouse.is_dir() or any(not lock.is_file() for lock in locks):
            raise ReleaseError(
                "worker archive must contain wheelhouse/ and a Monomer-MD requirements lock"
            )
        base_python = environment.get("NEXPOLY_WORKER_BASE_PYTHON", "")
        expected_base_identity = environment.get(
            "NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256",
            "",
        )
        if not expected_base_identity:
            raise ReleaseError(
                "NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256 must pin the frozen base Python"
            )
        before_identity = inspect_worker_base_python(
            base_python,
            expected_base_identity,
            environment,
        )
        before_toolchain = inspect_worker_toolchain(before_identity, environment)
        venv = self.staging / "worker-venv"
        self.run(
            [
                before_identity["resolved_path"],
                "-m",
                "venv",
                "--system-site-packages",
                str(venv),
            ],
            env=environment,
        )
        command = [
            str(venv / "bin" / "python"), "-m", "pip", "install",
            "--no-index", "--require-hashes", "--ignore-installed",
            "--only-binary=:all:", "--find-links", str(wheelhouse),
        ]
        for lock in locks:
            command.extend(["-r", str(lock)])
        self.run(command, env=environment)
        expectation_path = self.staging / "worker-lock-requirements.json"
        atomic_json(expectation_path, self.worker_requirement_document(bundle))
        self.verify_worker_venv(venv, expectation_path, environment)
        after_identity = inspect_worker_base_python(
            base_python,
            expected_base_identity,
            environment,
        )
        if after_identity != before_identity:
            raise ReleaseError("frozen Worker base Python changed while preparing release venv")
        after_toolchain = inspect_worker_toolchain(after_identity, environment)
        if after_toolchain != before_toolchain:
            raise ReleaseError("frozen Worker Conda/GROMACS identity changed while preparing release venv")
        self.worker_base_python_identity = after_identity
        self.worker_toolchain_identity = after_toolchain
        atomic_json(
            self.staging / "worker-base-python-identity.json",
            after_identity,
        )
        atomic_json(
            self.staging / "worker-toolchain-identity.json",
            after_toolchain,
        )

    def verify_image_labels(self, environment: dict[str, str]) -> None:
        for role, image in self.document["images"].items():
            result = subprocess.run(
                [
                    "docker", "image", "inspect", "--format",
                    '{{ index .Config.Labels "org.opencontainers.image.revision" }}',
                    image,
                ],
                cwd=self.root,
                env=environment,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            if result.stdout.strip() != self.sha:
                raise ReleaseError(f"{role} OCI revision label does not match release source SHA")

    def resolve_single_running_container(
        self,
        release: Path,
        service: str,
        environment: dict[str, str],
    ) -> str:
        result = subprocess.run(
            self.compose(release, "ps", "-q", service),
            cwd=self.root,
            env=environment,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        containers = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(containers) != 1:
            raise ReleaseError(
                f"expected exactly one running {service} container, found {len(containers)}"
            )
        return containers[0]

    def verify_runtime_images(self, environment: dict[str, str]) -> None:
        for service, role in (("backend", "backend"), ("nginx", "web")):
            container = self.resolve_single_running_container(
                self.release_dir,
                service,
                environment,
            )
            configured_image = subprocess.run(
                ["docker", "inspect", "--format", "{{.Config.Image}}", container],
                cwd=self.root,
                env=environment,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            if configured_image != self.document["images"][role]:
                raise ReleaseError(f"running {service} image does not match the release digest")

    def verify_postgres_loopback(
        self,
        release: Path,
        environment: dict[str, str],
    ) -> None:
        container = self.resolve_single_running_container(
            release,
            "lab-postgres",
            environment,
        )
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{json .NetworkSettings.Ports}}",
                container,
            ],
            cwd=self.root,
            env=environment,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        try:
            ports = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ReleaseError("running PostgreSQL port bindings are invalid") from exc
        bindings = ports.get("5432/tcp") if isinstance(ports, dict) else None
        expected_port = environment.get("NEXPOLY_POSTGRES_PORT", "55432")
        if (
            not isinstance(bindings, list)
            or len(bindings) != 1
            or not isinstance(bindings[0], dict)
            or bindings[0].get("HostIp") != "127.0.0.1"
            or bindings[0].get("HostPort") != expected_port
        ):
            raise ReleaseError(
                "running PostgreSQL must publish exactly "
                f"127.0.0.1:{expected_port}->5432"
            )

    def validate_current_runtime(self, environment: dict[str, str]) -> None:
        previous_sha = self.previous_state.get("source_sha")
        if not isinstance(previous_sha, str) or not SHA_RE.fullmatch(previous_sha):
            raise ReleaseError("current release state does not contain a valid source SHA")
        current = self.ops / "current"
        previous = self.ops / "releases" / previous_sha
        try:
            if not current.is_symlink() or current.resolve(strict=True) != previous.resolve(strict=True):
                raise ReleaseError("ops/current does not match release-state.json")
        except OSError as exc:
            raise ReleaseError("cannot resolve the current production release") from exc
        manifest = validate_manifest(load_manifest(previous / "release-manifest.json"), deployment_mode="auto")
        compatibility_floor = self.previous_state.get("schema_compatibility_floor")
        assert_release_supports_schema_floor(manifest, compatibility_floor)
        if self.previous_state.get("backend_image") != manifest["images"]["backend"]:
            raise ReleaseError("current Backend state differs from its release manifest")
        if self.previous_state.get("web_image") != manifest["images"]["web"]:
            raise ReleaseError("current Web state differs from its release manifest")
        if self.previous_state.get("asset_manifest_digest") != self.document.get("current_asset_manifest_digest"):
            raise ReleaseError("current asset pin differs from release state")
        if self.previous_state.get("byteff2_commit") != self.document.get("current_byteff2_commit"):
            raise ReleaseError("current ByteFF2 commit differs from release state")
        # Prove the complete rollback target before staging or mutating the
        # database.  /health alone cannot detect a missing hashed Web asset.
        self.public_web_static_smoke(environment)
        if release_uses_worker(manifest):
            identity_path = previous / "worker-base-python-identity.json"
            if not identity_path.is_file() or identity_path.is_symlink():
                raise ReleaseError("current release is missing its Worker base Python identity")
            identity = validate_worker_base_identity(load_manifest(identity_path))
            state_identity = validate_worker_base_identity(
                self.previous_state.get("worker_base_python")
            )
            if identity != state_identity:
                raise ReleaseError(
                    "current Worker base Python identity differs between release and state"
                )
            configured_python = environment.get("NEXPOLY_WORKER_BASE_PYTHON", "")
            configured_pin = environment.get(
                "NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256",
                "",
            )
            if configured_python != identity["configured_path"]:
                raise ReleaseError("current Worker base Python path differs from deploy.env")
            actual_identity = inspect_worker_base_python(
                configured_python,
                configured_pin,
                environment,
            )
            if actual_identity != identity:
                raise ReleaseError("current Worker base Python identity changed")
            toolchain_path = previous / "worker-toolchain-identity.json"
            if not toolchain_path.is_file() or toolchain_path.is_symlink():
                raise ReleaseError("current release is missing its Worker Conda/GROMACS identity")
            toolchain = validate_worker_toolchain_identity(load_manifest(toolchain_path))
            state_toolchain = validate_worker_toolchain_identity(
                self.previous_state.get("worker_toolchain")
            )
            if toolchain != state_toolchain:
                raise ReleaseError(
                    "current Worker Conda/GROMACS identity differs between release and state"
                )
            if inspect_worker_toolchain(actual_identity, environment) != toolchain:
                raise ReleaseError("current Worker Conda/GROMACS identity changed")
        self.run(
            self.compose(
                previous,
                "exec", "-T", "backend",
                "python", "-m", "app.postgres_preflight", "--mode", "runtime", "--strict",
                "--expected-source-sha", previous_sha,
            ),
            env=environment,
        )
        for raw_url in environment.get("NEXPOLY_HEALTH_URLS", "http://127.0.0.1:9000/health").split(","):
            url = raw_url.strip()
            if not url:
                continue
            try:
                with urllib.request.urlopen(url, timeout=20) as response:
                    if response.status < 200 or response.status >= 300:
                        raise ReleaseError(f"current health endpoint returned HTTP {response.status}: {url}")
            except OSError as exc:
                raise ReleaseError(f"current runtime is unhealthy: {url}: {exc}") from exc
        for service, role in (("backend", "backend"), ("nginx", "web")):
            container = self.resolve_single_running_container(previous, service, environment)
            configured_image = subprocess.run(
                ["docker", "inspect", "--format", "{{.Config.Image}}", container],
                cwd=self.root, env=environment, check=True, text=True, stdout=subprocess.PIPE,
            ).stdout.strip()
            if configured_image != manifest["images"][role]:
                raise ReleaseError(f"current {service} container differs from release state")
        self.verify_postgres_loopback(previous, environment)
        if release_uses_worker(manifest):
            worker = self.worker_request(environment, "GET", "/health")
            self.assert_worker_runtime_identity(worker, previous)
            if worker.get("status") != "ok" or worker.get("runtime_ready") is not True:
                raise ReleaseError("current monomer MD Worker is unhealthy")

    def drain(self, environment: dict[str, str], enabled: bool) -> None:
        operation = "drain" if enabled else "resume"
        release = self.candidate_dir
        # The postgres-init service already receives APP_POSTGRES_DSN through
        # its environment.  Never copy the credential into argv: run() logs
        # the command and the host exposes process arguments to local users.
        cli = ["python", "-m", "app.deployment_control_cli", operation]
        if enabled:
            cli.extend(["--actor", "release-controller", "--release-sha", self.sha, "--reason", f"deploying release {self.sha}"])
        else:
            cli.extend(["--actor", "release-controller", "--release-sha", self.sha])
        self.run(self.compose(release, "run", "--rm", "--no-deps", "postgres-init", *cli), env=environment)

    def worker_request(self, environment: dict[str, str], method: str, path: str) -> dict[str, Any]:
        socket_path = environment.get(
            "MONOMER_MD_WORKER_UDS",
            str(self.ops / "state" / "monomer-md-worker-socket" / "worker.sock"),
        )
        completed = subprocess.run(
            [
                "curl", "--fail", "--silent", "--show-error", "--max-time", "30",
                "--request", method, "--unix-socket", socket_path,
                f"http://monomer-md-worker{path}",
            ],
            cwd=self.root,
            env=environment,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            raise ReleaseError(f"monomer MD worker {path} request failed")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"monomer MD worker {path} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ReleaseError(f"monomer MD worker {path} returned an invalid shape")
        return payload

    def wait_for_worker_health(
        self,
        environment: dict[str, str],
        *,
        expected_release: Path,
        previous_instance_id: str | None = None,
    ) -> dict[str, Any]:
        timeout = int(environment.get("NEXPOLY_WORKER_HEALTH_TIMEOUT_SECONDS", "180"))
        if timeout < 1 or timeout > 600:
            raise ReleaseError("NEXPOLY_WORKER_HEALTH_TIMEOUT_SECONDS must be between 1 and 600")
        deadline = time.monotonic() + timeout
        last_error = "worker did not answer"
        while True:
            try:
                worker = self.worker_request(environment, "GET", "/health")
                self.assert_worker_runtime_identity(worker, expected_release)
                instance_id = worker.get("worker_instance_id")
                instance_changed = (
                    previous_instance_id is None
                    or (isinstance(instance_id, str) and instance_id and instance_id != previous_instance_id)
                )
                if (
                    worker.get("status") == "ok"
                    and worker.get("runtime_ready") is True
                    and worker.get("accepting_jobs") is True
                    and worker.get("default_steps") == 300
                    and worker.get("max_steps") == 300
                    and instance_changed
                ):
                    return worker
                last_error = "worker reported a degraded runtime"
            except ReleaseError as exc:
                last_error = str(exc)
            if time.monotonic() >= deadline:
                raise ReleaseError(f"monomer MD Worker health timed out: {last_error}")
            time.sleep(5)

    def assert_worker_runtime_identity(
        self,
        worker: dict[str, Any],
        expected_release: Path,
    ) -> None:
        """Bind Worker health to the exact release source tree and venv."""

        if not expected_release.is_dir() or expected_release.is_symlink():
            raise ReleaseError("expected Worker release directory is missing or unsafe")
        try:
            release_root = expected_release.resolve(strict=True)
            releases_root = (self.ops / "releases").resolve(strict=True)
        except OSError as exc:
            raise ReleaseError("cannot resolve the expected Worker release") from exc
        if release_root.parent != releases_root or SHA_RE.fullmatch(release_root.name) is None:
            raise ReleaseError("expected Worker release is outside ops/releases/<source-sha>")

        manifest_path = release_root / "release-manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise ReleaseError("expected Worker release manifest is missing or unsafe")
        manifest = validate_manifest(
            load_manifest(manifest_path),
            deployment_mode="bootstrap",
        )
        expected_sha = manifest["source_sha"]
        if expected_sha != release_root.name:
            raise ReleaseError("expected Worker release directory differs from its manifest SHA")

        venv = release_root / "worker-venv"
        if not venv.is_dir() or venv.is_symlink():
            raise ReleaseError("expected Worker release venv is missing or unsafe")
        try:
            venv_prefix = venv.resolve(strict=True)
        except OSError as exc:
            raise ReleaseError("cannot resolve the expected Worker release venv") from exc

        if worker.get("source_sha") != expected_sha:
            raise ReleaseError("monomer MD Worker source SHA differs from the expected release")
        if worker.get("source_root") != str(release_root):
            raise ReleaseError("monomer MD Worker source root differs from the expected release")
        if worker.get("venv_prefix") != str(venv_prefix):
            raise ReleaseError("monomer MD Worker venv differs from the expected release")
        base_identity_path = release_root / "worker-base-python-identity.json"
        if not base_identity_path.is_file() or base_identity_path.is_symlink():
            raise ReleaseError("expected Worker base Python identity is missing or unsafe")
        base_identity = validate_worker_base_identity(load_manifest(base_identity_path))
        executable = worker.get("python_executable")
        if executable != base_identity["resolved_path"]:
            raise ReleaseError(
                "monomer MD Worker Python executable differs from its pinned base identity"
            )

    def drain_worker(self, environment: dict[str, str]) -> dict[str, Any]:
        health = self.worker_request(environment, "GET", "/health")
        active_jobs = health.get("active_jobs")
        if isinstance(active_jobs, bool) or not isinstance(active_jobs, int) or active_jobs < 0:
            raise ReleaseError("monomer MD worker did not report a valid active job count")
        instance_id = health.get("worker_instance_id")
        if instance_id is not None and not isinstance(instance_id, str):
            raise ReleaseError("monomer MD worker reported an invalid instance ID")
        drained = self.worker_request(environment, "POST", "/drain")
        drained_active = drained.get("active_jobs")
        if (
            drained.get("status") != "draining"
            or isinstance(drained_active, bool)
            or not isinstance(drained_active, int)
            or drained_active < 0
        ):
            raise ReleaseError("monomer MD worker returned an invalid drain response")
        return {"supported": True, "active_jobs": drained_active, "worker_instance_id": instance_id}

    def resume_worker(self, environment: dict[str, str]) -> None:
        resumed = self.worker_request(environment, "POST", "/resume")
        active_jobs = resumed.get("active_jobs")
        if (
            resumed.get("status") != "ready"
            or isinstance(active_jobs, bool)
            or not isinstance(active_jobs, int)
            or active_jobs < 0
            or active_jobs > 1
        ):
            raise ReleaseError("monomer MD worker returned an invalid resume response")
        # The Worker has a single execution slot.  Resuming admission while an
        # existing job occupies that slot legitimately reports ready but not
        # accepting; an idle Worker must immediately accept new work.
        expected_accepting_jobs = active_jobs == 0
        if resumed.get("accepting_jobs") is not expected_accepting_jobs:
            raise ReleaseError("monomer MD worker returned an invalid resume response")

    def wait_for_jobs(self, environment: dict[str, str], *, ignore_monomer_md: bool = False) -> None:
        timeout = int(environment.get("NEXPOLY_DRAIN_TIMEOUT_SECONDS", "1800"))
        if timeout < 1 or timeout > 3600:
            raise ReleaseError("NEXPOLY_DRAIN_TIMEOUT_SECONDS must be between 1 and 3600")
        deadline = time.monotonic() + timeout
        while True:
            if self.bootstrap:
                result = subprocess.run(
                    self.compose(
                        self.candidate_dir, "run", "--rm", "--no-deps", "postgres-init",
                        "python", "-m", "app.deployment_control_cli", "status",
                    ),
                    cwd=self.root, env=environment, check=True, text=True, stdout=subprocess.PIPE,
                )
                try:
                    payload = json.loads(result.stdout)
                    active = validated_active_total(
                        payload,
                        {"monomer_md", "online_knowledge"},
                        ignore_monomer_md=ignore_monomer_md,
                    )
                except (ReleaseError, json.JSONDecodeError) as exc:
                    raise ReleaseError("bootstrap persistent deployment status is incomplete") from exc
            else:
                probe = (
                    "import json,urllib.request; "
                    "data=json.load(urllib.request.urlopen('http://127.0.0.1:8000/internal/deployment/status',timeout=10)); "
                    "print(json.dumps(data,separators=(',',':')))"
                )
                result = subprocess.run(
                    self.compose(self.candidate_dir, "exec", "-T", "backend", "python", "-c", probe),
                    cwd=self.root, env=environment, check=True, text=True, stdout=subprocess.PIPE,
                )
                try:
                    payload = json.loads(result.stdout)
                    active = validated_active_total(
                        payload,
                        set(ACTIVE_JOB_CATEGORIES),
                        ignore_monomer_md=ignore_monomer_md,
                    )
                except (ReleaseError, json.JSONDecodeError) as exc:
                    raise ReleaseError("internal deployment status is incomplete; refusing to stop workers") from exc
            if active == 0:
                return
            if time.monotonic() >= deadline:
                raise DeploymentDeferred(f"deployment deferred: {active} active job(s) remain")
            time.sleep(min(10, max(1, deadline - time.monotonic())))

    def restart_or_defer_worker(self, environment: dict[str, str]) -> None:
        health = self.worker_request(environment, "GET", "/health")
        active_jobs = health.get("active_jobs")
        if isinstance(active_jobs, bool) or not isinstance(active_jobs, int) or active_jobs < 0:
            raise ReleaseError("monomer MD worker did not report a valid active job count")
        instance_id = health.get("worker_instance_id")
        self.worker_previous_instance = instance_id if isinstance(instance_id, str) else None
        if active_jobs > 0:
            raise ReleaseError("monomer MD worker still has active jobs after the global drain gate")
        self.run(["systemctl", "--user", "restart", "nexpoly-monomer-md-worker.service"], env=environment)
        self.wait_for_worker_health(
            environment,
            expected_release=self.release_dir,
            previous_instance_id=self.worker_previous_instance,
        )

    def recover_drained_worker(self, environment: dict[str, str]) -> str:
        # POST /drain may have taken effect even when its response was lost or
        # malformed.  An attempted drain with no parsed response must therefore
        # be reconciled with an idempotent resume before the API drain is lifted.
        self.resume_worker(environment)
        return "resumed-after-failure"

    def select_previous_runtime_for_resume(self) -> Path:
        """Pin pre-switch recovery commands to the proven running release."""

        previous_sha = self.previous_state.get("source_sha")
        if not isinstance(previous_sha, str) or not SHA_RE.fullmatch(previous_sha):
            raise ReleaseError("cannot select a valid previous release for drain recovery")
        previous = self.ops / "releases" / previous_sha
        if not previous.is_dir() or previous.is_symlink():
            raise ReleaseError("previous release is unavailable for drain recovery")
        current = self.ops / "current"
        try:
            if not current.is_symlink() or current.resolve(strict=True) != previous.resolve(strict=True):
                raise ReleaseError(
                    "ops/current changed before pre-switch drain recovery"
                )
        except OSError as exc:
            raise ReleaseError(
                "cannot verify ops/current before pre-switch drain recovery"
            ) from exc
        self.candidate_dir = previous
        return previous

    def run_monomer_md_smoke(
        self,
        environment: dict[str, str],
        *,
        release: Path | None = None,
    ) -> None:
        timeout = int(environment.get("NEXPOLY_MONOMER_MD_SMOKE_TIMEOUT_SECONDS", "300"))
        if timeout < 30 or timeout > 3600:
            raise ReleaseError("NEXPOLY_MONOMER_MD_SMOKE_TIMEOUT_SECONDS must be between 30 and 3600")
        smoke_release = release or self.release_dir
        script = smoke_release / "scripts" / "monomer_md_smoke.py"
        if not script.is_file():
            raise ReleaseError("release does not contain the monomer MD smoke script")
        asset_root = Path(environment["NEXPOLY_ASSET_ROOT"])
        commit_marker = asset_root / "byteff2" / "BYTEFF2-COMMIT"
        try:
            expected_byteff2_commit = require_sha(
                commit_marker.read_text(encoding="ascii").strip(),
                "smoke ByteFF2 commit",
            )
        except OSError as exc:
            raise ReleaseError("cannot read the pinned BYTEFF2-COMMIT for Worker smoke") from exc
        with script.open("rb") as source:
            self.run(
                self.compose(
                    smoke_release,
                    "exec", "-T", "backend", "python", "-",
                    "--base-url", "http://127.0.0.1:8000",
                    "--timeout-seconds", str(timeout),
                    "--expected-byteff2-commit", expected_byteff2_commit,
                ),
                env=environment,
                stdin=source,
            )

    def run_ingress_isolated_monomer_smoke(
        self,
        environment: dict[str, str],
        *,
        release: Path | None = None,
    ) -> None:
        """Temporarily admit a Worker smoke only while public nginx is stopped."""

        smoke_release = release or self.release_dir
        self.run(self.compose(smoke_release, "stop", "nginx"), env=environment)
        try:
            self.drain(environment, False)
            self.run_monomer_md_smoke(environment, release=smoke_release)
        finally:
            self.drain(environment, True)

    def backup_database(self, environment: dict[str, str], from_sha: str) -> None:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"pre-{from_sha}-to-{self.sha}-{stamp}.dump"
        backup_dir = self.root / "backups"
        ensure_durable_directory(backup_dir)
        self.backup_path = backup_dir / name
        user = environment.get("NEXPOLY_POSTGRES_USER", "nexpoly")
        database = environment.get("NEXPOLY_POSTGRES_DB", "nexpoly")
        with self.backup_path.open("xb") as output:
            os.chmod(self.backup_path, 0o600)
            self.run(self.compose(self.candidate_dir, "exec", "-T", "lab-postgres", "pg_dump", "-U", user, "-d", database, "-Fc"), env=environment, stdout=output)
        fsync_regular_file(self.backup_path)
        with self.backup_path.open("rb") as source:
            self.run(self.compose(self.candidate_dir, "exec", "-T", "lab-postgres", "pg_restore", "--list"), env=environment, stdin=source, stdout=subprocess.DEVNULL)
        digest = sha256_file(self.backup_path)
        sidecar = {"schema_version": 1, "created_at": utc_now(), "from_sha": from_sha, "to_sha": self.sha, "file": name, "sha256": digest}
        atomic_json(self.backup_path.with_suffix(".dump.json"), sidecar)
        atomic_text(
            self.backup_path.with_suffix(".dump.sha256"),
            f"{digest.removeprefix('sha256:')}  {name}\n",
        )

    def candidate_asset_environment(self, environment: dict[str, str]) -> dict[str, str]:
        candidate = environment.copy()
        candidate["NEXPOLY_ASSET_ROOT"] = self.document["resolved_asset_root"]
        candidate["NEXPOLY_ASSET_MANIFEST_DIGEST"] = self.document[
            "resolved_asset_manifest_digest"
        ]
        return candidate

    def rebuild_datasets(self, environment: dict[str, str]) -> None:
        datasets = self.document.get("datasets_on_asset_change", [])
        if not datasets:
            raise ReleaseError("asset changes require explicit datasets_on_asset_change")
        command = self.compose(
            self.candidate_dir,
            "run",
            "--rm",
            "--no-deps",
            "postgres-init",
            "python",
            "-m",
            "app.import_postgres",
            "--rebuild",
            "--skip-migrations",
        )
        for dataset in datasets:
            command.extend(["--dataset", dataset])
        self.run(command, env=environment)

    def restore_database(
        self,
        environment: dict[str, str],
        *,
        release: Path,
    ) -> None:
        if self.backup_path is None or not self.backup_path.is_file():
            raise ReleaseError("database restore requires the verified pre-deploy dump")
        user = environment.get("NEXPOLY_POSTGRES_USER", "nexpoly")
        database = environment.get("NEXPOLY_POSTGRES_DB", "nexpoly")
        expected_digest = sha256_file(self.backup_path)
        sidecar = load_manifest(self.backup_path.with_suffix(".dump.json"))
        if sidecar.get("sha256") != expected_digest:
            raise ReleaseError("database dump digest differs from its sidecar")
        with self.backup_path.open("rb") as source:
            self.run(
                self.compose(
                    release,
                    "exec",
                    "-T",
                    "lab-postgres",
                    "pg_restore",
                    "--exit-on-error",
                    "--single-transaction",
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                    "--no-privileges",
                    "-U",
                    user,
                    "-d",
                    database,
                ),
                env=environment,
                stdin=source,
            )

    def backend_healthcheck(
        self,
        environment: dict[str, str],
        *,
        release: Path | None = None,
    ) -> None:
        """Verify the replacement backend before exposing it through nginx."""

        health_release = release or self.release_dir
        health_manifest = validate_manifest(
            load_manifest(health_release / "release-manifest.json"),
            deployment_mode="auto",
        )
        health_sha = health_manifest["source_sha"]
        self.run(
            self.compose(
                health_release,
                "exec", "-T", "backend",
                "python", "-m", "app.postgres_preflight", "--mode", "runtime", "--strict",
                "--expected-source-sha", health_sha,
            ),
            env=environment,
        )
        self.run(
            self.compose(
                health_release,
                "exec", "-T", "backend",
                "python", "-m", "app.gpu_preflight", "--mode", "ready",
            ),
            env=environment,
        )

    def run_contract_gpu_api_smoke(
        self,
        environment: dict[str, str],
        *,
        release: Path | None = None,
    ) -> None:
        raw_timeout = environment.get("NEXPOLY_CONTRACT_GPU_SMOKE_TIMEOUT_SECONDS", "900")
        try:
            timeout = int(raw_timeout)
        except ValueError as exc:
            raise ReleaseError(
                "NEXPOLY_CONTRACT_GPU_SMOKE_TIMEOUT_SECONDS must be an integer"
            ) from exc
        if timeout < 60 or timeout > 1800:
            raise ReleaseError(
                "NEXPOLY_CONTRACT_GPU_SMOKE_TIMEOUT_SECONDS must be between 60 and 1800"
            )
        smoke_release = release or self.release_dir
        self.run(
            self.compose(
                smoke_release,
                "exec", "-T", "backend", "python", "-c",
                CONTRACT_GPU_API_SMOKE_PROGRAM,
                str(timeout),
            ),
            env=environment,
        )

    def run_ingress_isolated_contract_smoke(
        self,
        environment: dict[str, str],
        *,
        release: Path | None = None,
    ) -> None:
        """Exercise real write APIs while nginx is stopped, then restore drain."""

        try:
            self.drain(environment, False)
            self.run_contract_gpu_api_smoke(environment, release=release)
        finally:
            # nginx is still stopped. Re-enable the persistent admission gate
            # before any ingress can be restored, including on smoke failure.
            self.drain(environment, True)

    def run_isolated_web_smoke(self, environment: dict[str, str]) -> None:
        """Verify the exact Web digest without publishing any host port."""

        container = f"nexpoly-web-smoke-{self.sha[:12]}"
        subprocess.run(
            ["docker", "rm", "-f", container],
            cwd=self.root,
            env=environment,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    container,
                    "--network",
                    "none",
                    self.document["images"]["web"],
                ],
                env=environment,
            )
            html = b""
            for _ in range(30):
                result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        container,
                        "wget",
                        "-qO-",
                        "http://127.0.0.1/",
                    ],
                    cwd=self.root,
                    env=environment,
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                if result.returncode == 0 and result.stdout:
                    html = result.stdout
                    break
                time.sleep(1)
            if b'<div id="root">' not in html:
                raise ReleaseError("isolated Web smoke did not return the application HTML")
            assets = re.findall(rb'(?:src|href)="(/assets/[^"?]+)', html)
            if not assets:
                raise ReleaseError("isolated Web smoke found no versioned static asset")
            asset = assets[0].decode("utf-8", "strict")
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
                    "wget",
                    "-qO",
                    "/dev/null",
                    f"http://127.0.0.1{asset}",
                ],
                cwd=self.root,
                env=environment,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                raise ReleaseError("isolated Web static asset smoke failed")
        except UnicodeError as exc:
            raise ReleaseError("isolated Web smoke returned an invalid asset path") from exc
        finally:
            subprocess.run(
                ["docker", "rm", "-f", container],
                cwd=self.root,
                env=environment,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def public_web_static_smoke(self, environment: dict[str, str]) -> None:
        """Verify the public Web root and one asset referenced by that exact HTML."""

        web_base = environment.get(
            "NEXPOLY_WEB_BASE_URL",
            "http://127.0.0.1:9000",
        ).rstrip("/")
        try:
            with urllib.request.urlopen(f"{web_base}/", timeout=20) as response:
                html = response.read(2 * 1024 * 1024)
                content_type = response.headers.get_content_type()
            if content_type != "text/html" or b'<div id="root">' not in html:
                raise ReleaseError("web root did not return the expected application HTML")
            assets = re.findall(rb'(?:src|href)="(/assets/[^"?]+)', html)
            if not assets:
                raise ReleaseError("web root did not reference a versioned static asset")
            asset_path = assets[0].decode("utf-8", "strict")
            with urllib.request.urlopen(f"{web_base}{asset_path}", timeout=20) as response:
                payload = response.read(1024)
                if response.status != 200 or not payload:
                    raise ReleaseError("versioned static asset smoke failed")
        except (OSError, UnicodeError) as exc:
            raise ReleaseError(f"web static-resource smoke failed: {exc}") from exc

    def healthcheck(self, environment: dict[str, str]) -> None:
        self.run(
            self.compose(
                self.release_dir,
                "exec", "-T", "backend",
                "python", "-m", "app.postgres_preflight", "--mode", "runtime", "--strict",
                "--expected-source-sha", self.sha,
            ),
            env=environment,
        )
        health_timeout = int(environment.get("NEXPOLY_RUNTIME_HEALTH_TIMEOUT_SECONDS", "180"))
        if health_timeout < 1 or health_timeout > 600:
            raise ReleaseError("NEXPOLY_RUNTIME_HEALTH_TIMEOUT_SECONDS must be between 1 and 600")
        urls = environment.get("NEXPOLY_HEALTH_URLS", "http://127.0.0.1:9000/health").split(",")
        for raw_url in urls:
            url = raw_url.strip()
            if not url:
                continue
            deadline = time.monotonic() + health_timeout
            last_error = "no response"
            while True:
                try:
                    with urllib.request.urlopen(url, timeout=20) as response:
                        if 200 <= response.status < 300:
                            break
                        last_error = f"HTTP {response.status}"
                except OSError as exc:
                    last_error = str(exc)
                if time.monotonic() >= deadline:
                    raise ReleaseError(f"health endpoint failed: {url}: {last_error}")
                time.sleep(5)

        self.public_web_static_smoke(environment)

        web_base = environment.get("NEXPOLY_WEB_BASE_URL", "http://127.0.0.1:9000").rstrip("/")

        polytao_enabled = environment.get("POLYTAO_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
        if polytao_enabled:
            polytao_url = environment.get(
                "NEXPOLY_POLYTAO_STATUS_URL",
                f"{web_base}/api/v1/conditional-generation/polytao/status",
            )
            try:
                with urllib.request.urlopen(polytao_url, timeout=30) as response:
                    status = json.load(response)
            except (OSError, ValueError) as exc:
                raise ReleaseError(f"PolyTAO status smoke failed: {exc}") from exc
            if not isinstance(status, dict) or status.get("enabled") is not True or status.get("available") is not True:
                raise ReleaseError("PolyTAO is enabled but did not report an available runtime")

        if release_uses_worker(self.document):
            if self.worker_restart_deferred:
                worker = self.worker_request(environment, "GET", "/health")
                self.assert_worker_runtime_identity(worker, self.release_dir)
                if worker.get("status") != "ok" or worker.get("draining") is not True:
                    raise ReleaseError("deferred monomer MD worker is not healthy and draining")
                if worker.get("accepting_jobs") is not False:
                    raise ReleaseError("deferred monomer MD worker unexpectedly accepts jobs")
            else:
                self.wait_for_worker_health(
                    environment,
                    expected_release=self.release_dir,
                    previous_instance_id=self.worker_previous_instance,
                )

            monomer_status_url = environment.get(
                "NEXPOLY_MONOMER_MD_STATUS_URL",
                f"{web_base}/api/v1/monomer-md/status",
            )
            try:
                with urllib.request.urlopen(monomer_status_url, timeout=30) as response:
                    monomer_status = json.load(response)
            except (OSError, ValueError) as exc:
                raise ReleaseError(f"monomer MD status smoke failed: {exc}") from exc
            if not isinstance(monomer_status, dict) or monomer_status.get("default_steps") != 300:
                raise ReleaseError("monomer MD backend did not report the 300-step contract")
            if monomer_status.get("available") is not True:
                raise ReleaseError("monomer MD backend did not report an available runtime")
            if self.worker_restart_deferred:
                if monomer_status.get("draining") is not True or monomer_status.get("can_submit") is not False:
                    raise ReleaseError("monomer MD backend did not report deferred drain state")
            elif monomer_status.get("can_submit") is not True:
                raise ReleaseError("monomer MD backend was not ready for smoke submission")

        self.verify_runtime_images(environment)
        self.verify_postgres_loopback(self.release_dir, environment)

    def switch_current(self, target: Path) -> None:
        current = self.ops / "current"
        temporary = self.ops / ".current.new"
        durable_unlink(temporary, missing_ok=True)
        temporary.symlink_to(target.relative_to(self.ops))
        temporary.replace(current)
        fsync_directory(self.ops)

    def clear_failed_bootstrap_release(self) -> None:
        """Return a rolled-back first deployment to a clean retryable state."""

        current = self.ops / "current"
        if current.exists() or current.is_symlink():
            if not current.is_symlink():
                raise ReleaseError("failed bootstrap left a non-symlink ops/current entry")
            try:
                target = current.resolve(strict=True)
                expected = self.release_dir.resolve(strict=True)
            except OSError as exc:
                raise ReleaseError("failed bootstrap current pointer cannot be resolved safely") from exc
            if target != expected:
                raise ReleaseError("failed bootstrap current pointer does not reference the target release")
            durable_unlink(current)
        if self.release_dir.exists() or self.release_dir.is_symlink():
            if not self.release_dir.is_dir() or self.release_dir.is_symlink():
                raise ReleaseError("failed bootstrap release path is not a safe directory")
            shutil.rmtree(self.release_dir)

    def switch_asset_pointer(self, target: Path) -> None:
        resolved, _, _ = inspect_asset_release(target)
        pointer = self.ops / "current-assets"
        temporary = self.ops / ".current-assets.new"
        durable_unlink(temporary, missing_ok=True)
        temporary.symlink_to(resolved)
        temporary.replace(pointer)
        fsync_directory(self.ops)

    def rollback_runtime(self, environment: dict[str, str]) -> None:
        previous_sha = self.previous_state.get("source_sha")
        if not isinstance(previous_sha, str) or not SHA_RE.fullmatch(previous_sha):
            raise ReleaseError("deployment failed and no valid previous release is available for rollback")
        previous = self.ops / "releases" / previous_sha
        if not previous.is_dir():
            raise ReleaseError("deployment failed and the previous release directory is unavailable")
        previous_manifest = validate_manifest(load_manifest(previous / "release-manifest.json"), deployment_mode="auto")
        compatibility_floor = self.previous_state.get("schema_compatibility_floor")
        assert_release_supports_schema_floor(previous_manifest, compatibility_floor)
        rollback_env = environment.copy()
        rollback_env["NEXPOLY_BACKEND_IMAGE"] = previous_manifest["images"]["backend"]
        rollback_env["NEXPOLY_WEB_IMAGE"] = previous_manifest["images"]["web"]
        self.run(
            self.compose(self.candidate_dir, "stop", "nginx", "backend"),
            env=rollback_env,
        )
        self.switch_current(previous)
        # Every subsequent smoke and conditional drain/resume must use the
        # restored release's postgres-init and Compose tree, never the failed
        # target bundle.
        self.candidate_dir = previous
        self.run(
            self.compose(
                previous,
                "up", "-d", "--no-build", "--wait", "--wait-timeout", "300",
                "lab-postgres", "backend",
            ),
            env=rollback_env,
        )
        # The failed candidate may already have persisted a snapshot carrying
        # its SHA.  Recreate the previous release's governed snapshot before
        # running the old strict preflight.
        self.refresh_analytics_snapshot(
            rollback_env,
            release=previous,
            source_sha=previous_sha,
        )
        self.backend_healthcheck(rollback_env, release=previous)
        self.run_ingress_isolated_contract_smoke(
            rollback_env,
            release=previous,
        )
        if release_uses_worker(previous_manifest):
            previous_instance_id: str | None = None
            try:
                worker = self.worker_request(rollback_env, "GET", "/health")
                instance_id = worker.get("worker_instance_id")
                if isinstance(instance_id, str) and instance_id:
                    previous_instance_id = instance_id
            except ReleaseError:
                pass
            self.run(
                ["systemctl", "--user", "restart", "nexpoly-monomer-md-worker.service"],
                env=rollback_env,
            )
            self.wait_for_worker_health(
                rollback_env,
                expected_release=previous,
                previous_instance_id=previous_instance_id,
            )
        elif release_uses_worker(self.document):
            self.run(
                ["systemctl", "--user", "stop", "nexpoly-monomer-md-worker.service"],
                env=rollback_env,
            )
        if release_uses_worker(previous_manifest):
            self.run_ingress_isolated_monomer_smoke(rollback_env, release=previous)
        self.run(
            self.compose(
                previous,
                "up", "-d", "--no-build", "--wait", "--wait-timeout", "120", "nginx",
            ),
            env=rollback_env,
        )
        self.validate_current_runtime(rollback_env)

    def cleanup_failed_release(self) -> None:
        """Remove a rolled-back target so the exact same SHA can be retried."""

        if not self.release_dir.exists() and not self.release_dir.is_symlink():
            return
        if not self.release_dir.is_dir() or self.release_dir.is_symlink():
            raise ReleaseError("failed release path is not a safe directory")
        current = self.ops / "current"
        if current.exists() or current.is_symlink():
            try:
                if current.resolve(strict=True) == self.release_dir.resolve(strict=True):
                    raise ReleaseError("refusing to delete the release referenced by ops/current")
            except OSError as exc:
                raise ReleaseError("cannot resolve ops/current while cleaning a failed release") from exc
        shutil.rmtree(self.release_dir)

    def cleanup_unrecorded_staging(self) -> None:
        """Remove a pre-mutation staging tree left before the first marker write.

        Bundle extraction, offline venv creation, image pull, label checks, and
        the first main-SHA check do not change the running release.  A hard
        interruption in that preparation window can therefore leave only the
        target ``<sha>.staging`` directory.  With no deployment marker there is
        no database/runtime transition to infer or roll back, so retry may
        safely remove that one controlled directory while holding deploy.lock.
        """

        if not self.staging.exists() and not self.staging.is_symlink():
            return
        if not self.staging.is_dir() or self.staging.is_symlink():
            raise ReleaseError("unrecorded staging path is not a safe directory")
        current = self.ops / "current"
        if current.exists() or current.is_symlink():
            try:
                if current.resolve(strict=True) == self.staging.resolve(strict=True):
                    raise ReleaseError("ops/current unexpectedly references unrecorded staging")
            except OSError as exc:
                raise ReleaseError(
                    "cannot resolve ops/current while cleaning unrecorded staging"
                ) from exc
        shutil.rmtree(self.staging)

    def marker_backup(self, marker: dict[str, Any]) -> Path:
        raw_path = marker.get("database_backup")
        raw_digest = marker.get("database_backup_sha256")
        if not isinstance(raw_path, str) or not isinstance(raw_digest, str):
            raise ReleaseError("interrupted data change has no verified backup evidence")
        backup = Path(raw_path)
        if (
            not backup.is_absolute()
            or backup.parent != self.root / "backups"
            or not backup.is_file()
            or backup.is_symlink()
        ):
            raise ReleaseError("interrupted deployment backup path is missing or unsafe")
        require_digest(raw_digest, "interrupted deployment backup digest")
        if sha256_file(backup) != raw_digest:
            raise ReleaseError("interrupted deployment backup digest does not match the dump")
        return backup

    def recover_interrupted_deployment(self, marker: dict[str, Any]) -> None:
        """Finish a fail-closed rollback while the caller holds deploy.lock."""

        marker_sha = require_sha(str(marker.get("source_sha", "")), "interrupted release SHA")
        if marker_sha != self.sha:
            raise ReleaseError("interrupted marker and release manifest identify different SHAs")
        phase = marker.get("phase")
        if phase not in {"prepared", "db-changed", "switched", "verified"}:
            raise ReleaseError("interrupted deployment has an unknown phase")
        previous_state = marker.get("previous_state")
        if not isinstance(previous_state, dict):
            raise ReleaseError("interrupted deployment is missing its previous release state")
        self.previous_state = previous_state
        self.bootstrap = marker.get("bootstrap") is True
        environment = self.environment()

        # A verified target whose durable release state is already committed
        # only needs its admission gate resumed.  Re-running a database restore
        # here would discard writes accepted after a previous successful resume.
        if phase == "verified" and self.state_path.is_file():
            committed = load_manifest(self.state_path)
            if committed.get("source_sha") == self.sha and committed.get("status") == "success":
                self.previous_state = committed
                self.candidate_dir = self.release_dir
                self.validate_current_runtime(environment)
                self.drain(environment, False)
                durable_unlink(self.in_progress_path)
                return

        if self.bootstrap:
            self.run_bootstrap_rollback(environment)
            # The persistent drain may have committed immediately before the
            # interrupted response.  Clear it only when it is still owned by
            # this bootstrap SHA; a foreign maintenance drain stays untouched.
            if marker.get("drain_attempted") is True:
                self.candidate_dir = (
                    self.release_dir if self.release_dir.is_dir() else self.staging
                )
                self.drain(environment, False)
            self.clear_failed_bootstrap_release()
            if self.staging.exists():
                shutil.rmtree(self.staging)
            durable_unlink(self.in_progress_path)
            return

        previous_sha = require_sha(
            str(previous_state.get("source_sha", "")),
            "interrupted deployment previous release SHA",
        )
        previous_release = self.ops / "releases" / previous_sha
        if not previous_release.is_dir() or previous_release.is_symlink():
            raise ReleaseError("interrupted deployment previous release is unavailable")
        target_runtime = self.release_dir if self.release_dir.is_dir() else self.staging
        if not target_runtime.is_dir() or target_runtime.is_symlink():
            raise ReleaseError("interrupted deployment target release is unavailable")
        self.candidate_dir = target_runtime

        data_change_started = marker.get("data_change_started") is True
        if data_change_started:
            self.backup_path = self.marker_backup(marker)
            previous_asset_root = Path(str(marker.get("previous_asset_root", "")))
            resolved_previous, previous_digest, previous_byteff2_commit = inspect_asset_release(
                previous_asset_root
            )
            if previous_digest != marker.get("previous_asset_digest"):
                raise ReleaseError("interrupted deployment previous asset evidence differs")
            self.switch_asset_pointer(resolved_previous)
            # environment() ran before reconciliation and may have observed
            # the candidate asset pointer.  Keep both the controller evidence
            # and the Compose/runtime environment aligned with the restored
            # previous pointer before validating the old release.
            self.document["current_asset_root"] = str(resolved_previous)
            self.document["current_asset_manifest_digest"] = previous_digest
            self.document["current_byteff2_commit"] = previous_byteff2_commit
            environment["NEXPOLY_ASSET_MANIFEST_DIGEST"] = previous_digest
            self.run(
                self.compose(target_runtime, "stop", "nginx", "backend"),
                env=environment,
            )
            self.run(
                ["systemctl", "--user", "stop", "nexpoly-monomer-md-worker.service"],
                env=environment,
            )
            self.restore_database(environment, release=previous_release)

        runtime_restarted = (
            marker.get("runtime_switch_started") is True
            or marker.get("database_change_started") is True
            or data_change_started
        )
        if runtime_restarted:
            self.rollback_runtime(environment)
        else:
            current = self.ops / "current"
            try:
                if not current.is_symlink() or current.resolve(strict=True) != previous_release.resolve(strict=True):
                    raise ReleaseError("interrupted deployment changed ops/current without recording a switch")
            except OSError as exc:
                raise ReleaseError("cannot verify ops/current during interrupted recovery") from exc

        # Both controls are idempotent.  Resume them only after the previous
        # runtime/database/assets have been verified.
        self.candidate_dir = previous_release
        if marker.get("worker_drain_attempted") is True and not runtime_restarted:
            self.resume_worker(environment)
        self.drain(environment, False)
        self.cleanup_failed_release()
        if self.staging.exists():
            shutil.rmtree(self.staging)
        durable_unlink(self.in_progress_path)

    def deploy(self) -> dict[str, Any]:
        self.ensure_root()
        if not self.apply:
            return self.plan()
        os.umask(0o077)
        with self.deployment_lock():
            if self.in_progress_path.exists():
                interrupted = load_manifest(self.in_progress_path)
                interrupted_sha = require_sha(
                    str(interrupted.get("source_sha", "")),
                    "interrupted release SHA",
                )
                interrupted_release = self.ops / "releases" / interrupted_sha
                interrupted_staging = self.ops / "releases" / f"{interrupted_sha}.staging"
                interrupted_root = (
                    interrupted_release if interrupted_release.is_dir() else interrupted_staging
                )
                interrupted_manifest = interrupted_root / "release-manifest.json"
                if (
                    not interrupted_manifest.is_file()
                    or interrupted_manifest.is_symlink()
                    or sha256_file(interrupted_manifest)
                    != interrupted.get("release_manifest_sha256")
                ):
                    raise ReleaseError(
                        "interrupted deployment lacks a matching verified release manifest"
                    )
                recovery = ReleaseController(
                    self.root,
                    interrupted_manifest,
                    "bootstrap" if interrupted.get("bootstrap") is True else "auto",
                    True,
                )
                recovery.recover_interrupted_deployment(interrupted)
                if self.state_path.is_file():
                    recovered_state = load_manifest(self.state_path)
                    if (
                        recovered_state.get("source_sha") == self.sha
                        and recovered_state.get("status") == "success"
                    ):
                        return recovered_state
            self.cleanup_unrecorded_staging()
            environment = self.environment()
            code_migration_mode = "expand"
            if self.state_path.exists():
                if self.mode == "bootstrap":
                    raise ReleaseError("bootstrap is forbidden after production release state is initialized")
                self.previous_state = load_manifest(self.state_path)
            else:
                self.bootstrap = True
                current = self.ops / "current"
                if current.exists() or current.is_symlink():
                    raise ReleaseError("first bootstrap requires both release-state.json and ops/current to be absent")
                if self.mode != "bootstrap" or environment.get("NEXPOLY_BOOTSTRAP_RELEASE_SHA") != self.sha:
                    raise ReleaseError(
                        "first release requires --mode bootstrap and NEXPOLY_BOOTSTRAP_RELEASE_SHA set to the target SHA"
                    )
                for key in (
                    "NEXPOLY_BOOTSTRAP_QUIESCE_COMMAND",
                    "NEXPOLY_BOOTSTRAP_ROLLBACK_COMMAND",
                    "NEXPOLY_BOOTSTRAP_LEGACY_RUNTIME_SHA256",
                ):
                    if not environment.get(key):
                        raise ReleaseError(f"{key} is required for the first maintenance-window release")
            if not self.bootstrap:
                code_migration_mode = code_deploy_migration_mode(
                    self.previous_state,
                    self.document,
                    deployment_mode=self.mode,
                    target_sha=self.sha,
                )
                compatibility_floor = self.previous_state.get("schema_compatibility_floor")
                assert_release_supports_schema_floor(self.document, compatibility_floor)
                self.validate_current_runtime(environment)
            previous_sha = previous_release_for_deploy(self.previous_state, self.sha)
            current_asset_digest = self.document.get(
                "current_asset_manifest_digest",
                self.previous_state.get(
                    "asset_manifest_digest",
                    self.document.get("asset_manifest_digest"),
                ),
            )
            target_asset_digest = self.document.get(
                "resolved_asset_manifest_digest",
                self.document.get("asset_manifest_digest"),
            )
            asset_changed = current_asset_digest != target_asset_digest
            if self.bootstrap and asset_changed:
                raise ReleaseError(
                    "bootstrap asset digest must match the pre-pinned reviewed baseline"
                )
            state = {
                **self.plan(),
                "status": "deploying",
                "phase": "prepared",
                "started_at": utc_now(),
                "previous_release": previous_sha,
                "previous_state": self.previous_state,
                "bootstrap": self.bootstrap,
                "asset_changed": asset_changed,
                "previous_asset_root": self.document["current_asset_root"],
                "previous_asset_digest": current_asset_digest,
                "target_asset_root": self.document["resolved_asset_root"],
                "target_asset_digest": target_asset_digest,
                "drain_enabled": False,
                "drain_attempted": False,
                "worker_drain_attempted": False,
                "database_change_started": False,
                "data_change_started": False,
                "asset_switch_started": False,
                "runtime_switch_started": False,
            }
            self.create_attempt_path()
            drained = False
            ingress_resumed = False
            bootstrap_quiesce_attempted = False
            bootstrap_cleanup_required = False
            safe_to_resume = False
            state_committed = False
            asset_switched = False
            actual_migrations: list[str] = []
            try:
                self.prepare_staging(environment)
                self.run(
                    self.compose(
                        self.candidate_dir,
                        "pull",
                        "lab-postgres",
                        "postgres-init",
                        "backend",
                        "nginx",
                    ),
                    env=environment,
                )
                self.verify_image_labels(environment)
                self.assert_still_current_main(environment)
                self.write_attempt(state)
                if self.bootstrap:
                    # The first release must prove the already-running database
                    # is loopback-only before invoking any legacy quiesce hook,
                    # backup, or migration.
                    self.verify_postgres_loopback(self.candidate_dir, environment)
                    # The legacy backend has no drain middleware, internal status
                    # endpoint, or deployment-control table. An explicit audited
                    # hook must isolate ingress and verify legacy in-process jobs
                    # before this controller is allowed to make its first backup.
                    bootstrap_quiesce_attempted = True
                    state["bootstrap_quiesce"] = self.run_bootstrap_quiesce(environment)
                    self.write_attempt(state)
                    self.backup_database(environment, previous_sha)
                    self.database_changed = True
                    state.update(
                        {
                            "phase": "db-changed",
                            "database_change_started": True,
                            "database_backup": str(self.backup_path),
                            "database_backup_sha256": sha256_file(self.backup_path),
                        }
                    )
                    self.write_attempt(state)
                    actual_migrations = self.run_migrations(environment, mode="bootstrap-expand")
                    drained = True
                    state["drain_attempted"] = True
                    self.write_attempt(state)
                    self.drain(environment, True)
                    state["drain_enabled"] = True
                    self.write_attempt(state)
                    self.wait_for_jobs(environment)
                else:
                    drained = True
                    state["drain_attempted"] = True
                    self.write_attempt(state)
                    self.drain(environment, True)
                    state["drain_enabled"] = True
                    self.write_attempt(state)
                    if release_uses_worker(self.document):
                        state["worker_drain_attempted"] = True
                        self.write_attempt(state)
                        self.worker_drain_info = self.drain_worker(environment)
                        state["worker_drain"] = self.worker_drain_info
                        self.write_attempt(state)
                    self.wait_for_jobs(environment)
                    self.backup_database(environment, previous_sha)
                    self.database_changed = True
                    state.update(
                        {
                            "phase": "db-changed",
                            "database_change_started": True,
                            "database_backup": str(self.backup_path),
                            "database_backup_sha256": sha256_file(self.backup_path),
                        }
                    )
                    self.write_attempt(state)
                    actual_migrations = self.run_migrations(environment, mode=code_migration_mode)
                candidate_environment = self.candidate_asset_environment(environment)
                if asset_changed:
                    # From this point a crash requires restoring the verified
                    # dump before the old runtime can accept writes again.
                    state["data_change_started"] = True
                    self.write_attempt(state)
                    self.rebuild_datasets(candidate_environment)
                    state["datasets_rebuilt"] = True
                    state["asset_switch_started"] = True
                    self.write_attempt(state)
                    self.switch_asset_pointer(Path(self.document["resolved_asset_root"]))
                    asset_switched = True
                    state["asset_switched"] = True
                    self.write_attempt(state)
                self.refresh_analytics_snapshot(candidate_environment)
                # Close the workflow/API freshness TOCTOU at the last safe
                # point before the old runtime is stopped and current moves.
                self.assert_still_current_main(environment)
                state["phase"] = "switched"
                state["runtime_switch_started"] = True
                self.write_attempt(state)
                self.run(
                    self.compose(self.candidate_dir, "stop", "nginx", "backend"),
                    env=environment,
                )
                if self.candidate_dir == self.staging:
                    self.staging.replace(self.release_dir)
                    fsync_directory(self.release_dir.parent)
                    self.candidate_dir = self.release_dir
                self.switch_current(self.release_dir)
                state["runtime_switched"] = True
                self.write_attempt(state)
                self.run(
                    self.compose(
                        self.release_dir,
                        "up", "-d", "--no-build", "--wait", "--wait-timeout", "300",
                        "lab-postgres", "backend",
                    ),
                    env=environment,
                )
                if release_uses_worker(self.document):
                    socket_dir = self.ops / "state" / "monomer-md-worker-socket"
                    socket_dir.mkdir(parents=True, exist_ok=True)
                    os.chmod(socket_dir, 0o700)
                    if self.bootstrap:
                        self.run(
                            ["systemctl", "--user", "restart", "nexpoly-monomer-md-worker.service"],
                            env=environment,
                        )
                        self.wait_for_worker_health(
                            environment,
                            expected_release=self.release_dir,
                        )
                    else:
                        self.restart_or_defer_worker(environment)
                self.backend_healthcheck(environment)
                self.run_ingress_isolated_contract_smoke(environment)
                if release_uses_worker(self.document) and not self.worker_restart_deferred:
                    self.run_ingress_isolated_monomer_smoke(environment)
                self.run_isolated_web_smoke(environment)
                # Real GPU/Worker smokes can take several minutes.  Recheck
                # main before nginx is started, while drain is still active.
                self.assert_still_current_main(environment)
                self.run(
                    self.compose(
                        self.release_dir,
                        "up", "-d", "--no-build", "--wait", "--wait-timeout", "120", "nginx",
                    ),
                    env=environment,
                )
                self.healthcheck(environment)
                # Close the last freshness window immediately before durable
                # success state and eventual admission resume.
                self.assert_still_current_main(environment)
                state["phase"] = "verified"
                self.write_attempt(state)
                migration_history = merge_applied_migrations(
                    self.previous_state.get("migrations"),
                    actual_migrations,
                )
                state.update(
                    {
                        "status": "success",
                        "completed_at": utc_now(),
                        "database_backup": str(self.backup_path) if self.backup_path else None,
                        "database_backup_sha256": sha256_file(self.backup_path) if self.backup_path else None,
                        "asset_manifest_digest": self.document["resolved_asset_manifest_digest"],
                        "asset_root": self.document["resolved_asset_root"],
                        "byteff2_commit": self.document["resolved_byteff2_commit"],
                        "worker_base_python": self.worker_base_python_identity,
                        "worker_toolchain": self.worker_toolchain_identity,
                        "migrations": migration_history,
                        "applied_migrations": actual_migrations,
                        "migration_manifest": self.document["migrations"],
                        "datasets_rebuilt": (
                            self.document["datasets_on_asset_change"] if asset_changed else []
                        ),
                        "worker_restart": "deferred" if self.worker_restart_deferred else "completed",
                    }
                )
                compatibility_floor = schema_compatibility_floor_after(
                    self.previous_state.get("schema_compatibility_floor"),
                    actual_migrations,
                    release_migration_records(self.document),
                )
                if "approved_contracts" in self.previous_state:
                    # Preserve checksum-bound approvals byte-for-byte. A code
                    # deployment cannot mint or widen contract approval.
                    approved_contract_migrations(self.previous_state)
                    state["approved_contracts"] = self.previous_state["approved_contracts"]
                elif "approved_contract_migrations" in self.previous_state:
                    approved_contract_migrations(self.previous_state)
                    state["approved_contract_migrations"] = self.previous_state[
                        "approved_contract_migrations"
                    ]
                else:
                    state["approved_contracts"] = []
                if compatibility_floor is not None:
                    state["schema_compatibility_floor"] = compatibility_floor
                epoch_barrier = validated_migration_epoch_barrier(self.previous_state)
                if epoch_barrier is not None:
                    state["migration_epoch_barrier"] = epoch_barrier
                    state["last_contract_operation"] = self.previous_state[
                        "last_contract_operation"
                    ]
                attempt_only_fields = {
                    "previous_state",
                    "bootstrap",
                    "previous_asset_root",
                    "previous_asset_digest",
                    "target_asset_root",
                    "target_asset_digest",
                    "drain_enabled",
                    "drain_attempted",
                    "worker_drain_attempted",
                    "worker_drain",
                    "database_change_started",
                    "data_change_started",
                    "datasets_rebuilt",
                    "asset_switch_started",
                    "asset_switched",
                    "runtime_switch_started",
                    "runtime_switched",
                }
                release_state = {
                    key: value for key, value in state.items() if key not in attempt_only_fields
                }
                # The new identity is durable while writes are still blocked.
                # Only then may admission reopen.
                atomic_json(self.state_path, release_state)
                state_committed = True
                safe_to_resume = True
                if drained:
                    state["drain_resume"] = "pending"
                    self.write_attempt(state)
                    self.drain(environment, False)
                    drained = False
                    ingress_resumed = True
                    state["drain_resume"] = "success"
                durable_unlink(self.in_progress_path, missing_ok=True)
                return release_state
            except Exception as exc:
                state.update({"status": failure_status(exc), "failed_at": utc_now(), "database_changed": self.database_changed, "error": str(exc)[:500]})
                if state_committed:
                    # Runtime, database, assets and release-state are already
                    # verified and durable.  Keep the marker and drain for an
                    # idempotent resume on the next controller invocation.
                    state["status"] = "verified-resume-pending"
                    state["drain_resume"] = "failed"
                    self.write_attempt(state)
                    safe_to_resume = False
                    raise
                if ingress_resumed:
                    try:
                        self.drain(environment, True)
                        ingress_resumed = False
                        drained = True
                        state["drain_reisolation"] = "success"
                    except Exception as isolation_exc:
                        state["drain_reisolation"] = "failed"
                        state["drain_reisolation_error"] = str(isolation_exc)[:500]
                        self.write_attempt(state)
                        raise ReleaseError(
                            "deployment state persistence failed and ingress could not be re-isolated"
                        ) from isolation_exc
                if self.bootstrap and bootstrap_quiesce_attempted:
                    try:
                        state["bootstrap_rollback"] = self.run_bootstrap_rollback(
                            environment
                        )
                        state["rollback"] = "success"
                        safe_to_resume = True
                        bootstrap_cleanup_required = True
                    except Exception as rollback_exc:
                        state["rollback"] = "failed"
                        state["rollback_error"] = str(rollback_exc)[:500]
                elif self.database_changed:
                    try:
                        if asset_changed:
                            previous_asset_root = Path(self.document["current_asset_root"])
                            if asset_switched:
                                self.switch_asset_pointer(previous_asset_root)
                                asset_switched = False
                            environment["NEXPOLY_ASSET_MANIFEST_DIGEST"] = str(
                                self.document["current_asset_manifest_digest"]
                            )
                            failed_release = (
                                self.release_dir
                                if self.release_dir.is_dir()
                                else self.candidate_dir
                            )
                            self.run(
                                self.compose(failed_release, "stop", "nginx", "backend"),
                                env=environment,
                            )
                            if release_uses_worker(self.document):
                                self.run(
                                    [
                                        "systemctl",
                                        "--user",
                                        "stop",
                                        "nexpoly-monomer-md-worker.service",
                                    ],
                                    env=environment,
                                )
                            previous_sha_value = self.previous_state.get("source_sha")
                            if not isinstance(previous_sha_value, str):
                                raise ReleaseError(
                                    "asset/data rollback requires a previous release SHA"
                                )
                            previous_release = self.ops / "releases" / previous_sha_value
                            self.restore_database(environment, release=previous_release)
                            state["database_restore"] = "success"
                        self.rollback_runtime(environment)
                        state["rollback"] = "success"
                        safe_to_resume = True
                    except Exception as rollback_exc:  # preserve both failures for operators
                        state["rollback"] = "failed"
                        state["rollback_error"] = str(rollback_exc)[:500]
                safe_to_resume = safe_to_resume or rollback_allows_resume(
                    self.database_changed, state.get("rollback")
                )
                if (
                    not self.bootstrap
                    and safe_to_resume
                    and not self.database_changed
                    and state.get("runtime_switch_started") is not True
                    and (
                        drained
                        or state.get("worker_drain_attempted") is True
                    )
                ):
                    try:
                        previous_runtime = self.select_previous_runtime_for_resume()
                        state["drain_resume_release"] = str(previous_runtime)
                    except Exception as selection_exc:
                        state["drain_resume_release"] = "unverified"
                        state["drain_resume_release_error"] = str(selection_exc)[:500]
                        safe_to_resume = False
                if (
                    not self.bootstrap
                    and safe_to_resume
                    and state.get("worker_drain_attempted") is True
                ):
                    try:
                        state["worker_drain"] = self.recover_drained_worker(environment)
                    except Exception as worker_exc:
                        state["worker_restart"] = "manual-intervention-required"
                        state["worker_restart_error"] = str(worker_exc)[:500]
                        safe_to_resume = False
                self.write_attempt(state)
                raise
            finally:
                if drained and safe_to_resume:
                    try:
                        self.drain(environment, False)
                        drained = False
                        state["drain_resume"] = "success"
                        self.write_attempt(state)
                    except Exception as resume_exc:
                        safe_to_resume = False
                        state["drain_resume"] = "failed"
                        state["drain_resume_error"] = str(resume_exc)[:500]
                        self.write_attempt(state)
                        raise ReleaseError(
                            "deployment rollback completed but drain resume failed; ingress remains isolated"
                        ) from resume_exc
                if bootstrap_cleanup_required:
                    self.clear_failed_bootstrap_release()
                if safe_to_resume and not self.bootstrap and not state_committed:
                    self.cleanup_failed_release()
                if safe_to_resume:
                    durable_unlink(self.in_progress_path, missing_ok=True)
                if self.staging.exists() and not self.in_progress_path.exists():
                    shutil.rmtree(self.staging)


class PolytaoContractMaintenance:
    """Execute the one reviewed destructive migration as a recoverable operation."""

    EXPECTED_ROWS = 9
    EXPECTED_STATUS_COUNTS = {"completed": 7, "failed": 2}

    def __init__(
        self,
        root: Path,
        manifest_path: Path,
        operation_id: str,
        apply: bool,
    ) -> None:
        if OPERATION_ID_RE.fullmatch(operation_id) is None:
            raise ReleaseError(
                "contract operation ID must be 8-128 lowercase safe characters"
            )
        self.controller = ReleaseController(root, manifest_path, "auto", apply)
        self.root = self.controller.root
        self.document = self.controller.document
        self.operation_id = operation_id
        self.apply = apply
        self.state_path = self.controller.state_path
        self.marker_path = self.controller.ops / "state" / "contract-0012-in-progress.json"
        self.journal_path = (
            self.controller.ops
            / "state"
            / "contract-operations"
            / f"{operation_id}.json"
        )
        self.audit_dir = (
            self.root / "backups" / "contracts" / "0012" / operation_id
        )
        self.verification_owner_path = (
            self.controller.ops
            / "state"
            / "contract-verification-databases"
            / f"{operation_id}.json"
        )
        self.contract_record = self._contract_record()

    def _contract_record(self) -> dict[str, Any]:
        if self.document.get("schema_version") != 2:
            raise ReleaseError("0012 maintenance requires a checksum-bound V2 release manifest")
        records = {
            record["version"]: record
            for record in release_migration_records(self.document)
        }
        record = records.get(POLYTAO_SCHEMA_COMPATIBILITY_FLOOR)
        if (
            record is None
            or record["kind"] != "contract"
            or record["checksum"] != POLYTAO_CONTRACT_CHECKSUM
            or record["epoch"] != 1
        ):
            raise ReleaseError("release manifest does not contain the reviewed 0012 contract identity")
        return record

    def plan(self) -> dict[str, Any]:
        return {
            "action": "maintain-contract-0012",
            "apply": self.apply,
            "production_root": str(self.root),
            "source_sha": self.document["source_sha"],
            "operation_id": self.operation_id,
            "contract": {
                "version": self.contract_record["version"],
                "checksum": self.contract_record["checksum"],
                "epoch": self.contract_record["epoch"],
            },
            "expected_archive": {
                "rows": self.EXPECTED_ROWS,
                "status_counts": self.EXPECTED_STATUS_COUNTS,
            },
            "audit_dir": str(self.audit_dir),
        }

    def _approved_record(
        self,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        approvals = state.get("approved_contracts", [])
        if not isinstance(approvals, list):
            raise ReleaseError("release state contains invalid approved contracts")
        approved_contract_migrations(state)
        return next(
            (
                record
                for record in approvals
                if record.get("version") == POLYTAO_SCHEMA_COMPATIBILITY_FLOOR
            ),
            None,
        )

    def _load_current_state(
        self,
        *,
        allow_completed_contract: bool = False,
    ) -> dict[str, Any]:
        if not self.state_path.is_file() or self.state_path.is_symlink():
            raise ReleaseError("0012 maintenance requires an initialized release state")
        state = load_manifest(self.state_path)
        if state.get("status") != "success":
            raise ReleaseError("0012 maintenance requires a successful current release")
        if state.get("source_sha") != self.document["source_sha"]:
            raise ReleaseError("0012 maintenance manifest must match the current release SHA")
        legacy_approvals = state.get("approved_contract_migrations")
        if legacy_approvals not in (None, []):
            raise ReleaseError(
                "name-only contract approvals must be reconciled before 0012 maintenance"
            )
        history = state.get("migrations")
        if not isinstance(history, list) or POLYTAO_CONTRACT_PREVIOUS_VERSION not in history:
            raise ReleaseError("0012 maintenance requires release-state history through 0011")
        if (
            POLYTAO_SCHEMA_COMPATIBILITY_FLOOR in history
            and not allow_completed_contract
        ):
            raise ReleaseError(
                "release-state already records 0012 without this maintenance operation"
            )
        approved_contract_migrations(state)
        return state

    def _bind_current_release(self, state: dict[str, Any]) -> Path:
        release = self.controller.ops / "releases" / self.document["source_sha"]
        manifest = release / "release-manifest.json"
        if (
            not release.is_dir()
            or release.is_symlink()
            or not manifest.is_file()
            or manifest.is_symlink()
        ):
            raise ReleaseError("current immutable release is unavailable for 0012 maintenance")
        supplied_digest = sha256_file(self.controller.manifest_path)
        if sha256_file(manifest) != supplied_digest:
            raise ReleaseError("0012 maintenance manifest differs from the current release artifact")
        recorded_digest = state.get("release_manifest_sha256")
        if recorded_digest is not None and recorded_digest != supplied_digest:
            raise ReleaseError("release state and 0012 maintenance manifest digests differ")
        return release

    def _write_marker(self, marker: dict[str, Any]) -> None:
        atomic_json(self.marker_path, marker)

    def _capture_json(
        self,
        program: str,
        environment: dict[str, str],
    ) -> dict[str, Any]:
        command = self.controller.compose(
            self.controller.candidate_dir,
            "run",
            "--rm",
            "--no-deps",
            "postgres-init",
            "python",
            "-c",
            program,
        )
        print(f"[release-controller] {shlex.join(command)}")
        result = subprocess.run(
            command,
            cwd=self.root,
            env=environment,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ReleaseError("contract verification returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ReleaseError("contract verification returned an invalid object")
        return payload

    @staticmethod
    def _database_environment(
        environment: dict[str, str],
        database: str,
    ) -> dict[str, str]:
        updated = environment.copy()
        for key in ("APP_POSTGRES_DSN", "PI_POSTGRES_DSN", "LAB_DATA_POSTGRES_DSN"):
            value = updated.get(key)
            if not value:
                continue
            parsed = urllib.parse.urlsplit(value)
            updated[key] = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, f"/{database}", "", "")
            )
        return updated

    def _verification_database_name(self) -> str:
        suffix = hashlib.sha256(self.operation_id.encode("ascii")).hexdigest()[:16]
        return f"nexpoly_contract_verify_{suffix}"

    def _load_verification_owner(self, database: str) -> dict[str, Any]:
        path = self.verification_owner_path
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_uid != os.geteuid()
            or stat.S_IMODE(path.stat().st_mode) != 0o600
        ):
            raise ReleaseError(
                "verification database exists without a safe operation ownership marker"
            )
        marker = load_manifest(path)
        if not isinstance(marker, dict) or set(marker) != {
            "schema_version",
            "operation_id",
            "source_sha",
            "database",
            "database_absent_before_create",
            "status",
            "created_at",
            "updated_at",
        }:
            raise ReleaseError("verification database ownership marker has an invalid shape")
        if (
            marker.get("schema_version") != 1
            or marker.get("operation_id") != self.operation_id
            or marker.get("source_sha") != self.document["source_sha"]
            or marker.get("database") != database
            or marker.get("database_absent_before_create") is not True
            or marker.get("status")
            not in {"create-intent", "created", "dropped"}
            or not isinstance(marker.get("created_at"), str)
            or not marker["created_at"].strip()
            or not isinstance(marker.get("updated_at"), str)
            or not marker["updated_at"].strip()
        ):
            raise ReleaseError("verification database ownership marker has an invalid identity")
        return marker

    def _write_verification_owner(
        self,
        database: str,
        status: str,
        *,
        previous: dict[str, Any] | None = None,
        database_absent_before_create: bool | None = None,
    ) -> dict[str, Any]:
        if status not in {"create-intent", "created", "dropped"}:
            raise ReleaseError("invalid verification database ownership state")
        if previous is None:
            if status != "create-intent" or database_absent_before_create is not True:
                raise ReleaseError(
                    "new verification ownership requires durable pre-create absence evidence"
                )
            absence_evidence = True
        else:
            if previous.get("database_absent_before_create") is not True:
                raise ReleaseError("verification ownership lost its absence evidence")
            absence_evidence = True
        timestamp = utc_now()
        marker = {
            "schema_version": 1,
            "operation_id": self.operation_id,
            "source_sha": self.document["source_sha"],
            "database": database,
            "database_absent_before_create": absence_evidence,
            "status": status,
            "created_at": (
                previous["created_at"] if previous is not None else timestamp
            ),
            "updated_at": timestamp,
        }
        atomic_json(self.verification_owner_path, marker)
        return marker

    def _canonical_contract_ledger_prefix(
        self,
        *,
        include_contract: bool,
    ) -> list[dict[str, str]]:
        policy_path = (
            self.controller.candidate_dir
            / "backend"
            / "migrations"
            / "postgres"
            / "manifest.json"
        )
        records = release_migrations_from_policy_manifest(
            policy_path,
            include_baseline=True,
        )
        release_records = release_migration_records(self.document)
        if release_records != [
            record for record in records if record["kind"] != "baseline"
        ]:
            raise ReleaseError(
                "0012 maintenance release manifest differs from canonical migration policy"
            )
        target_index = next(
            (
                index
                for index, record in enumerate(records)
                if record["version"] == POLYTAO_SCHEMA_COMPATIBILITY_FLOOR
            ),
            None,
        )
        if target_index is None:
            raise ReleaseError("canonical migration policy is missing the 0012 contract")
        limit = target_index + (1 if include_contract else 0)
        return [
            {"version": record["version"], "checksum": record["checksum"]}
            for record in records[:limit]
        ]

    def _canonical_contract_ledger_prefixes(self) -> list[list[dict[str, str]]]:
        """Return every non-empty, checksum-bound prefix through 0012."""

        through_contract = self._canonical_contract_ledger_prefix(
            include_contract=True
        )
        return [
            [dict(record) for record in through_contract[:length]]
            for length in range(1, len(through_contract) + 1)
        ]

    @staticmethod
    def _legacy_relation_expected(ledger: list[dict[str, str]]) -> bool:
        versions = {record["version"] for record in ledger}
        return (
            "0007_polytao_jobs" in versions
            and POLYTAO_SCHEMA_COMPATIBILITY_FLOOR not in versions
        )

    def _validate_database_inventory(
        self,
        payload: object,
        environment: dict[str, str],
        *,
        allow_contract: bool,
        allow_owned_verification: bool,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "target_database",
            "current_user",
            "databases",
            "ledger",
            "legacy_relation_present",
        }:
            raise ReleaseError("0012 database inventory has an invalid shape")
        expected_user = environment.get("NEXPOLY_POSTGRES_USER")
        if (
            payload.get("schema_version") != 1
            or payload.get("target_database") != "nexpoly"
            or not isinstance(expected_user, str)
            or payload.get("current_user") != expected_user
            or not isinstance(payload.get("legacy_relation_present"), bool)
        ):
            raise ReleaseError("0012 database inventory has an invalid target identity")
        raw_databases = payload.get("databases")
        if not isinstance(raw_databases, list):
            raise ReleaseError("0012 database inventory does not contain a database list")
        databases: dict[str, dict[str, Any]] = {}
        for record in raw_databases:
            if not isinstance(record, dict) or set(record) != {
                "name",
                "owner",
                "is_template",
                "allow_connections",
            }:
                raise ReleaseError("0012 database inventory contains an invalid record")
            name = record.get("name")
            if (
                not isinstance(name, str)
                or not name
                or name in databases
                or record.get("owner") != expected_user
                or not isinstance(record.get("is_template"), bool)
                or not isinstance(record.get("allow_connections"), bool)
            ):
                raise ReleaseError("0012 database inventory contains an invalid identity")
            databases[name] = dict(record)

        verification_database = self._verification_database_name()
        base_databases = {"nexpoly", "postgres", "template0", "template1"}
        registered_databases = {"nexpoly_dev", "nexpoly_md_health_opt"}
        extra_databases = set(databases).difference(
            base_databases | registered_databases
        )
        if extra_databases:
            if (
                not allow_owned_verification
                or extra_databases != {verification_database}
            ):
                raise ReleaseError(
                    "unknown or unregistered databases block 0012 maintenance: "
                    + ", ".join(sorted(extra_databases))
                )
            owner = self._load_verification_owner(verification_database)
            if owner["status"] not in {"create-intent", "created"}:
                raise ReleaseError(
                    "verification database cleanup lacks a live ownership marker"
                )
        if set(databases).intersection(base_databases) != base_databases:
            missing = sorted(base_databases.difference(databases))
            raise ReleaseError(
                "0012 database inventory is missing required system/target databases: "
                + ", ".join(missing)
            )
        expected_flags = {
            "nexpoly": (False, True),
            "postgres": (False, True),
            "template0": (True, False),
            "template1": (True, True),
        }
        for name, (is_template, allow_connections) in expected_flags.items():
            if (
                databases[name]["is_template"] != is_template
                or databases[name]["allow_connections"] != allow_connections
            ):
                raise ReleaseError(
                    f"0012 database inventory has an unexpected purpose/configuration for {name}"
                )
        if verification_database in databases and (
            databases[verification_database]["is_template"] is not False
            or databases[verification_database]["allow_connections"] is not True
        ):
            raise ReleaseError("verification database has an invalid purpose/configuration")
        for name in registered_databases.intersection(databases):
            if (
                databases[name]["is_template"] is not False
                or databases[name]["allow_connections"] is not True
            ):
                raise ReleaseError(
                    f"registered database {name} has an invalid purpose/configuration"
                )

        ledger = payload.get("ledger")
        if not isinstance(ledger, list) or any(
            not isinstance(record, dict)
            or set(record) != {"version", "checksum"}
            or not isinstance(record.get("version"), str)
            or SAFE_MIGRATION_RE.fullmatch(record["version"]) is None
            or not isinstance(record.get("checksum"), str)
            or MIGRATION_CHECKSUM_RE.fullmatch(record["checksum"]) is None
            for record in ledger
        ):
            raise ReleaseError("0012 database inventory has an invalid migration ledger")
        expected_before = self._canonical_contract_ledger_prefix(
            include_contract=False
        )
        expected_after = self._canonical_contract_ledger_prefix(
            include_contract=True
        )
        valid_ledgers = [expected_before]
        if allow_contract:
            valid_ledgers.append(expected_after)
        if ledger not in valid_ledgers:
            raise ReleaseError(
                "0012 maintenance requires the exact canonical migration ledger prefix"
            )
        contract_present = ledger == expected_after
        if payload["legacy_relation_present"] == contract_present:
            raise ReleaseError(
                "0012 database inventory relation state conflicts with its migration ledger"
            )
        result = dict(payload)
        result["database_purposes"] = {
            "nexpoly": "production-target",
            "postgres": "cluster-maintenance",
            "template0": "system-template-no-connect",
            "template1": "system-template",
        }
        if "nexpoly_dev" in databases:
            result["database_purposes"]["nexpoly_dev"] = (
                "registered-development-read-only-audit"
            )
        if "nexpoly_md_health_opt" in databases:
            result["database_purposes"]["nexpoly_md_health_opt"] = (
                "registered-temporary-md-health-read-only-audit"
            )
        if verification_database in databases:
            owner = self._load_verification_owner(verification_database)
            result["database_purposes"][verification_database] = {
                "create-intent": "operation-owned-isolated-restore-create-intent",
                "created": "operation-owned-isolated-restore",
            }[owner["status"]]
        return result

    def _validate_registered_database_audit(
        self,
        payload: object,
        environment: dict[str, str],
        database: str,
        *,
        expected_user: str | None = None,
    ) -> dict[str, Any]:
        if database not in {"nexpoly_dev", "nexpoly_md_health_opt"}:
            raise ReleaseError("unregistered database audit was requested")
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "database",
            "current_user",
            "transaction_read_only",
            "ledger",
            "legacy_relation_present",
        }:
            raise ReleaseError(f"registered database {database} audit has an invalid shape")
        if (
            payload.get("schema_version") != 1
            or payload.get("database") != database
            or payload.get("current_user")
            != (expected_user or environment.get("NEXPOLY_POSTGRES_USER"))
            or payload.get("transaction_read_only") is not True
            or not isinstance(payload.get("legacy_relation_present"), bool)
        ):
            raise ReleaseError(
                f"registered database {database} audit has an invalid identity"
            )
        ledger = payload.get("ledger")
        if not isinstance(ledger, list) or any(
            not isinstance(record, dict)
            or set(record) != {"version", "checksum"}
            or not isinstance(record.get("version"), str)
            or SAFE_MIGRATION_RE.fullmatch(record["version"]) is None
            or not isinstance(record.get("checksum"), str)
            or MIGRATION_CHECKSUM_RE.fullmatch(record["checksum"]) is None
            for record in ledger
        ):
            raise ReleaseError(
                f"registered database {database} audit has an invalid ledger"
            )
        after = self._canonical_contract_ledger_prefix(include_contract=True)
        if database == "nexpoly_dev":
            if ledger != after or payload["legacy_relation_present"] is not False:
                raise ReleaseError(
                    "nexpoly_dev must already contain the exact canonical 0012 contract"
                )
        else:
            if ledger not in self._canonical_contract_ledger_prefixes():
                raise ReleaseError(
                    "nexpoly_md_health_opt must have an exact known canonical ledger prefix"
                )
            if payload["legacy_relation_present"] is not self._legacy_relation_expected(
                ledger
            ):
                raise ReleaseError(
                    "nexpoly_md_health_opt relation state conflicts with its ledger"
                )
        return dict(payload)

    def _validate_external_database_inventory(
        self,
        payload: object,
        environment: dict[str, str],
    ) -> dict[str, Any]:
        """Validate mandatory, read-only evidence from independent DB stacks."""

        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "inventory_complete",
            "writable_target",
            "databases",
        }:
            raise ReleaseError("external database inventory has an invalid shape")
        if payload.get("schema_version") != 1 or payload.get("inventory_complete") is not True:
            raise ReleaseError("external database inventory is incomplete")
        if payload.get("writable_target") != {
            "stack": "production",
            "database": "nexpoly",
        }:
            raise ReleaseError(
                "external database registry must identify production/nexpoly as its only writable target"
            )
        raw_databases = payload.get("databases")
        if not isinstance(raw_databases, list):
            raise ReleaseError("external database inventory has no database list")

        expected_databases = set(CONTRACT_0012_EXTERNAL_AUDIT_USERS)
        records: dict[str, dict[str, Any]] = {}
        expected_fields = {
            "stack",
            "database",
            "current_user",
            "transaction_read_only",
            "role_superuser",
            "role_create_db",
            "role_create_role",
            "ledger",
            "legacy_relation_present",
        }
        for record in raw_databases:
            if not isinstance(record, dict) or set(record) != expected_fields:
                raise ReleaseError("external database inventory contains an invalid record")
            stack = record.get("stack")
            database = record.get("database")
            if (
                not isinstance(stack, str)
                or stack not in expected_databases
                or stack in records
                or database != stack
            ):
                raise ReleaseError(
                    "external database inventory contains an unknown, duplicate, or mismatched stack"
                )
            user_key = CONTRACT_0012_EXTERNAL_AUDIT_USERS[stack]
            expected_user = environment.get(user_key)
            if (
                not isinstance(expected_user, str)
                or re.fullmatch(r"[a-z_][a-z0-9_-]{0,62}", expected_user) is None
            ):
                raise ReleaseError(
                    f"0012 maintenance requires a pinned read-only audit user in {user_key}"
                )
            if (
                record.get("transaction_read_only") is not True
                or record.get("role_superuser") is not False
                or record.get("role_create_db") is not False
                or record.get("role_create_role") is not False
            ):
                raise ReleaseError(
                    f"external database audit for {stack} is not provably read-only"
                )
            normalized = {
                "schema_version": 1,
                "database": database,
                "current_user": record.get("current_user"),
                "transaction_read_only": record.get("transaction_read_only"),
                "ledger": record.get("ledger"),
                "legacy_relation_present": record.get("legacy_relation_present"),
            }
            self._validate_registered_database_audit(
                normalized,
                environment,
                stack,
                expected_user=expected_user,
            )
            records[stack] = dict(record)

        if set(records) != expected_databases:
            missing = sorted(expected_databases.difference(records))
            raise ReleaseError(
                "external database inventory is missing required stacks: "
                + ", ".join(missing)
            )
        return {
            "schema_version": 1,
            "inventory_complete": True,
            "writable_target": {
                "stack": "production",
                "database": "nexpoly",
            },
            "databases": [records[name] for name in sorted(records)],
        }

    def _capture_external_database_inventory(
        self,
        environment: dict[str, str],
    ) -> dict[str, Any]:
        configured = environment.get(CONTRACT_0012_EXTERNAL_AUDIT_COMMAND)
        if not isinstance(configured, str) or not configured.strip():
            raise ReleaseError(
                "0012 maintenance requires the external database audit command"
            )
        command = self.controller.bootstrap_hook_command(
            environment,
            CONTRACT_0012_EXTERNAL_AUDIT_COMMAND,
        )
        print(f"[release-controller] {shlex.join(command)}")
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                env=environment,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            raise ReleaseError("external database audit command failed") from exc
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ReleaseError("external database audit command returned invalid JSON") from exc
        return self._validate_external_database_inventory(payload, environment)

    def _pre_destructive_database_gate(
        self,
        environment: dict[str, str],
        *,
        allow_contract: bool = False,
        allow_owned_verification: bool = False,
        recorded_external_inventory: object | None = None,
    ) -> dict[str, Any]:
        inventory = self._validate_database_inventory(
            self._capture_json(CONTRACT_0012_INVENTORY_PROGRAM, environment),
            environment,
            allow_contract=allow_contract,
            allow_owned_verification=allow_owned_verification,
        )
        database_names = {record["name"] for record in inventory["databases"]}
        registered_audits: dict[str, dict[str, Any]] = {}
        for database in sorted(
            database_names.intersection({"nexpoly_dev", "nexpoly_md_health_opt"})
        ):
            audit = self._capture_json(
                CONTRACT_0012_DATABASE_AUDIT_PROGRAM,
                self._database_environment(environment, database),
            )
            registered_audits[database] = self._validate_registered_database_audit(
                audit,
                environment,
                database,
            )
        inventory["registered_database_audits"] = registered_audits
        external_inventory = (
            self._capture_external_database_inventory(environment)
            if recorded_external_inventory is None
            else self._validate_external_database_inventory(
                recorded_external_inventory,
                environment,
            )
        )
        external_by_database = {
            record["database"]: record
            for record in external_inventory["databases"]
        }
        for database, audit in registered_audits.items():
            external = external_by_database[database]
            if (
                audit["ledger"] != external["ledger"]
                or audit["legacy_relation_present"]
                is not external["legacy_relation_present"]
            ):
                raise ReleaseError(
                    f"same-cluster and external audit evidence disagree for {database}"
                )
        inventory["external_registered_database_inventory"] = external_inventory
        return inventory

    def _validate_archive_evidence(self, payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "row_count",
            "status_counts",
            "rows_sha256",
            "schema_sha256",
            "structure_counts",
        }:
            raise ReleaseError("0012 archive evidence has an invalid shape")
        if (
            payload.get("schema_version") != 2
            or payload.get("row_count") != self.EXPECTED_ROWS
            or payload.get("status_counts") != self.EXPECTED_STATUS_COUNTS
        ):
            raise ReleaseError("0012 archive evidence differs from the reviewed 9-row history")
        for key in ("rows_sha256", "schema_sha256"):
            value = payload.get(key)
            if not isinstance(value, str) or MIGRATION_CHECKSUM_RE.fullmatch(value) is None:
                raise ReleaseError(f"0012 archive evidence has an invalid {key}")
        structure_counts = payload.get("structure_counts")
        if (
            not isinstance(structure_counts, dict)
            or set(structure_counts) != {"columns", "indexes", "constraints", "triggers"}
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in structure_counts.values()
            )
        ):
            raise ReleaseError("0012 archive evidence has invalid structure counts")
        return dict(payload)

    def _archive_legacy_table(
        self,
        environment: dict[str, str],
        previous_state: dict[str, Any],
        database_inventory: dict[str, Any],
    ) -> dict[str, Any]:
        if self.audit_dir.exists() or self.audit_dir.is_symlink():
            raise ReleaseError("0012 audit directory already exists")
        ensure_durable_directory(self.audit_dir)
        atomic_json(self.audit_dir / "release-state.before.json", previous_state)
        atomic_json(
            self.audit_dir / "database-inventory.before.json",
            database_inventory,
        )

        self.controller.backup_database(
            environment,
            str(previous_state["source_sha"]),
        )
        if self.controller.backup_path is None:
            raise ReleaseError("0012 maintenance did not create a full database backup")
        for source in (
            self.controller.backup_path,
            self.controller.backup_path.with_suffix(".dump.json"),
            self.controller.backup_path.with_suffix(".dump.sha256"),
        ):
            target = self.audit_dir / source.name
            shutil.copyfile(source, target)
            os.chmod(target, 0o600)
            fsync_regular_file(target)

        user = environment.get("NEXPOLY_POSTGRES_USER", "nexpoly")
        database = environment.get("NEXPOLY_POSTGRES_DB", "nexpoly")
        table_dump = self.audit_dir / "generation.polytao_jobs.dump"
        with table_dump.open("xb") as output:
            os.chmod(table_dump, 0o600)
            self.controller.run(
                self.controller.compose(
                    self.controller.candidate_dir,
                    "exec",
                    "-T",
                    "lab-postgres",
                    "pg_dump",
                    "-U",
                    user,
                    "-d",
                    database,
                    "-Fc",
                    "--table=generation.polytao_jobs",
                ),
                env=environment,
                stdout=output,
            )
        fsync_regular_file(table_dump)
        schema_dump = self.audit_dir / "generation.schema.sql"
        with schema_dump.open("xb") as output:
            os.chmod(schema_dump, 0o600)
            self.controller.run(
                self.controller.compose(
                    self.controller.candidate_dir,
                    "exec",
                    "-T",
                    "lab-postgres",
                    "pg_dump",
                    "-U",
                    user,
                    "-d",
                    database,
                    "--schema-only",
                    "--schema=generation",
                ),
                env=environment,
                stdout=output,
            )
        fsync_regular_file(schema_dump)
        evidence = self._validate_archive_evidence(
            self._capture_json(CONTRACT_0012_AUDIT_PROGRAM, environment)
        )
        atomic_json(self.audit_dir / "legacy-table-evidence.json", evidence)
        return evidence

    def _drop_owned_verification_database(
        self,
        environment: dict[str, str],
        owner: dict[str, Any],
    ) -> dict[str, Any]:
        verification_database = self._verification_database_name()
        if owner.get("status") not in {"create-intent", "created"}:
            raise ReleaseError(
                "verification database cannot be dropped without a live ownership marker"
            )
        self.controller.run(
            self.controller.compose(
                self.controller.candidate_dir,
                "exec",
                "-T",
                "lab-postgres",
                "dropdb",
                "--if-exists",
                "--force",
                "-U",
                environment.get("NEXPOLY_POSTGRES_USER", "nexpoly"),
                verification_database,
            ),
            env=environment,
        )
        return self._write_verification_owner(
            verification_database,
            "dropped",
            previous=owner,
        )

    def _reconcile_owned_verification_database(
        self,
        environment: dict[str, str],
        *,
        recorded_database_inventory: object | None = None,
        initial_inventory: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve an operation-owned verification intent before admission.

        ``createdb`` can commit on the server even when the client reports a
        failure. A durable create intent therefore remains authoritative until
        a fresh, checksum-bound inventory proves the reserved name absent or
        proves that the exact operation-owned database exists and can be
        removed. Inventory or cleanup uncertainty is propagated deliberately,
        leaving the global operation marker and drain in place.
        """

        verification_database = self._verification_database_name()
        path = self.verification_owner_path
        if not path.exists() and not path.is_symlink():
            return {
                "database": verification_database,
                "status": "not-created",
            }

        owner = self._load_verification_owner(verification_database)
        recorded_external_inventory = None
        if isinstance(recorded_database_inventory, dict):
            recorded_external_inventory = recorded_database_inventory.get(
                "external_registered_database_inventory"
            )

        inventory = initial_inventory
        if inventory is None:
            inventory = self._pre_destructive_database_gate(
                environment,
                allow_contract=True,
                allow_owned_verification=True,
                recorded_external_inventory=recorded_external_inventory,
            )
        database_names = {
            record.get("name")
            for record in inventory.get("databases", [])
            if isinstance(record, dict)
        }
        present_before_cleanup = verification_database in database_names

        if owner["status"] in {"create-intent", "created"}:
            if present_before_cleanup:
                owner = self._drop_owned_verification_database(
                    environment,
                    owner,
                )
            else:
                owner = self._write_verification_owner(
                    verification_database,
                    "dropped",
                    previous=owner,
                )
        elif owner["status"] != "dropped":  # defensive: loader is stricter
            raise ReleaseError("verification database has an invalid ownership state")

        verified = self._pre_destructive_database_gate(
            environment,
            allow_contract=True,
            allow_owned_verification=True,
            recorded_external_inventory=recorded_external_inventory,
        )
        remaining_names = {
            record.get("name")
            for record in verified.get("databases", [])
            if isinstance(record, dict)
        }
        if verification_database in remaining_names:
            raise ReleaseError(
                "operation-owned verification database remains after cleanup"
            )
        return {
            "database": verification_database,
            "status": owner["status"],
            "present_before_cleanup": present_before_cleanup,
            "verified_absent": True,
        }

    def _verify_full_restore(
        self,
        environment: dict[str, str],
        expected_evidence: dict[str, Any],
    ) -> None:
        if self.controller.backup_path is None:
            raise ReleaseError("isolated restore requires the verified full backup")
        verification_database = self._verification_database_name()
        user = environment.get("NEXPOLY_POSTGRES_USER", "nexpoly")
        release = self.controller.candidate_dir
        inventory = self._pre_destructive_database_gate(
            environment,
            allow_owned_verification=True,
        )
        database_names = {record["name"] for record in inventory["databases"]}
        previous_owner: dict[str, Any] | None = None
        if self.verification_owner_path.exists() or self.verification_owner_path.is_symlink():
            previous_owner = self._load_verification_owner(verification_database)
        if verification_database in database_names:
            # Cleanup is permitted only after the inventory gate proves the
            # exact database belongs to this operation marker.
            previous_owner = self._drop_owned_verification_database(
                environment,
                previous_owner,
            )
        owner = self._write_verification_owner(
            verification_database,
            "create-intent",
            previous=previous_owner,
            database_absent_before_create=(previous_owner is None),
        )
        createdb_completed = False
        try:
            self.controller.run(
                self.controller.compose(
                    release,
                    "exec",
                    "-T",
                    "lab-postgres",
                    "createdb",
                    "-U",
                    user,
                    "--template=template0",
                    verification_database,
                ),
                env=environment,
            )
            createdb_completed = True
            owner = self._write_verification_owner(
                verification_database,
                "created",
                previous=owner,
            )
            with self.controller.backup_path.open("rb") as source:
                self.controller.run(
                    self.controller.compose(
                        release,
                        "exec",
                        "-T",
                        "lab-postgres",
                        "pg_restore",
                        "--exit-on-error",
                        "--single-transaction",
                        "--no-owner",
                        "--no-privileges",
                        "-U",
                        user,
                        "-d",
                        verification_database,
                    ),
                    env=environment,
                    stdin=source,
                )
            restored = self._validate_archive_evidence(
                self._capture_json(
                    CONTRACT_0012_AUDIT_PROGRAM,
                    self._database_environment(environment, verification_database),
                )
            )
            if restored != expected_evidence:
                raise ReleaseError("isolated full-backup restore differs from live archive evidence")
            atomic_json(self.audit_dir / "isolated-restore-evidence.json", restored)
        finally:
            current_owner = self._load_verification_owner(verification_database)
            if current_owner["status"] == "created" or (
                current_owner["status"] == "create-intent"
                and createdb_completed
            ):
                self._drop_owned_verification_database(
                    environment,
                    current_owner,
                )
            elif createdb_completed:
                raise ReleaseError(
                    "created verification database lacks a completed ownership marker"
                )

    def _audit_manifest(self) -> dict[str, Any]:
        records = []
        for path in sorted(self.audit_dir.iterdir()):
            if path.name == "AUDIT-MANIFEST.json" or not path.is_file() or path.is_symlink():
                continue
            records.append(
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
        manifest = {
            "schema_version": 1,
            "operation_id": self.operation_id,
            "contract_version": POLYTAO_SCHEMA_COMPATIBILITY_FLOOR,
            "contract_checksum": POLYTAO_CONTRACT_CHECKSUM,
            "files": records,
        }
        atomic_json(self.audit_dir / "AUDIT-MANIFEST.json", manifest)
        return self._validate_audit_manifest(manifest)

    def _validate_audit_manifest(self, manifest: object) -> dict[str, Any]:
        try:
            audit_status = self.audit_dir.lstat()
        except OSError as exc:
            raise ReleaseError("0012 audit directory is unavailable") from exc
        if (
            not stat.S_ISDIR(audit_status.st_mode)
            or self.audit_dir.is_symlink()
            or stat.S_IMODE(audit_status.st_mode) != 0o700
        ):
            raise ReleaseError("0012 audit directory must be a real mode-0700 directory")
        if not isinstance(manifest, dict) or set(manifest) != {
            "schema_version",
            "operation_id",
            "contract_version",
            "contract_checksum",
            "files",
        }:
            raise ReleaseError("0012 audit manifest has an invalid shape")
        if (
            manifest.get("schema_version") != 1
            or manifest.get("operation_id") != self.operation_id
            or manifest.get("contract_version") != POLYTAO_SCHEMA_COMPATIBILITY_FLOOR
            or manifest.get("contract_checksum") != POLYTAO_CONTRACT_CHECKSUM
            or not isinstance(manifest.get("files"), list)
        ):
            raise ReleaseError("0012 audit manifest has an invalid identity")
        seen: set[str] = set()
        for record in manifest["files"]:
            if not isinstance(record, dict) or set(record) != {"name", "size", "sha256"}:
                raise ReleaseError("0012 audit manifest contains an invalid file record")
            name = record.get("name")
            size = record.get("size")
            digest = record.get("sha256")
            if (
                not isinstance(name, str)
                or Path(name).name != name
                or name in {"", ".", "..", "AUDIT-MANIFEST.json"}
                or name in seen
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or not isinstance(digest, str)
                or DIGEST_RE.fullmatch(digest) is None
            ):
                raise ReleaseError("0012 audit manifest contains an invalid file record")
            seen.add(name)
            path = self.audit_dir / name
            try:
                path_status = path.lstat()
            except OSError as exc:
                raise ReleaseError(f"0012 audit file is unavailable: {name}") from exc
            if (
                not stat.S_ISREG(path_status.st_mode)
                or path.is_symlink()
                or stat.S_IMODE(path_status.st_mode) != 0o600
                or path_status.st_size != size
                or sha256_file(path) != digest
            ):
                raise ReleaseError(f"0012 audit file differs from its manifest: {name}")
        return dict(manifest)

    def _audit_manifest_from_marker(self, marker: dict[str, Any]) -> dict[str, Any]:
        path = self.audit_dir / "AUDIT-MANIFEST.json"
        expected_digest = marker.get("audit_manifest_sha256")
        if (
            not path.is_file()
            or path.is_symlink()
            or stat.S_IMODE(path.stat().st_mode) != 0o600
            or not isinstance(expected_digest, str)
            or DIGEST_RE.fullmatch(expected_digest) is None
            or sha256_file(path) != expected_digest
        ):
            raise ReleaseError("0012 recovery audit manifest is missing or differs from its marker")
        return self._validate_audit_manifest(load_manifest(path))

    def _success_journal(
        self,
        marker: dict[str, Any],
        approval: dict[str, Any],
        audit_manifest: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        backup = marker.get("database_backup")
        backup_digest = marker.get("database_backup_sha256")
        audit_digest = marker.get("audit_manifest_sha256")
        if (
            not isinstance(backup, str)
            or not isinstance(backup_digest, str)
            or DIGEST_RE.fullmatch(backup_digest) is None
            or not isinstance(audit_digest, str)
            or DIGEST_RE.fullmatch(audit_digest) is None
        ):
            raise ReleaseError("0012 success journal is missing immutable backup evidence")
        return {
            "schema_version": 1,
            "status": "success",
            "operation_id": self.operation_id,
            "source_sha": self.document["source_sha"],
            "approval": approval,
            "completed_at": approval["approved_at"],
            "database_backup": backup,
            "database_backup_sha256": backup_digest,
            "audit_manifest": audit_manifest,
            "audit_manifest_sha256": audit_digest,
            "verification": verification,
        }

    def _write_success_journal(self, journal: dict[str, Any]) -> None:
        if self.journal_path.exists() or self.journal_path.is_symlink():
            if (
                not self.journal_path.is_file()
                or self.journal_path.is_symlink()
                or load_manifest(self.journal_path) != journal
            ):
                raise ReleaseError("existing 0012 operation journal conflicts with recovery")
            return
        atomic_json(self.journal_path, journal)

    def _validate_success_journal(
        self,
        journal: object,
        approval: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(journal, dict) or set(journal) != {
            "schema_version",
            "status",
            "operation_id",
            "source_sha",
            "approval",
            "completed_at",
            "database_backup",
            "database_backup_sha256",
            "audit_manifest",
            "audit_manifest_sha256",
            "verification",
        }:
            raise ReleaseError("existing 0012 success journal has an invalid shape")
        if (
            journal.get("schema_version") != 1
            or journal.get("status") != "success"
            or journal.get("operation_id") != self.operation_id
            or journal.get("source_sha") != self.document["source_sha"]
            or journal.get("approval") != approval
            or journal.get("completed_at") != approval.get("approved_at")
            or journal.get("verification")
            != {"schema_version": 1, "verified": True}
        ):
            raise ReleaseError("existing 0012 success journal has an invalid identity")
        backup = journal.get("database_backup")
        backup_digest = journal.get("database_backup_sha256")
        if (
            not isinstance(backup, str)
            or not isinstance(backup_digest, str)
            or DIGEST_RE.fullmatch(backup_digest) is None
        ):
            raise ReleaseError("existing 0012 success journal has invalid backup evidence")
        backup_path = Path(backup)
        if (
            not backup_path.is_absolute()
            or backup_path.parent != self.root / "backups"
            or not backup_path.is_file()
            or backup_path.is_symlink()
            or sha256_file(backup_path) != backup_digest
        ):
            raise ReleaseError("existing 0012 success journal backup differs from disk")
        audit_manifest = self._validate_audit_manifest(journal.get("audit_manifest"))
        audit_path = self.audit_dir / "AUDIT-MANIFEST.json"
        audit_digest = journal.get("audit_manifest_sha256")
        if (
            not isinstance(audit_digest, str)
            or DIGEST_RE.fullmatch(audit_digest) is None
            or not audit_path.is_file()
            or audit_path.is_symlink()
            or stat.S_IMODE(audit_path.stat().st_mode) != 0o600
            or sha256_file(audit_path) != audit_digest
            or load_manifest(audit_path) != audit_manifest
        ):
            raise ReleaseError("existing 0012 success journal audit evidence differs from disk")
        return dict(journal)

    def _restore_previous_database(
        self,
        environment: dict[str, str],
        previous_state: dict[str, Any],
        *,
        recorded_database_inventory: object | None = None,
    ) -> None:
        recorded_external_inventory = None
        if isinstance(recorded_database_inventory, dict):
            recorded_external_inventory = recorded_database_inventory.get(
                "external_registered_database_inventory"
            )
        self._pre_destructive_database_gate(
            environment,
            allow_contract=True,
            allow_owned_verification=True,
            recorded_external_inventory=recorded_external_inventory,
        )
        release = self.controller.candidate_dir
        self.controller.run(
            self.controller.compose(release, "stop", "nginx", "backend"),
            env=environment,
        )
        if release_uses_worker(self.document):
            self.controller.run(
                ["systemctl", "--user", "stop", "nexpoly-monomer-md-worker.service"],
                env=environment,
            )
        self.controller.restore_database(environment, release=release)
        atomic_json(self.state_path, previous_state)
        self.controller.run(
            self.controller.compose(
                release,
                "up",
                "-d",
                "--no-build",
                "--wait",
                "--wait-timeout",
                "300",
                "lab-postgres",
                "backend",
            ),
            env=environment,
        )
        if release_uses_worker(self.document):
            self.controller.run(
                ["systemctl", "--user", "restart", "nexpoly-monomer-md-worker.service"],
                env=environment,
            )
            self.controller.wait_for_worker_health(environment, expected_release=release)
        self.controller.backend_healthcheck(environment, release=release)
        self.controller.run(
            self.controller.compose(
                release,
                "up",
                "-d",
                "--no-build",
                "--wait",
                "--wait-timeout",
                "120",
                "nginx",
            ),
            env=environment,
        )

    def _resume_admission(
        self,
        environment: dict[str, str],
        *,
        worker_was_drained: bool,
    ) -> None:
        if worker_was_drained and release_uses_worker(self.document):
            self.controller.resume_worker(environment)
        self.controller.drain(environment, False)

    def _recover(self, marker: dict[str, Any]) -> dict[str, Any]:
        if marker.get("operation_id") != self.operation_id:
            raise ReleaseError("a different 0012 maintenance operation requires recovery")
        if marker.get("source_sha") != self.document["source_sha"]:
            raise ReleaseError("0012 recovery marker belongs to a different release")
        previous_state = marker.get("previous_state")
        if not isinstance(previous_state, dict):
            raise ReleaseError("0012 recovery marker is missing previous release state")
        environment = self.controller.environment()
        current_release = self._bind_current_release(previous_state)
        self.controller.candidate_dir = current_release
        self.controller.previous_state = previous_state
        backup_path = marker.get("database_backup")
        if isinstance(backup_path, str):
            self.controller.backup_path = self.controller.marker_backup(marker)

        current_state = self._load_current_state(allow_completed_contract=True)
        recorded_database_inventory = marker.get("database_inventory")
        recorded_external_inventory = None
        if isinstance(recorded_database_inventory, dict):
            recorded_external_inventory = recorded_database_inventory.get(
                "external_registered_database_inventory"
            )
        inventory = self._pre_destructive_database_gate(
            environment,
            allow_contract=True,
            allow_owned_verification=True,
            recorded_external_inventory=recorded_external_inventory,
        )
        self._reconcile_owned_verification_database(
            environment,
            recorded_database_inventory=recorded_database_inventory,
            initial_inventory=inventory,
        )
        approved = self._approved_record(current_state)
        if (
            approved is not None
            and approved.get("checksum") == POLYTAO_CONTRACT_CHECKSUM
            and approved.get("operation_id") == self.operation_id
        ):
            verification = self._capture_json(CONTRACT_0012_VERIFY_PROGRAM, environment)
            if verification.get("verified") is not True:
                raise ReleaseError("committed 0012 operation could not be re-verified")
            audit_manifest = self._audit_manifest_from_marker(marker)
            self._write_success_journal(
                self._success_journal(
                    marker,
                    approved,
                    audit_manifest,
                    verification,
                )
            )
            self.controller.run(
                self.controller.compose(
                    current_release,
                    "up",
                    "-d",
                    "--no-build",
                    "--wait",
                    "--wait-timeout",
                    "120",
                    "nginx",
                ),
                env=environment,
            )
            self._resume_admission(
                environment,
                worker_was_drained=marker.get("worker_drain_attempted") is True,
            )
            durable_unlink(self.marker_path)
            return current_state

        if marker.get("database_change_started") is True:
            self._restore_previous_database(
                environment,
                previous_state,
                recorded_database_inventory=recorded_database_inventory,
            )
        self._resume_admission(
            environment,
            worker_was_drained=(
                marker.get("worker_drain_attempted") is True
                and marker.get("database_change_started") is not True
            ),
        )
        recovered_journal = {
            "schema_version": 1,
            "status": "recovered",
            "operation_id": self.operation_id,
            "source_sha": self.document["source_sha"],
            "recovered_at": utc_now(),
            "database_restored": marker.get("database_change_started") is True,
            "retry_requires_new_operation_id": True,
        }
        if self.journal_path.exists() or self.journal_path.is_symlink():
            if not self.journal_path.is_file() or self.journal_path.is_symlink():
                raise ReleaseError("interrupted 0012 operation journal is unsafe")
            existing_journal = load_manifest(self.journal_path)
            if (
                existing_journal.get("operation_id") != self.operation_id
                or existing_journal.get("source_sha") != self.document["source_sha"]
                or existing_journal.get("status") not in {"failed", "recovered"}
            ):
                raise ReleaseError("interrupted 0012 operation journal conflicts with recovery")
            if existing_journal.get("status") == "failed":
                recovered_journal["previous_failure"] = existing_journal
                atomic_json(self.journal_path, recovered_journal)
        else:
            atomic_json(self.journal_path, recovered_journal)
        durable_unlink(self.marker_path)
        raise ReleaseError(
            "recovered an interrupted 0012 operation; retry with a new operation ID"
        )

    def run(self) -> dict[str, Any]:
        self.controller.ensure_root()
        if not self.apply:
            return self.plan()
        os.umask(0o077)
        with self.controller.deployment_lock():
            if self.marker_path.exists():
                return self._recover(load_manifest(self.marker_path))
            if self.controller.in_progress_path.exists():
                raise ReleaseError("a code deployment requires recovery before 0012 maintenance")
            previous_state = self._load_current_state(allow_completed_contract=True)
            existing_approval = self._approved_record(previous_state)
            if existing_approval is not None:
                if (
                    existing_approval.get("checksum") == POLYTAO_CONTRACT_CHECKSUM
                    and existing_approval.get("operation_id") == self.operation_id
                ):
                    if (
                        not self.journal_path.is_file()
                        or self.journal_path.is_symlink()
                    ):
                        raise ReleaseError(
                            "0012 approval exists but its success journal is unavailable"
                        )
                    self._validate_success_journal(
                        load_manifest(self.journal_path),
                        existing_approval,
                    )
                    return previous_state
                raise ReleaseError("0012 is already approved by a different operation")
            if POLYTAO_SCHEMA_COMPATIBILITY_FLOOR in previous_state.get("migrations", []):
                raise ReleaseError(
                    "release-state already records 0012 without a valid maintenance approval"
                )
            if self.journal_path.exists() or self.journal_path.is_symlink():
                raise ReleaseError(
                    "0012 operation ID already has a journal; choose a new operation ID"
                )
            if self.audit_dir.exists() or self.audit_dir.is_symlink():
                raise ReleaseError(
                    "0012 operation ID already has audit evidence; choose a new operation ID"
                )
            environment = self.controller.environment()
            current_release = self._bind_current_release(previous_state)
            self.controller.candidate_dir = current_release
            self.controller.previous_state = previous_state
            self.controller.validate_current_runtime(environment)
            database_inventory = self._pre_destructive_database_gate(environment)

            marker: dict[str, Any] = {
                **self.plan(),
                "schema_version": 1,
                "status": "running",
                "phase": "prepared",
                "started_at": utc_now(),
                "previous_state": previous_state,
                "release_manifest_sha256": sha256_file(self.controller.manifest_path),
                "drain_attempted": False,
                "worker_drain_attempted": False,
                "database_change_started": False,
                "database_inventory": database_inventory,
            }
            self._write_marker(marker)
            worker_drained = False
            state_committed = False
            try:
                marker["drain_attempted"] = True
                self._write_marker(marker)
                self.controller.drain(environment, True)
                if release_uses_worker(self.document):
                    marker["worker_drain_attempted"] = True
                    self._write_marker(marker)
                    marker["worker_drain"] = self.controller.drain_worker(environment)
                    worker_drained = True
                    self._write_marker(marker)
                self.controller.wait_for_jobs(environment)
                marker["phase"] = "drained"
                self._write_marker(marker)

                evidence = self._archive_legacy_table(
                    environment,
                    previous_state,
                    database_inventory,
                )
                marker.update(
                    {
                        "phase": "backed-up",
                        "database_backup": str(self.controller.backup_path),
                        "database_backup_sha256": sha256_file(self.controller.backup_path),
                        "archive_evidence": evidence,
                    }
                )
                self._write_marker(marker)
                self._verify_full_restore(environment, evidence)
                audit_manifest = self._audit_manifest()
                marker["audit_manifest_sha256"] = sha256_file(
                    self.audit_dir / "AUDIT-MANIFEST.json"
                )
                marker["database_change_started"] = True
                marker["phase"] = "database-change-started"
                self._write_marker(marker)

                applied = self.controller.run_migrations(
                    environment,
                    mode="contract-0012",
                )
                if applied != [POLYTAO_SCHEMA_COMPATIBILITY_FLOOR]:
                    raise ReleaseError("0012 maintenance did not apply exactly the reviewed contract")
                marker["phase"] = "contract-applied"
                self._write_marker(marker)
                verification = self._capture_json(CONTRACT_0012_VERIFY_PROGRAM, environment)
                if verification.get("verified") is not True:
                    raise ReleaseError("0012 post-migration verification failed")
                self.controller.backend_healthcheck(environment, release=current_release)

                # Public ingress remains stopped while the smoke temporarily
                # opens only the persistent write gate inside the host network.
                self.controller.run(
                    self.controller.compose(current_release, "stop", "nginx"),
                    env=environment,
                )
                marker["nginx_stopped"] = True
                marker["phase"] = "verifying"
                self._write_marker(marker)
                self.controller.run_ingress_isolated_contract_smoke(
                    environment,
                    release=current_release,
                )

                approved_at = utc_now()
                approval = {
                    "version": POLYTAO_SCHEMA_COMPATIBILITY_FLOOR,
                    "checksum": POLYTAO_CONTRACT_CHECKSUM,
                    "operation_id": self.operation_id,
                    "approved_at": approved_at,
                }
                next_state = json.loads(json.dumps(previous_state))
                next_state["migrations"] = merge_applied_migrations(
                    previous_state.get("migrations"),
                    [POLYTAO_SCHEMA_COMPATIBILITY_FLOOR],
                )
                next_state["applied_migrations"] = [POLYTAO_SCHEMA_COMPATIBILITY_FLOOR]
                next_state["approved_contracts"] = [
                    *previous_state.get("approved_contracts", []),
                    approval,
                ]
                next_state.pop("approved_contract_migrations", None)
                next_state["schema_compatibility_floor"] = {
                    "version": POLYTAO_SCHEMA_COMPATIBILITY_FLOOR,
                    "checksum": POLYTAO_CONTRACT_CHECKSUM,
                }
                next_state["migration_epoch_barrier"] = {
                    "epoch": 1,
                    "contract": {
                        "version": POLYTAO_SCHEMA_COMPATIBILITY_FLOOR,
                        "checksum": POLYTAO_CONTRACT_CHECKSUM,
                    },
                    "operation_id": self.operation_id,
                    "approved_at": approved_at,
                }
                next_state["last_contract_operation"] = self.operation_id
                journal = self._success_journal(
                    marker,
                    approval,
                    audit_manifest,
                    verification,
                )
                atomic_json(self.state_path, next_state)
                state_committed = True
                marker["phase"] = "state-committed"
                self._write_marker(marker)
                self._write_success_journal(journal)
                self.controller.run(
                    self.controller.compose(
                        current_release,
                        "up",
                        "-d",
                        "--no-build",
                        "--wait",
                        "--wait-timeout",
                        "120",
                        "nginx",
                    ),
                    env=environment,
                )
                self._resume_admission(
                    environment,
                    worker_was_drained=worker_drained,
                )
                durable_unlink(self.marker_path)
                return next_state
            except Exception as exc:
                marker.update(
                    {
                        "status": "failed" if not state_committed else "resume-pending",
                        "failed_at": utc_now(),
                        "error": str(exc)[:500],
                    }
                )
                self._write_marker(marker)
                if state_committed:
                    raise
                rollback_error: Exception | None = None
                try:
                    marker["verification_database_cleanup"] = (
                        self._reconcile_owned_verification_database(
                            environment,
                            recorded_database_inventory=marker.get(
                                "database_inventory"
                            ),
                        )
                    )
                    self._write_marker(marker)
                    if marker.get("database_change_started") is True:
                        self._restore_previous_database(
                            environment,
                            previous_state,
                            recorded_database_inventory=marker.get(
                                "database_inventory"
                            ),
                        )
                    elif marker.get("nginx_stopped") is True:
                        self.controller.run(
                            self.controller.compose(
                                current_release,
                                "up",
                                "-d",
                                "--no-build",
                                "--wait",
                                "--wait-timeout",
                                "120",
                                "nginx",
                            ),
                            env=environment,
                        )
                    self._resume_admission(
                        environment,
                        worker_was_drained=(
                            worker_drained
                            and marker.get("database_change_started") is not True
                        ),
                    )
                except Exception as recovery_exc:  # fail closed with marker + drain
                    rollback_error = recovery_exc
                atomic_json(
                    self.journal_path,
                    {
                        "schema_version": 1,
                        "status": "failed",
                        "operation_id": self.operation_id,
                        "source_sha": self.document["source_sha"],
                        "failed_at": marker["failed_at"],
                        "error": marker["error"],
                        "rollback": "failed" if rollback_error else "success",
                        "rollback_error": str(rollback_error)[:500] if rollback_error else None,
                    },
                )
                if rollback_error is not None:
                    raise ReleaseError(
                        "0012 maintenance failed and rollback is incomplete; admission remains drained"
                    ) from rollback_error
                durable_unlink(self.marker_path)
                raise


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build-manifest")
    build.add_argument("--sha", required=True)
    build.add_argument("--ci-run-id", required=True)
    build.add_argument("--backend-image", required=True)
    build.add_argument("--web-image", required=True)
    build.add_argument("--release-bundle", required=True)
    build.add_argument("--release-input", default="release-input.json")
    build.add_argument("--migration", action="append", default=[])
    build.add_argument("--migration-manifest")
    build.add_argument("--output", required=True)

    verify = commands.add_parser("verify-manifest")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--sha")

    deploy = commands.add_parser("deploy")
    deploy.add_argument("--manifest", required=True)
    deploy.add_argument("--mode", choices=("auto", "bootstrap"), default="auto")
    deploy.add_argument("--production-root", default=os.environ.get("NEXPOLY_PRODUCTION_ROOT", str(PRODUCTION_ROOT)))
    deploy.add_argument("--apply", action="store_true")

    contract = commands.add_parser(
        "maintain-contract-0012",
        help="archive, restore-verify, and apply only the checksum-pinned 0012 contract",
    )
    contract.add_argument("--manifest", required=True)
    contract.add_argument("--operation-id", required=True)
    contract.add_argument(
        "--production-root",
        default=os.environ.get("NEXPOLY_PRODUCTION_ROOT", str(PRODUCTION_ROOT)),
    )
    contract.add_argument("--apply", action="store_true")

    worker_identity = commands.add_parser(
        "worker-base-identity",
        help="print the immutable identity to pin for a frozen Worker base Python",
    )
    worker_identity.add_argument("--python", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "build-manifest":
            document = build_manifest(args)
        elif args.command == "verify-manifest":
            document = verify_manifest_command(args)
        elif args.command == "deploy":
            document = ReleaseController(Path(args.production_root), Path(args.manifest), args.mode, args.apply).deploy()
        elif args.command == "maintain-contract-0012":
            document = PolytaoContractMaintenance(
                Path(args.production_root),
                Path(args.manifest),
                args.operation_id,
                args.apply,
            ).run()
        elif args.command == "worker-base-identity":
            document = inspect_worker_base_python(args.python, None, os.environ.copy())
        else:  # pragma: no cover
            raise ReleaseError(f"unsupported command: {args.command}")
    except (ReleaseError, OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"release-controller: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(document, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
