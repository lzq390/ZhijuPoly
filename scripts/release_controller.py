#!/usr/bin/env python3
"""Build, verify, and safely apply immutable NexPoly releases.

Mutating commands are dry-run by default.  A real production change requires
both ``--apply`` and the exact production root.  This makes the same CLI useful
in CI policy tests without giving a typo permission to touch a running stack.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
import email.parser
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, BinaryIO, Callable, Iterable, NamedTuple
import urllib.parse
import urllib.request


CONTROLLER_DIRECTORY = Path(__file__).resolve().parent
if str(CONTROLLER_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(CONTROLLER_DIRECTORY))

from monomer_worker_env import (  # noqa: E402 - load the verified sibling helper
    SAFE_SYSTEM_PATH,
    WorkerEnvError,
    build_worker_process_environment,
    load_worker_env,
)


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
BYTEFF2_FORMAL_RUNTIME_ASSETS = (
    (
        "submodules/bytemol/bytemol/toolkit/infer_molecule/bond_length_ref.csv",
        802,
        "caa78ff02c7e65fb0c8bcf240382fa8d90b0dfea85a4d9888c96eab04cc4a40e",
    ),
    (
        "byteff2/trained_models/fftrainer_config_in_use.yaml",
        986,
        "8245a5c6ad9b4aa9d180c8bb24d6f05c210f1724ffae93aec0ef4f88e5fd7ea3",
    ),
    (
        "byteff2/trained_models/optimal.pt",
        111_892_932,
        "ae47a6e6860b563908a2e0a83d4a3f6adc1c36b48f544e2241d24066d43d539c",
    ),
)
BYTEFF2_GIT_SOURCE = "https://github.com/ByteDance-Seed/byteff2.git"
BYTEFF2_GIT_REVISION = "8f2813407ba5fbecfb5ec5c69e10b124c5b5bdc2"
BYTEFF2_AUDITED_OVERLAY_SOURCE = "https://huggingface.co/ByteDance-Seed/byteff2"
BYTEFF2_AUDITED_OVERLAY_REVISION = "b92ac49058c113625012c1f50d98a7bf9cf4e46e"
BYTEFF2_AUDITED_OVERLAY_ASSETS = BYTEFF2_FORMAL_RUNTIME_ASSETS[1:]
BYTEFF2_AUDITED_OVERLAY_SOURCE_PATHS = {
    "byteff2/trained_models/fftrainer_config_in_use.yaml": (
        "trained_models/fftrainer_config_in_use.yaml"
    ),
    "byteff2/trained_models/optimal.pt": "trained_models/optimal.pt",
}
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
    # Runtime readiness endpoints are a fixed production contract.  Neither
    # deploy.env nor the inherited shell may redirect a strict gate.
    "MONOMER_MD_WORKER_UDS",
    "NEXPOLY_MONOMER_MD_PROTOCOLS_URL",
    "NEXPOLY_MONOMER_MD_STATUS_URL",
    "NEXPOLY_POLYTAO_STATUS_URL",
    "NEXPOLY_WEB_BASE_URL",
}
MONOMER_MD_REQUIRE_TRANSPORT_READY = "MONOMER_MD_REQUIRE_TRANSPORT_READY"
MAX_RUNTIME_RESPONSE_BYTES = 64 * 1024
PRODUCTION_WEB_BASE_URL = "http://127.0.0.1:9000"
PRODUCTION_HEALTH_URL = f"{PRODUCTION_WEB_BASE_URL}/health"
PRODUCTION_MONOMER_STATUS_URL = (
    f"{PRODUCTION_WEB_BASE_URL}/api/v1/monomer-md/status"
)
PRODUCTION_MONOMER_PROTOCOLS_URL = (
    f"{PRODUCTION_WEB_BASE_URL}/api/v1/monomer-md/protocols"
)
PRODUCTION_POLYTAO_STATUS_URL = (
    f"{PRODUCTION_WEB_BASE_URL}/api/v1/conditional-generation/polytao/status"
)
RUNTIME_ENDPOINT_OVERRIDE_KEYS = frozenset(
    {
        "MONOMER_MD_WORKER_UDS",
        "NEXPOLY_HEALTH_URLS",
        "NEXPOLY_MONOMER_MD_PROTOCOLS_URL",
        "NEXPOLY_MONOMER_MD_STATUS_URL",
        "NEXPOLY_POLYTAO_STATUS_URL",
        "NEXPOLY_WEB_BASE_URL",
    }
)
PACKAGE_MANAGER_ENV_PREFIXES = ("PIP_", "UV_", "CONDA_")
CANDIDATE_SAFE_INHERITED_KEYS = frozenset(
    {"HOME", "LANG", "LC_ALL", "LOGNAME", "TMPDIR", "TZ", "USER"}
)
STABLE_WORKER_ENV_HELPER_NAME = "monomer_worker_env.py"
PROVISIONING_OWNER_NAME = ".provisioning-owner.json"
PROVISIONING_READY_NAME = "provisioning-ready.json"
PROVISIONING_SCHEMA_VERSION = 1
# The runtime owns a 10-second TERM window plus process-group/cgroup proof and
# Broker lease release after its nominal probe deadline.  The controller's
# outer watchdog must not interrupt that normal fenced cleanup path.
CANDIDATE_PREFLIGHT_INTERNAL_CLEANUP_ALLOWANCE_SECONDS = 25.0
CANDIDATE_PREFLIGHT_TERM_GRACE_SECONDS = 10.0
CANDIDATE_PREFLIGHT_KILL_WAIT_SECONDS = 5.0
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
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
# Development-only verifier retained for ``prepare_dev_worker_venv.py``. The
# production provision/deploy/recovery call graph uses the static filesystem
# verifier and never executes this program.
DEV_WORKER_VENV_VERIFY_PROGRAM = r'''
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
CANDIDATE_EXEC_GATE_PROGRAM = r'''
import os
import sys

try:
    descriptor = int(os.environ.pop("NEXPOLY_GPU_EXEC_GATE_FD", ""))
    admitted = os.read(descriptor, 1)
finally:
    try:
        os.close(descriptor)
    except (NameError, OSError):
        pass
if admitted != b"1" or len(sys.argv) < 3 or sys.argv[1] != "--":
    raise SystemExit(126)
os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
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


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep loopback readiness probes on their reviewed origin."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class _ProcessIdentity(NamedTuple):
    pid: int
    start_ticks: int
    parent_pid: int
    process_group: int
    state: str


def _read_process_identity(pid: int) -> _ProcessIdentity | None:
    """Read a same-user Linux process identity without trusting PID alone."""

    process_root = Path(f"/proc/{pid}")
    try:
        metadata = process_root.stat()
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as exc:
        raise ReleaseError(
            "candidate Worker runtime preflight process identity is unreadable"
        ) from exc
    if metadata.st_uid != os.geteuid():
        raise ReleaseError(
            "candidate Worker runtime preflight process owner is unexpected"
        )
    try:
        raw = (process_root / "stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, UnicodeError) as exc:
        raise ReleaseError(
            "candidate Worker runtime preflight process identity is unreadable"
        ) from exc
    closing_parenthesis = raw.rfind(")")
    if closing_parenthesis < 0:
        raise ReleaseError(
            "candidate Worker runtime preflight process identity is malformed"
        )
    fields = raw[closing_parenthesis + 1 :].split()
    if len(fields) <= 19:
        raise ReleaseError(
            "candidate Worker runtime preflight process identity is malformed"
        )
    try:
        identity = _ProcessIdentity(
            pid=pid,
            start_ticks=int(fields[19]),
            parent_pid=int(fields[1]),
            process_group=int(fields[2]),
            state=fields[0],
        )
    except (TypeError, ValueError) as exc:
        raise ReleaseError(
            "candidate Worker runtime preflight process identity is malformed"
        ) from exc
    if (
        identity.start_ticks <= 0
        or identity.parent_pid < 0
        or identity.process_group <= 0
        or len(identity.state) != 1
    ):
        raise ReleaseError(
            "candidate Worker runtime preflight process identity is malformed"
        )
    return identity


def _process_identity_is_live(identity: _ProcessIdentity) -> bool:
    current = _read_process_identity(identity.pid)
    return bool(
        current is not None
        and current.start_ticks == identity.start_ticks
        and current.state != "Z"
    )


def _set_child_subreaper(enabled: bool) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(
        PR_SET_CHILD_SUBREAPER,
        int(enabled),
        0,
        0,
        0,
    )
    if result != 0:
        raise ReleaseError(
            "candidate Worker runtime preflight containment is unavailable"
        )


def _child_subreaper_enabled() -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    value = ctypes.c_int()
    result = libc.prctl(
        PR_GET_CHILD_SUBREAPER,
        ctypes.byref(value),
        0,
        0,
        0,
    )
    if result != 0:
        raise ReleaseError(
            "candidate Worker runtime preflight containment is unavailable"
        )
    return value.value == 1


@contextlib.contextmanager
def _candidate_child_subreaper() -> Iterable[dict[int, _ProcessIdentity]]:
    """Adopt daemonized candidate descendants until cleanup is proven."""

    baseline_children = _direct_child_identities(os.getpid())
    if baseline_children:
        raise ReleaseError(
            "candidate Worker runtime preflight requires an exclusive child process scope"
        )
    was_enabled = _child_subreaper_enabled()
    if not was_enabled:
        _set_child_subreaper(True)
    try:
        yield baseline_children
    finally:
        consecutive_empty_scans = 0
        for _attempt in range(4):
            current_children = _direct_child_identities(os.getpid())
            _reap_candidate_zombies(current_children)
            current_children = _direct_child_identities(os.getpid())
            if current_children:
                raise ReleaseError(
                    "candidate Worker runtime preflight process cleanup could not be proven"
                )
            consecutive_empty_scans += 1
            if consecutive_empty_scans >= 2:
                break
            time.sleep(0.01)
        if consecutive_empty_scans < 2:  # pragma: no cover - defensive
            raise ReleaseError(
                "candidate Worker runtime preflight process cleanup could not be proven"
            )
        if not was_enabled:
            _set_child_subreaper(False)
            if _child_subreaper_enabled():
                raise ReleaseError(
                    "candidate Worker runtime preflight containment could not be restored"
                )


@contextlib.contextmanager
def _deferred_candidate_signals() -> Iterable[Callable[[], int | None]]:
    """Turn controller termination signals into cooperative cleanup requests."""

    received: list[int] = []
    previous: dict[signal.Signals, Any] = {}

    def defer(signal_number: int, _frame: Any) -> None:
        received.append(signal_number)

    for signal_number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous[signal_number] = signal.getsignal(signal_number)
        signal.signal(signal_number, defer)
    completed_normally = False
    try:
        yield lambda: received[-1] if received else None
        completed_normally = True
    finally:
        for signal_number, handler in previous.items():
            signal.signal(signal_number, handler)
    if completed_normally and received:
        raise ReleaseError(
            "candidate Worker runtime preflight was interrupted safely"
        )


def _direct_process_children(pid: int) -> tuple[int, ...]:
    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(
            encoding="ascii"
        )
    except (FileNotFoundError, ProcessLookupError) as exc:
        if pid == os.getpid():
            raise ReleaseError(
                "candidate Worker runtime preflight child inventory is unavailable"
            ) from exc
        return ()
    except (OSError, UnicodeError) as exc:
        raise ReleaseError(
            "candidate Worker runtime preflight child inventory is unreadable"
        ) from exc
    children: list[int] = []
    for value in raw.split():
        if not value.isascii() or not value.isdigit():
            raise ReleaseError(
                "candidate Worker runtime preflight child inventory is malformed"
            )
        child = int(value)
        if child <= 0 or child in children:
            raise ReleaseError(
                "candidate Worker runtime preflight child inventory is malformed"
            )
        children.append(child)
    return tuple(children)


def _direct_child_identities(pid: int) -> dict[int, _ProcessIdentity]:
    identities: dict[int, _ProcessIdentity] = {}
    for child_pid in _direct_process_children(pid):
        child = _read_process_identity(child_pid)
        if child is not None:
            identities[child_pid] = child
    return identities


def _adopt_candidate_children(
    identities: dict[int, _ProcessIdentity],
    baseline_children: dict[int, _ProcessIdentity],
) -> None:
    """Capture descendants reparented to this process by subreaper mode."""

    for pid, child in _direct_child_identities(os.getpid()).items():
        baseline = baseline_children.get(pid)
        if baseline is not None and baseline.start_ticks == child.start_ticks:
            continue
        identities.setdefault(pid, child)
    _extend_process_descendants(identities)


def _reap_candidate_zombies(
    identities: dict[int, _ProcessIdentity],
    *,
    excluded_pids: frozenset[int] = frozenset(),
) -> None:
    """Reap only verified candidate children adopted by this controller."""

    for identity in identities.values():
        if identity.pid in excluded_pids:
            # A subprocess.Popen root must be reaped by poll()/wait() so its
            # returncode and destructor state stay synchronized.
            continue
        current = _read_process_identity(identity.pid)
        if (
            current is None
            or current.start_ticks != identity.start_ticks
            or current.parent_pid != os.getpid()
            or current.state != "Z"
        ):
            continue
        try:
            os.waitpid(identity.pid, os.WNOHANG)
        except ChildProcessError:
            continue


def _extend_process_descendants(
    identities: dict[int, _ProcessIdentity],
) -> None:
    """Add currently reachable descendants while preserving start identities."""

    queue = list(identities)
    visited: set[int] = set()
    while queue:
        pid = queue.pop()
        if pid in visited:
            continue
        visited.add(pid)
        parent = identities.get(pid)
        if parent is None or not _process_identity_is_live(parent):
            continue
        for child_pid in _direct_process_children(pid):
            if child_pid not in identities:
                child = _read_process_identity(child_pid)
                if child is None:
                    continue
                identities[child_pid] = child
            queue.append(child_pid)


def _pidfd_open(pid: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    descriptor = libc.pidfd_open(pid, 0)
    if descriptor < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    return int(descriptor)


def _pidfd_send_signal(
    descriptor: int,
    signal_number: signal.Signals,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.pidfd_send_signal(descriptor, int(signal_number), None, 0)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _signal_verified_processes(
    identities: dict[int, _ProcessIdentity],
    signal_number: signal.Signals,
) -> None:
    for identity in identities.values():
        current = _read_process_identity(identity.pid)
        if current is None or current.start_ticks != identity.start_ticks:
            continue
        if current.pid == os.getpid() or current.state == "Z":
            continue
        try:
            descriptor = _pidfd_open(identity.pid)
        except ProcessLookupError:
            continue
        except OSError as exc:
            raise ReleaseError(
                "candidate Worker runtime preflight cleanup could not bind its process tree"
            ) from exc
        try:
            # Close the PID-reuse window between /proc validation and signal.
            current = _read_process_identity(identity.pid)
            if current is None or current.start_ticks != identity.start_ticks:
                continue
            _pidfd_send_signal(descriptor, signal_number)
        except ProcessLookupError:
            continue
        except OSError as exc:
            raise ReleaseError(
                "candidate Worker runtime preflight cleanup could not signal its process tree"
            ) from exc
        finally:
            os.close(descriptor)


def _freeze_candidate_process_tree(
    identities: dict[int, _ProcessIdentity],
    baseline_children: dict[int, _ProcessIdentity],
) -> None:
    """Stop a changing descendant tree before sending group termination."""

    if not identities:
        raise ReleaseError(
            "candidate Worker runtime preflight cleanup could not identify its process"
        )
    stable_stopped_scans = 0
    previous: set[int] | None = None
    for _attempt in range(100):
        _adopt_candidate_children(identities, baseline_children)
        _signal_verified_processes(identities, signal.SIGSTOP)
        _adopt_candidate_children(identities, baseline_children)
        live_states = {
            identity.pid: current.state
            for identity in identities.values()
            if (
                (current := _read_process_identity(identity.pid)) is not None
                and current.start_ticks == identity.start_ticks
                and current.state != "Z"
            )
        }
        current_pids = set(identities)
        if (
            current_pids == previous
            and all(state in {"T", "t"} for state in live_states.values())
        ):
            stable_stopped_scans += 1
        else:
            stable_stopped_scans = 0
        if stable_stopped_scans >= 2:
            return
        previous = current_pids
        time.sleep(0.01)
    raise ReleaseError(
        "candidate Worker runtime preflight cleanup could not stabilize its process tree"
    )


def _wait_for_candidate_process_tree(
    process: subprocess.Popen[bytes],
    identities: dict[int, _ProcessIdentity],
    baseline_children: dict[int, _ProcessIdentity],
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        process.poll()
        _adopt_candidate_children(identities, baseline_children)
        _reap_candidate_zombies(
            identities,
            excluded_pids=frozenset({process.pid}),
        )
        if not any(_process_identity_is_live(item) for item in identities.values()):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def normalized_deploy_boolean(value: object, name: str) -> bool:
    """Normalize one deploy-only boolean without accepting implicit values."""

    if not isinstance(value, str):
        raise ReleaseError(f"{name} must be true or false")
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ReleaseError(f"{name} must be true or false")


def decode_bounded_json_object(payload: bytes, label: str) -> dict[str, Any]:
    """Decode a bounded object without reflecting any response content."""

    if len(payload) > MAX_RUNTIME_RESPONSE_BYTES:
        raise ReleaseError(f"{label} exceeded the 64 KiB response limit")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ReleaseError(f"{label} returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ReleaseError(f"{label} returned an invalid shape")
    return decoded


def worker_transport_is_strict_ready(payload: object) -> bool:
    """Validate the strict Worker/status Transport readiness triple."""

    if not isinstance(payload, dict):
        return False
    protocols = payload.get("protocols")
    if not isinstance(protocols, dict):
        return False
    transport = protocols.get("Transport")
    return bool(
        isinstance(transport, dict)
        and transport.get("supported") is True
        and transport.get("runtime_ready") is True
        and "runtime_error" in transport
        and transport.get("runtime_error") is None
    )


def current_worker_allows_transport_repair(payload: object) -> bool:
    """Allow only an isolated Transport degradation during strict repair.

    The candidate is proven separately before any image, database, or release
    mutation.  This narrow compatibility path lets that candidate repair an
    older Worker whose top-level readiness included Transport, without
    accepting recovery, Broker, ByteFF2-root, database, or other protocol
    failures as a safe rollback target.
    """

    if not isinstance(payload, dict) or payload.get("status") != "degraded":
        return False
    active_jobs = payload.get("active_jobs")
    if (
        isinstance(active_jobs, bool)
        or not isinstance(active_jobs, int)
        or active_jobs < 0
        or payload.get("runtime_ready") is not False
        or payload.get("db_configured") is not True
        or payload.get("byteff2_root_exists") is not True
        or (
            payload.get("gpu_broker_enabled") is True
            and payload.get("gpu_broker_ready") is not True
        )
    ):
        return False
    protocols = payload.get("protocols")
    if not isinstance(protocols, dict) or not protocols:
        return False
    transport = protocols.get("Transport")
    if not isinstance(transport, dict):
        return False
    if (
        transport.get("supported") is not True
        or transport.get("runtime_ready") is not False
        or not isinstance(transport.get("runtime_error"), str)
        or not transport["runtime_error"]
    ):
        return False
    other_protocols = 0
    for protocol, health in protocols.items():
        if protocol == "Transport":
            continue
        other_protocols += 1
        if not isinstance(protocol, str) or not isinstance(health, dict):
            return False
        if (
            health.get("supported") is not True
            or health.get("runtime_ready") is not True
            or "runtime_error" not in health
            or health.get("runtime_error") is not None
        ):
            return False
    return other_protocols > 0


def protocol_catalog_transport_is_strict_ready(payload: object) -> bool:
    """Validate exactly one strict Transport record in Backend /protocols."""

    if not isinstance(payload, dict):
        return False
    protocols = payload.get("protocols")
    if not isinstance(protocols, list):
        return False
    matches = [
        item
        for item in protocols
        if isinstance(item, dict) and item.get("protocol") == "Transport"
    ]
    if len(matches) != 1:
        return False
    transport = matches[0]
    return bool(
        transport.get("supported") is True
        and transport.get("runtime_ready") is True
        and "runtime_error" in transport
        and transport.get("runtime_error") is None
    )


def candidate_preflight_transport_is_strict_ready(payload: object) -> bool:
    """Validate the candidate preflight's deliberately small safe schema."""

    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "runtime_ready",
        "transport",
    }:
        return False
    transport = payload.get("transport")
    return bool(
        payload.get("schema_version") == 1
        and not isinstance(payload.get("schema_version"), bool)
        and payload.get("runtime_ready") is True
        and isinstance(transport, dict)
        and set(transport) == {"supported", "runtime_ready", "runtime_error"}
        and transport.get("supported") is True
        and transport.get("runtime_ready") is True
        and transport.get("runtime_error") is None
    )


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


