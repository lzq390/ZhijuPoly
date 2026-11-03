#!/usr/bin/env python3
"""Publish only the development 9001 frontend's sealed MCP proxy overlay.

The current immutable UI is the baseline. No database, backend, OpenScience or
production container is started, stopped or recreated by this controller.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    from scripts import property_filter_mcp_dev as mcp_dev
except ModuleNotFoundError:
    import property_filter_mcp_dev as mcp_dev

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".runtime/property-filter-mcp/gateway"
TRUSTED_ROOT = Path("/home/devuser/.local/share/nexpoly/main-dev/ui-releases")
LIVE = "nexpoly_dev-frontend-dev-1"
PROJECT = "nexpoly_dev"
SERVICE = "frontend-dev"
NETWORK = "nexpoly_dev_default"
TARGET = "http://nexpoly_mcp_dev-property-filter-mcp-1:18110"
ALLOWED_HOSTS = "127.0.0.1:9001,localhost:9001,114.214.255.154:9001"
CANDIDATE_PORT = 29101
BASELINE_VITE_SHA256 = "f9ff53f32fd30833d362200241074ffa108494d1b8768b9b758510d44326ae77"
PATCH_FILES = ("frontend/vite.config.ts", "frontend/build/development-mcp-proxy.ts")
SOURCE_MOUNTS = {"/app/frontend": "frontend", "/app/contracts": "contracts",
                 "/app/backend/tests/fixtures": "backend/tests/fixtures", "/app/polymer_md": "polymer_md"}
OWNER_LABEL = "com.nexpoly.mcp-gateway.owner"
OP_LABEL = "com.nexpoly.mcp-gateway.operation"
OVERLAY = "com.nexpoly.source-overlay."
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class GatewayError(RuntimeError):
    pass


def require(condition, message: str) -> None:
    if not condition:
        raise GatewayError(message)


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def run(command: list[str], *, timeout: int = 180, check: bool = True) -> str:
    # Never inherit Compose selectors or print Docker output/private configuration.
    env = {key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")}
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=env,
                                cwd=ROOT, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise GatewayError("Gateway container operation did not complete; raw diagnostics suppressed.") from None
    if check and result.returncode:
        raise GatewayError("Gateway container operation failed; raw diagnostics suppressed.")
    return result.stdout if result.returncode == 0 else ""


def inspect_container(name: str) -> dict:
    values = json.loads(run(["docker", "inspect", name]))
    require(len(values) == 1, "Container inspection did not identify one container.")
    return values[0]


def private_directory(path: Path) -> None:
    missing = []
    cursor = path
    while not cursor.exists():
        require(not cursor.is_symlink(), "Private directory must not be a symlink.")
        missing.append(cursor)
        cursor = cursor.parent
    require(cursor.resolve() == cursor and cursor.is_dir(), "Private directory ancestry is unsafe.")
    for item in reversed(missing):
        item.mkdir(mode=0o700)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and path.resolve() == path, "Private directory ownership differs.")
    path.chmod(0o700)


def read_private(path: Path) -> bytes:
    require(path.resolve() == path, "Private file must not contain symbolic links.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600, "Private file must be owned mode 0600.")
        payload = stream.read(8 * 1024 * 1024 + 1)
    require(len(payload) <= 8 * 1024 * 1024, "Private configuration exceeds the bounded file size.")
    return payload


def write_private(path: Path, payload: bytes) -> None:
    private_directory(path.parent)
    if path.exists() or path.is_symlink():
        read_private(path)
    temporary = path.parent / ("." + path.name + "." + secrets.token_hex(8))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def inventory(source: Path, *, sealed: bool = True) -> dict[str, str]:
    require(source.is_dir() and source.resolve() == source, "Source root must be a real directory.")
    result = {}
    for path in [source, *source.rglob("*")]:
        info = path.lstat()
        require(not stat.S_ISLNK(info.st_mode) and (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)),
                "Source contains a symlink or special file.")
        require(info.st_uid == os.getuid(), "Source ownership differs from the invoking user.")
        require(not sealed or not info.st_mode & 0o222, "Source release is not sealed read-only.")
        if stat.S_ISREG(info.st_mode):
            result[path.relative_to(source).as_posix()] = digest(path.read_bytes())
    return result


def validate_manifest(source: Path, manifest: dict, expected_hash: str) -> dict[str, str]:
    require(isinstance(manifest, dict) and manifest and SHA256.fullmatch(expected_hash), "Source manifest identity is invalid.")
    for name, value in manifest.items():
        require(isinstance(name, str) and isinstance(value, str) and SHA256.fullmatch(value), "Source manifest entry is invalid.")
        path = PurePosixPath(name)
        require(not path.is_absolute() and name == path.as_posix() and ".." not in path.parts
                and "\\" not in name, "Source manifest path is unsafe.")
    require(digest(canonical(manifest)) == expected_hash, "Source manifest identity differs from its label.")
    require(inventory(source) == manifest, "Source tree differs from its complete manifest.")
    return manifest


def verify_live(info: dict | None = None) -> dict:
    info = info or inspect_container(LIVE)
    labels = info.get("Config", {}).get("Labels") or {}
    require(info.get("Name") == "/" + LIVE and info.get("State", {}).get("Running") is True
            and labels.get("com.docker.compose.project") == PROJECT
            and labels.get("com.docker.compose.service") == SERVICE, "Live frontend is not the approved running development container.")
    path = Path(labels.get("com.docker.compose.project.config_files", ""))
    require(path.name == "promote.compose.private.json" and path.parent.parent == TRUSTED_ROOT
            and labels.get("com.docker.compose.project.working_dir") == str(path.parent), "Live frontend Compose provenance is not a trusted UI release.")
    config_bytes = read_private(path)
    document = json.loads(config_bytes)
    require(set(document.get("services", {})) == {SERVICE}, "Promotion configuration must contain only the frontend.")
    service = document["services"][SERVICE]
    require(set(document.get("networks", {})) == {"default"}
            and document["networks"]["default"] == {"external": True, "name": NETWORK}
            and service.get("networks") == ["default"], "Frontend network configuration differs from its development baseline.")
    require(set(info.get("NetworkSettings", {}).get("Networks", {})) == {NETWORK}, "Frontend network attachment differs.")
    ports = service.get("ports", [])
    bindings = info.get("NetworkSettings", {}).get("Ports", {})
    require(len(ports) == 1 and ports[0].get("target") == 5173 and str(ports[0].get("published")) == "9001"
            and ports[0].get("protocol") == "tcp" and ports[0].get("host_ip") == "0.0.0.0"
            and bindings == {"5173/tcp": [{"HostIp": "0.0.0.0", "HostPort": "9001"}]}, "Frontend publication must remain development 9001 to 5173.")
    config = info["Config"]
    require(service.get("image") == config.get("Image") == info.get("Image")
            and re.fullmatch(r"sha256:[0-9a-f]{64}", service.get("image", "")), "Frontend must use its current immutable image ID.")
    require(config.get("Cmd") == service.get("command") and config.get("WorkingDir") == service.get("working_dir") == "/app/frontend", "Frontend command or working directory differs.")
    environment = dict(item.split("=", 1) for item in config.get("Env", []) if "=" in item)
    expected_env = service.get("environment", {})
    require(isinstance(expected_env, dict) and all(isinstance(v, str) and environment.get(k) == v for k, v in expected_env.items()), "Frontend environment differs from its private release.")
    require(not {key for key in environment if key.startswith(("DEV_", "VITE_"))} - set(expected_env), "Frontend contains an unrecorded development setting.")
    require(all(labels.get(k) == v for k, v in service.get("labels", {}).items()), "Frontend release labels differ.")
    source = path.parent / "source"
    manifest = json.loads(read_private(path.parent / "source.sha256.json"))
    source_hash = labels.get(OVERLAY + "sha256", "")
    validate_manifest(source, manifest, source_hash)
    desired = {v["target"]: v for v in service.get("volumes", [])}
    actual = {v["Destination"]: v for v in info.get("Mounts", [])}
    require(set(desired) == set(actual) == set(SOURCE_MOUNTS) | {"/app/frontend/node_modules"}, "Frontend mount inventory differs.")
    for target, relative in SOURCE_MOUNTS.items():
        want, found = desired[target], actual[target]
        expected_path = str(source / relative)
        require(want.get("type") == found.get("Type") == "bind" and want.get("source") == found.get("Source") == expected_path
                and want.get("read_only") is True and found.get("RW") is False
                and want.get("bind", {}).get("create_host_path") is False, "Frontend immutable source mount differs.")
    deps = desired["/app/frontend/node_modules"]
    volume = document.get("volumes", {}).get(deps.get("source"), {})
    require(deps.get("type") == actual["/app/frontend/node_modules"].get("Type") == "volume"
            and volume.get("external") is True
            and volume.get("name") == actual["/app/frontend/node_modules"].get("Name"), "Frontend dependency volume differs.")
    return {"container_id": info["Id"], "promote_path": str(path), "promote_sha256": digest(config_bytes),
            "source_hash": source_hash, "release": str(path.parent), "image": service["image"],
            "dependency_volume": volume["name"], "document": document, "manifest": manifest}


def compose(config: Path, project: str, *arguments: str) -> str:
    return run(["docker", "compose", "--env-file", "/dev/null", "--project-directory", str(config.parent),
                "-p", project, "-f", str(config), *arguments])


def container_ids() -> set[str]:
    return set(run(["docker", "ps", "--all", "--no-trunc", "--format", "{{.ID}}"]).splitlines())


def protected_ids(live_id: str) -> list[str]:
    return sorted(container_ids() - {live_id})


def check_protected(record: dict) -> None:
    require(set(record["protected_ids"]) <= container_ids(), "A protected container identity changed during the gateway operation.")


def check_cas(baseline: dict) -> None:
    current = verify_live()
    require(all(current[key] == baseline[key] for key in ("container_id", "promote_path", "promote_sha256", "source_hash")),
            "Development frontend changed since preparation; refusing promotion.")


def patch_document(baseline: dict, source: Path, source_hash: str, revision: str) -> dict:
    document = copy.deepcopy(baseline["document"])
    service = document["services"][SERVICE]
    service["environment"].update(DEV_MCP_PROXY_TARGET=TARGET, DEV_MCP_PROXY_ALLOWED_HOSTS=ALLOWED_HOSTS)
    service["labels"].update({OVERLAY + "baseline": Path(baseline["release"]).name,
                              OVERLAY + "revision": revision, OVERLAY + "sha256": source_hash})
    for volume in service["volumes"]:
        if volume["target"] in SOURCE_MOUNTS:
            volume["source"] = str(source / SOURCE_MOUNTS[volume["target"]])
    return document


def build_candidate_compose(document: dict, dependency_volume: str, operation: str) -> dict:
    candidate = copy.deepcopy(document)
    service = candidate["services"][SERVICE]
    service["ports"] = [{"host_ip": "127.0.0.1", "published": str(CANDIDATE_PORT), "target": 5173, "protocol": "tcp"}]
    service["environment"]["DEV_MCP_PROXY_ALLOWED_HOSTS"] = ALLOWED_HOSTS + f",127.0.0.1:{CANDIDATE_PORT}"
    service["labels"].update({OWNER_LABEL: str(STATE), OP_LABEL: operation})
    deps = next(v["source"] for v in service["volumes"] if v["target"] == "/app/frontend/node_modules")
    candidate["volumes"][deps] = {"external": True, "name": dependency_volume}
    return candidate


def prepare_source(baseline: dict, operation_dir: Path) -> tuple[Path, dict]:
    require(baseline["manifest"].get(PATCH_FILES[0]) == BASELINE_VITE_SHA256
            and PATCH_FILES[1] not in baseline["manifest"], "Current UI has a different Vite baseline; an explicit rebase is required.")
    revision = run(["git", "rev-parse", "HEAD"]).strip()
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Gateway source revision is invalid.")
    patches = {relative: (ROOT / relative).read_bytes() for relative in PATCH_FILES}
    for relative, payload in patches.items():
        committed = subprocess.run(["git", "show", "HEAD:" + relative], cwd=ROOT, capture_output=True)
        require(committed.returncode == 0 and committed.stdout == payload, "Gateway proxy sources must match the recorded commit.")
    temporary = operation_dir / "source"
    shutil.copytree(Path(baseline["release"]) / "source", temporary)
    require(inventory(temporary) == baseline["manifest"], "Copied UI differs from its baseline.")
    for path in [temporary, *temporary.rglob("*")]:
        if path.is_dir():
            path.chmod(0o755)
    for relative, payload in patches.items():
        path = temporary / relative
        path.unlink(missing_ok=True)
        path.write_bytes(payload)
    manifest = inventory(temporary, sealed=False)
    require({key: value for key, value in manifest.items() if key not in PATCH_FILES}
            == {key: value for key, value in baseline["manifest"].items() if key not in PATCH_FILES}, "Gateway patch changed unrelated UI source.")
    source_hash = digest(canonical(manifest))
    # Keep each attempt independent so a failed candidate or a later rollback
    # does not prevent retrying the same source delta.
    release = TRUSTED_ROOT / ("mcp-gateway-" + source_hash + "-" + operation_dir.name)
    require(not release.exists() and not release.is_symlink(), "Gateway release directory already exists; inspect prior operation state.")
    private_directory(release)
    shutil.copytree(temporary, release / "source")
    shutil.rmtree(temporary)
    for path in [release / "source", *(release / "source").rglob("*")]:
        path.chmod(0o555 if path.is_dir() else 0o444)
    write_private(release / "source.sha256.json", canonical(manifest) + b"\n")
    document = patch_document(baseline, release / "source", source_hash, revision)
    write_private(release / "promote.compose.private.json", canonical(document) + b"\n")
    validate_manifest(release / "source", manifest, source_hash)
    return release, document


def http_request(port: int, path: str, *, payload: dict | None = None, token: str | None = None):
    require(port in {9001, CANDIDATE_PORT} and path.startswith("/") and not path.startswith("//"), "HTTP probe target is not a fixed development entry.")
    # Vite's HTML fallback only serves the existing SPA to HTML-capable GETs.
    headers = {"Accept": "application/json, text/event-stream" if payload is not None else "*/*"}
    if payload is not None:
        headers.update({"Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"})
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=canonical(payload) if payload is not None else None, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), mcp_dev.NoRedirect())
    try:
        response = opener.open(request, timeout=35)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read(512 * 1024 + 1)
        require(len(body) <= 512 * 1024, "Development probe response exceeds its size limit.")
        return response.status, response.headers.get("Content-Type", "").split(";", 1)[0], body


def probe_baseline(port: int) -> dict:
    result = {}
    for path in ("/", "/health", "/api/v1/auth/session"):
        status, content_type, body = http_request(port, path)
        require(status in {200, 401} and (path != "/" or status == 200 and content_type == "text/html"),
                "Development frontend baseline endpoint is unavailable.")
        result[path] = {"status": status, "content_type": content_type}
    return result


def smoke(port: int, baseline: dict) -> dict:
    for attempt in range(60):
        try:
            status, content_type, body = http_request(port, "/mcp", payload={})
            if status == 401 and content_type == "application/json":
                break
        except (OSError, urllib.error.URLError):
            pass
        if attempt == 59:
            raise GatewayError("Gateway did not expose the authenticated MCP route within its startup deadline.")
        time.sleep(0.5)
    require(probe_baseline(port) == baseline, "Gateway changed an existing frontend/API endpoint status or media type.")
    values = dict(line.split("=", 1) for line in mcp_dev.read_private("client.env").splitlines() if line)
    token = values.get("NEXPOLY_MCP_TOKEN", "")
    require(values.get("NEXPOLY_MCP_URL") == mcp_dev.BASE_URL + "/mcp" and 32 <= len(token) <= 256,
            "Gateway smoke requires the existing private development credential.")
    counter = 0

    def rpc(method: str, params: dict):
        nonlocal counter
        counter += 1
        status, content_type, body = http_request(port, "/mcp", token=token,
            payload={"jsonrpc": "2.0", "id": counter, "method": method, "params": params})
        require(status == 200 and content_type == "application/json", "Gateway MCP protocol request did not return JSON success.")
        document = json.loads(body)
        require("error" not in document and isinstance(document.get("result"), dict), "Gateway MCP returned a protocol error.")
        return document["result"]

    def tool(name: str, arguments: dict):
        result = rpc("tools/call", {"name": name, "arguments": arguments})
        output = result.get("structuredContent", {})
        require(not result.get("isError") and output.get("ok") is True, "Gateway MCP tool call did not succeed.")
        return output

    rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "nexpoly-gateway-smoke", "version": "1"}})
    listed = rpc("tools/list", {})
    require(sorted(tool["name"] for tool in listed.get("tools", [])) == sorted(mcp_dev.TOOLS), "Gateway MCP tool inventory differs.")
    catalog = tool("list_filter_properties", {"page_size": 5})
    options = [option for option in catalog.get("options", [])
               if option.get("min_value") is not None and option.get("max_value") is not None]
    require(options, "Gateway smoke needs a numeric catalog option to exercise search and histogram.")
    option = options[0]
    search = tool("search_polymers", {"dataset_revision": catalog["dataset_revision"], "page_size": 1,
                  "filters": [{"option_key": option["option_key"], "min_value": option["min_value"], "max_value": option["max_value"]}]})
    require(search.get("matched_material_count", 0) > 0, "Gateway search did not match its catalog's numeric range.")
    histogram = tool("get_property_histogram", {"dataset_revision": catalog["dataset_revision"], "option_key": option["option_key"]})
    require(histogram.get("unit") == option["unit"]
            and histogram.get("histogram", {}).get("total_count") == option["measurement_count"], "Gateway histogram differs from its catalog option.")
    return {"unauthenticated_mcp": 401, "protocol_tools": list(mcp_dev.TOOLS), "search": "ok", "histogram": "ok"}


def save_record(record: dict) -> None:
    operation = record["operation"]
    require(re.fullmatch(r"[0-9a-f]{16}", operation), "Gateway operation identity is invalid.")
    write_private(STATE / "operations" / operation / "state.json", canonical(record) + b"\n")
    write_private(STATE / "latest.json", canonical({"operation": operation}) + b"\n")


def saved_record() -> dict | None:
    if not (STATE / "latest.json").exists():
        return None
    operation = json.loads(read_private(STATE / "latest.json"))["operation"]
    require(isinstance(operation, str) and re.fullmatch(r"[0-9a-f]{16}", operation), "Saved gateway operation identity is invalid.")
    record = json.loads(read_private(STATE / "operations" / operation / "state.json"))
    require(record.get("operation") == operation and record.get("owner") == str(STATE), "Saved gateway operation belongs to another runtime.")
    require(record.get("candidate_project") == "nexpoly_mcp_gateway_" + operation
            and record.get("candidate_volume") == "nexpoly_mcp_gateway_" + operation + "_deps", "Saved gateway candidate identity is invalid.")
    return record


@contextmanager
def operation_lock():
    private_directory(STATE)
    descriptor = os.open(STATE / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(descriptor)
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid()
                and stat.S_IMODE(metadata.st_mode) == 0o600, "Gateway operation lock ownership differs.")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def owned_candidate_ids(record: dict) -> list[str]:
    return run(["docker", "ps", "--all", "--no-trunc", "--filter", "label=" + OWNER_LABEL + "=" + str(STATE),
                "--filter", "label=" + OP_LABEL + "=" + record["operation"], "--format", "{{.ID}}"]).splitlines()


def cleanup(record: dict) -> None:
    # Inspect every object again before deletion; never use project-wide down,
    # orphan removal, an unverified name, or a live dependency volume.
    ids = owned_candidate_ids(record)
    for container_id in ids:
        info = inspect_container(container_id)
        labels = info.get("Config", {}).get("Labels", {}) or {}
        require(labels.get(OWNER_LABEL) == str(STATE) and labels.get(OP_LABEL) == record["operation"]
                and info.get("Name", "").startswith("/" + record["candidate_project"] + "-")
                and info.get("Id") == container_id and container_id not in record["protected_ids"], "Candidate cleanup found a foreign container.")
        run(["docker", "rm", "--force", container_id])
    raw = run(["docker", "volume", "inspect", record["candidate_volume"]], check=False)
    if raw:
        volume = json.loads(raw)[0]
        labels = volume.get("Labels") or {}
        require(volume.get("Name") == record["candidate_volume"]
                and volume["Name"] != record["baseline"]["dependency_volume"]
                and labels.get(OWNER_LABEL) == str(STATE) and labels.get(OP_LABEL) == record["operation"], "Candidate cleanup found a foreign dependency volume.")
        run(["docker", "volume", "rm", record["candidate_volume"]])


def start_candidate(record: dict, document: dict) -> Path:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", CANDIDATE_PORT))
        except OSError:
            raise GatewayError("Candidate loopback port 29101 is already occupied.") from None
    name = record["candidate_volume"]
    require(name != record["baseline"]["dependency_volume"]
            and not run(["docker", "volume", "inspect", name], check=False), "Candidate dependency volume already exists.")
    run(["docker", "volume", "create", "--label", OWNER_LABEL + "=" + str(STATE),
         "--label", OP_LABEL + "=" + record["operation"], name])
    run(["docker", "run", "--name", record["candidate_project"] + "-deps-copy", "--network", "none",
         "--label", OWNER_LABEL + "=" + str(STATE), "--label", OP_LABEL + "=" + record["operation"],
         "--mount", "type=volume,source=" + record["baseline"]["dependency_volume"] + ",target=/from,readonly",
         "--mount", "type=volume,source=" + name + ",target=/to", record["baseline"]["image"],
         "sh", "-eu", "-c", "cp -a /from/. /to/\nrm -rf /to/.vite /to/.vite-temp"], timeout=300)
    config = STATE / "operations" / record["operation"] / "candidate.compose.private.json"
    write_private(config, canonical(build_candidate_compose(document, name, record["operation"])) + b"\n")
    compose(config, record["candidate_project"], "up", "-d", "--no-deps", "--force-recreate", SERVICE)
    return config


def verify_promoted(record: dict) -> dict:
    current = verify_live()
    require(current["promote_path"] == record["promote_path"]
            and current["promote_sha256"] == record["promote_sha256"]
            and current["source_hash"] == record["source_hash"], "Current frontend is not this gateway promotion.")
    if record.get("promoted_id"):
        require(current["container_id"] == record["promoted_id"], "Promoted frontend was replaced by another operation.")
    return current


def verify_restored(record: dict) -> dict:
    current = verify_live()
    baseline = record["baseline"]
    require(all(current[key] == baseline[key] for key in ("promote_path", "promote_sha256", "source_hash")), "Restored frontend identity differs from its baseline.")
    if record.get("restored_id"):
        require(current["container_id"] == record["restored_id"], "Restored frontend was replaced by another operation.")
    return current


def restore(record: dict) -> None:
    baseline = record["baseline"]
    old_path = Path(baseline["promote_path"])
    backup = STATE / "operations" / record["operation"] / "rollback.compose.private.json"
    require(digest(read_private(old_path)) == digest(read_private(backup)) == baseline["promote_sha256"], "Recorded rollback configuration has changed.")
    validate_manifest(Path(baseline["release"]) / "source", baseline["manifest"], baseline["source_hash"])
    if record.get("restored_id"):
        # Retry only the checks on the exact container this operation restored.
        # A later publication, even of identical baseline source, is not ours.
        current = verify_restored(record)
    else:
        raw = run(["docker", "inspect", LIVE], check=False)
        if raw:
            info = json.loads(raw)[0]
            labels = info.get("Config", {}).get("Labels") or {}
            require(labels.get("com.docker.compose.project") == PROJECT
                    and labels.get("com.docker.compose.service") == SERVICE, "Rollback refuses to replace a foreign frontend.")
            if info.get("Id") == baseline["container_id"]:
                # A failed Compose recreate may leave our old container stopped.
                # Its immutable ID/config still identifies the rollback baseline;
                # requiring Running here would prevent the controller repairing it.
                require(labels.get("com.docker.compose.project.config_files") == baseline["promote_path"]
                        and labels.get(OVERLAY + "sha256") == baseline["source_hash"], "Stopped rollback baseline identity differs.")
            else:
                require(labels.get("com.docker.compose.project.config_files") == record.get("promote_path")
                        and labels.get(OVERLAY + "sha256") == record.get("source_hash")
                        and (not record.get("promoted_id") or info.get("Id") == record["promoted_id"]), "Rollback refuses to replace a foreign frontend.")
        compose(old_path, PROJECT, "up", "-d", "--no-deps", "--force-recreate", SERVICE)
        current = verify_restored(record)
        # Persist ownership before fallible HTTP/protected-container checks.
        # A crash before this save still requires inspection; do not infer that
        # an arbitrary new container using the baseline belongs to this rollback.
        record.update(phase="restoring", restored_id=current["container_id"])
        save_record(record)
    for attempt in range(60):
        try:
            require(probe_baseline(9001) == record["http_baseline"], "Restored frontend endpoint checks differ.")
            break
        except (OSError, urllib.error.URLError, GatewayError):
            if attempt == 59:
                raise GatewayError("Restored frontend did not pass its endpoint checks.") from None
            time.sleep(0.5)
    check_protected(record)
    record["phase"] = "rolled_back"
    save_record(record)


def status() -> dict:
    record = saved_record()
    current = verify_live()
    return {"action": "status", "url": "http://127.0.0.1:9001/mcp", "phase": record["phase"] if record else "not_applied",
            "operation": record["operation"] if record else None, "source_hash": current["source_hash"],
            "gateway_configured": current["document"]["services"][SERVICE]["environment"].get("DEV_MCP_PROXY_TARGET") == TARGET}


def apply() -> dict:
    with operation_lock():
        previous = saved_record()
        if previous and previous["phase"] == "promoted":
            verify_promoted(previous)
            cleanup(previous)
            return {**status(), "action": "apply", "already_applied": True}
        require(not previous or previous["phase"] in {"rolled_back", "failed_before_promotion"}, "An interrupted gateway operation requires rollback or inspection before another apply.")
        if previous:
            cleanup(previous)
        baseline = verify_live()
        mcp_dev.verify_project_ownership()
        mcp = inspect_container("nexpoly_mcp_dev-property-filter-mcp-1")
        mcp_labels = mcp.get("Config", {}).get("Labels") or {}
        require(mcp.get("State", {}).get("Running") is True and NETWORK in mcp.get("NetworkSettings", {}).get("Networks", {})
                and mcp_labels.get("com.docker.compose.project") == mcp_dev.PROJECT
                and mcp_labels.get("com.docker.compose.service") == mcp_dev.SERVICE
                and mcp_labels.get("com.docker.compose.project.working_dir") == str(ROOT),
                "Independent MCP must already be owned by this checkout and running on the existing development network.")
        operation = secrets.token_hex(8)
        record = {"operation": operation, "owner": str(STATE), "phase": "preparing", "baseline": baseline,
                  "candidate_project": "nexpoly_mcp_gateway_" + operation,
                  "candidate_volume": "nexpoly_mcp_gateway_" + operation + "_deps",
                  "protected_ids": protected_ids(baseline["container_id"]), "http_baseline": probe_baseline(9001)}
        directory = STATE / "operations" / operation
        private_directory(directory)
        write_private(directory / "rollback.compose.private.json", read_private(Path(baseline["promote_path"])))
        save_record(record)
        live_mutation_started = False
        try:
            release, document = prepare_source(baseline, directory)
            path = release / "promote.compose.private.json"
            record.update(promote_path=str(path), promote_sha256=digest(read_private(path)),
                          source_hash=document["services"][SERVICE]["labels"][OVERLAY + "sha256"])
            write_private(directory / "promote.compose.private.json", read_private(path))
            save_record(record)
            start_candidate(record, document)
            record["candidate_smoke"] = smoke(CANDIDATE_PORT, record["http_baseline"])
            cleanup(record)
            check_cas(baseline)
            check_protected(record)
            require(digest(read_private(path)) == record["promote_sha256"], "Gateway promotion configuration changed after candidate validation.")
            manifest = json.loads(read_private(release / "source.sha256.json"))
            validate_manifest(release / "source", manifest, record["source_hash"])
            record["phase"] = "promoting"
            save_record(record)
            live_mutation_started = True
            compose(path, PROJECT, "up", "-d", "--no-deps", "--force-recreate", SERVICE)
            current = verify_promoted(record)
            record["promoted_id"] = current["container_id"]
            save_record(record)
            record["live_smoke"] = smoke(9001, record["http_baseline"])
            check_protected(record)
            record["phase"] = "promoted"
            save_record(record)
        except Exception:
            if live_mutation_started:
                try:
                    restore(record)
                except Exception:
                    record["phase"] = "rollback_required"
                    save_record(record)
                    raise GatewayError("Gateway promotion and automatic rollback did not complete; inspect the private saved operation before continuing.") from None
                raise GatewayError("Gateway promotion failed; the original frontend was restored.") from None
            record["phase"] = "failed_before_promotion"
            save_record(record)
            raise GatewayError("Gateway candidate preparation or validation failed; the live frontend was not promoted.") from None
        finally:
            cleanup(record)
        return {**status(), "action": "apply", "candidate_smoke": "ok", "live_smoke": "ok"}


def rollback() -> dict:
    with operation_lock():
        record = saved_record()
        require(record is not None, "No gateway operation is recorded for rollback.")
        current_raw = run(["docker", "inspect", LIVE], check=False)
        live_id = json.loads(current_raw)[0]["Id"] if current_raw else None
        # Other services may have been legitimately updated since apply. Protect
        # the identities present at the start of this rollback transaction.
        record["protected_ids"] = sorted(container_ids() - {live_id} - set(owned_candidate_ids(record)))
        if record["phase"] == "rolled_back":
            verify_restored(record)
            cleanup(record)
            return {**status(), "action": "rollback", "already_rolled_back": True}
        if record["phase"] in {"preparing", "failed_before_promotion"}:
            check_cas(record["baseline"])
            cleanup(record)
            check_protected(record)
            record["phase"] = "rolled_back"
            save_record(record)
        else:
            restore(record)
            cleanup(record)
        return {**status(), "action": "rollback"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "rollback", "status"))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = {"apply": apply, "rollback": rollback, "status": status}[args.action]()
        print(json.dumps(result))
        return 0
    except Exception:
        # Do not interpolate exceptions: Docker, HTTP or JSON errors may embed
        # the private Compose environment or an Authorization-bearing request.
        print(json.dumps({"error": "property_filter_mcp_gateway_failed", "action": args.action,
                          "message": "Gateway operation failed; inspect private operation state. Raw diagnostics and credentials are suppressed."}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
