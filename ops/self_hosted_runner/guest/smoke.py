#!/usr/bin/env python3
"""Credential-free checks executed inside the trusted template build VM."""
from __future__ import annotations

import http.client
from contextlib import closing
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import time
import urllib.request

PROXY_HOST = "10.0.2.2"
PROXY_PORT = 27892
PROXY_HEALTH_URL = "https://github.com/robots.txt"
BACKGROUND_APT_TIMERS = ("apt-daily.timer", "apt-daily-upgrade.timer")
BACKGROUND_APT_UNITS = (*BACKGROUND_APT_TIMERS, "apt-daily.service", "apt-daily-upgrade.service")
APT_PERIODIC_CONFIG = "/etc/apt/apt.conf.d/99zz-nexpoly-periodic"
APT_PERIODIC_KEYS = ("Enable", "Update-Package-Lists", "Download-Upgradeable-Packages", "Unattended-Upgrade")
APT_PERIODIC_POLICY = "".join(f'APT::Periodic::{key} "0";\n' for key in APT_PERIODIC_KEYS)
NEEDRESTART_MAIN = Path("/etc/needrestart/needrestart.conf")
NEEDRESTART_CONFIG = "/etc/needrestart/conf.d/99-nexpoly-ci.conf"
NEEDRESTART_POLICY = "$nrconf{restart} = 'l';\n"
APT_LOCKS = (("/var/lib/dpkg/lock-frontend", False), ("/var/lib/dpkg/lock", False),
             ("/var/lib/apt/lists/lock", False), ("/var/cache/apt/archives/lock", False),
             ("/var/lib/apt/daily_lock", True))


def check_needrestart_policy() -> None:
    # needrestart 3.6 evaluates its main Perl config, which loads conf.d; check
    # that effective setting without invoking needrestart or scanning processes.
    # Preserve the snippet even if the package is not installed in the image.
    config = NEEDRESTART_MAIN if NEEDRESTART_MAIN.is_file() else Path(NEEDRESTART_CONFIG)
    parser = ("our (%nrconf, $LOGPREF); $nrconf{verbosity}=0; "
              "open my $fh, '<', $ARGV[0] or die 'config unavailable'; "
              "my $text = do { local $/; <$fh> }; close $fh; eval $text; "
              "die 'invalid config' if $@; exit(($nrconf{restart} // '') eq 'l' ? 0 : 1);")
    subprocess.run(["perl", "-e", parser, str(config)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)


def apt_locks_available() -> bool:
    """Probe the actual package-manager locks without creating or truncating them."""
    for filename, flock in APT_LOCKS:
        try:
            descriptor = os.open(filename, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError("Unexpected APT lock file type")
            try:
                (fcntl.flock if flock else fcntl.lockf)(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
        finally:
            os.close(descriptor)
    return True


def check_background_updates(wait_seconds: float = 0) -> None:
    """Only a quiet, permanently masked periodic updater may be sealed."""
    # Ubuntu APT 2.8.3 apt.systemd.daily exits when Periodic::Enable is zero;
    # cron's apt-compat also skips systemd systems. Explicit apt-get is unaffected.
    check_needrestart_policy()
    arguments = [item for index, key in enumerate(APT_PERIODIC_KEYS)
                 for item in (f"VALUE{index}", f"APT::Periodic::{key}")]
    policy = subprocess.run(["apt-config", "shell", *arguments], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
    if set(policy.stdout.splitlines()) != {f"VALUE{index}='0'" for index in range(len(APT_PERIODIC_KEYS))}:
        raise RuntimeError("Guest periodic APT configuration is not disabled")
    deadline = time.monotonic() + wait_seconds
    next_progress = 0.0
    while True:
        remaining = deadline - time.monotonic()
        result = subprocess.run(
            ["systemctl", "show", "--no-pager", "--property=Id,UnitFileState,ActiveState,Job", "--", *BACKGROUND_APT_UNITS],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            timeout=min(5, max(0.1, remaining)) if wait_seconds else 5)
        states = {}
        for block in result.stdout.split("\n\n"):
            values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            identity = values.get("Id")
            if identity in BACKGROUND_APT_UNITS:
                if identity in states:
                    raise RuntimeError("Duplicate periodic APT unit state")
                states[identity] = values
        if (set(states) != set(BACKGROUND_APT_UNITS)
                or any(values.get("UnitFileState") != "masked" for values in states.values())):
            raise RuntimeError("Guest periodic APT units are not permanently masked")
        quiet = all(values.get("ActiveState") in {"inactive", "failed"} and values.get("Job") == ""
                    for values in states.values())
        if quiet and apt_locks_available():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Background APT activity did not become idle before its deadline")
        if time.monotonic() >= next_progress:
            print(f"Waiting for existing APT activity to finish ({int(wait_seconds - remaining)}/{int(wait_seconds)} seconds); no process is terminated.",
                  file=sys.stderr, flush=True)
            next_progress = time.monotonic() + 30
        time.sleep(min(1, remaining))


def check_runtime_tools() -> None:
    from bootstrap import ensure_user_session, drop_privileges, runner_environment
    ensure_user_session()
    subprocess.run(["/usr/bin/python3", "-c", "import importlib.metadata; importlib.metadata.distribution('pip')"],
                   env=runner_environment(), preexec_fn=drop_privileges,
                   check=True, timeout=30)


def denied_direct(address: str, port: int) -> None:
    try:
        connection = socket.create_connection((address, port), timeout=3)
    except OSError:
        return
    connection.close()
    raise RuntimeError("Guest unexpectedly has direct network access")


def check_direct_egress() -> None:
    for port in (17891, 27891):
        denied_direct("10.0.2.2", port)  # Both SSH upstreams remain host-only.
    denied_direct("1.1.1.1", 443)  # Public HTTPS must go through the proxy.


def main() -> None:
    config = Path("/etc/nexpoly-ci-runner")
    if not (config / "build-mode").is_file():
        raise RuntimeError("Smoke checks require the trusted template build profile")
    check_background_updates()
    check_runtime_tools()
    addresses = socket.getaddrinfo("api.github.com", 443, type=socket.SOCK_STREAM)
    if not addresses or not all(ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise RuntimeError("Guest DNS did not resolve a public GitHub address")
    proxy = f"http://{PROXY_HOST}:{PROXY_PORT}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy}))
    # Transport health must not consume the shared anonymous REST API quota.
    request = urllib.request.Request(PROXY_HEALTH_URL, headers={"User-Agent": "nexpoly-runner-health/1.0"})
    with opener.open(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError("Guest proxy HTTPS health check failed")
        response.read(1024)
    # A plain HTTP proxy request avoids urllib's implicit loopback bypass.
    with closing(http.client.HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=10)) as connection:
        connection.request("GET", "http://127.0.0.1/")
        response = connection.getresponse()
        if response.status != 403:
            raise RuntimeError("Proxy did not deny a loopback destination")
        response.read(1024)
    check_direct_egress()
    subprocess.run(["docker", "compose", "version"], check=True, timeout=30)
    lock = json.loads((config / "software-lock.json").read_text())
    image = lock["smoke_image"]["reference"]
    subprocess.run(["docker", "run", "--rm", "--network", "none", image],
                   check=True, timeout=180)
    subprocess.run(["docker", "image", "rm", image], check=True, timeout=30)
    print(json.dumps({"guest_smoke": "passed", "checks": ["background_apt_disabled", "runner_identity", "user_bus", "system_python_pip", "dns", "https_proxy", "private_proxy_denied", "direct_egress_denied", "docker_compose", "docker_run"]}))


if __name__ == "__main__":
    main()
