#!/usr/bin/env python3
"""Probe the local Monomer-MD backend status without leaking response details."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable
from typing import TextIO
from urllib.parse import urlparse


MAX_RESPONSE_BYTES = 64 * 1024
SAFE_STATUS_FIELDS = (
    "enabled",
    "available",
    "worker_status",
    "worker_mode",
    "db_configured",
    "byteff2_root_exists",
    "runtime_ready",
    "active_jobs",
    "database_active_jobs",
    "oldest_active_heartbeat_age_seconds",
    "max_active_jobs",
    "accepting_jobs",
    "draining",
    "busy",
    "can_submit",
)
SAFE_TEXT_VALUES = {
    "worker_mode": {"demo", "mock", "real", "unknown"},
    "worker_status": {"degraded", "ok", "unknown", "unreachable"},
}


class ProbeFailure(RuntimeError):
    """A classified probe failure whose message is safe for deployment logs."""

    def __init__(self, kind: str, detail: int | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


def _validate_local_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ProbeFailure("invalid_url")


def fetch_status(url: str, timeout_seconds: int) -> bytes:
    """Fetch one bounded response with an end-to-end wall-clock deadline."""

    _validate_local_url(url)
    command = [
        "curl",
        "--disable",
        "--fail",
        "--silent",
        "--show-error",
        "--noproxy",
        "*",
        "--proto",
        "=http",
        "--connect-timeout",
        str(min(timeout_seconds, 5)),
        "--max-time",
        str(timeout_seconds),
        "--max-filesize",
        str(MAX_RESPONSE_BYTES),
        "--header",
        "Accept: application/json",
        "--write-out",
        "\n%{http_code}",
        url,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout_seconds + 2,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ProbeFailure("timeout") from None
    except FileNotFoundError:
        raise ProbeFailure("curl_missing") from None
    except OSError:
        raise ProbeFailure("network_error") from None

    payload, separator, status_text = completed.stdout.rpartition(b"\n")
    status_code = int(status_text) if separator and status_text.isdigit() else None
    if completed.returncode == 28:
        raise ProbeFailure("timeout")
    if completed.returncode == 63:
        raise ProbeFailure("response_too_large")
    if completed.returncode != 0:
        if status_code is not None and status_code >= 100 and status_code != 200:
            raise ProbeFailure("http_error", status_code)
        raise ProbeFailure("network_error")
    if status_code != 200:
        if status_code is not None and status_code >= 100:
            raise ProbeFailure("http_error", status_code)
        raise ProbeFailure("invalid_http_status")
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ProbeFailure("response_too_large")
    return payload


def decode_status(payload: bytes) -> dict[str, object]:
    """Decode an object response without reflecting its contents on failure."""

    if len(payload) > MAX_RESPONSE_BYTES:
        raise ProbeFailure("response_too_large")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ProbeFailure("invalid_json") from None
    if not isinstance(decoded, dict):
        raise ProbeFailure("invalid_shape")
    return decoded


def safe_status_summary(status: dict[str, object]) -> str:
    """Return only bounded scalar fields approved for deployment logs."""

    summary: dict[str, object] = {}
    for field in SAFE_STATUS_FIELDS:
        value = status.get(field)
        if isinstance(value, bool):
            summary[field] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            summary[field] = value
        elif isinstance(value, str) and value in SAFE_TEXT_VALUES.get(field, set()):
            summary[field] = value
    return json.dumps(summary, sort_keys=True, separators=(",", ":"))


def _failure_label(failure: ProbeFailure) -> str:
    if failure.kind == "http_error" and failure.detail is not None:
        return f"HTTP {failure.detail}"
    return {
        "invalid_json": "invalid JSON",
        "invalid_http_status": "invalid HTTP status",
        "invalid_shape": "invalid JSON shape",
        "invalid_url": "non-loopback URL rejected",
        "curl_missing": "curl is unavailable",
        "network_error": "network error",
        "response_too_large": "response exceeded 64 KiB",
        "timeout": "request timed out",
        "unavailable": "status reported unavailable",
    }.get(failure.kind, "probe failed")


def probe_status(
    url: str,
    *,
    timeout_seconds: int,
    retries: int,
    retry_delay_seconds: float = 2.0,
    fetch: Callable[[str, int], bytes] = fetch_status,
    sleep: Callable[[float], None] = time.sleep,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> bool:
    """Return true once the endpoint reports ``available is true``."""

    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        try:
            status = decode_status(fetch(url, timeout_seconds))
            if status.get("available") is not True:
                raise ProbeFailure("unavailable")
        except ProbeFailure as exc:
            print(
                f"Monomer-MD status attempt {attempt}/{attempts} failed: "
                f"{_failure_label(exc)}.",
                file=stderr,
            )
            if attempt < attempts:
                sleep(retry_delay_seconds)
            continue

        print(safe_status_summary(status), file=stdout)
        return True

    print(
        f"Monomer-MD status remained unavailable after {attempts} attempts.",
        file=stderr,
    )
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=40)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay-seconds", type=float, default=2.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.timeout_seconds <= 300:
        parser.error("--timeout-seconds must be between 1 and 300")
    if not 0 <= args.retries <= 3:
        parser.error("--retries must be between 0 and 3")
    if not 0 <= args.retry_delay_seconds <= 30:
        parser.error("--retry-delay-seconds must be between 0 and 30")

    return 0 if probe_status(
        args.url,
        timeout_seconds=args.timeout_seconds,
        retries=args.retries,
        retry_delay_seconds=args.retry_delay_seconds,
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
