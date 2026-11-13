#!/usr/bin/env python3
"""Build and verify a credential-free, sealed Ubuntu CI VM template.

The only VM launcher is the installed, restricted host systemd unit. Callers of
build_template must own the controller's global lock and stop normal admission.
render_seed is intentionally usable without root, network access, or KVM.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

PACKAGE = Path(__file__).resolve().parent
PROFILE = "nexpoly-ci-ephemeral-v1"
GIB = 1024 ** 3
VIRTUAL_SIZE = 160 * GIB
MAX_TEMPLATE_BYTES = 25 * GIB
DEFAULT_LOCK = PACKAGE / "guest/software-lock.json"
CLOUD_KEYRING = Path("/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg")
DOCKER_ROOT = "https://download.docker.com/linux/ubuntu/"
IDLE_TIMEOUT = 45
TOTAL_TIMEOUT = 1800
PROGRESS_INTERVAL = 10
DOWNLOAD_CHUNK_BYTES = 256 * 1024
BUILD_LOG_BYTES = 8 * 1024 ** 2
BUILD_LOG_COUNT = 32
BUILD_LOG_MAX_AGE = 7 * 24 * 3600
BUILD_STAGES = frozenset({"preflight", "apt_update", "apt_dependencies", "docker_packages",
                          "buildx_download", "runner_download", "runner_dependencies",
                          "guest_configuration", "docker_start", "smoke", "seal"})
BUILD_ERROR_KINDS = frozenset({"command_failed", "http_error", "timeout", "network_error",
                              "filesystem_error", "validation_error", "unexpected_error"})


class TemplateError(RuntimeError):
    pass


class BuildConsoleCapture:
    """Capture only the credential-free build VM, independently of guest disks."""

    def __init__(self, state: Path, instance_id: str):
        if not re.fullmatch(r"[0-9a-f]{32}", instance_id):
            raise TemplateError("Invalid build log instance ID")
        self.directory = state / "logs/template-builds"
        self.path = self.directory / f"template-build-{instance_id}.log"
        self.socket_path = f"/run/nexpoly-runner-vm-{instance_id}/serial.sock"
        self.stop_event = threading.Event()

    def __enter__(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = self.directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise TemplateError("Build log directory must be a protected controller directory")
        retained = []
        for path in self.directory.glob("template-build-*.log"):
            if not re.fullmatch(r"template-build-[0-9a-f]{32}\.log", path.name):
                continue
            item = path.lstat()
            if not stat.S_ISREG(item.st_mode) or item.st_uid != os.geteuid() or item.st_nlink != 1:
                continue
            retained.append((item.st_mtime, path))
        for index, (modified, path) in enumerate(sorted(retained, reverse=True)):
            if index >= BUILD_LOG_COUNT - 1 or modified < time.time() - BUILD_LOG_MAX_AGE:
                path.unlink()
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.output = os.fdopen(fd, "w+b", buffering=0)
        self.output.write(b"Trusted template build console; oldest output is dropped at 8 MiB.\n")
        self.thread = threading.Thread(target=self._capture, name="template-console", daemon=True)
        self.thread.start()
        return self

    def _append(self, chunk: bytes) -> None:
        if self.output.tell() + len(chunk) > BUILD_LOG_BYTES:
            self.output.seek(-BUILD_LOG_BYTES // 2, os.SEEK_END)
            tail = self.output.read()
            self.output.seek(0)
            self.output.truncate()
            self.output.write(b"[Older template console output dropped]\n" + tail)
        self.output.write(chunk)

    def _capture(self) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(0.25)
                while not self.stop_event.is_set():
                    try:
                        stream.connect(self.socket_path)
                        break
                    except (FileNotFoundError, ConnectionRefusedError):
                        self.stop_event.wait(0.1)
                else:
                    return
                while not self.stop_event.is_set():
                    try:
                        chunk = stream.recv(64 * 1024)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    self._append(chunk)
        except OSError:
            self._append(b"\n[Template console capture ended unexpectedly]\n")

    def __exit__(self, exc_type, *_):
        if exc_type is not None:
            # The failed lifecycle event can precede the guest's final traceback.
            self.thread.join(timeout=1)
        self.stop_event.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            # Do not close a file that the capture thread may still be writing.
            raise TemplateError(f"Build console capture did not stop: {self.path}")
        self.output.close()


def guest_build_failure(event: dict, log: Path) -> TemplateError:
    """Only fixed classifications and bounded integers can reach CLI output."""
    stage = event.get("stage")
    if not isinstance(stage, str) or stage not in BUILD_STAGES:
        stage = "unknown"
    kind = event.get("error_kind")
    details = [kind] if isinstance(kind, str) and kind in BUILD_ERROR_KINDS else []
    for key, lower, upper in (("exit_code", -255, 255), ("http_status", 100, 599)):
        value = event.get(key)
        if type(value) is int and lower <= value <= upper:
            details.append(f"{key}={value}")
    classification = f" ({', '.join(details)})" if details else ""
    return TemplateError(f"Guest template preparation failed at {stage}{classification}; build console: {log}")


def execute(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(args, text=True, capture_output=True, check=False, **kwargs)
    if result.returncode:
        raise TemplateError(f"{Path(args[0]).name} failed: {result.stderr[-1500:]}")
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def regular(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise TemplateError(f"Expected a regular file: {path}")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_lock(path: Path) -> dict:
    regular(path)
    value = json.loads(path.read_text())
    if value.get("schema_version") != 1:
        raise TemplateError("Unsupported software lock schema")
    if not re.fullmatch(r"hello-world@sha256:[0-9a-f]{64}", value.get("smoke_image", {}).get("reference", "")):
        raise TemplateError("Pin the official hello-world smoke image by digest")
    artifacts = [(value["cloud_image"], "cloud-images.ubuntu.com"),
                 (value["runner"], "github.com"), (value["buildx"], "github.com")]
    packages = value["docker_apt"]["packages"]
    if {p["package"] for p in packages} != {"docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin"} or len(packages) != 4:
        raise TemplateError("Docker lock must include the four reviewed packages")
    artifacts.extend((p, "download.docker.com") for p in packages)
    for item, hostname in artifacts:
        parsed = urllib.parse.urlsplit(item["url"])
        if parsed.scheme != "https" or parsed.hostname != hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise TemplateError("Software lock requires official, credential-free HTTPS URLs")
        if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise TemplateError("Missing pinned SHA256")
        if not isinstance(item["version"], str) or not re.fullmatch(r"[A-Za-z0-9.:~+-]+", item["version"]):
            raise TemplateError("Invalid pinned version")
    cloud = value["cloud_image"]
    if not re.fullmatch(r"https://cloud-images\.ubuntu\.com/releases/noble/release-[0-9]{8}/ubuntu-24\.04-server-cloudimg-amd64\.img", cloud["url"]):
        raise TemplateError("Pin a dated Ubuntu 24.04 amd64 release")
    for section in (cloud, value["docker_apt"]):
        if not re.fullmatch(r"[A-F0-9]{40}", section["signing_fingerprint"]):
            raise TemplateError("Pin a signing-key fingerprint")
    return value


def download(url: str, destination: Path, expected: str | None = None, proxy: str | None = None) -> None:
    if destination.exists():
        regular(destination)
        if expected and sha256(destination) == expected:
            return
        destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy else urllib.request.ProxyHandler()
    opener = urllib.request.build_opener(handler)
    temporary = destination.with_name(destination.name + ".partial")
    artifact = re.sub(r"[^A-Za-z0-9._+-]", "_", destination.name)[:160]
    offset = 0
    if temporary.exists() or temporary.is_symlink():
        regular(temporary)
        if expected:
            if sha256(temporary) == expected:
                os.replace(temporary, destination)
                print(f"Verified completed partial download: {artifact}", file=sys.stderr, flush=True)
                return
            offset = temporary.stat().st_size
        else:
            temporary.unlink()
    started = time.monotonic()
    deadline = started + TOTAL_TIMEOUT
    last_report = started
    received = offset
    total = None
    preserve_partial = bool(expected)

    def reject(message: str) -> None:
        nonlocal preserve_partial
        preserve_partial = False
        raise TemplateError(message)

    request = urllib.request.Request(url, headers={"Range": f"bytes={offset}-", "Accept-Encoding": "identity"}) if offset else url
    suffix = f"; attempting resume at {offset} bytes" if offset else ""
    print(f"Opening download: {artifact}{suffix}", file=sys.stderr, flush=True)
    try:
        with opener.open(request, timeout=IDLE_TIMEOUT) as source:
            length = source.headers.get("Content-Length") if hasattr(source, "headers") else None
            if length is not None:
                if not length.isdecimal():
                    reject(f"Invalid download length for {artifact}")
                total = int(length)
            status = getattr(source, "status", 200)
            if offset and status == 206:
                content_range = source.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", content_range)
                if not match:
                    reject(f"Invalid download range for {artifact}")
                first, last, extent = map(int, match.groups())
                if first != offset or last < first or last != extent - 1 or total != last - first + 1:
                    reject(f"Invalid download range for {artifact}")
                total = extent
                mode = "ab"
            elif status == 200:
                if offset:
                    print(f"Server ignored resume for {artifact}; restarting from byte 0", file=sys.stderr, flush=True)
                offset = received = 0
                mode = "wb" if temporary.exists() else "xb"
            else:
                reject(f"Unexpected download response for {artifact}: HTTP {status}")
            size = f"{total / (1024 ** 2):.1f} MiB" if total is not None else "unknown size"
            print(f"Downloading {artifact}: {size}; resumed offset {offset} bytes; idle limit {IDLE_TIMEOUT}s, total limit {TOTAL_TIMEOUT}s", file=sys.stderr, flush=True)
            # read1 performs one underlying read, so a slow trickle cannot hold
            # an entire large read open and hide the deadline or progress.
            read = getattr(source, "read1", source.read)
            with temporary.open(mode) as target:
                while True:
                    if time.monotonic() >= deadline:
                        raise TemplateError(f"Download total time limit exceeded for {artifact}")
                    chunk = read(DOWNLOAD_CHUNK_BYTES)
                    now = time.monotonic()
                    if now >= deadline:
                        raise TemplateError(f"Download total time limit exceeded for {artifact}")
                    if not chunk:
                        break
                    target.write(chunk)
                    received += len(chunk)
                    if total is not None and received > total:
                        reject(f"Download length mismatch for {artifact}")
                    if now - last_report >= PROGRESS_INTERVAL:
                        elapsed = max(now - started, 0.001)
                        extent = f"{received}/{total}" if total is not None else str(received)
                        detail = (f" ({received / (1024 ** 2):.1f}/{total / (1024 ** 2):.1f} MiB, {received / total:.1%})"
                                  if total else f" ({received / (1024 ** 2):.1f} MiB)")
                        print(f"Download {artifact}: {extent} bytes{detail}, {(received - offset) / elapsed / (1024 ** 2):.2f} MiB/s", file=sys.stderr, flush=True)
                        last_report = now
            if total is not None and received != total:
                raise TemplateError(f"Download length mismatch for {artifact}")
        if expected and sha256(temporary) != expected:
            reject("Downloaded artifact does not match its pinned checksum")
        os.replace(temporary, destination)
        print(f"Download complete: {artifact} ({received} bytes)", file=sys.stderr, flush=True)
    except urllib.error.HTTPError as exc:
        if exc.code == 416:
            reject(f"Download range rejected for {artifact}: HTTP 416; retry to restart")
        raise TemplateError(f"Download failed for {artifact}: HTTP {exc.code}") from None
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        # urllib exceptions may embed full URLs and proxy credentials.
        raise TemplateError(f"Download failed for {artifact}: {type(exc).__name__}") from None
    finally:
        if preserve_partial and temporary.exists():
            print(f"Partial download retained: {artifact} ({temporary.stat().st_size} bytes); retry to resume", file=sys.stderr, flush=True)
        else:
            temporary.unlink(missing_ok=True)


def verify_signature(signature: Path, data: Path | None, keyring: Path, fingerprint: str) -> None:
    args = ["gpgv", "--status-fd", "1", "--keyring", str(keyring), str(signature)]
    if data is not None:
        args.append(str(data))
    result = execute(args)
    valid = [line.split() for line in result.stdout.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
    if not any(fingerprint in line[2:] for line in valid):
        raise TemplateError("Signature was not made by the pinned signing key")


def verify_cloud_metadata(lock: dict, cache: Path, proxy: str | None = None) -> None:
    cloud = lock["cloud_image"]
    base, name = cloud["url"].rsplit("/", 1)
    sums, signature = cache / "SHA256SUMS", cache / "SHA256SUMS.gpg"
    download(base + "/SHA256SUMS", sums, proxy=proxy)
    download(base + "/SHA256SUMS.gpg", signature, proxy=proxy)
    verify_signature(signature, sums, CLOUD_KEYRING, cloud["signing_fingerprint"])
    lines = [line.split() for line in sums.read_text().splitlines()]
    matches = [parts[0] for parts in lines if len(parts) == 2 and parts[1].lstrip("*") == name]
    if matches != [cloud["sha256"]]:
        raise TemplateError("Signed Ubuntu checksum differs from the software lock")


def verify_docker_metadata(lock: dict, cache: Path, proxy: str | None = None) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    key, release, packages = cache / "docker.asc", cache / "InRelease", cache / "Packages.gz"
    download(DOCKER_ROOT + "gpg", key, proxy=proxy)
    download(DOCKER_ROOT + "dists/noble/InRelease", release, proxy=proxy)
    keyring = cache / "docker.gpg"
    keyring.unlink(missing_ok=True)
    # Isolate GnuPG's state: never create/import anything in the operator's HOME.
    gnupg = cache / "gnupg"
    gnupg.mkdir(mode=0o700, exist_ok=True)
    execute(["gpg", "--homedir", str(gnupg), "--batch", "--no-options", "--dearmor", "--output", str(keyring), str(key)])
    verify_signature(release, None, keyring, lock["docker_apt"]["signing_fingerprint"])
    expected = None
    in_sha256 = False
    for line in release.read_text().splitlines():
        if line == "SHA256:":
            in_sha256 = True
        elif in_sha256 and line and not line[0].isspace():
            in_sha256 = False
        if in_sha256 and len(line.split()) == 3 and line.split()[2] == "stable/binary-amd64/Packages.gz":
            expected = line.split()[0]
    if not expected:
        raise TemplateError("Docker signed metadata has no SHA256 for Packages.gz")
    download(DOCKER_ROOT + "dists/noble/stable/binary-amd64/Packages.gz", packages, expected, proxy)
    blocks = []
    for block in gzip.decompress(packages.read_bytes()).decode().split("\n\n"):
        blocks.append(dict(line.split(": ", 1) for line in block.splitlines() if ": " in line and not line.startswith(" ")))
    for artifact in lock["docker_apt"]["packages"]:
        matches = [b for b in blocks if b.get("Package") == artifact["package"] and b.get("Version") == artifact["version"]]
        if len(matches) != 1 or matches[0].get("SHA256") != artifact["sha256"] or DOCKER_ROOT + matches[0].get("Filename", "") != artifact["url"]:
            raise TemplateError("Docker package pin is not present in current signed repository metadata")


def render_seed(lock: dict, output_dir: Path, instance_id: str = "nexpoly-template") -> tuple[Path, Path]:
    """Write deterministic cloud-config JSON (valid YAML), without executing it."""
    output_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for name in ("bootstrap.py", "build.py", "smoke.py", "job-event.py", "job-started.sh", "job-completed.sh"):
        files.append({"path": f"/usr/local/lib/nexpoly-ci-runner/{name}", "permissions": "0755", "encoding": "b64", "content": base64.b64encode((PACKAGE / "guest" / name).read_bytes()).decode()})
    files.extend([
        {"path": "/etc/systemd/system/nexpoly-ci-bootstrap.service", "content": (PACKAGE / "guest/nexpoly-ci-bootstrap.service").read_text()},
        {"path": "/etc/nexpoly-ci-runner/build-mode", "content": "trusted-template-build\n"},
        {"path": "/etc/nexpoly-ci-runner/software-lock.json", "permissions": "0644", "content": json.dumps(lock)},
    ])
    data = {"users": [{"name": "runner", "shell": "/bin/bash", "lock_passwd": True}],
            "ssh_pwauth": False, "disable_root": True,
            "write_files": files,
            "runcmd": [["python3", "/usr/local/lib/nexpoly-ci-runner/build.py"]]}
    user_data, meta_data = output_dir / "user-data", output_dir / "meta-data"
    user_data.write_text("#cloud-config\n" + json.dumps(data, indent=2) + "\n")
    meta_data.write_text(json.dumps({"instance-id": instance_id, "local-hostname": "nexpoly-template", "nexpoly-mode": "build"}) + "\n")
    render_network_config(output_dir)
    return user_data, meta_data


def render_network_config(output_dir: Path) -> Path:
    # QEMU's dns= changes its virtual resolver address, not the upstream DNS.
    # Explicitly reject DHCP DNS so systemd-resolved cannot prefer 10.0.2.3.
    path = output_dir / "network-config"
    path.write_text(json.dumps({"version": 2, "ethernets": {"ci": {
        "match": {"driver": "virtio_net"}, "dhcp4": True, "dhcp6": False,
        "dhcp4-overrides": {"use-dns": False},
        "nameservers": {"addresses": ["202.38.64.7"]},
    }}}, indent=2) + "\n")
    return path


def create_seed(instance_dir: Path, user_data: Path, meta_data: Path) -> None:
    execute(["cloud-localds", "--network-config", str(instance_dir / "network-config"),
             str(instance_dir / "seed.iso"), str(user_data), str(meta_data)])


def _config(config: object) -> dict:
    if isinstance(config, dict):
        return config
    # Accept common.Config without tying offline seed rendering to that module.
    return {"paths": {"state": str(config.state)}, "network": {"proxy_port": config.proxy_port},
            "policy": {"boot_timeout_seconds": config.boot_timeout_seconds}}


def prepare_instance(config: object, lock_path: Path, instance_dir: Path) -> dict:
    config = _config(config)
    lock = read_lock(lock_path)
    state = Path(config["paths"]["state"])
    cache = state / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    proxy = f"http://127.0.0.1:{config['network']['proxy_port']}"
    print("Template stage: verify signed Ubuntu and Docker metadata", file=sys.stderr, flush=True)
    verify_cloud_metadata(lock, cache, proxy)
    verify_docker_metadata(lock, cache / "docker-metadata", proxy)
    cloud = cache / ("ubuntu-" + lock["cloud_image"]["sha256"] + ".qcow2")
    print("Template stage: download and verify Ubuntu cloud image", file=sys.stderr, flush=True)
    download(lock["cloud_image"]["url"], cloud, lock["cloud_image"]["sha256"], proxy)
    info = json.loads(execute(["qemu-img", "info", "--output=json", str(cloud)]).stdout)
    if info.get("format") != "qcow2" or info.get("backing-filename") or info.get("encrypted"):
        raise TemplateError("Cloud image must be a standalone unencrypted qcow2")
    instance_dir.mkdir(mode=0o750, parents=True, exist_ok=False)
    instance_dir.chmod(0o750)  # Controller UMask=0077 must not hide it from QEMU.
    disk = instance_dir / "disk.qcow2"
    print("Template stage: convert and expand the VM disk", file=sys.stderr, flush=True)
    execute(["qemu-img", "convert", "-f", "qcow2", "-O", "qcow2", str(cloud), str(disk)])
    execute(["qemu-img", "resize", str(disk), "160G"])
    user_data, meta_data = render_seed(lock, instance_dir, instance_dir.name)
    create_seed(instance_dir, user_data, meta_data)
    return lock


def prepare_runner_instance(config: object, instance_dir: Path, manifest: dict) -> None:
    """Create an execution overlay and a fresh seed; caller journals its UUID."""
    config = _config(config)
    state = Path(config["paths"]["state"])
    image = state / "templates" / manifest["image"]
    verify_template(image, manifest)
    instance_dir.mkdir(mode=0o750, parents=True, exist_ok=False)
    instance_dir.chmod(0o750)
    execute(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(image),
             str(instance_dir / "disk.qcow2"), "160G"])
    user_data, meta_data = instance_dir / "user-data", instance_dir / "meta-data"
    user_data.write_text("#cloud-config\n" + json.dumps({"users": [], "ssh_pwauth": False, "disable_root": True}) + "\n")
    meta_data.write_text(json.dumps({"instance-id": instance_dir.name, "local-hostname": "nexpoly-ci", "nexpoly-mode": "runner"}) + "\n")
    render_network_config(instance_dir)
    create_seed(instance_dir, user_data, meta_data)
    if os.geteuid() == 0:
        account = pwd.getpwnam("nexpoly-vm")
        os.chown(instance_dir, 0, account.pw_gid)
        for name in ("disk.qcow2", "seed.iso"):
            os.chown(instance_dir / name, account.pw_uid, account.pw_gid)
            (instance_dir / name).chmod(0o600)


def check_sealed_file(image: Path) -> None:
    regular(image)
    details = image.stat()
    if details.st_mode & 0o222:
        raise TemplateError("Template must be read-only")
    if details.st_uid != 0:
        raise TemplateError("Template must be owned by root")
    if details.st_blocks * 512 > MAX_TEMPLATE_BYTES:
        raise TemplateError("Template allocation exceeds the 25 GiB budget")


def verify_template(image: Path, manifest: Path | dict) -> dict:
    if isinstance(manifest, Path):
        regular(manifest)
        manifest = json.loads(manifest.read_text())
    if manifest.get("schema_version") != 1 or manifest.get("profile") != PROFILE or manifest.get("virtual_size_bytes") != VIRTUAL_SIZE:
        raise TemplateError("Invalid template manifest contract")
    if not re.fullmatch(r"base-[0-9a-f]{64}\.qcow2", manifest.get("image", "")) or image.name != manifest["image"]:
        raise TemplateError("Template name must be its content-addressed basename")
    check_sealed_file(image)
    if sha256(image) != manifest.get("sha256") or image.name != f"base-{manifest['sha256']}.qcow2":
        raise TemplateError("Template checksum mismatch")
    info = json.loads(execute(["qemu-img", "info", "--output=json", str(image)]).stdout)
    if info.get("format") != "qcow2" or info.get("backing-filename") or info.get("encrypted") or info.get("virtual-size") != VIRTUAL_SIZE:
        raise TemplateError("Template must be a standalone 160 GiB qcow2")
    return manifest


def _active(unit: str) -> bool:
    result = execute(["systemctl", "show", unit, "--property=ActiveState,MainPID,ControlGroup"])
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if properties.get("ActiveState") not in ("inactive", "failed"):
        return True
    if not properties.get("MainPID", "").isdecimal():
        raise TemplateError("Cannot establish whether the build VM has stopped")
    if int(properties["MainPID"]) != 0:
        return True
    if "ControlGroup" not in properties:
        raise TemplateError("Cannot establish whether the build VM cgroup has stopped")
    group = properties["ControlGroup"]
    if group:
        if not group.startswith("/") or ".." in Path(group).parts:
            raise TemplateError("Invalid systemd cgroup path")
        events = Path("/sys/fs/cgroup") / group.lstrip("/") / "cgroup.events"
        try:
            if "populated 0" not in events.read_text().splitlines():
                return True
        except FileNotFoundError:
            pass  # systemd already removed the empty cgroup.
    return False


def prune_templates(templates: Path, keep: set[str]) -> None:
    """Only remove our root-owned sealed content-addressed images, under the lock."""
    for candidate in templates.glob("base-*.qcow2"):
        if candidate.name in keep:
            continue
        if not re.fullmatch(r"base-[0-9a-f]{64}\.qcow2", candidate.name):
            continue
        check_sealed_file(candidate)
        candidate.unlink()


def build_template(config: object, lock_path: Path = DEFAULT_LOCK) -> dict:
    """Caller owns global lock. Build exclusively through the restricted VM unit."""
    if os.geteuid() != 0:
        raise TemplateError("Template building requires the installed root controller; seed rendering does not")
    config = _config(config)
    state = Path(config["paths"]["state"])
    instance_id = uuid.uuid4().hex
    instance = state / "instances" / instance_id
    unit = f"nexpoly-runner-vm@{instance_id}.service"
    journal = state / "controller/template-build.json"
    if journal.exists():
        raise TemplateError("A previous template build needs reconciliation")
    atomic_json(journal, {"schema_version": 1, "instance_id": instance_id, "phase": "preparing", "created_at": datetime.now(timezone.utc).isoformat()})
    try:
        lock = prepare_instance(config, Path(lock_path), instance)
        account = pwd.getpwnam("nexpoly-vm")
        os.chown(instance, 0, account.pw_gid)
        for name in ("disk.qcow2", "seed.iso"):
            os.chown(instance / name, account.pw_uid, account.pw_gid)
            (instance / name).chmod(0o600)
        atomic_json(journal, {"schema_version": 1, "instance_id": instance_id, "phase": "building", "created_at": datetime.now(timezone.utc).isoformat()})
        print("Template stage: start the isolated build VM; guest preparation can take up to one hour", file=sys.stderr, flush=True)
        ready = False
        with BuildConsoleCapture(state, instance_id) as console:
            print(f"Template build console: {console.path}", file=sys.stderr, flush=True)
            execute(["systemctl", "start", unit])
            event_path = Path(f"/run/nexpoly-runner-vm-{instance_id}/events.sock")
            deadline = time.monotonic() + 3600
            stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                while time.monotonic() < deadline:
                    try:
                        stream.connect(str(event_path))
                        break
                    except (FileNotFoundError, ConnectionRefusedError):
                        if not _active(unit):
                            raise TemplateError(f"Build VM stopped before its event channel became available; build console: {console.path}")
                        time.sleep(0.5)
                else:
                    raise TemplateError(f"Build event channel timeout; build console: {console.path}")
                stream.settimeout(2)
                pending = bytearray()
                last_stage = None
                while time.monotonic() < deadline:
                    try:
                        chunk = stream.recv(4096)
                    except socket.timeout:
                        chunk = None
                    if chunk:
                        pending.extend(chunk)
                        if len(pending) > 8192:
                            raise TemplateError("Build event exceeded its size bound")
                        while b"\n" in pending:
                            line, remainder = pending.split(b"\n", 1)
                            pending[:] = remainder
                            event = json.loads(line)
                            if not isinstance(event, dict):
                                raise TemplateError("Invalid build lifecycle event")
                            if event.get("event") == "template_stage":
                                stage = event.get("stage")
                                if isinstance(stage, str) and stage in BUILD_STAGES and stage != last_stage:
                                    print(f"Guest template stage: {stage}", file=sys.stderr, flush=True)
                                    last_stage = stage
                            if event.get("event") == "failed":
                                raise guest_build_failure(event, console.path)
                            if event.get("event") == "template_ready":
                                if event.get("profile") != PROFILE or event.get("runner_version") != lock["runner"]["version"] or event.get("smoke_passed") is not True:
                                    raise TemplateError("Guest template completion did not match the lock")
                                ready = True
                    if not _active(unit):
                        break
                    time.sleep(0.2)
                else:
                    raise TemplateError(f"Template build exceeded one hour; build console: {console.path}")
            finally:
                stream.close()
        if not ready:
            raise TemplateError(f"Build VM shut down without a verified completion event; build console: {console.path}")
        # No running guest may write the image while it is sealed and hashed.
        print("Template stage: stop the build VM and verify the sealed image", file=sys.stderr, flush=True)
        execute(["systemctl", "stop", unit])
        if _active(unit):
            raise TemplateError("Build VM cgroup still contains processes; image retained")
        execute(["qemu-img", "check", str(instance / "disk.qcow2")])
        if (instance / "disk.qcow2").stat().st_blocks * 512 > MAX_TEMPLATE_BYTES:
            raise TemplateError("Built template allocation exceeds the 25 GiB budget")
        digest = sha256(instance / "disk.qcow2")
        templates = state / "templates"
        templates.mkdir(mode=0o755, parents=True, exist_ok=True)
        templates.chmod(0o755)
        previous = None
        current = templates / "current.json"
        if current.exists():
            regular(current)
            previous = json.loads(current.read_text())
            if not re.fullmatch(r"base-[0-9a-f]{64}\.qcow2", previous.get("image", "")):
                raise TemplateError("Previous template has an invalid basename")
        image = templates / f"base-{digest}.qcow2"
        os.chown(instance / "disk.qcow2", 0, 0)
        (instance / "disk.qcow2").chmod(0o444)
        os.replace(instance / "disk.qcow2", image)
        manifest = {"schema_version": 1, "image": image.name, "sha256": digest,
                    "runner_version": lock["runner"]["version"], "profile": PROFILE,
                    "virtual_size_bytes": VIRTUAL_SIZE, "created_at": datetime.now(timezone.utc).isoformat(),
                    "software_lock": lock}
        verify_template(image, manifest)
        keep = {image.name}
        if previous:
            keep.add(previous["image"])
        prune_templates(templates, keep)
        if shutil.disk_usage(state).free < 40 * GIB:
            raise TemplateError("Template promotion would violate the 40 GiB storage reserve")
        atomic_json(current, manifest)
        if previous and previous["image"] != image.name:
            atomic_json(templates / "previous.json", previous)
        else:
            (templates / "previous.json").unlink(missing_ok=True)
        shutil.rmtree(instance)
        journal.unlink()
        return manifest
    except Exception:
        subprocess.run(["systemctl", "stop", unit], check=False, capture_output=True)
        # Keep only the root-owned lifecycle journal for explicit reconciliation;
        # do not reuse a partially built VM or silently promote its image.
        if not _active(unit):
            shutil.rmtree(instance, ignore_errors=True)
            journal.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render-seed")
    render.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    render.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--image", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "render-seed":
        for path in render_seed(read_lock(args.lock), args.output):
            print(path)
    elif args.command == "verify":
        print(json.dumps(verify_template(args.image, args.manifest), sort_keys=True))


if __name__ == "__main__":
    main()
