#!/usr/bin/env python3
"""Operate only the isolated MCP development service and its read-only role.

No production settings are loaded. Database passwords and bearer tokens never
appear in command arguments, Compose environment values, or command output.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "property-filter-mcp"
DEV_ROOT = Path("/data/lzq/gith/nexpoly-dev")
DEV_CONTAINER = "nexpoly_dev-lab-postgres-1"
DEV_NETWORK = "nexpoly_dev_default"
DATABASE = "nexpoly_dev"
ROLE = "nexpoly_mcp_dev_reader"
ROLE_MARKER = "nexpoly-property-filter-mcp-development-v1"
PROJECT = "nexpoly_mcp_dev"
SERVICE = "property-filter-mcp"
CLIENT_ID = "openscience-dev"
BASE_URL = "http://127.0.0.1:18110"
TOOLS = ("list_filter_properties", "search_polymers", "get_property_histogram")
TABLES = (
    "core.polymer_property_filter_records",
    "governance.property_filter_options_snapshots",
    "governance.import_batches",
    "governance.source_files",
)


class DevError(RuntimeError):
    pass


def run(command: list[str], *, payload: str | None = None, env=None,
        diagnostics: str | None = None) -> str:
    if diagnostics is not None:
        # Only compose build/up callers opt in. SQL and credential-bearing
        # command input must never be written to this diagnostic stream.
        if payload is not None or diagnostics not in {"docker-up.log", "docker-build.log"}:
            raise DevError("Diagnostic logging is restricted to container build/start output.")
        private_directory()
        path = RUNTIME / diagnostics
        if path.exists() or path.is_symlink():
            read_private(diagnostics)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        print(json.dumps({"action": "container-operation", "private_diagnostics": str(path)}), flush=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as log:
                result = subprocess.run(command, text=True, stdout=log, stderr=subprocess.STDOUT,
                                        cwd=ROOT, env=env, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            raise DevError(f"Container operation did not complete; private diagnostics: {path}") from None
        if result.returncode:
            raise DevError(f"Container operation failed; private diagnostics: {path}")
        return ""
    try:
        result = subprocess.run(command, input=payload, text=True, capture_output=True,
                                cwd=ROOT, env=env, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        raise DevError("Development command could not complete; no credentials were printed.") from None
    if result.returncode:
        # psql can include submitted SQL in errors, and Docker can include mounts.
        raise DevError("Development command failed; inspect the named development service locally.")
    return result.stdout


def docker_field(field: str):
    return json.loads(run(["docker", "inspect", "--format", "{{json " + field + "}}", DEV_CONTAINER]))


def verify_project_ownership() -> None:
    containers = run(["docker", "ps", "--all", "--filter",
                      "label=com.docker.compose.project=" + PROJECT, "--format", "{{.ID}}"])
    for container in containers.splitlines():
        labels = json.loads(run(["docker", "inspect", "--format", "{{json .Config.Labels}}", container])) or {}
        if (labels.get("com.docker.compose.project") != PROJECT
                or labels.get("com.docker.compose.service") != SERVICE
                or labels.get("com.docker.compose.project.working_dir") != str(ROOT)):
            raise DevError("The existing MCP development project belongs to another checkout; refusing to change it.")
        mounts = json.loads(run(["docker", "inspect", "--format", "{{json .Mounts}}", container]))
        credentials = [mount for mount in mounts if mount.get("Destination") == "/run/property-filter-mcp"]
        if (len(credentials) != 1 or credentials[0].get("Type") != "bind"
                or credentials[0].get("Source") != str(RUNTIME / "server")
                or credentials[0].get("RW") is not False):
            raise DevError("The existing MCP credential mount belongs to another runtime; refusing to change it.")


def verify_dev_target() -> None:
    labels = docker_field(".Config.Labels") or {}
    expected = {
        "com.docker.compose.project": "nexpoly_dev",
        "com.docker.compose.service": "lab-postgres",
        "com.docker.compose.project.working_dir": str(DEV_ROOT),
    }
    if any(labels.get(key) != value for key, value in expected.items()):
        raise DevError("Database container does not match the approved development project.")
    configs = labels.get("com.docker.compose.project.config_files", "").split(",")
    required = {str(DEV_ROOT / name) for name in ("docker-compose.yml", "docker-compose.dev.yml")}
    if not required.issubset(configs) or any("prod" in Path(name).name for name in configs):
        raise DevError("Database Compose provenance is not the approved development configuration.")
    if not docker_field(".State.Running") or DEV_NETWORK not in docker_field(".NetworkSettings.Networks"):
        raise DevError("The approved development database is not running on its development network.")
    identity = psql("SELECT current_database() || ':' || current_user;").strip()
    if identity != DATABASE + ":nexpoly_dev":
        raise DevError("Development database identity differs from the approved target.")


def psql(sql: str) -> str:
    return run(["docker", "exec", "-i", DEV_CONTAINER, "psql", "-X", "-qAt",
                "--set", "ON_ERROR_STOP=1", "--username", "nexpoly_dev", "--dbname", DATABASE], payload=sql)


def private_directory() -> None:
    for directory in (ROOT / ".runtime", RUNTIME, RUNTIME / "server"):
        if directory.is_symlink():
            raise DevError("Runtime directories must not be symbolic links.")
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise DevError("Runtime directories must belong to the invoking user.")
    RUNTIME.chmod(0o700)
    (RUNTIME / "server").chmod(0o700)


def read_private(name: str) -> str:
    path = RUNTIME / name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise DevError("Credential files must be ordinary, owner-only mode 0600 files.")
    return path.read_text(encoding="utf-8")


def write_private(name: str, payload: str) -> None:
    path = RUNTIME / name
    if path.exists() or path.is_symlink():
        read_private(name)
    temporary = path.parent / ("." + path.name + "." + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def operation_lock():
    private_directory()
    path = RUNTIME / "operation.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise DevError("Operation lock must have mode 0600.")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def scram_verifier(password: str) -> str:
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("ascii"), salt, 4096)
    stored = hashlib.sha256(hmac.new(salted, b"Client Key", hashlib.sha256).digest()).digest()
    server = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    encode = lambda value: base64.b64encode(value).decode("ascii")
    return f"SCRAM-SHA-256$4096:{encode(salt)}${encode(stored)}:{encode(server)}"


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def provision_sql(password: str, *, allow_existing: bool = True) -> str:
    tables = ", ".join(TABLES)
    return f"""BEGIN;
