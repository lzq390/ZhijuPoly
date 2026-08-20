#!/usr/bin/env python3
"""Fail-closed release transaction for the independently deployed OpenScience UI."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEPLOYMENT_DIR = Path("/home/devuser/gsx/OpenScienceCodexUI")
DEFAULT_STATE_ROOT = Path(
    "/data/lzq/gith/nexpoly-runtime/state/openscience-ui-releases"
)
SERVICE = "openscience-ui"
LIVE_CONTAINER = "openscience-ui-poc-openscience-ui-1"
CANARY_PORT = 19011
NETWORK = "openscience-poc"
PLAYWRIGHT_IMAGE = (
    "mcr.microsoft.com/playwright@sha256:"
    "c091b21d9fae78c76e85cd4356431e9b018402f172a214fc7d7a5e9a7e29d8ac"
)
BASE_IMAGE_ID = (
    "sha256:e7d25a1b6d515daec641c8de9c98265f275991eee2396dc578ce9c2fcfdeb197"
)
BASE_MANIFEST = BASE_IMAGE_ID
PARENT_ORIGINS = (
    "http://114.214.255.154:9000,http://114.214.255.154:9001"
)
PATCHED_STATIC_TREE = (
    "sha256:32f45b16e585ef348b4a83a9763412476568ec1781aecb5be69ebd7d7f3c54fd"
)
PARENT_POLICY_SHA256 = (
    "sha256:955ae6f5f3d0710dcaacc0906f6326a4ba99321a0e47fc928c198c8967dd0042"
)
IMAGE = re.compile(r"^ghcr\.io/lzq390/openscience-ui@sha256:[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
OPERATION_ID = re.compile(r"^openscience-[a-z0-9][a-z0-9-]{7,79}$")


class ReleaseError(RuntimeError):
    pass


def validate_operation_id(value: str) -> str:
    if not OPERATION_ID.fullmatch(value):
        raise ReleaseError("operation ID is invalid")
    return value


def canonical_json(document: Any) -> bytes:
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def digest(document: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(document)).hexdigest()


def file_sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return "sha256:" + value.hexdigest()


def require_private_regular(path: Path, *, executable: bool = False) -> os.stat_result:
    if path.is_symlink():
        raise ReleaseError(f"unsafe symlink: {path}")
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise ReleaseError(f"required path is missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ReleaseError(f"required path is not a regular file: {path}")
    if metadata.st_mode & 0o022:
        raise ReleaseError(f"required path is group/world writable: {path}")
    if executable and not metadata.st_mode & stat.S_IXUSR:
        raise ReleaseError(f"required helper is not owner-executable: {path}")
    return metadata


def run(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "command failed"
        raise ReleaseError(f"{arguments[0]} failed: {message[:1000]}")
    return result


def docker_inspect(target: str) -> dict[str, Any]:
    result = run(["docker", "inspect", target])
    document = json.loads(result.stdout)
    if not isinstance(document, list) or len(document) != 1 or not isinstance(document[0], dict):
        raise ReleaseError(f"docker inspect returned an invalid document for {target}")
    return document[0]


def image_labels(image: str) -> tuple[str, dict[str, str]]:
    document = docker_inspect(image)
    image_id = document.get("Id")
    labels = document.get("Config", {}).get("Labels")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise ReleaseError("candidate image ID is invalid")
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
    ):
        raise ReleaseError("candidate image labels are invalid")
    return image_id, labels


def validate_candidate(image: str, sha: str) -> dict[str, str]:
    if not IMAGE.fullmatch(image):
        raise ReleaseError("candidate image must be the immutable OpenScience GHCR digest")
    if not SHA.fullmatch(sha):
        raise ReleaseError("target SHA must contain 40 lowercase hexadecimal characters")
    run(["docker", "pull", image])
    image_id, labels = image_labels(image)
    expected = {
        "org.opencontainers.image.revision": sha,
        "org.opencontainers.image.source": "https://github.com/lzq390/ZhijuPoly",
        "com.nexpoly.openscience.base-image-id": BASE_IMAGE_ID,
        "com.nexpoly.openscience.base-manifest": BASE_MANIFEST,
        "com.nexpoly.openscience.parent-origins": PARENT_ORIGINS,
        "com.nexpoly.openscience.derived-static-tree": PATCHED_STATIC_TREE,
        "com.nexpoly.openscience.parent-policy-sha256": PARENT_POLICY_SHA256,
    }
    for key, value in expected.items():
        if labels.get(key) != value:
            raise ReleaseError(f"candidate image label differs: {key}")
    verifier = REPOSITORY_ROOT / "scripts" / "ci" / "test_openscience_overlay_image.sh"
    require_private_regular(verifier, executable=True)
    run([str(verifier), image, sha], cwd=REPOSITORY_ROOT)
    return {"id": image_id, "revision": sha, "reference": image}


def validate_browser_runtime() -> dict[str, str]:
    overlay_root = REPOSITORY_ROOT / "ops" / "openscience-ui-overlay"
    package_lock = overlay_root / "package-lock.json"
    playwright_package = overlay_root / "node_modules" / "playwright" / "package.json"
    require_private_regular(package_lock)
    require_private_regular(playwright_package)
    package = json.loads(playwright_package.read_text(encoding="utf-8"))
    if package.get("name") != "playwright" or package.get("version") != "1.62.1":
        raise ReleaseError("installed OpenScience Playwright probe package differs")
    run(["docker", "pull", PLAYWRIGHT_IMAGE])
    image = docker_inspect(PLAYWRIGHT_IMAGE)
    image_id = image.get("Id")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise ReleaseError("Playwright probe image identity is invalid")
    return {
        "image": PLAYWRIGHT_IMAGE,
        "image_id": image_id,
        "package_lock_sha256": file_sha256(package_lock),
        "playwright_version": "1.62.1",
    }


def parse_env_image(payload: bytes) -> str:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseError("OpenScience .env is not UTF-8") from exc
    values = []
    for line in text.splitlines():
        if line.startswith("OPENSCIENCE_UI_IMAGE="):
            values.append(line.split("=", 1)[1].strip())
    if len(values) != 1 or not values[0]:
        raise ReleaseError("OpenScience .env must contain one non-empty image assignment")
    return values[0]


def replace_env_image(payload: bytes, image: str) -> bytes:
    if not IMAGE.fullmatch(image):
        raise ReleaseError("replacement image is not an immutable OpenScience digest")
    text = payload.decode("utf-8")
    replacement = f"OPENSCIENCE_UI_IMAGE={image}"
    lines = text.splitlines(keepends=True)
    matched = 0
    output = []
    for line in lines:
        ending = "\n" if line.endswith("\n") else ""
        body = line[:-1] if ending else line
        if body.startswith("OPENSCIENCE_UI_IMAGE="):
            matched += 1
            output.append(replacement + ending)
        else:
            output.append(line)
    if matched != 1:
        raise ReleaseError("OpenScience .env image assignment count differs")
    return "".join(output).encode("utf-8")


def compose_document(deployment_dir: Path) -> dict[str, Any]:
    result = run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(deployment_dir),
            "--env-file",
            str(deployment_dir / ".env"),
            "-f",
            str(deployment_dir / "docker-compose.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=deployment_dir,
    )
    document = json.loads(result.stdout)
    if not isinstance(document, dict):
        raise ReleaseError("OpenScience Compose config is invalid")
    service = document.get("services", {}).get(SERVICE)
    if not isinstance(service, dict):
        raise ReleaseError("OpenScience Compose service is missing")
    ports = service.get("ports")
    if not isinstance(ports, list) or len(ports) != 1:
        raise ReleaseError("OpenScience Compose must publish exactly one port")
    port = ports[0]
    if not isinstance(port, dict) or (
        str(port.get("published")) != "9011"
        or int(port.get("target", 0)) != 4454
        or port.get("host_ip") != "0.0.0.0"
    ):
        raise ReleaseError("OpenScience Compose port identity differs")
    if (
        str(service.get("mem_limit")) != "536870912"
        or service.get("cpus") != 1
        or service.get("pids_limit") != 128
        or service.get("cap_drop") != ["ALL"]
        or service.get("security_opt") != ["no-new-privileges:true"]
        or service.get("restart") != "unless-stopped"
        or service.get("stop_grace_period") != "10s"
        or set(service.get("networks", {})) != {"openscience"}
    ):
        raise ReleaseError("OpenScience Compose resource or isolation policy differs")
    return document


def live_identity() -> dict[str, Any]:
    document = docker_inspect(LIVE_CONTAINER)
    state = document.get("State", {})
    health = state.get("Health", {}).get("Status")
    if state.get("Running") is not True or health != "healthy":
        raise ReleaseError("live OpenScience container is not running and healthy")
    image_id = document.get("Image")
    configured_image = document.get("Config", {}).get("Image")
    if not isinstance(image_id, str) or not isinstance(configured_image, str):
        raise ReleaseError("live OpenScience image identity is invalid")
    host_config = document.get("HostConfig", {})
    networks = document.get("NetworkSettings", {}).get("Networks", {})
    if (
        host_config.get("Memory") != 536_870_912
        or host_config.get("NanoCpus") != 1_000_000_000
        or host_config.get("PidsLimit") != 128
        or host_config.get("CapDrop") != ["ALL"]
        or host_config.get("SecurityOpt") != ["no-new-privileges:true"]
        or host_config.get("RestartPolicy", {}).get("Name") != "unless-stopped"
        or not isinstance(networks, dict)
        or set(networks) != {NETWORK}
    ):
        raise ReleaseError("live OpenScience resource or isolation policy differs")
    return {
        "container_id": document.get("Id"),
        "configured_image": configured_image,
        "image_id": image_id,
        "health": health,
        "resources": {
            "memory": host_config["Memory"],
            "nano_cpus": host_config["NanoCpus"],
            "pids_limit": host_config["PidsLimit"],
            "cap_drop": host_config["CapDrop"],
            "security_opt": host_config["SecurityOpt"],
            "restart": host_config["RestartPolicy"]["Name"],
            "network": NETWORK,
        },
    }


def build_plan(
    *,
    image: str,
    sha: str,
    operation_id: str,
    deployment_dir: Path,
) -> dict[str, Any]:
    validate_operation_id(operation_id)
    deployment_dir = deployment_dir.resolve()
    env_path = deployment_dir / ".env"
    compose_path = deployment_dir / "docker-compose.yml"
    require_private_regular(env_path)
    require_private_regular(compose_path)
    env_payload = env_path.read_bytes()
    current_env_image = parse_env_image(env_payload)
    candidate = validate_candidate(image, sha)
    browser_runtime = validate_browser_runtime()
    compose = compose_document(deployment_dir)
    compose_image = compose["services"][SERVICE].get("image")
    if compose_image != current_env_image:
        raise ReleaseError("rendered Compose image differs from the exact .env assignment")
    live = live_identity()
    if live["configured_image"] != current_env_image:
        raise ReleaseError("live OpenScience configured image differs from Compose")
    if live["image_id"] == candidate["id"]:
        raise ReleaseError("candidate OpenScience image is already live")
    return {
        "schema_version": 1,
        "action": "openscience-ui-release",
        "operation_id": operation_id,
        "target_sha": sha,
        "candidate": candidate,
        "current": live,
        "deployment": {
            "directory": str(deployment_dir),
            "compose_sha256": file_sha256(compose_path),
            "env_sha256": "sha256:" + hashlib.sha256(env_payload).hexdigest(),
            "env_image": current_env_image,
            "service": SERVICE,
            "live_port": 9011,
            "container_port": 4454,
            "canary_port": CANARY_PORT,
            "network": NETWORK,
        },
        "trusted_parent_origins": PARENT_ORIGINS.split(","),
        "browser_probe": browser_runtime,
    }


def atomic_write(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def write_json(path: Path, document: Any) -> None:
    atomic_write(path, canonical_json(document), 0o600)


@contextlib.contextmanager
def release_lock(state_root: Path) -> Iterator[None]:
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_root, 0o700)
    lock_path = state_root / "release.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def update_journal(operation_dir: Path, phase: str, **extra: Any) -> None:
    journal_path = operation_dir / "journal.json"
    current: dict[str, Any] = {}
    if journal_path.exists():
        current = json.loads(journal_path.read_text(encoding="utf-8"))
    current.update(extra)
    current["schema_version"] = 1
    current["phase"] = phase
    current["updated_at_unix"] = int(time.time())
    write_json(journal_path, current)


def compose_up(deployment_dir: Path) -> None:
    run(
        [
            "docker",
            "compose",
            "--project-directory",
            str(deployment_dir),
            "--env-file",
            str(deployment_dir / ".env"),
            "-f",
            str(deployment_dir / "docker-compose.yml"),
            "up",
            "--detach",
            "--no-deps",
            "--force-recreate",
            "--pull",
            "never",
            SERVICE,
        ],
        cwd=deployment_dir,
    )


def wait_http(url: str, *, expected: str | None = None) -> None:
    last_error = "request did not run"
    for _ in range(30):
        result = run(
            ["curl", "--fail", "--silent", "--show-error", "--max-time", "5", url],
            check=False,
        )
        if result.returncode == 0 and (expected is None or result.stdout.strip() == expected):
            return
        last_error = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
        time.sleep(1)
    raise ReleaseError(f"OpenScience HTTP probe failed for {url}: {last_error[:500]}")


def run_browser_probe(container: str) -> None:
    verifier = REPOSITORY_ROOT / "scripts" / "ci" / "test_openscience_bridge_browser.sh"
    require_private_regular(verifier, executable=True)
    run([str(verifier), "--container", container], cwd=REPOSITORY_ROOT)


def run_canary(plan: dict[str, Any]) -> None:
    operation_id = plan["operation_id"]
    name = f"nexpoly-{operation_id}-canary"
    image = plan["candidate"]["reference"]
    existing = run(["docker", "inspect", name], check=False)
    if existing.returncode == 0:
        run(["docker", "rm", "--force", name])
    try:
        run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                "--network",
                NETWORK,
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "128",
                "--memory",
                "512m",
                "--cpus",
                "1.0",
                "--publish",
                f"127.0.0.1:{CANARY_PORT}:4454",
                image,
            ]
        )
        wait_http(f"http://127.0.0.1:{CANARY_PORT}/healthz", expected="ok")
        wait_http(f"http://127.0.0.1:{CANARY_PORT}/openscience-api/global/health")
        run_browser_probe(name)
    finally:
        run(["docker", "rm", "--force", name], check=False)


def verify_live_candidate(plan: dict[str, Any]) -> None:
    live = live_identity()
    if live["image_id"] != plan["candidate"]["id"]:
        raise ReleaseError("live OpenScience container did not switch to the candidate image")
    if live["configured_image"] != plan["candidate"]["reference"]:
        raise ReleaseError("live OpenScience configured reference differs from the candidate digest")
    wait_http("http://127.0.0.1:9011/healthz", expected="ok")
    wait_http("http://127.0.0.1:9011/openscience-api/global/health")
    run_browser_probe(LIVE_CONTAINER)


def restore_previous(
    *,
    operation_dir: Path,
    deployment_dir: Path,
    plan: dict[str, Any],
) -> None:
    backup_path = operation_dir / "env.before"
    require_private_regular(backup_path)
    env_path = deployment_dir / ".env"
    metadata = env_path.stat()
    original = backup_path.read_bytes()
    if "sha256:" + hashlib.sha256(original).hexdigest() != plan["deployment"]["env_sha256"]:
        raise ReleaseError("OpenScience .env backup digest differs from the plan")
    candidate = replace_env_image(original, plan["candidate"]["reference"])
    current = env_path.read_bytes()
    if current not in {original, candidate}:
        raise ReleaseError("OpenScience .env changed outside the release transaction")
    if current == candidate:
        atomic_write(env_path, original, stat.S_IMODE(metadata.st_mode))

    raw_live = docker_inspect(LIVE_CONTAINER)
    raw_state = raw_live.get("State", {})
    raw_health = raw_state.get("Health", {}).get("Status")
    raw_image_id = raw_live.get("Image")
    raw_configured_image = raw_live.get("Config", {}).get("Image")
    if (
        raw_image_id != plan["current"]["image_id"]
        or raw_configured_image != plan["deployment"]["env_image"]
        or raw_state.get("Running") is not True
        or raw_health != "healthy"
    ):
        compose_up(deployment_dir)
    live = live_identity()
    if live["image_id"] != plan["current"]["image_id"]:
        raise ReleaseError("OpenScience rollback did not restore the previous image ID")
    if live["configured_image"] != plan["deployment"]["env_image"]:
        raise ReleaseError("OpenScience rollback did not restore the previous image reference")
    wait_http("http://127.0.0.1:9011/healthz", expected="ok")
    wait_http("http://127.0.0.1:9011/openscience-api/global/health")


def apply_release(arguments: argparse.Namespace) -> dict[str, Any]:
    validate_operation_id(arguments.operation_id)
    state_root = arguments.state_root.resolve()
    operation_dir = state_root / arguments.operation_id
    deployment_dir = arguments.deployment_dir.resolve()
    with release_lock(state_root):
        if operation_dir.exists():
            if operation_dir.is_symlink() or not operation_dir.is_dir():
                raise ReleaseError("existing OpenScience operation path is unsafe")
            journal_path = operation_dir / "journal.json"
            plan_path = operation_dir / "plan.json"
            if not journal_path.is_file() or not plan_path.is_file():
                raise ReleaseError("existing OpenScience operation is incomplete or unsafe")
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            persisted_plan = json.loads(plan_path.read_text(encoding="utf-8"))
            if digest(persisted_plan) != arguments.confirm_plan_sha256:
                raise ReleaseError("existing OpenScience operation plan confirmation differs")
            if journal.get("phase") == "completed":
                verify_live_candidate(persisted_plan)
                return journal
            if journal.get("phase") == "rolled-back":
                raise ReleaseError("OpenScience operation is already rolled back")
            update_journal(operation_dir, "rollback-intent", error="interrupted apply resumed")
            restore_previous(
                operation_dir=operation_dir,
                deployment_dir=deployment_dir,
                plan=persisted_plan,
            )
            update_journal(operation_dir, "rolled-back", error="interrupted apply recovered")
            raise ReleaseError("interrupted OpenScience apply was rolled back; use a new operation ID")

        plan = build_plan(
            image=arguments.image,
            sha=arguments.sha,
            operation_id=arguments.operation_id,
            deployment_dir=deployment_dir,
        )
        plan_sha256 = digest(plan)
        if plan_sha256 != arguments.confirm_plan_sha256:
            raise ReleaseError(
                f"OpenScience plan confirmation differs; expected {plan_sha256}"
            )
        compose_path = deployment_dir / "docker-compose.yml"
        env_path = deployment_dir / ".env"
        if file_sha256(compose_path) != plan["deployment"]["compose_sha256"]:
            raise ReleaseError("OpenScience Compose file changed after planning")
        env_payload = env_path.read_bytes()
        if "sha256:" + hashlib.sha256(env_payload).hexdigest() != plan["deployment"]["env_sha256"]:
            raise ReleaseError("OpenScience .env changed after planning")
        operation_dir.mkdir(mode=0o700)
        write_json(operation_dir / "plan.json", plan)
        atomic_write(operation_dir / "env.before", env_payload, 0o600)
        update_journal(operation_dir, "prepared", plan_sha256=plan_sha256)
        env_mutated = False
        try:
            run_canary(plan)
            update_journal(operation_dir, "canary-verified", plan_sha256=plan_sha256)
            if env_path.read_bytes() != env_payload:
                raise ReleaseError("OpenScience .env changed during candidate verification")
            if file_sha256(compose_path) != plan["deployment"]["compose_sha256"]:
                raise ReleaseError("OpenScience Compose file changed during candidate verification")
            replacement = replace_env_image(env_payload, arguments.image)
            update_journal(operation_dir, "env-switch-intent", plan_sha256=plan_sha256)
            atomic_write(env_path, replacement, stat.S_IMODE(env_path.stat().st_mode))
            env_mutated = True
            update_journal(operation_dir, "env-switched", plan_sha256=plan_sha256)
            compose_up(deployment_dir)
            update_journal(operation_dir, "service-switched", plan_sha256=plan_sha256)
            verify_live_candidate(plan)
            result = {
                "schema_version": 1,
                "phase": "completed",
                "operation_id": arguments.operation_id,
                "plan_sha256": plan_sha256,
                "target_sha": arguments.sha,
                "candidate_image": arguments.image,
            }
            update_journal(operation_dir, "completed", **result)
            return result
        except Exception as exc:
            update_journal(operation_dir, "rollback-intent", error=str(exc)[:500])
            if env_mutated:
                try:
                    restore_previous(
                        operation_dir=operation_dir,
                        deployment_dir=deployment_dir,
                        plan=plan,
                    )
                except Exception as rollback_error:
                    update_journal(
                        operation_dir,
                        "rollback-failed",
                        error=str(exc)[:500],
                        rollback_error=str(rollback_error)[:500],
                    )
                    raise ReleaseError(
                        f"OpenScience release failed and rollback failed: {rollback_error}"
                    ) from exc
            update_journal(operation_dir, "rolled-back", error=str(exc)[:500])
            raise ReleaseError("OpenScience release failed and was rolled back") from exc


def rollback_release(arguments: argparse.Namespace) -> dict[str, Any]:
    validate_operation_id(arguments.operation_id)
    state_root = arguments.state_root.resolve()
    operation_dir = state_root / arguments.operation_id
    deployment_dir = arguments.deployment_dir.resolve()
    with release_lock(state_root):
        plan_path = operation_dir / "plan.json"
        journal_path = operation_dir / "journal.json"
        require_private_regular(plan_path)
        require_private_regular(journal_path)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        if plan.get("deployment", {}).get("directory") != str(deployment_dir):
            raise ReleaseError("OpenScience rollback deployment directory differs from the plan")
        if digest(plan) != arguments.confirm_plan_sha256:
            raise ReleaseError("OpenScience rollback plan confirmation differs")
        if journal.get("phase") == "rolled-back":
            return journal
        if journal.get("phase") not in {"completed", "rollback-intent"}:
            raise ReleaseError("only a completed OpenScience release can use explicit rollback")
        if journal.get("phase") == "completed":
            update_journal(operation_dir, "rollback-intent", plan_sha256=digest(plan))
        restore_previous(
            operation_dir=operation_dir,
            deployment_dir=deployment_dir,
            plan=plan,
        )
        result = {
            "schema_version": 1,
            "phase": "rolled-back",
            "operation_id": arguments.operation_id,
            "plan_sha256": digest(plan),
        }
        update_journal(operation_dir, "rolled-back", **result)
        return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--deployment-dir", type=Path, default=DEFAULT_DEPLOYMENT_DIR
    )
    result.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    commands = result.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    plan.add_argument("--image", required=True)
    plan.add_argument("--sha", required=True)
    plan.add_argument("--operation-id", required=True)

    apply = commands.add_parser("apply")
    apply.add_argument("--image", required=True)
    apply.add_argument("--sha", required=True)
    apply.add_argument("--operation-id", required=True)
    apply.add_argument("--confirm-plan-sha256", required=True)

    rollback = commands.add_parser("rollback")
    rollback.add_argument("--operation-id", required=True)
    rollback.add_argument("--confirm-plan-sha256", required=True)

    status = commands.add_parser("status")
    status.add_argument("--operation-id", required=True)
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        if arguments.command == "plan":
            plan = build_plan(
                image=arguments.image,
                sha=arguments.sha,
                operation_id=arguments.operation_id,
                deployment_dir=arguments.deployment_dir,
            )
            output = {"plan": plan, "plan_sha256": digest(plan)}
        elif arguments.command == "apply":
            output = apply_release(arguments)
        elif arguments.command == "rollback":
            output = rollback_release(arguments)
        else:
            validate_operation_id(arguments.operation_id)
            path = arguments.state_root.resolve() / arguments.operation_id / "journal.json"
            require_private_regular(path)
            output = json.loads(path.read_text(encoding="utf-8"))
        sys.stdout.buffer.write(canonical_json(output))
        return 0
    except (OSError, ReleaseError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
