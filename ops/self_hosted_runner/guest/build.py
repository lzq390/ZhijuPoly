#!/usr/bin/env python3
"""Trusted template preparation, executed only inside the disposable build VM."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import traceback
import urllib.error
import urllib.request

HERE = Path("/usr/local/lib/nexpoly-ci-runner")
CONFIG = Path("/etc/nexpoly-ci-runner")
PROXY = "http://10.0.2.2:27892"
NO_PROXY = "localhost,127.0.0.1,::1,postgres,backend,lab-postgres"
# Ubuntu 24.04 x64 tools + Chromium requirements are identical in the locked
# Playwright 1.62.1 and 1.63.0 releases. Preinstall OS libraries and fonts only:
# https://github.com/microsoft/playwright/blob/v1.63.0/packages/playwright-core/src/server/registry/nativeDeps.ts
CHROMIUM_SYSTEM_PACKAGES = (
    "xvfb", "fonts-noto-color-emoji", "fonts-unifont", "libfontconfig1",
    "libfreetype6", "xfonts-cyrillic", "xfonts-scalable", "fonts-liberation",
    "fonts-ipafont-gothic", "fonts-wqy-zenhei", "fonts-tlwg-loma-otf",
    "fonts-freefont-ttf", "libasound2t64", "libatk-bridge2.0-0t64",
    "libatk1.0-0t64", "libatspi2.0-0t64", "libcairo2", "libcups2t64",
    "libdbus-1-3", "libdrm2", "libgbm1", "libglib2.0-0t64", "libnspr4",
    "libnss3", "libpango-1.0-0", "libx11-6", "libxcb1", "libxcomposite1",
    "libxdamage1", "libxext6", "libxfixes3", "libxkbcommon0", "libxrandr2",
)
STAGES = frozenset({"preflight", "apt_update", "apt_dependencies", "docker_packages",
                    "buildx_download", "runner_download", "runner_dependencies",
                    "guest_configuration", "docker_start", "smoke", "seal"})
CURRENT_STAGE = "preflight"


def emit(event: str, **fields: object) -> None:
    from bootstrap import emit as emit_event
    emit_event(event, **fields)


def stage(name: str) -> None:
    """Send only a fixed phase name, never command arguments or output."""
    if name not in STAGES:
        raise ValueError("unknown template build stage")
    global CURRENT_STAGE
    CURRENT_STAGE = name
    emit("template_stage", stage=name)


def report_failure(error: Exception) -> None:
    """Classify failures without serializing exception text or command data."""
    details: dict[str, object] = {
        "reason": "template_build_failed",
        "stage": CURRENT_STAGE if CURRENT_STAGE in STAGES else "preflight",
        "error_kind": "unexpected_error",
    }
    if isinstance(error, subprocess.CalledProcessError):
        details["error_kind"] = "command_failed"
        if type(error.returncode) is int and -255 <= error.returncode <= 255:
            details["exit_code"] = error.returncode
    elif isinstance(error, urllib.error.HTTPError):
        details["error_kind"] = "http_error"
        if type(error.code) is int and 100 <= error.code <= 599:
            details["http_status"] = error.code
    elif isinstance(error, TimeoutError):
        details["error_kind"] = "timeout"
    elif isinstance(error, urllib.error.URLError):
        details["error_kind"] = "timeout" if isinstance(error.reason, TimeoutError) else "network_error"
    elif isinstance(error, OSError):
        details["error_kind"] = "filesystem_error"
    elif isinstance(error, (ValueError, RuntimeError, tarfile.TarError)):
        details["error_kind"] = "validation_error"
    emit("failed", **details)


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def download(artifact: dict, destination: Path) -> None:
    digest = hashlib.sha256()
    with urllib.request.urlopen(artifact["url"], timeout=120) as source, destination.open("wb") as target:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            target.write(chunk)
    if digest.hexdigest() != artifact["sha256"]:
        destination.unlink()
        raise ValueError("artifact checksum mismatch")


def write(path: str, content: str, mode: int = 0o644) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    target.chmod(mode)


def main() -> None:
    stage("preflight")
    if os.geteuid() != 0 or not (CONFIG / "build-mode").exists():
        raise RuntimeError("this command requires the trusted guest build profile")
    if 'VERSION_ID="24.04"' not in Path("/etc/os-release").read_text():
        raise RuntimeError("Ubuntu 24.04 is required")
    CONFIG.chmod(0o755)
    lock = json.loads((CONFIG / "software-lock.json").read_text())
    env_lines = []
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        os.environ[key] = PROXY
        env_lines.append(f'{key}={PROXY}')
    for key in ("no_proxy", "NO_PROXY"):
        os.environ[key] = NO_PROXY
        env_lines.append(f'{key}={NO_PROXY}')
    os.environ["DEBIAN_FRONTEND"] = "noninteractive"
    write("/etc/apt/apt.conf.d/80-nexpoly-proxy", f'Acquire::http::Proxy "{PROXY}";\nAcquire::https::Proxy "{PROXY}";\n')
    write("/etc/environment", "\n".join(env_lines) + "\n")
    stage("apt_update")
    run("apt-get", "update")
    stage("apt_dependencies")
    run("apt-get", "install", "-y", "--no-install-recommends", "ca-certificates", "curl", "git", "jq", "sudo", "python3-venv", "build-essential", "unzip", "zip", "rsync", "zstd", "xz-utils", "iptables", "nftables", "shellcheck", "postgresql-client-16", "libpq-dev",
        *CHROMIUM_SYSTEM_PACKAGES)
    with tempfile.TemporaryDirectory(prefix="nexpoly-template-") as directory:
        temp = Path(directory)
        packages = []
        stage("docker_packages")
        for artifact in lock["docker_apt"]["packages"]:
            path = temp / (artifact["package"] + ".deb")
            download(artifact, path)
            packages.append(str(path))
        run("apt-get", "install", "-y", "--no-install-recommends", *packages)
        run("apt-mark", "hold", "docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin")
        stage("buildx_download")
        buildx = Path("/usr/local/lib/docker/cli-plugins/docker-buildx")
        buildx.parent.mkdir(parents=True, exist_ok=True)
        download(lock["buildx"], buildx)
        buildx.chmod(0o755)
        stage("runner_download")
        archive = temp / "runner.tar.gz"
        download(lock["runner"], archive)
        runner = Path("/opt/actions-runner")
        runner.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as package:
            package.extractall(runner, filter="data")
        stage("runner_dependencies")
        run(str(runner / "bin/installdependencies.sh"))
    stage("guest_configuration")
    run("usermod", "-aG", "docker,sudo", "runner")
    write("/etc/sudoers.d/90-nexpoly-runner", "runner ALL=(ALL:ALL) NOPASSWD:ALL\n", 0o440)
    write("/etc/nexpoly-ci-runner/profile", "nexpoly-ci-ephemeral-v1\n")
    write("/etc/docker/daemon.json", json.dumps({"dns": ["202.38.64.7"], "proxies": {"http-proxy": PROXY, "https-proxy": PROXY, "no-proxy": NO_PROXY}}, indent=2) + "\n")
    write("/etc/systemd/system/docker.service.d/proxy.conf", "[Service]\n" + "\n".join(f'Environment="{line}"' for line in env_lines) + "\n")
    write("/home/runner/.docker/config.json", json.dumps({"proxies": {"default": {"httpProxy": PROXY, "httpsProxy": PROXY, "noProxy": NO_PROXY}}}, indent=2) + "\n", 0o600)
    write("/etc/systemd/resolved.conf.d/90-nexpoly.conf", "[Resolve]\nDNS=202.38.64.7\nFallbackDNS=\n")
    write("/etc/udev/rules.d/90-nexpoly-runner.rules", 'SUBSYSTEM=="virtio-ports", ATTR{name}=="org.nexpoly.runner.events", GROUP="docker", MODE="0660"\n')
    Path("/opt/hostedtoolcache").mkdir(exist_ok=True)
    run("chown", "-R", "runner:runner", "/home/runner", "/opt/actions-runner", "/opt/hostedtoolcache")
    stage("docker_start")
    run("systemctl", "daemon-reload")
    run("systemctl", "restart", "docker")
    run("docker", "version")
    run("docker", "buildx", "version")
    stage("smoke")
    run("python3", str(HERE / "smoke.py"))
    stage("seal")
    run("systemctl", "enable", "docker", "nexpoly-ci-bootstrap.service")
    for filename in (".runner", ".credentials", ".credentials_rsaparams"):
        if (Path("/opt/actions-runner") / filename).exists():
            raise RuntimeError("template contains an already registered runner")
    run("apt-get", "clean")
    # Keep signed APT indexes so each fresh VM revalidates existing metadata
    # instead of downloading every index again. apt-get clean removes .debs;
    # all job writes still live only in that VM's disposable overlay.
    shutil.rmtree("/opt/actions-runner/_diag", ignore_errors=True)
    for key in Path("/etc/ssh").glob("ssh_host_*"):
        key.unlink()
    (CONFIG / "build-mode").unlink()
    run("cloud-init", "clean", "--logs", "--machine-id", "--seed")
    run("sync")
    emit("template_ready", profile="nexpoly-ci-ephemeral-v1", runner_version=lock["runner"]["version"], smoke_passed=True)


def entrypoint() -> None:
    try:
        main()
    except Exception as error:
        # The host starts teardown after the failure event. Flush the trusted
        # build traceback to its console first so diagnostics precede teardown.
        traceback.print_exc(file=sys.stderr)
        sys.stderr.flush()
        report_failure(error)
        raise SystemExit(1) from None
    finally:
        subprocess.run(["systemctl", "poweroff", "--no-block"], check=False)


if __name__ == "__main__":
    entrypoint()