SELECT pg_advisory_xact_lock(18110, 5432);
DO $mcp$
BEGIN
  IF current_database() <> {literal(DATABASE)} THEN RAISE EXCEPTION 'wrong database'; END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname={literal(ROLE)}) THEN
    IF NOT {'true' if allow_existing else 'false'} THEN RAISE EXCEPTION 'role was concurrently provisioned'; END IF;
    IF (SELECT shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname={literal(ROLE)})
       IS DISTINCT FROM {literal(ROLE_MARKER)} THEN RAISE EXCEPTION 'unmanaged existing role'; END IF;
  ELSE
    CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
    COMMENT ON ROLE {ROLE} IS {literal(ROLE_MARKER)};
  END IF;
  IF EXISTS (SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname={literal(ROLE)}))
     OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname={literal(ROLE)} AND
         (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls OR rolinherit OR NOT rolcanlogin))
     OR EXISTS (SELECT 1 FROM pg_class WHERE relowner=(SELECT oid FROM pg_roles WHERE rolname={literal(ROLE)}))
     OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner=(SELECT oid FROM pg_roles WHERE rolname={literal(ROLE)}))
  THEN RAISE EXCEPTION 'role is not isolated'; END IF;
END $mcp$;
ALTER ROLE {ROLE} PASSWORD {literal(scram_verifier(password))};
ALTER ROLE {ROLE} SET default_transaction_read_only = on;
ALTER ROLE {ROLE} SET statement_timeout = '20s';
ALTER ROLE {ROLE} SET idle_in_transaction_session_timeout = '30s';
GRANT CONNECT ON DATABASE {DATABASE} TO {ROLE};
GRANT USAGE ON SCHEMA core, governance TO {ROLE};
GRANT SELECT ON {tables} TO {ROLE};
DO $mcp$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%'
      AND c.relkind IN ('r','v','m','p','f') AND (
        has_table_privilege({literal(ROLE)},c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
        OR (has_table_privilege({literal(ROLE)},c.oid,'SELECT') AND
          n.nspname||'.'||c.relname NOT IN ({','.join(literal(table) for table in TABLES)})))
  ) OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspname NOT LIKE 'pg_%'
    AND nspname <> 'information_schema' AND has_schema_privilege({literal(ROLE)},oid,'CREATE'))
  THEN RAISE EXCEPTION 'role privileges exceed read-only filter scope'; END IF;
