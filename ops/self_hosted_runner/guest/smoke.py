#!/usr/bin/env python3
"""Credential-free checks executed inside the trusted template build VM."""
from __future__ import annotations

import http.client
from contextlib import closing
import ipaddress
import json
from pathlib import Path
import socket
import subprocess
import urllib.request

PROXY_HOST = "10.0.2.2"
PROXY_PORT = 27892
PROXY_HEALTH_URL = "https://github.com/robots.txt"


def denied_direct(address: str, port: int) -> None:
    try:
        connection = socket.create_connection((address, port), timeout=3)
    except OSError:
        return
    connection.close()
    raise RuntimeError("Guest unexpectedly has direct network access")


def main() -> None:
    config = Path("/etc/nexpoly-ci-runner")
    if not (config / "build-mode").is_file():
        raise RuntimeError("Smoke checks require the trusted template build profile")
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
    denied_direct("10.0.2.2", 27891)  # SSH's upstream proxy must remain host-only.
    denied_direct("1.1.1.1", 443)  # All public HTTPS must go through the proxy.
    subprocess.run(["docker", "compose", "version"], check=True, timeout=30)
    lock = json.loads((config / "software-lock.json").read_text())
    image = lock["smoke_image"]["reference"]
    subprocess.run(["docker", "run", "--rm", "--network", "none", image],
                   check=True, timeout=180)
    subprocess.run(["docker", "image", "rm", image], check=True, timeout=30)
    print(json.dumps({"guest_smoke": "passed", "checks": ["dns", "https_proxy", "private_proxy_denied", "direct_egress_denied", "docker_compose", "docker_run"]}))


if __name__ == "__main__":
    main()
