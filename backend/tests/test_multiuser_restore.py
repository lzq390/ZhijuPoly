"""Real PostgreSQL dump/restore plus private-file and identity acceptance.

Requires a disposable ISOLATION_TEST_ADMIN_DSN and matching pg_dump/pg_restore.
Missing database configuration is rejected by the mandatory suite's skip gate;
missing/incompatible PostgreSQL binaries fail this test explicitly.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest

from app.auth.assets import archive_assets, asset_snapshot, file_sha256
from app.auth.context import Identity, user_context
from app.auth.cutover import backup_state_seal
from app.auth.schema import CHILD_TABLES, OWNER_TABLES, validate_isolation_schema, validate_runtime_role
from app.config import Settings
from app.main import create_app
from app.postgres_database import postgres_connection
from app.postgres_preflight import SCHEMA_TARGET_ISOLATION, run_preflight
from app.services.polymerization_batch.models import BatchSettings
from app.services.polymerization_batch.service import BatchService
from test_multiuser_support import (
    auth_database, multiuser_accounts, multiuser_case, owner, seed_private_resources,
)
from test_private_http_support import authenticated_client


def _postgres_tools(server_major):
    configured = os.environ.get("MULTIUSER_PG_BIN")
    standard = Path(f"/usr/lib/postgresql/{server_major}/bin")
    selected = {}
    for name in ("pg_dump", "pg_restore"):
        candidate = (Path(configured) / name) if configured else standard / name
        if not candidate.is_file() and not configured:
            candidate = Path(shutil.which(name) or "/nonexistent-postgresql-tool")
        if not candidate.is_file():
            pytest.fail(f"{name} is required; install PostgreSQL {server_major} client tools or set MULTIUSER_PG_BIN")
        result = subprocess.run([str(candidate), "--version"], text=True, capture_output=True, timeout=10)
        version = re.search(r"PostgreSQL\)\s+(\d+)\.", result.stdout)
        if result.returncode or not version or int(version[1]) != server_major:
            pytest.fail(f"{name} must match PostgreSQL server major {server_major}; incompatible tool at {candidate}")
        selected[name] = candidate
    return selected


def _run_pg_tool(binary, dsn, *arguments):
    # Keep connection details out of argv, diagnostics and pytest failures.
    parameters = conninfo_to_dict(dsn)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    mappings = {"dbname": "PGDATABASE", "host": "PGHOST", "hostaddr": "PGHOSTADDR",
                "port": "PGPORT", "user": "PGUSER", "password": "PGPASSWORD",
                "sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT",
                "sslkey": "PGSSLKEY", "options": "PGOPTIONS", "client_encoding": "PGCLIENTENCODING"}
    for key, variable in mappings.items():
        if key in parameters:
            environment[variable] = parameters[key]
    environment.update(PGCONNECT_TIMEOUT="5", PGAPPNAME="multiuser-restore-contract")
    result = subprocess.run([str(binary), *map(str, arguments)], env=environment,
                            capture_output=True, timeout=90)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace")
        for secret in (dsn, parameters.get("password", "")):
            if secret:
                detail = detail.replace(secret, "[redacted]")
        detail = re.sub(r"postgres(?:ql)?://[^\s]+", "[redacted DSN]", detail)
        detail = re.sub(r"password\s*=\s*[^\s]+", "password=[redacted]", detail)
        pytest.fail(f"{binary.name} failed with exit {result.returncode}: {detail[:1500]}")


def _ownership_inventory(connection):
    result = {}
    for relation in OWNER_TABLES:
        rows = connection.execute(sql.SQL("SELECT owner_user_id::text AS owner,count(*) AS n FROM {} GROUP BY 1 ORDER BY 1")
                                  .format(sql.Identifier(*relation.split(".")))).fetchall()
        result[relation] = rows
    for relation, (parent, key, _) in CHILD_TABLES.items():
        rows = connection.execute(sql.SQL("SELECT p.owner_user_id::text AS owner,count(*) AS n FROM {} c "
                                           "JOIN {} p ON c.job_id=p.{} GROUP BY 1 ORDER BY 1")
                                  .format(sql.Identifier(*relation.split(".")),
                                          sql.Identifier(*parent.split(".")), sql.Identifier(key))).fetchall()
        result[relation] = rows
    return result


def _seed_files(case, resources, directory):
    roots = {"batch_imports": directory / "batch" / "imports", "batch_jobs": directory / "batch" / "jobs",
             "md": directory / "md", "dft": directory / "dft"}
    for root in roots.values():
        root.mkdir(parents=True)
    with psycopg.connect(case.database["admin"]) as connection:
        for index, ids in enumerate(resources):
            content = (json.dumps({"owner": owner(case, index), "marker": f"restore-user-{index}"}, sort_keys=True) + "\n").encode()
            locations = {
                "batch_imports": (ids["import"], "source_a.csv"),
                "batch_jobs": (ids["batch"], "results.csv"),
                "md": (ids["md"], "result.json"),
                "dft": (ids["dft"], "artifacts/result.json"),
            }
            manifests = {}
            for name, (resource_id, filename) in locations.items():
                target = roots[name] / resource_id / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o600)
                manifests[name] = {"size_bytes": len(content), "sha256": file_sha256(target)}
            files = {"a": {"filename": "source_a.csv", "path": f"imports/{ids['import']}/source_a.csv",
                           "format": "csv", **manifests["batch_imports"]}}
            connection.execute("UPDATE polymerization_batch.imports SET files=%s WHERE id=%s",
                               (Jsonb(files), ids["import"]))
            artifact = {"path": f"jobs/{ids['batch']}/results.csv", "media_type": "text/csv", **manifests["batch_jobs"]}
            connection.execute("UPDATE polymerization_batch.jobs SET import_id=%s,options=options||%s,artifacts=%s "
                               "WHERE id=%s", (ids["import"], Jsonb({"import_id": ids["import"]}),
                                               Jsonb({"results.csv": artifact}), ids["batch"]))
            connection.execute("UPDATE monomer_dft.artifacts SET size_bytes=%s,sha256=%s WHERE job_id=%s AND artifact_id='result'",
                               (len(content), manifests["dft"]["sha256"], ids["dft"]))
    return roots


def test_multiuser_real_database_and_private_files_restore_preserves_identity_and_rls(multiuser_case, tmp_path):
    case = multiuser_case
    resources = seed_private_resources(case)
    with psycopg.connect(case.database["admin"]) as connection:
        server_major = connection.info.server_version // 10000
    binaries = _postgres_tools(server_major)
    destination = "multiuser_restore_" + uuid4().hex
    restored = {kind: make_conninfo(dsn, dbname=destination) for kind, dsn in case.database.items()}
    with tempfile.TemporaryDirectory(prefix="private-restore-", dir=tmp_path) as temporary:
        private_directory = Path(temporary)
        source_roots = _seed_files(case, resources, private_directory / "source")
        dump_directory = private_directory / "backup"
        dump_directory.mkdir(mode=0o700)
        dump = dump_directory / "database.dump"
        descriptor = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        with psycopg.connect(case.database["admin"], row_factory=dict_row) as connection:
            before = backup_state_seal(connection)
            ownership = _ownership_inventory(connection)
        assert all(before[relation]["rows"] == 3 for relation in (*OWNER_TABLES, *CHILD_TABLES))
        assert all(len(rows) == 3 and all(row["n"] == 1 for row in rows) for rows in ownership.values())
        receipt = archive_assets(source_roots, dump_directory, before)
        _run_pg_tool(binaries["pg_dump"], case.database["admin"], "--format=custom", "--file", dump)
        assert dump.stat().st_size > 0 and dump.stat().st_mode & 0o777 == 0o600
        try:
            with psycopg.connect(case.database["admin"], autocommit=True) as connection:
                connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(destination)))
            _run_pg_tool(binaries["pg_restore"], restored["admin"], "--exit-on-error", "--single-transaction",
                         "--dbname", destination, dump)
            restored_root = private_directory / "restored"
            restored_roots = {"batch_imports": restored_root / "batch" / "imports", "batch_jobs": restored_root / "batch" / "jobs",
                              "md": restored_root / "md", "dft": restored_root / "dft"}
            for name, item in receipt.items():
                archive = Path(item["archive_path"])
                assert file_sha256(archive) == item["archive_sha256"]
                staging = private_directory / "unpack" / name
                staging.mkdir(parents=True)
                with tarfile.open(archive, "r:gz") as source:
                    source.extractall(staging, filter="data")
                restored_roots[name].parent.mkdir(parents=True, exist_ok=True)
                (staging / "assets").rename(restored_roots[name])
                assert asset_snapshot(restored_roots[name]) == item["files"]
                assert asset_snapshot(source_roots[name]) == item["files"]
            with psycopg.connect(restored["admin"], row_factory=dict_row) as connection:
                assert backup_state_seal(connection) == before
                assert _ownership_inventory(connection) == ownership
            with psycopg.connect(case.database["admin"], row_factory=dict_row) as connection:
                assert backup_state_seal(connection) == before, "Backup/restore must not mutate its source database"

            auth_settings = replace(case.auth_settings, application_dsn=restored["api"],
                                    auth_dsn=restored["auth"], service_dsn=restored["service"])
            # Test actual API logins, not SET ROLE as the restoring superuser.
            for index in (0, 1):
                with user_context(Identity(owner(case, index)), auth_settings), postgres_connection(restored["api"]) as connection:
                    assert validate_runtime_role(connection, "nexpoly_api")["privilege_group"] == "nexpoly_api"
                    validate_isolation_schema(connection)
                    for relation, key in OWNER_TABLES.items():
                        rows = connection.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(*relation.split(".")))).fetchall()
                        assert len(rows) == 1 and str(rows[0]["owner_user_id"]) == owner(case, index), relation
                    for relation, (parent, key, _) in CHILD_TABLES.items():
                        expected_job = resources[index]["batch" if parent.startswith("polymerization_batch") else "dft"]
                        rows = connection.execute(sql.SQL("SELECT job_id::text AS job_id FROM {}")
                                                  .format(sql.Identifier(*relation.split(".")))).fetchall()
                        assert rows == [{"job_id": expected_job}], relation
            with postgres_connection(restored["api"]) as connection:
                for relation in (*OWNER_TABLES, *CHILD_TABLES):
                    assert connection.execute(sql.SQL("SELECT count(*) AS n FROM {}")
                                              .format(sql.Identifier(*relation.split(".")))).fetchone()["n"] == 0
            settings = Settings(app_postgres_dsn=restored["api"], pi_postgres_dsn=restored["api"], model_enabled=False,
                                gen_model_enabled=False, retro_model_enabled=False, gpu_broker_enabled=False)
            report = run_preflight(settings, dsn=restored["service"], mode="schema", strict=True,
                                   schema_target=SCHEMA_TARGET_ISOLATION)
            assert report["strict_ok"], report["strict_errors"]
            assert report["service_identity"]["privilege_group"] == "nexpoly_service"

            app = create_app(settings)
            app.state.polymerization_batch = BatchService(restored["api"], BatchSettings(enabled=True, storage_root=restored_root / "batch"))
            clients = []
            try:
                for index in (0, 1):
                    clients.append(authenticated_client(app, restored, user=case.users[index]))
                app.state.auth.assert_application_ready()
                # Restored credential hashes support real login, and restored
                # live session hashes still resolve to their original owner.
                for index, client in enumerate(clients):
                    token = case.clients[index].cookies.get(case.auth_settings.cookie_name)
                    restored_session = app.state.auth.resolve(token)
                    assert restored_session is not None
                    assert app.state.auth.identity(restored_session).user_id == owner(case, index)
                    own = resources[index]["batch"]
                    foreign = resources[1-index]["batch"]
                    listing = client.get("/api/v1/monomer-polymerization/batch/jobs")
                    assert listing.status_code == 200 and listing.json()["total"] == 1
                    assert listing.json()["items"][0]["job_id"] == own
                    download = client.get(f"/api/v1/monomer-polymerization/batch/jobs/{own}/artifacts/results.csv")
                    assert download.status_code == 200
                    assert download.content == (restored_roots["batch_jobs"] / own / "results.csv").read_bytes()
                    for tail in ("", "/artifacts/results.csv"):
                        response = client.get(f"/api/v1/monomer-polymerization/batch/jobs/{foreign}{tail}")
                        absent = client.get(f"/api/v1/monomer-polymerization/batch/jobs/{uuid4().hex}{tail}")
                        assert response.status_code == absent.status_code == 404
                        assert response.json() == absent.json()
            finally:
                for client in clients:
                    client.close()
        finally:
            # Also attempt cleanup when creation committed but its acknowledgement
            # was lost; the UUID name belongs only to this test invocation.
            with psycopg.connect(case.database["admin"], autocommit=True) as connection:
                connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(destination)))
    assert not private_directory.exists(), "Private dump and restored files must be removed"
    with psycopg.connect(case.database["admin"]) as connection:
        assert not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (destination,)).fetchone()