END $mcp$;
COMMIT;
"""


def create_client(*, replace: bool = False) -> None:
    if not replace and (RUNTIME / "server/clients.json").exists():
        registry = json.loads(read_private("server/clients.json"))
        read_private("client.env")
        if not isinstance(registry.get("clients"), list):
            raise DevError("Existing client registry is invalid.")
        return
    token = secrets.token_urlsafe(48)
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    entry = {"client_id": CLIENT_ID, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
             "expires_at": expires, "tools": list(TOOLS)}
    write_private("client.env", f"NEXPOLY_MCP_URL={BASE_URL}/mcp\nNEXPOLY_MCP_TOKEN={token}\n")
    write_private("server/clients.json", json.dumps({"clients": [entry]}, indent=2) + "\n")


def database_password() -> str:
    password = read_private("db-password").strip()
    if len(password) < 32 or not all(char.isascii() and (char.isalnum() or char in "-_") for char in password):
        raise DevError("Development database password format is invalid.")
    return password


def expected_database_dsn(password: str) -> str:
    return f"postgresql://{ROLE}:{password}@{DEV_CONTAINER}:5432/{DATABASE}"


def verify_local_database_dsn(password: str | None = None) -> None:
    expected = expected_database_dsn(database_password() if password is None else password)
    actual = read_private("server/dsn").strip()
    if not hmac.compare_digest(actual.encode("utf-8"), expected.encode("utf-8")):
        raise DevError("Local database credentials do not match the fixed MCP development target; refusing to continue.")


def setup() -> dict:
    verify_project_ownership()
    verify_dev_target()
    with operation_lock():
        password_path = RUNTIME / "db-password"
        has_local_password = password_path.exists()
        role_exists = psql(f"SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname={literal(ROLE)});\n").strip() == "t"
        if role_exists and not has_local_password:
            raise DevError("The managed development role already exists but this checkout has no database credential; refusing to reset it.")
        if role_exists and not (RUNTIME / "server/dsn").exists():
            raise DevError("The managed development role exists without a completed local setup; refusing to reset it.")
        if not password_path.exists():
            write_private("db-password", secrets.token_urlsafe(48) + "\n")
        password = database_password()
        if role_exists:
            verify_local_database_dsn(password)
        psql(provision_sql(password, allow_existing=role_exists))
        write_private("server/dsn", expected_database_dsn(password) + "\n")
        create_client()
    return {"action": "setup", "database": DATABASE, "role": ROLE, "client_id": CLIENT_ID,
            "runtime": str(RUNTIME), "production_changed": False}


def compose(*arguments: str) -> str:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("COMPOSE_", "NEXPOLY_MCP_"))}
    environment.update(NEXPOLY_MCP_DEV_RUNTIME=str(RUNTIME / "server"),
                       NEXPOLY_MCP_DEV_UID=str(os.getuid()), NEXPOLY_MCP_DEV_GID=str(os.getgid()))
    diagnostic = ("docker-" + arguments[0] + ".log") if arguments and arguments[0] in {"up", "build"} else None
    return run(["docker", "compose", "--env-file", "/dev/null", "--project-directory", str(ROOT),
                "-p", PROJECT, "-f", str(ROOT / "docker-compose.mcp-dev.yml"), *arguments],
               env=environment, diagnostics=diagnostic)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # A loopback endpoint must never redirect its bearer credential elsewhere.
        return None


def smoke() -> dict:
    values = dict(line.split("=", 1) for line in read_private("client.env").splitlines() if line)
    if values.get("NEXPOLY_MCP_URL") != BASE_URL + "/mcp":
        raise DevError("Smoke checks are restricted to the development loopback endpoint.")
    token = values["NEXPOLY_MCP_TOKEN"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def rpc(method: str, params: dict, request_id: int):
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode()
        request = urllib.request.Request(BASE_URL + "/mcp", data=body, headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"})
        with opener.open(request, timeout=30) as response:
            payload = json.loads(response.read())
        if "error" in payload:
            raise DevError("Development MCP returned a protocol error.")
        return payload["result"]
    initialized = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "nexpoly-dev-smoke", "version": "1"}}, 1)
    listed = rpc("tools/list", {}, 2)
    if sorted(tool["name"] for tool in listed["tools"]) != sorted(TOOLS):
        raise DevError("Development MCP tool inventory differs from the three approved tools.")
    result = rpc("tools/call", {"name": "list_filter_properties", "arguments": {}}, 3)
    if result.get("isError"):
        raise DevError("Development property catalog call failed.")
    return {"action": "smoke", "protocol": initialized["protocolVersion"], "tools": list(TOOLS),
            "catalog_call": "ok"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "up", "down", "status", "smoke", "revoke", "rotate-token"))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == "setup":
            result = setup()
        elif args.action == "up":
            verify_project_ownership()
            verify_dev_target()
            verify_local_database_dsn()
            read_private("server/clients.json")
            compose("up", "-d", "--build", "--wait", "--wait-timeout", "90", SERVICE)
            result = {"action": "up", "url": BASE_URL + "/mcp", "project": PROJECT}
        elif args.action == "down":
            verify_project_ownership()
            compose("down")
            result = {"action": "down", "project": PROJECT, "credentials_retained": True}
        elif args.action == "status":
            result = {"action": "status", "project": PROJECT, "services": compose("ps", "--format", "json").strip()}
        elif args.action == "smoke":
            result = smoke()
        else:
            verify_project_ownership()
            with operation_lock():
                if args.action == "revoke":
                    write_private("server/clients.json", json.dumps({"clients": []}) + "\n")
                else:
                    create_client(replace=True)
            result = {"action": args.action, "client_id": CLIENT_ID, "effective": "next request"}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (DevError, OSError, ValueError, KeyError, urllib.error.URLError) as error:
        print(json.dumps({"error": "property_filter_mcp_dev_failed", "action": args.action,
                          "message": str(error) if isinstance(error, DevError) else
                          "Development operation failed; credentials and raw diagnostics are suppressed."}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