def directory_inventory_digest(
    root: Path,
    *,
    excluded_top_level: frozenset[str] = frozenset(),
) -> str:
    """Hash one deploy-user-owned tree without following any symlink.

    Records bind relative path, entry type, permissions, symlink target, file
    size, and file content.  This is intentionally independent from mtimes so
    a copied immutable bundle has the same identity, while any executable or
    installed-package mutation is still detected before deployment.
    """

    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise ReleaseError("provisioned directory inventory root is unavailable") from exc
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root.is_symlink()
        or root_metadata.st_uid != os.geteuid()
    ):
        raise ReleaseError("provisioned directory inventory root is unsafe")

    records: list[dict[str, object]] = [
        {
            "path": ".",
            "type": "directory",
            "mode": stat.S_IMODE(root_metadata.st_mode),
        }
    ]
    for current_raw, directories, files in os.walk(root, followlinks=False):
        current = Path(current_raw)
        relative_current = current.relative_to(root)
        if relative_current.parts and relative_current.parts[0] in excluded_top_level:
            directories[:] = []
            continue
        directories.sort()
        files.sort()
        retained_directories: list[str] = []
        for name in directories:
            relative = relative_current / name
            if relative.parts[0] in excluded_top_level:
                continue
            path = current / name
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise ReleaseError("provisioned directory inventory changed") from exc
            if metadata.st_uid != os.geteuid():
                raise ReleaseError("provisioned directory has a foreign owner")
            if stat.S_ISLNK(metadata.st_mode):
                try:
                    target = os.readlink(path)
                except OSError as exc:
                    raise ReleaseError("provisioned directory symlink is unreadable") from exc
                records.append(
                    {
                        "path": relative.as_posix(),
                        "type": "symlink",
                        "mode": stat.S_IMODE(metadata.st_mode),
                        "target": target,
                    }
                )
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                raise ReleaseError("provisioned directory contains an unsafe entry")
            records.append(
                {
                    "path": relative.as_posix(),
                    "type": "directory",
                    "mode": stat.S_IMODE(metadata.st_mode),
                }
            )
            retained_directories.append(name)
        directories[:] = retained_directories

        for name in files:
            relative = relative_current / name
            if relative.parts[0] in excluded_top_level:
                continue
            path = current / name
            try:
                before = path.lstat()
            except OSError as exc:
                raise ReleaseError("provisioned file inventory changed") from exc
            if before.st_uid != os.geteuid():
                raise ReleaseError("provisioned file has a foreign owner")
            if stat.S_ISLNK(before.st_mode):
                try:
                    target = os.readlink(path)
                except OSError as exc:
                    raise ReleaseError("provisioned file symlink is unreadable") from exc
                records.append(
                    {
                        "path": relative.as_posix(),
                        "type": "symlink",
                        "mode": stat.S_IMODE(before.st_mode),
                        "target": target,
                    }
                )
                continue
            if not stat.S_ISREG(before.st_mode):
                raise ReleaseError("provisioned directory contains a non-file entry")
            digest = sha256_file(path)
            try:
                after = path.lstat()
            except OSError as exc:
                raise ReleaseError("provisioned file changed while hashing") from exc
            identity_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns")
            if any(getattr(before, field) != getattr(after, field) for field in identity_fields):
                raise ReleaseError("provisioned file changed while hashing")
            records.append(
                {
                    "path": relative.as_posix(),
                    "type": "file",
                    "mode": stat.S_IMODE(before.st_mode),
                    "size": before.st_size,
                    "sha256": digest,
                }
            )
    return canonical_json_digest(records)


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


def validate_byteff2_audited_overlay(
    value: object,
    *,
    require_exact_identity: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"source", "revision", "files"}:
        raise ReleaseError("pinned asset manifest has invalid ByteFF2 overlay metadata")
    source = value.get("source")
    revision = value.get("revision")
    files = value.get("files")
    if (
        not isinstance(source, str)
        or not source
        or not isinstance(revision, str)
        or SHA_RE.fullmatch(revision) is None
        or not isinstance(files, list)
    ):
        raise ReleaseError("pinned asset manifest has invalid ByteFF2 overlay metadata")
    normalized: dict[str, tuple[str, int, str]] = {}
    for record in files:
        if not isinstance(record, dict) or set(record) != {
            "source_path",
            "path",
            "size",
            "sha256",
        }:
            raise ReleaseError("pinned asset manifest has invalid ByteFF2 overlay metadata")
        source_path = record.get("source_path")
        relative = record.get("path")
        size = record.get("size")
        checksum = record.get("sha256")
        source_pure = (
            PurePosixPath(source_path)
            if isinstance(source_path, str)
            else PurePosixPath(".")
        )
        pure = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath(".")
        if (
            not isinstance(source_path, str)
            or not source_path
            or source_pure.is_absolute()
            or ".." in source_pure.parts
            or str(source_pure) != source_path
            or not isinstance(relative, str)
            or not relative
            or pure.is_absolute()
            or ".." in pure.parts
            or str(pure) != relative
            or relative in normalized
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
            or not isinstance(checksum, str)
            or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
        ):
            raise ReleaseError("pinned asset manifest has invalid ByteFF2 overlay metadata")
        normalized[relative] = (source_path, size, checksum)
    if require_exact_identity:
        expected = {
            relative: (
                BYTEFF2_AUDITED_OVERLAY_SOURCE_PATHS[relative],
                size,
                checksum,
            )
            for relative, size, checksum in BYTEFF2_AUDITED_OVERLAY_ASSETS
        }
        if (
            source != BYTEFF2_AUDITED_OVERLAY_SOURCE
            or revision != BYTEFF2_AUDITED_OVERLAY_REVISION
            or normalized != expected
        ):
            raise ReleaseError(
                "candidate asset manifest has the wrong ByteFF2 audited overlay contract"
            )
    return dict(value)


