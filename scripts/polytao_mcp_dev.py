#!/usr/bin/env python3
"""Operate only the independent PolyTAO MCP development project.

Passwords are prompted interactively and never stored. Normal smoke checks do
not submit generation; use verify_polytao_mcp.py for an explicit generation run.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import getpass
import hashlib
import hmac
from http.cookies import SimpleCookie
import json
import os
import re
from pathlib import Path
import secrets
import socket
import sqlite3
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "polytao-mcp"
DEV_ROOT = Path("/data/lzq/gith/nexpoly-dev")
DEV_CONTAINER = "nexpoly_dev-backend-1"
DEV_NETWORK = "nexpoly_dev_default"
CLIENT_NETWORK = "nexpoly_polytao_mcp_clients_dev"
BACKEND_URL = "http://nexpoly_dev-backend-1:8000"
BACKEND_LOOPBACK = "http://127.0.0.1:18000"
PROJECT = "nexpoly_polytao_mcp_dev"
SERVICE = "polytao-mcp"
IMAGE = "nexpoly-polytao-mcp:dev"
CLIENT_ID = "openscience-polytao-dev"
UI_CLIENT_ID = "openscience-polytao-dev-ui"
BASE_URL = "http://127.0.0.1:18111"
TOOLS = ("get_polytao_capabilities", "calculate_polytao_descriptors",
         "prepare_polytao_generation", "get_polytao_draft", "get_polytao_job")
UI_TOOLS = ("get_polytao_draft", "update_polytao_draft", "confirm_polytao_generation", "cancel_polytao_draft")
ALL_TOOLS = tuple(dict.fromkeys((*TOOLS, *UI_TOOLS)))


class DevError(RuntimeError):
    pass


def run(command: list[str], *, payload: str | None = None, env=None,
        diagnostics: str | None = None) -> str:
    if diagnostics is not None:
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
        raise DevError("Development command could not complete; private diagnostics suppressed.") from None
    if result.returncode:
        raise DevError("Development command failed; private diagnostics suppressed.")
    return result.stdout


def docker_field(field: str):
    return json.loads(run(["docker", "inspect", "--format", "{{json " + field + "}}", DEV_CONTAINER]))


def verify_dev_target() -> None:
    labels = docker_field(".Config.Labels") or {}
    expected = {"com.docker.compose.project": "nexpoly_dev", "com.docker.compose.service": "backend",
                "com.docker.compose.project.working_dir": str(DEV_ROOT)}
    if any(labels.get(key) != value for key, value in expected.items()):
        raise DevError("Backend does not match the fixed development project and checkout.")
    configs = labels.get("com.docker.compose.project.config_files", "").split(",")
    required = {str(DEV_ROOT / name) for name in ("docker-compose.yml", "docker-compose.dev.yml",
                                                "docker-compose.user-isolation-dev.yml")}
    if not required.issubset(configs) or any("prod" in Path(name).name for name in configs):
        raise DevError("Backend Compose provenance is not the approved development configuration.")
    if not docker_field(".State.Running") or DEV_NETWORK not in docker_field(".NetworkSettings.Networks"):
        raise DevError("The fixed development backend is not running on its development network.")
    environment = dict(item.split("=", 1) for item in docker_field(".Config.Env") if "=" in item)
    if (environment.get("GPU_BROKER_ENVIRONMENT") != "dev"
            or environment.get("AUTH_COOKIE_SECURE", "").lower() != "false"
            or not environment.get("AUTH_COOKIE_NAME", "").startswith("nexpoly_dev_")
            or environment.get("AUTH_ISOLATED_AB_SESSION_POLICY")
            or environment.get("AUTH_ISOLATED_ACCEPTANCE_POLICY")):
        raise DevError("Backend is not the ordinary development HTTP authentication target.")
    image = docker_field(".Config.Image")
    if not isinstance(image, str) or not image.startswith("nexpoly-dev-backend:"):
        raise DevError("Backend image is not the development backend image.")
    ports = docker_field(".NetworkSettings.Ports") or {}
    if not any(item.get("HostIp") == "127.0.0.1" and item.get("HostPort") == "18000"
               for item in ports.get("8000/tcp", [])):
        raise DevError("Backend login requires the fixed loopback-only 18000 publication.")


def verify_project_ownership() -> None:
    containers = run(["docker", "ps", "--all", "--filter", "label=com.docker.compose.project=" + PROJECT,
                      "--format", "{{.ID}}"])
    for container in containers.splitlines():
        labels = json.loads(run(["docker", "inspect", "--format", "{{json .Config.Labels}}", container])) or {}
        if (labels.get("com.docker.compose.project") != PROJECT
                or labels.get("com.docker.compose.service") != SERVICE
                or labels.get("com.docker.compose.project.working_dir") != str(ROOT)):
            raise DevError("Existing PolyTAO MCP project belongs to another checkout; refusing to change it.")
        configs = labels.get("com.docker.compose.project.config_files", "").split(",")
        if configs != [str(ROOT / "docker-compose.polytao-mcp-dev.yml")]:
            raise DevError("Existing PolyTAO MCP Compose provenance is not the fixed independent configuration.")
        image = run(["docker", "inspect", "--format", "{{.Config.Image}}", container]).strip()
        if image != IMAGE:
            raise DevError("Existing PolyTAO MCP project uses an unexpected image.")
        mounts = json.loads(run(["docker", "inspect", "--format", "{{json .Mounts}}", container]))
        expected = {"/run/polytao-mcp": (str(RUNTIME / "server"), False),
                    "/var/lib/polytao-mcp": (str(RUNTIME / "journal"), True)}
        allowed_tmpfs = [mount for mount in mounts
                         if mount.get("Destination") == "/tmp" and mount.get("Type") == "tmpfs"]
        if len(allowed_tmpfs) > 1 or len(mounts) != len(expected) + len(allowed_tmpfs):
            raise DevError("Existing PolyTAO MCP mount inventory exceeds the independent service configuration.")
        for destination, (source, writable) in expected.items():
            matches = [mount for mount in mounts if mount.get("Destination") == destination]
            if (len(matches) != 1 or matches[0].get("Type") != "bind"
                    or matches[0].get("Source") != source or matches[0].get("RW") is not writable):
                raise DevError("Existing PolyTAO MCP mount belongs to another runtime; refusing to change it.")


def private_directory() -> None:
    for directory in (ROOT / ".runtime", RUNTIME, RUNTIME / "server", RUNTIME / "journal"):
        if directory.is_symlink():
            raise DevError("Runtime directories must not be symbolic links.")
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise DevError("Runtime directories must belong to the invoking user.")
        if directory != ROOT / ".runtime":
            directory.chmod(0o700)


def read_private(name: str, *, max_bytes: int = 65536) -> str:
    path = RUNTIME / name
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise DevError("Private files must be ordinary owner-only mode 0600 files.")
        payload = handle.read(max_bytes + 1)
        if len(payload.encode()) > max_bytes:
            raise DevError("Private runtime file is too large.")
        return payload


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
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def operation_lock():
    private_directory()
    descriptor = os.open(RUNTIME / "operation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600):
            raise DevError("Operation lock must be a private ordinary file.")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def canonical_uuid(value: str) -> str:
    try:
        identifier = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise DevError("A canonical ordinary development account UUID is required.") from None
    if str(identifier) != value or identifier.int in {0, 1}:
        raise DevError("A canonical ordinary development account UUID is required.")
    return value


def identity() -> dict:
    value = json.loads(read_private("server/identity.json"))
    if (not isinstance(value, dict) or set(value) != {"account_uuid", "backend_url"}
            or value.get("backend_url") != BACKEND_URL):
        raise DevError("Development identity does not match the fixed backend target.")
    canonical_uuid(value["account_uuid"])
    return value


def create_client(*, replace: bool = False) -> None:
    if not replace and (RUNTIME / "server/clients.json").exists():
        registry = json.loads(read_private("server/clients.json"))
        clients = registry.get("clients")
        if not isinstance(clients, list):
            raise DevError("Existing client registry is invalid.")
        # A revoked registry remains revoked. Setup never re-enables it.
        if not clients:
            read_private("client.env")
            return
        by_id = {item.get("client_id"): item for item in clients if isinstance(item, dict)}
        if (len(by_id) != len(clients) or CLIENT_ID not in by_id
                or not set(by_id) <= {CLIENT_ID, UI_CLIENT_ID}):
            raise DevError("Existing client registry has unexpected identities.")
        model = by_id[CLIENT_ID]
        token = dict(line.split("=", 1) for line in read_private("client.env").splitlines()).get("NEXPOLY_MCP_TOKEN", "")
        if (not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token)
                or model.get("token_sha256") != hashlib.sha256(token.encode()).hexdigest()
                or model.get("owner_client_id") is not None):
            raise DevError("Existing model credential does not match its fixed registry identity.")
        legacy = {"get_polytao_capabilities", "calculate_polytao_descriptors", "submit_polytao_generation", "get_polytao_job"}
        if set(model.get("tools", [])) == legacy:
            model["tools"] = list(TOOLS)
        elif not set(model.get("tools", [])) <= set(TOOLS):
            raise DevError("Existing model credential has unsupported permissions.")
        if UI_CLIENT_ID not in by_id:
            ui_token = secrets.token_urlsafe(48)
            write_private("ui-client.env", f"NEXPOLY_MCP_URL={BASE_URL}/mcp\nNEXPOLY_MCP_TOKEN={ui_token}\n")
            clients.append({"client_id": UI_CLIENT_ID, "owner_client_id": CLIENT_ID,
                            "token_sha256": hashlib.sha256(ui_token.encode()).hexdigest(),
                            "expires_at": model["expires_at"], "tools": list(UI_TOOLS)})
        else:
            ui = by_id[UI_CLIENT_ID]
            ui_token = dict(line.split("=", 1) for line in read_private("ui-client.env").splitlines()).get("NEXPOLY_MCP_TOKEN", "")
            if (not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", ui_token)
                    or ui.get("token_sha256") != hashlib.sha256(ui_token.encode()).hexdigest()
                    or ui.get("owner_client_id") != CLIENT_ID or not set(ui.get("tools", [])) <= set(UI_TOOLS)):
                raise DevError("Existing UI credential does not match its fixed owner or permissions.")
        updated = json.dumps(registry, indent=2) + "\n"
        if updated != read_private("server/clients.json"):
            write_private("server/clients.json", updated)
        return
    token = secrets.token_urlsafe(48)
    ui_token = secrets.token_urlsafe(48)
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    entry = {"client_id": CLIENT_ID, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
             "expires_at": expires, "tools": list(TOOLS)}
    write_private("client.env", f"NEXPOLY_MCP_URL={BASE_URL}/mcp\nNEXPOLY_MCP_TOKEN={token}\n")
    ui_entry = {"client_id": UI_CLIENT_ID, "owner_client_id": CLIENT_ID,
                "token_sha256": hashlib.sha256(ui_token.encode()).hexdigest(),
                "expires_at": expires, "tools": list(UI_TOOLS)}
    write_private("ui-client.env", f"NEXPOLY_MCP_URL={BASE_URL}/mcp\nNEXPOLY_MCP_TOKEN={ui_token}\n")
    write_private("server/clients.json", json.dumps({"clients": [entry, ui_entry]}, indent=2) + "\n")


def initialize_journal(*, first_setup: bool) -> None:
    # This module intentionally uses only the standard library; setup does not
    # need the MCP runtime installed and never starts a model or container.
    sys.path.insert(0, str(ROOT / "backend"))
    from app.polytao_mcp_journal import JournalError, SubmissionJournal
    path = RUNTIME / "journal" / "submissions.sqlite3"
    try:
        if first_setup:
            SubmissionJournal.initialize(path)
        else:
            expected = read_private("journal/submissions.sqlite3.identity").strip()
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600):
                    raise DevError("Submission journal must be an owner-only ordinary file.")
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
                actual = connection.execute("SELECT identity FROM metadata").fetchall()
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok" or actual != [(expected,)]:
                    raise DevError("Submission journal validation failed; preserve it for recovery.")
    except (OSError, sqlite3.Error, JournalError):
        raise DevError("Submission journal unavailable; preserve its files and identity for operator recovery.") from None


def setup(account_uuid: str | None) -> dict:
    with operation_lock():
        verify_project_ownership()
        verify_dev_target()
        first_setup = not (RUNTIME / "server/identity.json").exists()
        if not first_setup:
            current = identity()
            if account_uuid is not None and account_uuid != current["account_uuid"]:
                raise DevError("Account identity is fixed for this runtime; refusing to change it.")
        else:
            if account_uuid is None:
                raise DevError("First setup requires --account-uuid for a dedicated existing member account.")
            write_private("server/identity.json", json.dumps({"account_uuid": canonical_uuid(account_uuid),
                                                            "backend_url": BACKEND_URL}) + "\n")
        initialize_journal(first_setup=first_setup)
        create_client()
    return {"action": "setup", "project": PROJECT, "client_id": CLIENT_ID, "runtime": str(RUNTIME),
            "login_required": not (RUNTIME / "server/session.json").exists()}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def read_json_response(response, *, max_bytes: int = 262144):
    payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise DevError("Development response exceeded its configured limit.")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise DevError("Development response is not a JSON object.")
    return value


def validate_account(value: dict, account_uuid: str) -> None:
    user = value.get("user") or {}
    if (value.get("authenticated") is not True or user.get("id") != account_uuid
            or user.get("must_change_password") is not False or value.get("account_type") != "member"
            or (value.get("capabilities") or {}).get("polytao.use") is not True):
        raise DevError("Login must be the fixed member account with PolyTAO access and no password-change requirement.")
    if not isinstance(value.get("csrf_token"), str) or not value["csrf_token"]:
        raise DevError("Backend did not return a CSRF token.")
    canonical_uuid(value.get("session_id"))


def login(username: str | None = None) -> dict:
    with operation_lock():
        verify_project_ownership()
        verify_dev_target()
        target = identity()
        username = username or input("Dedicated development username: ").strip()
        password = getpass.getpass("Dedicated development password: ")
        headers = {"Content-Type": "application/json", "Host": "nexpoly_dev-backend-1:8000", "Origin": BACKEND_URL}
        request = urllib.request.Request(BACKEND_LOOPBACK + "/api/v1/auth/login",
            data=json.dumps({"username": username, "password": password}).encode(), headers=headers)
        password = ""
        with opener().open(request, timeout=15) as response:
            account = read_json_response(response)
            cookies = response.headers.get_all("Set-Cookie", [])
        validate_account(account, target["account_uuid"])
        parsed = SimpleCookie()
        for cookie in cookies:
            parsed.load(cookie)
        matches = [cookie for name, cookie in parsed.items() if name.startswith("nexpoly_dev_")]
        if len(matches) != 1 or not matches[0].value or matches[0]["secure"]:
            raise DevError("Backend did not return one ordinary development HTTP session cookie.")
        cookie = matches[0]
        try:
            max_age = int(cookie["max-age"])
        except (TypeError, ValueError):
            raise DevError("Backend session cookie expiry is missing.") from None
        if not 0 < max_age <= 12 * 3600:
            raise DevError("Development session expiry exceeds the fixed 12-hour policy.")
        cookie_header = cookie.key + "=" + cookie.value
        session_headers = {"Host": "nexpoly_dev-backend-1:8000", "Cookie": cookie_header,
                           "X-Session-Context": account["session_id"]}
        request = urllib.request.Request(BACKEND_LOOPBACK + "/api/v1/auth/session", headers=session_headers)
        with opener().open(request, timeout=15) as response:
            confirmed = read_json_response(response)
        validate_account(confirmed, target["account_uuid"])
        if (confirmed["session_id"] != account["session_id"]
                or not hmac.compare_digest(confirmed["csrf_token"], account["csrf_token"])):
            raise DevError("Session identity changed during login verification.")
        session = {**target, "cookie": cookie_header, "session_id": confirmed["session_id"],
                   "csrf_token": confirmed["csrf_token"],
                   "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=max_age - 5)).isoformat()}
        write_private("server/session.json", json.dumps(session) + "\n")
    return {"action": "login", "account_uuid": target["account_uuid"], "expires_at": session["expires_at"],
            "password_stored": False}


def verify_session_file() -> None:
    value = json.loads(read_private("server/session.json"))
    target = identity()
    if any(value.get(key) != expected for key, expected in target.items()):
        raise DevError("Session does not belong to the fixed account and backend target.")
    expiry = datetime.fromisoformat(value["expires_at"])
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise DevError("Development session expired; run renew-session for the same account.")


def compose(*arguments: str) -> str:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("COMPOSE_", "NEXPOLY_MCP_", "NEXPOLY_POLYTAO_MCP_"))}
    environment.update(NEXPOLY_POLYTAO_MCP_DEV_RUNTIME=str(RUNTIME / "server"),
                       NEXPOLY_POLYTAO_MCP_DEV_JOURNAL=str(RUNTIME / "journal"),
                       NEXPOLY_POLYTAO_MCP_DEV_UID=str(os.getuid()),
                       NEXPOLY_POLYTAO_MCP_DEV_GID=str(os.getgid()),
                       NEXPOLY_POLYTAO_MCP_DEV_ACCOUNT_UUID=identity()["account_uuid"])
    diagnostic = ("docker-" + arguments[0] + ".log") if arguments and arguments[0] in {"up", "build"} else None
    return run(["docker", "compose", "--env-file", "/dev/null", "--project-directory", str(ROOT), "-p", PROJECT,
                "-f", str(ROOT / "docker-compose.polytao-mcp-dev.yml"), *arguments], env=environment,
               diagnostics=diagnostic)


def verify_port_available() -> None:
    # Existing owned services are handled by Compose; another listener must not
    # be mistaken for this development service.
    existing = run(["docker", "ps", "--filter", "label=com.docker.compose.project=" + PROJECT,
                    "--format", "{{.ID}}"])
    if existing.strip():
        return
    with socket.socket() as listener:
        try:
            # Permit rebinding after down without treating HTTP TIME_WAIT as a live listener.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 18111))
        except OSError:
            raise DevError("Development loopback port 18111 is already in use.") from None


def rpc_client(*, ui: bool = False):
    values = dict(line.split("=", 1) for line in read_private("ui-client.env" if ui else "client.env").splitlines() if line)
    if values.get("NEXPOLY_MCP_URL") != BASE_URL + "/mcp":
        raise DevError("Checks are restricted to the development loopback MCP endpoint.")
    token = values.get("NEXPOLY_MCP_TOKEN", "")
    if not 32 <= len(token) <= 256:
        raise DevError("MCP client token is missing or invalid.")
    connection = opener()
    def rpc(method: str, params: dict, request_id: int):
        request = urllib.request.Request(BASE_URL + "/mcp", data=json.dumps({"jsonrpc": "2.0", "id": request_id,
            "method": method, "params": params}).encode(), headers={"Authorization": "Bearer " + token,
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25"})
        with connection.open(request, timeout=30) as response:
            payload = read_json_response(response)
        if "error" in payload:
            raise DevError("Development MCP returned a protocol error.")
        return payload["result"]
    return rpc


def smoke() -> dict:
    rpc = rpc_client()
    initialized = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "nexpoly-polytao-readonly-smoke", "version": "1"}}, 1)
    listed = rpc("tools/list", {}, 2)
    if sorted(tool["name"] for tool in listed["tools"]) != sorted(TOOLS):
        raise DevError("PolyTAO MCP model inventory differs from the approved read and prepare tools.")
    ui_rpc = rpc_client(ui=True)
    ui_rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "nexpoly-polytao-confirmation-readonly-smoke", "version": "1"}}, 4)
    ui_listed = ui_rpc("tools/list", {}, 5)
    if sorted(tool["name"] for tool in ui_listed["tools"]) != sorted(UI_TOOLS):
        raise DevError("PolyTAO MCP UI inventory differs from the approved draft actions.")
    result = rpc("tools/call", {"name": "get_polytao_capabilities", "arguments": {}}, 3)
    if result.get("isError") or result.get("structuredContent", {}).get("ok") is not True:
        raise DevError("PolyTAO capability call failed; check the same-account development session.")
    return {"action": "smoke", "protocol": initialized["protocolVersion"], "model_tools": list(TOOLS),
            "ui_tools": list(UI_TOOLS),
            "capabilities_call": "ok", "generation_submitted": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "login", "renew-session", "up", "down", "status", "smoke",
                                           "revoke", "rotate-token"))
    parser.add_argument("--account-uuid", help="Dedicated existing member UUID; fixed on initial setup")
    parser.add_argument("--username", help="Dedicated account username; password is always prompted")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == "setup":
            result = setup(args.account_uuid)
        elif args.action in {"login", "renew-session"}:
            result = login(args.username)
            result["action"] = args.action
        elif args.action == "up":
            with operation_lock():
                verify_project_ownership()
                verify_dev_target()
                verify_session_file()
                read_private("server/clients.json")
                verify_port_available()
                compose("up", "-d", "--build", "--wait", "--wait-timeout", "90", SERVICE)
            result = {"action": "up", "url": BASE_URL + "/mcp", "project": PROJECT}
        elif args.action in {"down", "status"}:
            with operation_lock():
                verify_project_ownership()
                output = compose("down") if args.action == "down" else compose("ps", "--format", "json")
            result = {"action": args.action, "project": PROJECT, "credentials_and_journal_retained": True}
            if args.action == "status":
                result["services"] = output.strip()
        elif args.action == "smoke":
            result = smoke()
        else:
            with operation_lock():
                verify_project_ownership()
                if args.action == "revoke":
                    write_private("server/clients.json", '{"clients":[]}\n')
                else:
                    identity()
                    create_client(replace=True)
            result = {"action": args.action, "client_id": CLIENT_ID, "effective": "next request"}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (DevError, OSError, ValueError, KeyError, ImportError, urllib.error.URLError) as error:
        print(json.dumps({"error": "polytao_mcp_dev_failed", "action": args.action,
                          "message": str(error) if isinstance(error, DevError) else
                          "Development operation failed; credentials and raw diagnostics are suppressed."}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