def validate_byteff2_source(
    value: Any,
    *,
    manifest_commit: str,
    require_exact_identity: bool,
) -> dict[str, str]:
    """Bind a ByteFF2 tree to its authoritative Git source and revision."""

    if not isinstance(value, dict) or set(value) != {"source", "revision"}:
        raise ReleaseError("pinned asset manifest has invalid ByteFF2 source metadata")
    source = value.get("source")
    revision = value.get("revision")
    if (
        not isinstance(source, str)
        or not isinstance(revision, str)
        or SHA_RE.fullmatch(revision) is None
        or source != BYTEFF2_GIT_SOURCE
        or revision != manifest_commit
    ):
        raise ReleaseError("pinned asset manifest has invalid ByteFF2 source metadata")
    if require_exact_identity and revision != BYTEFF2_GIT_REVISION:
        raise ReleaseError(
            "candidate asset manifest has the wrong ByteFF2 Git source contract"
        )
    return {"source": source, "revision": revision}


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
    base_fields = {
        "schema_version",
        "byteff2_commit",
        "byteff2_submodules",
        "assets",
    }
    schema_version = document.get("schema_version") if isinstance(document, dict) else None
    schema_valid = bool(
        isinstance(document, dict)
        and (
            (schema_version == 1 and set(document) == base_fields)
            or (
                schema_version == 2
                and set(document)
                == base_fields | {"byteff2_source", "byteff2_audited_overlays"}
            )
        )
    )
    if (
        not schema_valid
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
    if schema_version == 2:
        validate_byteff2_source(
            document["byteff2_source"],
            manifest_commit=manifest_commit,
            require_exact_identity=False,
        )
        validate_byteff2_audited_overlay(
            document["byteff2_audited_overlays"],
            require_exact_identity=False,
        )
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


def _verify_candidate_byteff2_runtime_file(
    asset_root: Path,
    relative_value: str,
    expected_size: int,
    expected_digest: str,
) -> None:
    relative = PurePosixPath(relative_value)
    parent = asset_root / "byteff2"
    for component in relative.parts[:-1]:
        parent /= component
        try:
            parent_status = parent.lstat()
        except OSError as exc:
            raise ReleaseError(
                "candidate ByteFF2 formal runtime asset is missing or unsafe"
            ) from exc
        if not stat.S_ISDIR(parent_status.st_mode) or parent.is_symlink():
            raise ReleaseError(
                "candidate ByteFF2 formal runtime asset is missing or unsafe"
            )
    path = parent / relative.name
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseError(
            "candidate ByteFF2 formal runtime asset is missing or unsafe"
        ) from exc
    try:
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o222:
                raise ReleaseError(
                    "candidate ByteFF2 formal runtime asset must be a read-only regular file"
                )
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
            after = os.fstat(descriptor)
        except OSError as exc:
            raise ReleaseError(
                "candidate ByteFF2 formal runtime asset could not be verified"
            ) from exc
    finally:
        os.close(descriptor)
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if (
        before_identity != after_identity
        or size != before.st_size
        or size != expected_size
        or digest.hexdigest() != expected_digest
    ):
        raise ReleaseError(
            "candidate ByteFF2 formal runtime asset differs from its fixed manifest contract"
        )


def validate_candidate_byteff2_runtime_assets(asset_root: Path) -> None:
    """Require every exact asset needed by a formal ByteFF2 execution.

    Protocol import already reads the bond table, while force-field model files
    are loaded later. A content-addressed manifest can therefore be internally
    consistent yet false-ready. This contract applies only to the resolved
    candidate; the legacy current asset may be the defect the deployment fixes.
    """

    manifest_path = asset_root / "ASSET-MANIFEST.json"
    try:
        root_status = asset_root.lstat()
        manifest_status = manifest_path.lstat()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(
            "candidate ByteFF2 formal runtime asset manifest is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(root_status.st_mode)
        or asset_root.is_symlink()
        or not stat.S_ISREG(manifest_status.st_mode)
        or manifest_path.is_symlink()
    ):
        raise ReleaseError(
            "candidate ByteFF2 formal runtime asset manifest is unsafe"
        )
    assets = manifest.get("assets") if isinstance(manifest, dict) else None
    records = assets.get("byteff2") if isinstance(assets, dict) else None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        raise ReleaseError(
            "candidate asset manifest must use the audited ByteFF2 schema v2"
        )
    if not isinstance(records, list):
        raise ReleaseError(
            "candidate asset manifest omits a ByteFF2 formal runtime asset record"
        )
    if not isinstance(manifest, dict) or "byteff2_audited_overlays" not in manifest:
        raise ReleaseError(
            "candidate asset manifest omits the ByteFF2 audited overlay contract"
        )
    if "byteff2_source" not in manifest:
        raise ReleaseError(
            "candidate asset manifest omits the ByteFF2 Git source contract"
        )
    manifest_commit = require_sha(
        str(manifest.get("byteff2_commit", "")),
        "candidate asset ByteFF2 commit",
    )
    validate_byteff2_source(
        manifest["byteff2_source"],
        manifest_commit=manifest_commit,
        require_exact_identity=True,
    )
    validate_byteff2_audited_overlay(
        manifest["byteff2_audited_overlays"],
        require_exact_identity=True,
    )
    for relative, fixed_size, fixed_digest in BYTEFF2_FORMAL_RUNTIME_ASSETS:
        matches = [
            record
            for record in records
            if isinstance(record, dict) and record.get("path") == relative
        ]
        if len(matches) != 1:
            raise ReleaseError(
                "candidate asset manifest omits a ByteFF2 formal runtime asset record"
            )
        record = matches[0]
        if (
            set(record) != {"path", "size", "sha256"}
            or record.get("size") != fixed_size
            or isinstance(record.get("size"), bool)
            or record.get("sha256") != fixed_digest
        ):
            raise ReleaseError(
                "candidate asset manifest has the wrong ByteFF2 formal runtime asset contract"
            )
        _verify_candidate_byteff2_runtime_file(
            asset_root,
            relative,
            fixed_size,
            fixed_digest,
        )


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


def inspect_managed_asset_release(
    configured_digest: str,
    *,
    require_byteff2_runtime_assets: bool,
) -> tuple[Path, str, str]:
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
    if require_byteff2_runtime_assets:
        validate_candidate_byteff2_runtime_assets(resolved)
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


def rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish a Linux filesystem entry without replacement."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:  # pragma: no cover - production glibc contract
        raise ReleaseError("atomic no-replace release publication is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,  # AT_FDCWD
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,  # RENAME_NOREPLACE
    )
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise ReleaseError(
                "release publication destination appeared concurrently"
            )
        raise ReleaseError("atomic no-replace release publication failed") from OSError(
            error,
            os.strerror(error),
        )


def fsync_tree(root: Path) -> None:
    """Durably flush one private release tree without following symlinks."""

    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise ReleaseError("cannot inspect provisioned release before READY") from exc
    if not stat.S_ISDIR(root_metadata.st_mode) or root.is_symlink():
        raise ReleaseError("provisioned release is unsafe before READY")
    directories: list[Path] = []
    for current_raw, child_directories, files in os.walk(root, followlinks=False):
        current = Path(current_raw)
        directories.append(current)
        child_directories.sort()
        files.sort()
        retained: list[str] = []
        for name in child_directories:
            child = current / name
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                raise ReleaseError("provisioned release contains an unsafe entry")
            retained.append(name)
        child_directories[:] = retained
        for name in files:
            path = current / name
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise ReleaseError("provisioned release contains an unsafe entry")
            flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in reversed(directories):
        fsync_directory(directory)


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


def safe_extract_tar(
    archive: Path,
    destination: Path,
    *,
    existing_owned_directory: bool = False,
    forbidden_top_level: frozenset[str] = frozenset(),
) -> None:
    if existing_owned_directory:
        try:
            metadata = destination.lstat()
        except OSError as exc:
            raise ReleaseError("archive destination is unavailable") from exc
        if (
            destination.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ReleaseError("archive destination is not an owned private directory")
    else:
        destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive, "r:*") as source:
        members = source.getmembers()
        for member in members:
            relative = PurePosixPath(member.name)
            if not relative.parts and member.isdir() and member.name in {".", "./"}:
                continue
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ReleaseError(f"unsafe archive path: {member.name}")
            if relative.parts[0] in forbidden_top_level:
                raise ReleaseError(
                    f"archive contains a reserved release control path: {relative.parts[0]}"
                )
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
        # Deploy and interrupted recovery consume only a separately
        # provisioned, READY-sealed final release.  Provisioning temporarily
        # overrides this while it owns ``<sha>.staging``.
        self.candidate_dir = self.release_dir
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
        self.deploy_transport_required = False
        self.worker_values: dict[str, str] = {}
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

    def validate_stable_worker_env_helper(self) -> None:
        """Bind systemd's stable parser to the reviewed candidate helper."""

        source = CONTROLLER_DIRECTORY / STABLE_WORKER_ENV_HELPER_NAME
        installed = self.config_dir / STABLE_WORKER_ENV_HELPER_NAME
        for path, expected_mode, label in (
            (source, 0o700, "candidate Worker environment helper"),
            (installed, 0o700, "stable Worker environment helper"),
        ):
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise ReleaseError(f"{label} is missing or unsafe") from exc
            if (
                not stat.S_ISREG(metadata.st_mode)
                or path.is_symlink()
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != expected_mode
            ):
                raise ReleaseError(
                    f"{label} must be a deploy-user-owned mode-{expected_mode:04o} regular file"
                )
        if sha256_file(source) != sha256_file(installed):
            raise ReleaseError(
                "stable Worker environment helper differs from the reviewed candidate"
            )

    def load_and_validate_worker_values(
        self,
        deploy_values: dict[str, str],
    ) -> dict[str, str]:
        """Validate worker.env without injecting it into this process."""

        self.validate_stable_worker_env_helper()
        worker_path = self.config_dir / "worker.env"
        try:
            worker_values = load_worker_env(worker_path)
        except WorkerEnvError as exc:
            raise ReleaseError(
                f"worker.env contains forbidden or invalid configuration: {exc}"
            ) from exc

        worker_dsn = worker_values.get("APP_POSTGRES_DSN")
        if not worker_dsn:
            raise ReleaseError("worker.env is missing required non-empty value: APP_POSTGRES_DSN")
        validate_postgres_dsn(
            worker_dsn,
            "worker.env APP_POSTGRES_DSN",
            expected_user=deploy_values["NEXPOLY_POSTGRES_USER"],
            expected_password=deploy_values["NEXPOLY_POSTGRES_PASSWORD"],
            expected_host="127.0.0.1",
            expected_port=55432,
            expected_database=deploy_values["NEXPOLY_POSTGRES_DB"],
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
            "MONOMER_MD_PYTHON": str(
                self.ops / "current" / "worker-venv" / "bin" / "python"
            ),
            "MONOMER_MD_JOB_ROOT": str(
                self.ops / "state" / "monomer-md-worker-runs"
            ),
            "MONOMER_MD_WORKER_UDS": str(
                self.ops / "state" / "monomer-md-worker-socket" / "worker.sock"
            ),
            "MONOMER_MD_WORKER_MODE": "real",
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

        normalized_deploy_boolean(
            worker_values.get("MONOMER_MD_GPU_BROKER_ENABLED", "false"),
            "MONOMER_MD_GPU_BROKER_ENABLED",
        )
        transport_smoke_enabled = normalized_deploy_boolean(
            worker_values.get("MONOMER_MD_TRANSPORT_CUDA_SMOKE_ENABLED", "true"),
            "MONOMER_MD_TRANSPORT_CUDA_SMOKE_ENABLED",
        )
        configured_openmm = worker_values.get("BYTEFF2_OPENMM_DIR", "")
        if configured_openmm and not Path(configured_openmm).is_absolute():
            raise ReleaseError("BYTEFF2_OPENMM_DIR must be an absolute path")
        if self.deploy_transport_required:
            if configured_openmm != (
                "/home/devuser/miniconda3/envs/byteff2-repro/"
                "byteff2_openmm/openmm"
            ):
                raise ReleaseError(
                    "strict Transport deployment requires the pinned BYTEFF2_OPENMM_DIR"
                )
            if not transport_smoke_enabled:
                raise ReleaseError(
                    "strict Transport deployment requires the CUDA smoke probe"
                )
        return worker_values

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
        transport_setting = values.pop(
            MONOMER_MD_REQUIRE_TRANSPORT_READY,
            "false",
        )
        self.deploy_transport_required = normalized_deploy_boolean(
            transport_setting,
            MONOMER_MD_REQUIRE_TRANSPORT_READY,
        )
        if self.deploy_transport_required and not release_uses_worker(self.document):
            raise ReleaseError(
                "strict Transport readiness requires a Monomer-MD Worker release payload"
            )
        forbidden_values = sorted(FORBIDDEN_DEPLOY_ENV_OVERRIDES.intersection(values))
        if forbidden_values:
            raise ReleaseError(
                "deploy.env contains forbidden process/runtime overrides: "
                + ", ".join(forbidden_values)
            )
        package_manager_values = sorted(
            key
            for key in values
            if key.startswith(PACKAGE_MANAGER_ENV_PREFIXES)
        )
        if package_manager_values:
            raise ReleaseError(
                "deploy.env contains forbidden package-manager overrides: "
                + ", ".join(package_manager_values)
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
        configured_health_url = values.get(
            "NEXPOLY_HEALTH_URLS",
            PRODUCTION_HEALTH_URL,
        )
        if configured_health_url != PRODUCTION_HEALTH_URL:
            raise ReleaseError(
                "NEXPOLY_HEALTH_URLS must use the fixed production health endpoint"
            )
        values["NEXPOLY_HEALTH_URLS"] = PRODUCTION_HEALTH_URL
        configured_hooks = sorted(
            key for key in FORBIDDEN_DEPLOY_HOOKS if values.get(key) or os.environ.get(key)
        )
        if configured_hooks:
            raise ReleaseError(
                "custom production drain/job hooks are forbidden: " + ", ".join(configured_hooks)
            )
        if release_uses_worker(self.document):
            self.worker_values = self.load_and_validate_worker_values(values)
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
            expected_digest,
            require_byteff2_runtime_assets=release_uses_worker(self.document),
        )
        self.document["current_asset_manifest_digest"] = actual_asset_digest
        self.document["current_byteff2_commit"] = current_byteff2_commit
        self.document["current_asset_root"] = str(current_asset_root)
        self.document["resolved_asset_manifest_digest"] = target_digest
        self.document["resolved_byteff2_commit"] = target_byteff2_commit
        self.document["resolved_asset_root"] = str(target_asset_root)
        environment = os.environ.copy()
        for key in tuple(environment):
            if (
                key in FORBIDDEN_DEPLOY_ENV_OVERRIDES
                or key in RUNTIME_ENDPOINT_OVERRIDE_KEYS
                or key.startswith(PACKAGE_MANAGER_ENV_PREFIXES)
                or key.startswith(("BASH_FUNC_", "LD_", "PYTHON"))
            ):
                environment.pop(key, None)
        # Every release subprocess starts from one reviewed executable search
        # path. In particular, venv creation/pip verification must not inherit
        # a caller-controlled Python or dynamic-loader runtime.
        environment["PATH"] = SAFE_SYSTEM_PATH
        # The deployment gate is a controller decision sourced only from the
        # validated deploy.env.  A same-named inherited shell variable must
        # neither override that decision nor reach Docker/Worker children.
        environment.pop(MONOMER_MD_REQUIRE_TRANSPORT_READY, None)
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

    def _provision_staging(self, environment: dict[str, str]) -> None:
        """Extract and build one candidate; only ``provision-release`` calls this."""

        if self.release_dir.exists():
            raise ReleaseError(f"release directory already exists for {self.sha}")
        if self.staging.exists():
            raise ReleaseError(f"staging directory already exists for {self.sha}")
        archive = verify_artifact(
            self.manifest_path.parent,
            self.document["release_bundle"],
            "release bundle",
        )
        unpublished = Path(
            tempfile.mkdtemp(
                prefix=f".{self.sha}.provisioning-",
                dir=self.staging.parent,
            )
        )
        os.chmod(unpublished, 0o700)
        try:
            atomic_json(
                unpublished / PROVISIONING_OWNER_NAME,
                {
                    "schema_version": PROVISIONING_SCHEMA_VERSION,
                    "source_sha": self.sha,
                    "release_manifest_sha256": sha256_file(self.manifest_path),
                    "release_bundle_sha256": self.document["release_bundle"]["sha256"],
                    "owner_token": secrets.token_hex(32),
                },
            )
            # Publish the staging name only after its durable owner identity
            # exists. A crash before this rename leaves no ambiguous
            # <sha>.staging; a crash after it always leaves a token that retry
            # can validate.
            rename_noreplace(unpublished, self.staging)
        except BaseException:
            if unpublished.exists() and unpublished.is_dir() and not unpublished.is_symlink():
                shutil.rmtree(unpublished)
            raise
        fsync_directory(self.staging.parent)
        safe_extract_tar(
            archive,
            self.staging,
            existing_owned_directory=True,
            forbidden_top_level=frozenset(
                {
                    PROVISIONING_OWNER_NAME,
                    PROVISIONING_READY_NAME,
                    "release-manifest.json",
                    "worker-venv",
                    "worker-lock-requirements.json",
                    "worker-base-python-identity.json",
                    "worker-toolchain-identity.json",
                }
            ),
        )
        self.candidate_dir = self.staging
        for required in ("docker-compose.yml", "docker-compose.prod.yml"):
            if not (self.staging / required).is_file():
                raise ReleaseError(f"control archive is missing {required}")
        shutil.copy2(self.manifest_path, self.staging / "release-manifest.json")
        os.chmod(self.staging / "release-manifest.json", 0o600)
        self._validate_candidate_compose(self.staging, environment)

    def _validate_candidate_compose(
        self,
        candidate: Path,
        environment: dict[str, str],
    ) -> None:
        rendered = subprocess.run(
            self.compose(candidate, "config", "--images"), cwd=self.root, env=environment,
            check=True, text=True, stdout=subprocess.PIPE,
        ).stdout.splitlines()
        application_images = {self.document["images"]["backend"], self.document["images"]["web"]}
        if not application_images.issubset(set(rendered)):
            raise ReleaseError("rendered production Compose does not contain both manifest image digests")
        if any(image.startswith(("nexpoly-backend:", "nexpoly-nginx:")) or image.endswith(":latest") for image in rendered):
            raise ReleaseError("rendered production Compose contains a mutable application image")
        config_result = subprocess.run(
            self.compose(candidate, "config", "--format", "json"),
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
    def _load_private_provisioning_document(
        path: Path,
        label: str,
    ) -> dict[str, Any]:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ReleaseError(f"{label} is missing or unsafe") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_RUNTIME_RESPONSE_BYTES
        ):
            raise ReleaseError(f"{label} must be a deploy-user-owned mode-0600 file")
        return load_manifest(path)

    def _provisioning_owner(self, candidate: Path) -> dict[str, Any]:
        owner = self._load_private_provisioning_document(
            candidate / PROVISIONING_OWNER_NAME,
            "release provisioning owner",
        )
        if set(owner) != {
            "schema_version",
            "source_sha",
            "release_manifest_sha256",
            "release_bundle_sha256",
            "owner_token",
        }:
            raise ReleaseError("release provisioning owner has an invalid shape")
        token = owner.get("owner_token")
        if (
            owner.get("schema_version") != PROVISIONING_SCHEMA_VERSION
            or owner.get("source_sha") != self.sha
            or owner.get("release_manifest_sha256") != sha256_file(self.manifest_path)
            or owner.get("release_bundle_sha256")
            != self.document["release_bundle"]["sha256"]
            or not isinstance(token, str)
            or re.fullmatch(r"[0-9a-f]{64}", token) is None
        ):
            raise ReleaseError("release provisioning owner identity does not match")
        return owner

    def _provisioning_evidence(
        self,
        candidate: Path,
        environment: dict[str, str],
        *,
        require_bundle_artifact: bool = True,
        sealed_ready: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Recompute every READY-bound identity without installing packages."""

        self._require_safe_provisioned_release(candidate)

        release_manifest = candidate / "release-manifest.json"
        copied_manifest = self._load_private_provisioning_document(
            release_manifest,
            "provisioned release manifest",
        )
        supplied_manifest = validate_manifest(
            load_manifest(self.manifest_path),
            deployment_mode=self.mode,
        )
        validate_manifest(copied_manifest, deployment_mode=self.mode)
        manifest_digest = sha256_file(self.manifest_path)
        if (
            copied_manifest != supplied_manifest
            or sha256_file(release_manifest) != manifest_digest
        ):
            raise ReleaseError("provisioned release manifest differs from the deployment manifest")
        if require_bundle_artifact:
            verify_artifact(
                self.manifest_path.parent,
                self.document["release_bundle"],
                "release bundle",
            )

        bundle = self.worker_bundle_dir(candidate)
        wheelhouse = bundle / "wheelhouse"
        if not wheelhouse.is_dir() or wheelhouse.is_symlink():
            raise ReleaseError("provisioned Worker wheelhouse is missing or unsafe")
        expectation_path = candidate / "worker-lock-requirements.json"
        expectation = self._load_private_provisioning_document(
            expectation_path,
            "Worker lock expectation",
        )
        actual_expectation = self.worker_requirement_document(bundle)
        if expectation != actual_expectation:
            raise ReleaseError("provisioned Worker lock expectation differs from its lock")

        base_identity = validate_worker_base_identity(
            self._load_private_provisioning_document(
                candidate / "worker-base-python-identity.json",
                "Worker base Python identity",
            )
        )
        toolchain_identity = validate_worker_toolchain_identity(
            self._load_private_provisioning_document(
                candidate / "worker-toolchain-identity.json",
                "Worker toolchain identity",
            )
        )
        current_base = inspect_worker_base_python(
            environment.get("NEXPOLY_WORKER_BASE_PYTHON", ""),
            environment.get("NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256", ""),
            environment,
        )
        current_toolchain = inspect_worker_toolchain(current_base, environment)
        if current_base != base_identity or current_toolchain != toolchain_identity:
            raise ReleaseError("provisioned Worker frozen runtime identity has changed")

        venv = candidate / "worker-venv"
        if not venv.is_dir() or venv.is_symlink():
            raise ReleaseError("provisioned Worker venv is missing or unsafe")
        try:
            venv_prefix = venv.resolve(strict=True)
        except OSError as exc:
            raise ReleaseError("provisioned Worker venv cannot be resolved") from exc

        # Hash every candidate-controlled byte before executing the candidate
        # venv or parsing candidate Compose.  With a READY record this closes
        # the otherwise dangerous ordering where tampered code could run first
        # and only then be found to differ from its seal.
        owner = self._provisioning_owner(candidate)
        evidence = {
            "schema_version": PROVISIONING_SCHEMA_VERSION,
            "status": "ready",
            "source_sha": self.sha,
            "release_manifest_sha256": manifest_digest,
            "release_bundle_sha256": self.document["release_bundle"]["sha256"],
            "requirements_sha256": canonical_json_digest(expectation),
            "wheelhouse_inventory_sha256": directory_inventory_digest(wheelhouse),
            "payload_inventory_sha256": directory_inventory_digest(
                candidate,
                excluded_top_level=frozenset(
                    {"worker-venv", PROVISIONING_READY_NAME}
                ),
            ),
            "venv_inventory_sha256": directory_inventory_digest(venv),
            "venv_prefix": str(venv_prefix),
            "worker_base_identity_sha256": base_identity["identity_sha256"],
            "worker_toolchain_identity_sha256": toolchain_identity["identity_sha256"],
            "owner_token": owner["owner_token"],
        }
        if sealed_ready is not None and any(
            sealed_ready.get(key) != value for key, value in evidence.items()
        ):
            raise ReleaseError(
                "release provisioning READY evidence does not match candidate"
            )

        self.verify_worker_venv(venv, expectation, base_identity)

        # Static verification never starts candidate Python or imports its
        # site-packages. Re-establish every candidate and frozen-runtime
        # boundary before Compose consumes the tree, then repeat the tree proof
        # after Compose validation.
        self._require_safe_provisioned_release(candidate)
        post_runtime_base = inspect_worker_base_python(
            environment.get("NEXPOLY_WORKER_BASE_PYTHON", ""),
            environment.get("NEXPOLY_WORKER_BASE_PYTHON_IDENTITY_SHA256", ""),
            environment,
        )
        post_runtime_toolchain = inspect_worker_toolchain(
            post_runtime_base,
            environment,
        )
        if post_runtime_base != base_identity or post_runtime_toolchain != toolchain_identity:
            raise ReleaseError(
                "provisioned Worker frozen runtime changed during verification"
            )
        post_runtime_inventories = {
            "wheelhouse_inventory_sha256": directory_inventory_digest(wheelhouse),
            "payload_inventory_sha256": directory_inventory_digest(
                candidate,
                excluded_top_level=frozenset(
                    {"worker-venv", PROVISIONING_READY_NAME}
                ),
            ),
            "venv_inventory_sha256": directory_inventory_digest(venv),
        }
        if any(
            evidence[key] != value
            for key, value in post_runtime_inventories.items()
        ):
            raise ReleaseError(
                "provisioned candidate changed while its runtime was being verified"
            )

        self._validate_candidate_compose(candidate, environment)
        self._require_safe_provisioned_release(candidate)
        post_execution_inventories = {
            "wheelhouse_inventory_sha256": directory_inventory_digest(wheelhouse),
            "payload_inventory_sha256": directory_inventory_digest(
                candidate,
                excluded_top_level=frozenset(
                    {"worker-venv", PROVISIONING_READY_NAME}
                ),
            ),
            "venv_inventory_sha256": directory_inventory_digest(venv),
        }
        if any(
            evidence[key] != value
            for key, value in post_execution_inventories.items()
        ):
            raise ReleaseError(
                "provisioned candidate changed while its runtime was being verified"
            )
        self.worker_base_python_identity = base_identity
        self.worker_toolchain_identity = toolchain_identity
        return evidence

    def _require_safe_provisioned_release(self, candidate: Path) -> Path:
        try:
            candidate_metadata = candidate.lstat()
            releases_root = (self.ops / "releases").resolve(strict=True)
            resolved_candidate = candidate.resolve(strict=True)
        except OSError as exc:
            raise ReleaseError("provisioned release directory is unavailable") from exc
        if (
            not stat.S_ISDIR(candidate_metadata.st_mode)
            or candidate.is_symlink()
            or candidate_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(candidate_metadata.st_mode) != 0o700
            or resolved_candidate.parent != releases_root
            or resolved_candidate.name != self.sha
        ):
            raise ReleaseError("provisioned release directory is unsafe")
        return resolved_candidate

    def _validate_provisioned_ready(
        self,
        environment: dict[str, str],
        *,
        require_bundle_artifact: bool,
    ) -> None:
        self.candidate_dir = self.release_dir
        ready_path = self.release_dir / PROVISIONING_READY_NAME
        ready = self._load_private_provisioning_document(
            ready_path,
            "release provisioning READY record",
        )
        ready_digest = sha256_file(ready_path)
        expected_fields = {
            "schema_version",
            "status",
            "source_sha",
            "release_manifest_sha256",
            "release_bundle_sha256",
            "requirements_sha256",
            "wheelhouse_inventory_sha256",
            "payload_inventory_sha256",
            "venv_inventory_sha256",
            "venv_prefix",
            "worker_base_identity_sha256",
            "worker_toolchain_identity_sha256",
            "owner_token",
            "provisioned_at",
        }
        if set(ready) != expected_fields:
            raise ReleaseError("release provisioning READY record has an invalid shape")
        provisioned_at = ready.get("provisioned_at")
        if not isinstance(provisioned_at, str) or not provisioned_at:
            raise ReleaseError("release provisioning READY record has no timestamp")
        evidence = self._provisioning_evidence(
            self.release_dir,
            environment,
            require_bundle_artifact=require_bundle_artifact,
            sealed_ready=ready,
        )
        if any(ready.get(key) != value for key, value in evidence.items()):
            raise ReleaseError("release provisioning READY evidence does not match candidate")
        if sha256_file(ready_path) != ready_digest:
            raise ReleaseError(
                "release provisioning READY record changed during validation"
            )

    def prepare_staging(self, environment: dict[str, str]) -> None:
        """Validate a separately provisioned candidate; never build or install."""

        if self.staging.exists() or self.staging.is_symlink():
            raise ReleaseError("unfinished release provisioning staging exists")
        self._validate_provisioned_ready(
            environment,
            require_bundle_artifact=True,
        )

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
        expectation: dict[str, Any],
        base_identity: dict[str, Any],
    ) -> None:
        """Statically verify a venv without importing candidate site hooks."""

        if (
            set(expectation) != {"schema_version", "requirements"}
            or expectation.get("schema_version") != 1
            or not isinstance(expectation.get("requirements"), list)
        ):
            raise ReleaseError("invalid Worker lock expectation document")
        try:
            metadata = venv.lstat()
            resolved_venv = venv.resolve(strict=True)
        except OSError as exc:
            raise ReleaseError("Worker venv is missing or unsafe") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or venv.is_symlink()
            or metadata.st_uid != os.geteuid()
        ):
            raise ReleaseError("Worker venv is missing or unsafe")

        configuration = venv / "pyvenv.cfg"
        try:
            configuration_metadata = configuration.lstat()
            configuration_text = configuration.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ReleaseError("Worker venv configuration is missing or unsafe") from exc
        if (
            not stat.S_ISREG(configuration_metadata.st_mode)
            or configuration.is_symlink()
            or configuration_metadata.st_uid != os.geteuid()
            or configuration_metadata.st_size > 64 * 1024
        ):
            raise ReleaseError("Worker venv configuration is missing or unsafe")
        configuration_values: dict[str, str] = {}
        for raw_line in configuration_text.splitlines():
            key, separator, value = raw_line.partition("=")
            if not separator or not key.strip() or key.strip() in configuration_values:
                raise ReleaseError("Worker venv configuration is malformed")
            configuration_values[key.strip()] = value.strip()
        if configuration_values.get("include-system-site-packages", "").lower() != "true":
            raise ReleaseError("Worker venv must inherit the frozen base packages")
        if f"{self.sha}.staging" in configuration_text:
            raise ReleaseError("Worker venv configuration retains a staging path")

        candidate_python = venv / "bin" / "python"
        try:
            python_metadata = candidate_python.lstat()
            resolved_python = candidate_python.resolve(strict=True)
            expected_python = Path(str(base_identity["resolved_path"])).resolve(
                strict=True
            )
        except (KeyError, OSError) as exc:
            raise ReleaseError("Worker venv Python identity is missing or unsafe") from exc
        if (
            not (stat.S_ISREG(python_metadata.st_mode) or stat.S_ISLNK(python_metadata.st_mode))
            or not resolved_python.is_file()
            or resolved_python != expected_python
            or not os.access(candidate_python, os.X_OK)
        ):
            raise ReleaseError("Worker venv Python differs from the frozen base")
        bin_directory = venv / "bin"
        for script in sorted(bin_directory.iterdir(), key=lambda item: item.name):
            try:
                script_metadata = script.lstat()
            except OSError as exc:
                raise ReleaseError("Worker venv script inventory is unsafe") from exc
            if stat.S_ISLNK(script_metadata.st_mode):
                continue
            if not stat.S_ISREG(script_metadata.st_mode):
                raise ReleaseError("Worker venv script inventory is unsafe")
            try:
                with script.open("rb") as source:
                    first_line = source.readline(16 * 1024)
            except OSError as exc:
                raise ReleaseError("Worker venv script is unreadable") from exc
            if first_line.startswith(b"#!") and f"{self.sha}.staging".encode() in first_line:
                raise ReleaseError("Worker venv script retains a staging shebang")

        lib = venv / "lib"
        if not lib.is_dir() or lib.is_symlink():
            raise ReleaseError("Worker venv library root is missing or unsafe")
        site_roots = sorted(lib.glob("python*/site-packages"))
        if len(site_roots) != 1:
            raise ReleaseError("Worker venv must have exactly one local site-packages")
        site_root = site_roots[0]
        try:
            site_metadata = site_root.lstat()
            resolved_site = site_root.resolve(strict=True)
        except OSError as exc:
            raise ReleaseError("Worker venv site-packages is missing or unsafe") from exc
        if (
            not stat.S_ISDIR(site_metadata.st_mode)
            or site_root.is_symlink()
            or site_metadata.st_uid != os.geteuid()
            or not resolved_site.is_relative_to(resolved_venv)
        ):
            raise ReleaseError("Worker venv site-packages escapes the release venv")

        local: dict[str, list[str]] = {}
        metadata_directories = sorted(site_root.glob("*.dist-info")) + sorted(
            site_root.glob("*.egg-info")
        )
        for distribution in metadata_directories:
            try:
                distribution_metadata = distribution.lstat()
                resolved_distribution = distribution.resolve(strict=True)
            except OSError as exc:
                raise ReleaseError("Worker distribution metadata is unsafe") from exc
            if (
                not stat.S_ISDIR(distribution_metadata.st_mode)
                or distribution.is_symlink()
                or distribution_metadata.st_uid != os.geteuid()
                or not resolved_distribution.is_relative_to(resolved_site)
            ):
                raise ReleaseError("Worker distribution metadata is unsafe")
            metadata_name = "METADATA" if distribution.name.endswith(".dist-info") else "PKG-INFO"
            metadata_path = distribution / metadata_name
            record_path = distribution / "RECORD"
            try:
                package_metadata = metadata_path.lstat()
                payload = metadata_path.read_bytes()
            except OSError as exc:
                raise ReleaseError("Worker distribution metadata is incomplete") from exc
            if (
                not stat.S_ISREG(package_metadata.st_mode)
                or metadata_path.is_symlink()
                or package_metadata.st_uid != os.geteuid()
                or len(payload) > 4 * 1024 * 1024
            ):
                raise ReleaseError("Worker distribution metadata is unsafe")
            if distribution.name.endswith(".dist-info") and (
                not record_path.is_file() or record_path.is_symlink()
            ):
                raise ReleaseError("Worker distribution RECORD is missing or unsafe")
            parsed = email.parser.BytesParser().parsebytes(payload, headersonly=True)
            raw_name = parsed.get("Name")
            version = parsed.get("Version")
            if not isinstance(raw_name, str) or not isinstance(version, str) or not version:
                raise ReleaseError("Worker distribution identity is incomplete")
            name = canonical_distribution_name(raw_name)
            local.setdefault(name, []).append(version)

        for requirement in expectation["requirements"]:
            if (
                not isinstance(requirement, dict)
                or set(requirement) != {"name", "version"}
                or not isinstance(requirement.get("name"), str)
                or not isinstance(requirement.get("version"), str)
            ):
                raise ReleaseError("invalid Worker requirement record")
            name = canonical_distribution_name(requirement["name"])
            version = requirement["version"]
            versions = local.get(name, [])
            if versions != [version]:
                raise ReleaseError(
                    "locked Worker distribution is not installed exactly once in "
                    f"release venv: {name}=={version} (local versions: {versions})"
                )

    @contextlib.contextmanager
    def worker_build_environment(
        self,
        environment: dict[str, str],
    ):
        """Yield an isolated build environment whose scratch is outside READY."""

        scratch_parent = self.ops / "state" / "worker-build-scratch"
        scratch_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_metadata = scratch_parent.lstat()
        if (
            scratch_parent.is_symlink()
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        ):
            raise ReleaseError("Worker build scratch parent is unsafe")
        scratch = Path(
            tempfile.mkdtemp(prefix=f"{self.sha}-", dir=scratch_parent)
        )
        os.chmod(scratch, 0o700)
        build_home = scratch / "home"
        build_tmp = scratch / "tmp"
        build_home.mkdir(mode=0o700)
        build_tmp.mkdir(mode=0o700)
        build_environment = {
            "HOME": str(build_home),
            "PATH": SAFE_SYSTEM_PATH,
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "TMPDIR": str(build_tmp),
            "XDG_CACHE_HOME": str(build_home / ".cache"),
            "XDG_CONFIG_HOME": str(build_home / ".config"),
        }
        for key in ("LANG", "LC_ALL", "TZ"):
            if environment.get(key):
                build_environment[key] = environment[key]
        try:
            yield build_environment
        finally:
            shutil.rmtree(scratch, ignore_errors=False)

    def prepare_worker(self, environment: dict[str, str]) -> None:
        candidate = self.candidate_dir
        nested_bundle = candidate / "worker-bundle"
        bundle = nested_bundle if nested_bundle.is_dir() else candidate
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
        venv = candidate / "worker-venv"
        expectation_path = candidate / "worker-lock-requirements.json"
        with self.worker_build_environment(environment) as build_environment:
            self.run(
                [
                    before_identity["resolved_path"],
                    "-I",
                    "-m",
                    "venv",
                    "--system-site-packages",
                    str(venv),
                ],
                env=build_environment,
            )
            self.verify_worker_venv(
                venv,
                {"schema_version": 1, "requirements": []},
                before_identity,
            )
            command = [
                before_identity["resolved_path"],
                "-I",
                "-m",
                "pip",
                "--isolated",
                "--python",
                str(venv / "bin" / "python"),
                "install",
                "--no-index", "--require-hashes", "--ignore-installed",
                "--no-cache-dir", "--only-binary=:all:", "--find-links", str(wheelhouse),
            ]
            for lock in locks:
                command.extend(["-r", str(lock)])
            self.run(command, env=build_environment)
            expectation = self.worker_requirement_document(bundle)
            atomic_json(expectation_path, expectation)
            self.verify_worker_venv(venv, expectation, before_identity)
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
            candidate / "worker-base-python-identity.json",
            after_identity,
        )
        atomic_json(
            candidate / "worker-toolchain-identity.json",
            after_toolchain,
        )

    def candidate_worker_environment(
        self,
        environment: dict[str, str],
        *,
        preflight_job_root: Path,
    ) -> dict[str, str]:
        """Build a child-only environment bound to the provisioned release."""

        if not self.worker_values:
            raise ReleaseError("candidate Worker environment has not been validated")
        candidate = self.candidate_dir
        candidate_python = candidate / "worker-venv" / "bin" / "python"
        candidate_module = (
            candidate
            / "workers"
            / "monomer_md_worker"
            / "app"
            / "runtime_preflight.py"
        )
        if not candidate_python.is_file() or not os.access(candidate_python, os.X_OK):
            raise ReleaseError("candidate Worker venv is missing or unsafe")
        if not candidate_module.is_file() or candidate_module.is_symlink():
            raise ReleaseError("candidate Worker runtime preflight module is missing or unsafe")
        asset_root = Path(str(self.document.get("resolved_asset_root", "")))
        byteff2_root = asset_root / "byteff2"
        if not asset_root.is_absolute() or not byteff2_root.is_dir():
            raise ReleaseError("candidate ByteFF2 asset root is missing or unsafe")
        overrides = {
            # Runtime preflight has no database role.  Remove its credential
            # and bind any incidental output to disposable state scratch so the
            # pre-mutation proof cannot write production job state.
            "APP_POSTGRES_DSN": "",
            "BYTEFF2_ROOT": str(byteff2_root),
            "MONOMER_MD_JOB_ROOT": str(preflight_job_root),
            "MONOMER_MD_PYTHON": str(candidate_python),
            "MONOMER_MD_WORKER_ID": f"candidate-preflight-{self.sha[:12]}",
            "PYTHONPATH": (
                f"{candidate}:{byteff2_root}:"
                f"{byteff2_root / 'submodules' / 'bytemol'}"
            ),
        }
        candidate_inherited = {
            key: environment[key]
            for key in CANDIDATE_SAFE_INHERITED_KEYS
            if environment.get(key)
        }
        try:
            return build_worker_process_environment(
                self.worker_values,
                inherited=candidate_inherited,
                overrides=overrides,
            )
        except WorkerEnvError as exc:
            raise ReleaseError("candidate Worker environment could not be constructed") from exc

    @staticmethod
    def _nvidia_smi_rows(payload: str, columns: int) -> list[list[str]]:
        rows: list[list[str]] = []
        for raw in payload.splitlines():
            line = raw.strip()
            if not line or line.lower().startswith("no running processes"):
                continue
            values = [value.strip() for value in line.split(",")]
            if len(values) != columns or any(not value for value in values):
                raise ReleaseError("nvidia-smi returned an invalid inventory")
            rows.append(values)
        return rows

    def _nvidia_smi_query(
        self,
        query: str,
        environment: dict[str, str],
    ) -> str:
        try:
            completed = subprocess.run(
                ["nvidia-smi", query, "--format=csv,noheader,nounits"],
                cwd=self.root,
                env=environment,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseError("target GPU inventory is unavailable") from exc
        if completed.returncode != 0 or len(completed.stdout) > MAX_RUNTIME_RESPONSE_BYTES:
            raise ReleaseError("target GPU inventory is unavailable")
        return completed.stdout

    def assert_direct_transport_gpu_idle(
        self,
        candidate_environment: dict[str, str],
    ) -> None:
        """Fail closed on any compute process for direct (non-Broker) smoke."""

        installed_rows = self._nvidia_smi_rows(
            self._nvidia_smi_query("--query-gpu=index,uuid", candidate_environment),
            2,
        )
        installed = {index: uuid for index, uuid in installed_rows}
        if not installed or len(set(installed.values())) != len(installed):
            raise ReleaseError("target GPU inventory is invalid")
        device_spec = candidate_environment.get(
            "MONOMER_MD_CUDA_VISIBLE_DEVICES",
            candidate_environment.get("NEXPOLY_GPU_DEVICE", ""),
        )
        tokens = device_spec.split(",")
        if any(not token or token != token.strip() for token in tokens):
            raise ReleaseError("target Worker GPU selection is invalid")
        selected: set[str] = set()
        for token in tokens:
            matches: list[str]
            if token in installed:
                matches = [installed[token]]
            elif token.startswith("GPU-"):
                matches = [uuid for uuid in installed.values() if uuid.startswith(token)]
            else:
                matches = []
            if len(matches) != 1:
                raise ReleaseError("target Worker GPU selection is invalid")
            selected.add(matches[0])
        if not selected:
            raise ReleaseError("target Worker GPU selection is invalid")

        process_rows = self._nvidia_smi_rows(
            self._nvidia_smi_query(
                "--query-compute-apps=pid,gpu_uuid",
                candidate_environment,
            ),
            2,
        )
        for pid, gpu_uuid in process_rows:
            if not pid.isdigit():
                raise ReleaseError("target GPU process inventory is invalid")
            if gpu_uuid in selected:
                raise DeploymentDeferred(
                    "strict Transport preflight deferred because the target GPU is busy"
                )

    def _terminate_candidate_preflight_process(
        self,
        process: subprocess.Popen[bytes],
        identities: dict[int, _ProcessIdentity],
        baseline_children: dict[int, _ProcessIdentity],
        *,
        broker_governed: bool,
    ) -> None:
        root = _read_process_identity(process.pid)
        if root is not None:
            identities.setdefault(root.pid, root)
        root_only = (
            {process.pid: identities[process.pid]}
            if process.pid in identities
            else {}
        )
        # The preflight root translates TERM into asyncio cancellation.  In
        # Broker mode that cooperative path must run prepare_process_termination
        # before any CUDA child receives a POSIX signal.
        _signal_verified_processes(root_only, signal.SIGTERM)
        if _wait_for_candidate_process_tree(
            process,
            identities,
            baseline_children,
            CANDIDATE_PREFLIGHT_TERM_GRACE_SECONDS,
        ):
            _reap_candidate_zombies(
                identities,
                excluded_pids=frozenset({process.pid}),
            )
            return

        if broker_governed:
            # Killing the outer Python root is safe; killing a registered CUDA
            # descendant outside Broker cgroup/MPS cleanup is not.  Any such
            # survivor keeps the release fail-closed for operator recovery.
            _signal_verified_processes(root_only, signal.SIGKILL)
            deadline = time.monotonic() + CANDIDATE_PREFLIGHT_KILL_WAIT_SECONDS
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            _adopt_candidate_children(identities, baseline_children)
            process.poll()
            _reap_candidate_zombies(
                identities,
                excluded_pids=frozenset({process.pid}),
            )
            if any(
                _process_identity_is_live(identity)
                for identity in identities.values()
            ):
                raise ReleaseError(
                    "candidate Broker-governed runtime cleanup could not be proven"
                )
            return

        # A launcher or CUDA helper may have called setsid() and ignored TERM.
        # Direct mode has no Broker/MPS cgroup contract, so freeze the complete
        # owned tree before the normal TERM-to-KILL escalation.
        _freeze_candidate_process_tree(identities, baseline_children)
        _signal_verified_processes(identities, signal.SIGTERM)
        _signal_verified_processes(identities, signal.SIGCONT)
        if _wait_for_candidate_process_tree(
            process,
            identities,
            baseline_children,
            CANDIDATE_PREFLIGHT_TERM_GRACE_SECONDS,
        ):
            _reap_candidate_zombies(
                identities,
                excluded_pids=frozenset({process.pid}),
            )
            return
        _freeze_candidate_process_tree(identities, baseline_children)
        _signal_verified_processes(identities, signal.SIGKILL)
        if not _wait_for_candidate_process_tree(
            process,
            identities,
            baseline_children,
            CANDIDATE_PREFLIGHT_KILL_WAIT_SECONDS,
        ):
            raise ReleaseError(
                "candidate Worker runtime preflight process cleanup could not be proven"
            )
        process.poll()
        _reap_candidate_zombies(
            identities,
            excluded_pids=frozenset({process.pid}),
        )

    def _run_candidate_preflight_process(
        self,
        command: list[str],
        environment: dict[str, str],
        *,
        timeout_seconds: float,
        broker_governed: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        output_reader = -1
        output_writer = -1
        gate_reader = -1
        gate_writer = -1
        selector: selectors.BaseSelector | None = None
        try:
            output_reader, output_writer = os.pipe2(os.O_CLOEXEC)
            gate_reader, gate_writer = os.pipe2(os.O_CLOEXEC)
            os.set_blocking(output_reader, False)
            selector = selectors.DefaultSelector()
            # Allocate and register every fallible output resource before the
            # child exists. The child itself starts behind a separate exec gate.
            selector.register(output_reader, selectors.EVENT_READ)

            with (
                _deferred_candidate_signals() as pending_signal,
                _candidate_child_subreaper() as baseline_children,
            ):
                process: subprocess.Popen[bytes] | None = None
                root_pidfd = -1
                identities: dict[int, _ProcessIdentity] = {}
                output = bytearray()
                output_eof = False
                gate_opened = False
                termination_attempted = False
                deadline = time.monotonic() + timeout_seconds

                def terminate_root_handle() -> None:
                    if process is None or process.poll() is not None:
                        return
                    try:
                        if root_pidfd >= 0:
                            _pidfd_send_signal(root_pidfd, signal.SIGTERM)
                        else:
                            process.terminate()
                    except ProcessLookupError:
                        return
                    except OSError as exc:
                        raise ReleaseError(
                            "candidate Worker runtime preflight root cleanup failed"
                        ) from exc
                    term_deadline = (
                        time.monotonic()
                        + CANDIDATE_PREFLIGHT_TERM_GRACE_SECONDS
                    )
                    while process.poll() is None and time.monotonic() < term_deadline:
                        time.sleep(0.05)
                    if process.poll() is not None:
                        return
                    try:
                        if root_pidfd >= 0:
                            _pidfd_send_signal(root_pidfd, signal.SIGKILL)
                        else:
                            process.kill()
                    except ProcessLookupError:
                        return
                    except OSError as exc:
                        raise ReleaseError(
                            "candidate Worker runtime preflight root cleanup failed"
                        ) from exc
                    kill_deadline = (
                        time.monotonic()
                        + CANDIDATE_PREFLIGHT_KILL_WAIT_SECONDS
                    )
                    while process.poll() is None and time.monotonic() < kill_deadline:
                        time.sleep(0.05)
                    if process.poll() is None:
                        raise ReleaseError(
                            "candidate Worker runtime preflight root cleanup "
                            "could not be proven"
                        )

                def terminate_candidate() -> None:
                    nonlocal termination_attempted
                    if termination_attempted or process is None:
                        return
                    termination_attempted = True
                    try:
                        self._terminate_candidate_preflight_process(
                            process,
                            identities,
                            baseline_children,
                            broker_governed=broker_governed,
                        )
                    except BaseException:
                        # pidfd remains an exact root handle even if /proc
                        # becomes unreadable during containment.
                        terminate_root_handle()
                        raise

                try:
                    gate_environment = dict(environment)
                    gate_environment["NEXPOLY_GPU_EXEC_GATE_FD"] = str(
                        gate_reader
                    )
                    process = subprocess.Popen(
                        [
                            "/usr/bin/python3",
                            "-I",
                            "-S",
                            "-c",
                            CANDIDATE_EXEC_GATE_PROGRAM,
                            "--",
                            *command,
                        ],
                        cwd=self.candidate_dir,
                        env=gate_environment,
                        stdout=output_writer,
                        stderr=subprocess.DEVNULL,
                        pass_fds=(gate_reader,),
                        start_new_session=True,
                    )
                    os.close(output_writer)
                    output_writer = -1
                    os.close(gate_reader)
                    gate_reader = -1

                    root_pidfd = _pidfd_open(process.pid)
                    root = _read_process_identity(process.pid)
                    if root is None:
                        raise ReleaseError(
                            "candidate Worker runtime preflight root exited before containment"
                        )
                    identities[root.pid] = root

                    # Do not release candidate code into a signal/deadline
                    # that became pending while its pidfd identity was being
                    # established.  The closed gate lets the common finalizer
                    # contain it without any runtime import or CUDA work.
                    if pending_signal() is not None:
                        raise ReleaseError(
                            "candidate Worker runtime preflight was interrupted safely"
                        )
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(command, timeout_seconds)

                    os.write(gate_writer, b"1")
                    gate_opened = True
                    os.close(gate_writer)
                    gate_writer = -1

                    while True:
                        current_root = _read_process_identity(process.pid)
                        if current_root is not None:
                            identities.setdefault(current_root.pid, current_root)
                        _adopt_candidate_children(identities, baseline_children)

                        if pending_signal() is not None:
                            terminate_candidate()
                            raise ReleaseError(
                                "candidate Worker runtime preflight was interrupted safely"
                            )

                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            terminate_candidate()
                            raise subprocess.TimeoutExpired(command, timeout_seconds)

                        events = selector.select(timeout=min(0.1, remaining))
                        for key, _mask in events:
                            remaining_capacity = (
                                MAX_RUNTIME_RESPONSE_BYTES + 1 - len(output)
                            )
                            chunk = os.read(
                                key.fd,
                                max(1, min(64 * 1024, remaining_capacity)),
                            )
                            if chunk:
                                output.extend(chunk)
                                if len(output) > MAX_RUNTIME_RESPONSE_BYTES:
                                    terminate_candidate()
                                    raise ReleaseError(
                                        "candidate Worker runtime preflight exceeded "
                                        "the 64 KiB response limit"
                                    )
                            else:
                                selector.unregister(output_reader)
                                output_eof = True

                        return_code = process.poll()
                        if return_code is None:
                            continue
                        _adopt_candidate_children(identities, baseline_children)
                        _reap_candidate_zombies(
                            identities,
                            excluded_pids=frozenset({process.pid}),
                        )
                        if any(
                            _process_identity_is_live(identity)
                            for identity in identities.values()
                            if identity.pid != process.pid
                        ):
                            terminate_candidate()
                            raise ReleaseError(
                                "candidate Worker runtime preflight left a running descendant"
                            )
                        if not output_eof:
                            # All verified writers have exited, so the bounded
                            # pipe reports EOF without further output growth.
                            continue
                        return subprocess.CompletedProcess(
                            args=command,
                            returncode=return_code,
                            stdout=bytes(output),
                            stderr=None,
                        )
                except BaseException as original_error:
                    if gate_writer >= 0:
                        os.close(gate_writer)
                        gate_writer = -1
                    if process is not None:
                        try:
                            if not gate_opened:
                                # Closing the gate prevents candidate code from
                                # ever executing; the isolated gate exits 126.
                                try:
                                    process.wait(timeout=2.0)
                                except subprocess.TimeoutExpired:
                                    terminate_root_handle()
                            else:
                                terminate_candidate()
                            _adopt_candidate_children(
                                identities,
                                baseline_children,
                            )
                            process.poll()
                            _reap_candidate_zombies(
                                identities,
                                excluded_pids=frozenset({process.pid}),
                            )
                        except BaseException as cleanup_error:
                            raise cleanup_error from original_error
                    raise
                finally:
                    if root_pidfd >= 0:
                        os.close(root_pidfd)
                    if process is not None:
                        process.poll()
                    _reap_candidate_zombies(
                        identities,
                        excluded_pids=(
                            frozenset({process.pid})
                            if process is not None
                            else frozenset()
                        ),
                    )
        finally:
            if selector is not None:
                selector.close()
            for descriptor in (
                output_reader,
                output_writer,
                gate_reader,
                gate_writer,
            ):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def run_candidate_runtime_preflight(
        self,
        environment: dict[str, str],
    ) -> dict[str, Any]:
        """Prove candidate Transport readiness before Docker/DB/runtime changes."""

        if not self.deploy_transport_required:
            return {"required": False}
        scratch_parent = self.ops / "state" / "candidate-preflight"
        scratch_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = scratch_parent.lstat()
        if (
            scratch_parent.is_symlink()
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise ReleaseError("candidate preflight scratch parent is unsafe")
        preflight_job_root = Path(
            tempfile.mkdtemp(prefix=f"{self.sha}-", dir=scratch_parent)
        )
        os.chmod(preflight_job_root, 0o700)
        try:
            return self._run_candidate_runtime_preflight_with_scratch(
                environment,
                preflight_job_root,
            )
        finally:
            shutil.rmtree(preflight_job_root, ignore_errors=False)

    def _run_candidate_runtime_preflight_with_scratch(
        self,
        environment: dict[str, str],
        preflight_job_root: Path,
    ) -> dict[str, Any]:
        candidate_environment = self.candidate_worker_environment(
            environment,
            preflight_job_root=preflight_job_root,
        )
        broker_enabled = normalized_deploy_boolean(
            self.worker_values.get("MONOMER_MD_GPU_BROKER_ENABLED", "false"),
            "MONOMER_MD_GPU_BROKER_ENABLED",
        )
        if not broker_enabled:
            self.assert_direct_transport_gpu_idle(candidate_environment)

        raw_timeout = self.worker_values.get(
            "MONOMER_MD_HEALTH_PROBE_TIMEOUT_SECONDS",
            "30",
        )
        try:
            probe_timeout = int(raw_timeout)
        except ValueError as exc:
            raise ReleaseError(
                "MONOMER_MD_HEALTH_PROBE_TIMEOUT_SECONDS must be an integer"
            ) from exc
        if probe_timeout < 1 or probe_timeout > 30:
            raise ReleaseError(
                "MONOMER_MD_HEALTH_PROBE_TIMEOUT_SECONDS must be between 1 and 30"
            )
        command = [
            str(self.candidate_dir / "worker-venv" / "bin" / "python"),
            "-m",
            "workers.monomer_md_worker.app.runtime_preflight",
            "--require-transport-ready",
        ]
        try:
            completed = self._run_candidate_preflight_process(
                command,
                candidate_environment,
                timeout_seconds=(
                    probe_timeout
                    + CANDIDATE_PREFLIGHT_INTERNAL_CLEANUP_ALLOWANCE_SECONDS
                ),
                broker_governed=broker_enabled,
            )
        except subprocess.TimeoutExpired as exc:
            raise ReleaseError("candidate Worker runtime preflight timed out") from exc
        except OSError as exc:
            raise ReleaseError("candidate Worker runtime preflight could not start") from exc
        if completed.returncode != 0:
            raise ReleaseError("candidate Worker runtime preflight failed")
        payload = decode_bounded_json_object(
            completed.stdout,
            "candidate Worker runtime preflight",
        )
        if not candidate_preflight_transport_is_strict_ready(payload):
            raise ReleaseError(
                "candidate Worker runtime preflight did not satisfy strict Transport readiness"
            )
        if not broker_enabled:
            # Close the direct-mode TOCTOU window. Any external process that
            # appeared while CUDA smoke ran invalidates the whole preflight.
            self.assert_direct_transport_gpu_idle(candidate_environment)
        print(
            "[release-controller] candidate Worker preflight: "
            "runtime_ready=true transport_ready=true"
        )
        return {
            "required": True,
            "runtime_ready": True,
            "transport_ready": True,
            "broker_governed": broker_enabled,
        }

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
        # This is a fixed production endpoint, not a caller-controlled list.
        # Disable proxies and redirects just as the candidate/post-switch gates
        # do, so the legacy-current proof cannot be satisfied off-host.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        try:
            with opener.open(PRODUCTION_HEALTH_URL, timeout=20) as response:
                if response.status < 200 or response.status >= 300:
                    raise ReleaseError(
                        "current health endpoint returned a non-success status"
                    )
        except ReleaseError:
            raise
        except OSError as exc:
            raise ReleaseError("current production runtime is unhealthy") from exc
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
            healthy = (
                worker.get("status") == "ok"
                and worker.get("runtime_ready") is True
            )
            strict_transport_repair = (
                self.deploy_transport_required
                and current_worker_allows_transport_repair(worker)
            )
            if not healthy and not strict_transport_repair:
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
        socket_path = str(
            self.ops / "state" / "monomer-md-worker-socket" / "worker.sock"
        )
        completed = subprocess.run(
            [
                "curl", "--disable", "--fail", "--silent", "--show-error",
                "--noproxy", "*", "--proto", "=http", "--max-time", "30",
                "--max-filesize", str(MAX_RUNTIME_RESPONSE_BYTES),
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
        return decode_bounded_json_object(
            completed.stdout.encode("utf-8"),
            f"monomer MD worker {path}",
        )

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
                    and (
                        not self.deploy_transport_required
                        or worker_transport_is_strict_ready(worker)
                    )
                ):
                    return worker
                last_error = "worker did not satisfy the required readiness contract"
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

    def wait_for_worker_idle(self, environment: dict[str, str]) -> dict[str, Any]:
        """Wait for an already-drained Worker to release its execution slot."""

        timeout = int(environment.get("NEXPOLY_DRAIN_TIMEOUT_SECONDS", "1800"))
        if timeout < 1 or timeout > 3600:
            raise ReleaseError("NEXPOLY_DRAIN_TIMEOUT_SECONDS must be between 1 and 3600")
        deadline = time.monotonic() + timeout
        while True:
            health = self.worker_request(environment, "GET", "/health")
            active_jobs = health.get("active_jobs")
            if (
                isinstance(active_jobs, bool)
                or not isinstance(active_jobs, int)
                or active_jobs < 0
            ):
                raise ReleaseError(
                    "monomer MD worker did not report a valid active job count"
                )
            if health.get("draining") is not True or health.get("accepting_jobs") is not False:
                raise ReleaseError("monomer MD worker did not remain in drain mode")
            if active_jobs == 0:
                return health
            if time.monotonic() >= deadline:
                raise DeploymentDeferred(
                    "deployment deferred: monomer MD Worker still has active jobs"
                )
            time.sleep(min(5, max(1, deadline - time.monotonic())))

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

        del environment  # Endpoints are a fixed production release contract.
        web_base = PRODUCTION_WEB_BASE_URL
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        try:
            with opener.open(f"{web_base}/", timeout=20) as response:
                html = response.read(2 * 1024 * 1024)
                content_type = response.headers.get_content_type()
            if content_type != "text/html" or b'<div id="root">' not in html:
                raise ReleaseError("web root did not return the expected application HTML")
            assets = re.findall(rb'(?:src|href)="(/assets/[^"?]+)', html)
            if not assets:
                raise ReleaseError("web root did not reference a versioned static asset")
            asset_path = assets[0].decode("utf-8", "strict")
            with opener.open(f"{web_base}{asset_path}", timeout=20) as response:
                payload = response.read(1024)
                if response.status != 200 or not payload:
                    raise ReleaseError("versioned static asset smoke failed")
        except (OSError, UnicodeError) as exc:
            raise ReleaseError(f"web static-resource smoke failed: {exc}") from exc

    def fetch_local_json_object(
        self,
        url: str,
        *,
        label: str,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Fetch one loopback-only bounded JSON object without logging its body."""

        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ReleaseError(f"{label} must use a loopback HTTP URL")
        # Do not let inherited HTTP(S)_PROXY settings turn a nominally local
        # readiness check into an external request carrying runtime metadata.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        try:
            with opener.open(url, timeout=timeout) as response:
                if response.status < 200 or response.status >= 300:
                    raise ReleaseError(f"{label} returned a non-success HTTP status")
                payload = response.read(MAX_RUNTIME_RESPONSE_BYTES + 1)
        except ReleaseError:
            raise
        except (OSError, ValueError) as exc:
            raise ReleaseError(f"{label} request failed") from exc
        return decode_bounded_json_object(payload, label)

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
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        for url in (PRODUCTION_HEALTH_URL,):
            deadline = time.monotonic() + health_timeout
            last_error = "no response"
            while True:
                try:
                    with opener.open(url, timeout=20) as response:
                        if 200 <= response.status < 300:
                            break
                        last_error = f"HTTP {response.status}"
                except OSError as exc:
                    last_error = str(exc)
                if time.monotonic() >= deadline:
                    raise ReleaseError(f"health endpoint failed: {url}: {last_error}")
                time.sleep(5)

        self.public_web_static_smoke(environment)

        polytao_enabled = environment.get("POLYTAO_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
        if polytao_enabled:
            status = self.fetch_local_json_object(
                PRODUCTION_POLYTAO_STATUS_URL,
                label="PolyTAO status",
            )
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

            monomer_status = self.fetch_local_json_object(
                PRODUCTION_MONOMER_STATUS_URL,
                label="monomer MD status",
            )
            if monomer_status.get("default_steps") != 300:
                raise ReleaseError("monomer MD backend did not report the 300-step contract")
            if monomer_status.get("available") is not True:
                raise ReleaseError("monomer MD backend did not report an available runtime")
            if (
                self.deploy_transport_required
                and not worker_transport_is_strict_ready(monomer_status)
            ):
                raise ReleaseError(
                    "monomer MD backend status did not satisfy strict Transport readiness"
                )
            if self.worker_restart_deferred:
                if monomer_status.get("draining") is not True or monomer_status.get("can_submit") is not False:
                    raise ReleaseError("monomer MD backend did not report deferred drain state")
            elif monomer_status.get("can_submit") is not True:
                raise ReleaseError("monomer MD backend was not ready for smoke submission")

            if self.deploy_transport_required:
                protocol_catalog = self.fetch_local_json_object(
                    PRODUCTION_MONOMER_PROTOCOLS_URL,
                    label="monomer MD protocols",
                )
                if (
                    protocol_catalog.get("enabled") is not True
                    or protocol_catalog.get("available") is not True
                    or not protocol_catalog_transport_is_strict_ready(protocol_catalog)
                ):
                    raise ReleaseError(
                        "monomer MD protocol catalog did not satisfy strict Transport readiness"
                    )

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
        """Detach a rolled-back bootstrap without deleting a READY release."""

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
            ready = self.release_dir / PROVISIONING_READY_NAME
            if ready.exists() or ready.is_symlink():
                if not ready.is_file() or ready.is_symlink():
                    raise ReleaseError(
                        "failed bootstrap release has an ambiguous READY record"
                    )
                return
            self._remove_owned_incomplete_provisioning(self.release_dir)

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
        """Retain a READY rolled-back target so deploy can retry without pip."""

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
        ready = self.release_dir / PROVISIONING_READY_NAME
        if ready.exists() or ready.is_symlink():
            if not ready.is_file() or ready.is_symlink():
                raise ReleaseError("failed release has an ambiguous READY record")
            return
        self._remove_owned_incomplete_provisioning(self.release_dir)

    def cleanup_unrecorded_staging(self) -> None:
        """Remove only a source/manifest/token-owned incomplete provisioning."""

        if not self.staging.exists() and not self.staging.is_symlink():
            return
        self._remove_owned_incomplete_provisioning(self.staging)

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

        # Recovery is an execution path, not a cleanup shortcut.  Re-establish
        # the exact immutable candidate identity before running any target
        # Compose command, Worker program, drain operation, or database action.
        # A staging tree is never executable and is deliberately left for
        # explicit owner-validated cleanup.
        if self.staging.exists() or self.staging.is_symlink():
            raise ReleaseError(
                "interrupted deployment has an unfinished staging directory"
            )
        try:
            release_metadata = self.release_dir.lstat()
        except OSError as exc:
            raise ReleaseError(
                "interrupted deployment target release is unavailable"
            ) from exc
        if (
            not stat.S_ISDIR(release_metadata.st_mode)
            or self.release_dir.is_symlink()
            or release_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(release_metadata.st_mode) != 0o700
        ):
            raise ReleaseError("interrupted deployment target release is unsafe")
        ready_digest = require_digest(
            str(marker.get("provisioning_ready_sha256", "")),
            "interrupted deployment provisioning READY digest",
        )
        ready_path = self.release_dir / PROVISIONING_READY_NAME
        try:
            ready_metadata = ready_path.lstat()
        except OSError as exc:
            raise ReleaseError(
                "interrupted deployment provisioning READY record is missing"
            ) from exc
        if (
            not stat.S_ISREG(ready_metadata.st_mode)
            or ready_path.is_symlink()
            or ready_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(ready_metadata.st_mode) != 0o600
            or sha256_file(ready_path) != ready_digest
        ):
            raise ReleaseError(
                "interrupted deployment provisioning READY digest does not match"
            )
        self._validate_provisioned_ready(
            environment,
            require_bundle_artifact=False,
        )
        self.candidate_dir = self.release_dir

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
                self.drain(environment, False)
            self.clear_failed_bootstrap_release()
            durable_unlink(self.in_progress_path)
            return

        previous_sha = require_sha(
            str(previous_state.get("source_sha", "")),
            "interrupted deployment previous release SHA",
        )
        previous_release = self.ops / "releases" / previous_sha
        if not previous_release.is_dir() or previous_release.is_symlink():
            raise ReleaseError("interrupted deployment previous release is unavailable")
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
                self.compose(self.release_dir, "stop", "nginx", "backend"),
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
        durable_unlink(self.in_progress_path)

    def _remove_owned_incomplete_provisioning(self, candidate: Path) -> None:
        if not candidate.exists() and not candidate.is_symlink():
            return
        if not candidate.is_dir() or candidate.is_symlink():
            raise ReleaseError("incomplete provisioning path is unsafe")
        try:
            releases_root = (self.ops / "releases").resolve(strict=True)
            resolved_candidate = candidate.resolve(strict=True)
        except OSError as exc:
            raise ReleaseError("incomplete provisioning path cannot be resolved") from exc
        if (
            resolved_candidate.parent != releases_root
            or candidate.name not in {self.sha, f"{self.sha}.staging"}
        ):
            raise ReleaseError("incomplete provisioning path is outside its release slot")
        if (candidate / PROVISIONING_READY_NAME).exists() or (
            candidate / PROVISIONING_READY_NAME
        ).is_symlink():
            raise ReleaseError("refusing to remove a READY or ambiguous provisioned release")
        self._provisioning_owner(candidate)
        current = self.ops / "current"
        if current.exists() or current.is_symlink():
            try:
                if current.resolve(strict=True) == candidate.resolve(strict=True):
                    raise ReleaseError(
                        "refusing to remove provisioning referenced by ops/current"
                    )
            except OSError as exc:
                raise ReleaseError(
                    "cannot resolve ops/current while cleaning provisioning"
                ) from exc
        tombstone = candidate.parent / (
            f".{self.sha}.discard-{secrets.token_hex(16)}"
        )
        rename_noreplace(candidate, tombstone)
        fsync_directory(candidate.parent)
        self._remove_owned_provisioning_tombstone(tombstone)

    def _remove_owned_provisioning_tombstone(self, tombstone: Path) -> None:
        """Finish deletion while retaining owner proof until the final unlink."""

        prefix = f".{self.sha}.discard-"
        try:
            releases_root = (self.ops / "releases").resolve(strict=True)
            resolved = tombstone.resolve(strict=True)
        except OSError as exc:
            raise ReleaseError("provisioning tombstone is unavailable") from exc
        if (
            resolved.parent != releases_root
            or not tombstone.name.startswith(prefix)
            or not tombstone.is_dir()
            or tombstone.is_symlink()
        ):
            raise ReleaseError("provisioning tombstone is unsafe")
        ready = tombstone / PROVISIONING_READY_NAME
        if ready.exists() or ready.is_symlink():
            raise ReleaseError("provisioning tombstone unexpectedly contains READY")
        self._provisioning_owner(tombstone)
        owner = tombstone / PROVISIONING_OWNER_NAME
        for child in sorted(tombstone.iterdir(), key=lambda item: item.name):
            if child.name == PROVISIONING_OWNER_NAME:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        owner.unlink()
        tombstone.rmdir()
        fsync_directory(tombstone.parent)

    def _cleanup_owned_provisioning_tombstones(self) -> None:
        releases = self.ops / "releases"
        prefix = f".{self.sha}.discard-"
        for candidate in sorted(releases.iterdir(), key=lambda item: item.name):
            if candidate.name.startswith(prefix):
                self._remove_owned_provisioning_tombstone(candidate)

    def provision(self) -> dict[str, Any]:
        """Explicitly build a per-SHA Worker venv without touching runtime state."""

        self.ensure_root()
        plan = {**self.plan(), "action": "provision-release"}
        if not self.apply:
            return plan
        os.umask(0o077)
        with self.deployment_lock():
            if self.in_progress_path.exists() or self.in_progress_path.is_symlink():
                if (
                    not self.in_progress_path.is_file()
                    or self.in_progress_path.is_symlink()
                ):
                    raise ReleaseError("interrupted deployment marker is unsafe")
                interrupted = load_manifest(self.in_progress_path)
                interrupted_sha = require_sha(
                    str(interrupted.get("source_sha", "")),
                    "interrupted release SHA",
                )
                interrupted_manifest_digest = require_digest(
                    str(interrupted.get("release_manifest_sha256", "")),
                    "interrupted release manifest digest",
                )
                interrupted_ready_digest = require_digest(
                    str(interrupted.get("provisioning_ready_sha256", "")),
                    "interrupted deployment provisioning READY digest",
                )
                if (
                    interrupted_sha != self.sha
                    or interrupted_manifest_digest != sha256_file(self.manifest_path)
                    or (interrupted.get("bootstrap") is True)
                    != (self.mode == "bootstrap")
                    or interrupted.get("phase")
                    not in {"prepared", "db-changed", "switched", "verified"}
                ):
                    raise ReleaseError(
                        "release provisioning is forbidden during a different "
                        "unfinished deployment"
                    )
                environment = self.environment()
                self.prepare_staging(environment)
                ready_path = self.release_dir / PROVISIONING_READY_NAME
                if sha256_file(ready_path) != interrupted_ready_digest:
                    raise ReleaseError(
                        "interrupted deployment provisioning READY digest does not match"
                    )
                return {
                    **plan,
                    "status": "interrupted-ready",
                    "release": str(self.release_dir),
                }
            environment = self.environment()
            self._cleanup_owned_provisioning_tombstones()
            if self.release_dir.exists() or self.release_dir.is_symlink():
                if (self.release_dir / PROVISIONING_READY_NAME).is_file():
                    self.prepare_staging(environment)
                    self.assert_still_current_main(environment)
                    return {
                        **plan,
                        "status": "already-ready",
                        "release": str(self.release_dir),
                    }
                self._remove_owned_incomplete_provisioning(self.release_dir)
            if self.staging.exists() or self.staging.is_symlink():
                self._remove_owned_incomplete_provisioning(self.staging)

            created_candidate = False
            try:
                created_candidate = True
                self._provision_staging(environment)
                self.assert_still_current_main(environment)
                rename_noreplace(self.staging, self.release_dir)
                fsync_directory(self.release_dir.parent)
                self.candidate_dir = self.release_dir
                # Create the venv only after the source tree is at its final
                # immutable path.  Console-script shebangs, pyvenv.cfg, and
                # bytecode filenames must never capture a disappearing
                # <sha>.staging prefix.
                self.prepare_worker(environment)
                evidence = self._provisioning_evidence(
                    self.release_dir,
                    environment,
                )
                self.assert_still_current_main(environment)
                fsync_tree(self.release_dir)
                atomic_json(
                    self.release_dir / PROVISIONING_READY_NAME,
                    {**evidence, "provisioned_at": utc_now()},
                )
                # Recompute from disk after READY is durable; deploy performs
                # this same validation and contains no package installation.
                self.prepare_staging(environment)
                return {
                    **plan,
                    "status": "ready",
                    "release": str(self.release_dir),
                    "venv_prefix": evidence["venv_prefix"],
                }
            except BaseException:
                if created_candidate:
                    for candidate in (self.staging, self.release_dir):
                        if not (candidate.exists() or candidate.is_symlink()):
                            continue
                        ready = candidate / PROVISIONING_READY_NAME
                        if ready.exists() or ready.is_symlink():
                            # READY (including an ambiguous symlink) is never
                            # removed by an exception path.
                            continue
                        self._remove_owned_incomplete_provisioning(candidate)
                raise

    def deploy(self) -> dict[str, Any]:
        self.ensure_root()
        if not self.apply:
            return self.plan()
        os.umask(0o077)
        with self.deployment_lock():
            if self.in_progress_path.exists() or self.in_progress_path.is_symlink():
                if not self.in_progress_path.is_file() or self.in_progress_path.is_symlink():
                    raise ReleaseError("interrupted deployment marker is unsafe")
                interrupted = load_manifest(self.in_progress_path)
                interrupted_sha = require_sha(
                    str(interrupted.get("source_sha", "")),
                    "interrupted release SHA",
                )
                interrupted_release = self.ops / "releases" / interrupted_sha
                interrupted_staging = self.ops / "releases" / f"{interrupted_sha}.staging"
                if interrupted_staging.exists() or interrupted_staging.is_symlink():
                    raise ReleaseError(
                        "interrupted deployment has an unfinished staging directory"
                    )
                try:
                    interrupted_release_metadata = interrupted_release.lstat()
                except OSError as exc:
                    raise ReleaseError(
                        "interrupted deployment target release is unavailable"
                    ) from exc
                if (
                    not stat.S_ISDIR(interrupted_release_metadata.st_mode)
                    or interrupted_release.is_symlink()
                    or interrupted_release_metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(interrupted_release_metadata.st_mode) != 0o700
                ):
                    raise ReleaseError("interrupted deployment target release is unsafe")
                interrupted_manifest = interrupted_release / "release-manifest.json"
                if (
                    not interrupted_manifest.is_file()
                    or interrupted_manifest.is_symlink()
                    or sha256_file(interrupted_manifest)
                    != interrupted.get("release_manifest_sha256")
                ):
                    raise ReleaseError(
                        "interrupted deployment lacks a matching verified release manifest"
                    )
                interrupted_ready = interrupted_release / PROVISIONING_READY_NAME
                interrupted_ready_digest = require_digest(
                    str(interrupted.get("provisioning_ready_sha256", "")),
                    "interrupted deployment provisioning READY digest",
                )
                if (
                    not interrupted_ready.is_file()
                    or interrupted_ready.is_symlink()
                    or sha256_file(interrupted_ready) != interrupted_ready_digest
                ):
                    raise ReleaseError(
                        "interrupted deployment lacks a matching provisioning READY record"
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
            if self.staging.exists() or self.staging.is_symlink():
                raise ReleaseError(
                    "unfinished release provisioning exists; rerun provision-release first"
                )
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
                self.assert_still_current_main(environment)
                state["provisioning_ready_sha256"] = sha256_file(
                    self.release_dir / PROVISIONING_READY_NAME
                )
                # The separately provisioned READY tree is immutable,
                # validated, and freshness-bound. Record its exact digest
                # before the first Worker/Docker/database mutation.
                self.write_attempt(state)
                if self.deploy_transport_required:
                    if not self.bootstrap:
                        state["worker_drain_attempted"] = True
                        self.write_attempt(state)
                        self.worker_drain_info = self.drain_worker(environment)
                        idle_health = self.wait_for_worker_idle(environment)
                        self.worker_drain_info = {
                            **self.worker_drain_info,
                            "active_jobs": idle_health["active_jobs"],
                        }
                        state["worker_drain"] = self.worker_drain_info
                        self.write_attempt(state)
                    state["transport_preflight"] = self.run_candidate_runtime_preflight(
                        environment
                    )
                    # Candidate code ran under the deploy user.  Re-seal the
                    # complete payload before the first candidate Compose
                    # command so a malicious/broken .pth or probe cannot alter
                    # what Docker will consume.
                    self.prepare_staging(environment)
                    if (
                        sha256_file(self.release_dir / PROVISIONING_READY_NAME)
                        != state["provisioning_ready_sha256"]
                    ):
                        raise ReleaseError(
                            "candidate provisioning READY record changed during preflight"
                        )
                    self.assert_still_current_main(environment)
                    self.write_attempt(state)
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
                    if (
                        release_uses_worker(self.document)
                        and self.worker_drain_info is None
                    ):
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
                # The potentially long drain/migration/rebuild window must not
                # let a changed candidate escape its provisioning seal.
                self.prepare_staging(environment)
                if (
                    sha256_file(self.release_dir / PROVISIONING_READY_NAME)
                    != state["provisioning_ready_sha256"]
                ):
                    raise ReleaseError(
                        "candidate provisioning READY record changed during deployment"
                    )
                self.assert_still_current_main(environment)
                state["phase"] = "switched"
                state["runtime_switch_started"] = True
                self.write_attempt(state)
                self.run(
                    self.compose(self.candidate_dir, "stop", "nginx", "backend"),
                    env=environment,
                )
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
                if (
                    (self.staging.exists() or self.staging.is_symlink())
                    and not self.in_progress_path.exists()
                    and not self.in_progress_path.is_symlink()
                ):
                    self.cleanup_unrecorded_staging()


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

    provision = commands.add_parser(
        "provision-release",
        help="explicitly build and seal the target-SHA Worker release before deploy",
    )
    provision.add_argument("--manifest", required=True)
    provision.add_argument("--mode", choices=("auto", "bootstrap"), default="auto")
    provision.add_argument(
        "--production-root",
        default=os.environ.get("NEXPOLY_PRODUCTION_ROOT", str(PRODUCTION_ROOT)),
    )
    provision.add_argument("--apply", action="store_true")

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
        elif args.command == "provision-release":
            document = ReleaseController(
                Path(args.production_root),
                Path(args.manifest),
                args.mode,
                args.apply,
            ).provision()
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
