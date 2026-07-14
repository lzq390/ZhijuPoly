#!/usr/bin/env python3
"""Probe the bounded Backend protocol catalog without logging free-form data."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

from monomer_backend_status_probe import (
    ProbeFailure,
    decode_status,
    fetch_status,
)


def _transport_entry(catalog: dict[str, object]) -> dict[str, object] | None:
    protocols = catalog.get("protocols")
    if not isinstance(protocols, list):
        raise ProbeFailure("invalid_protocol_catalog")
    matches = [
        item
        for item in protocols
        if isinstance(item, dict) and item.get("protocol") == "Transport"
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def transport_is_ready(catalog: dict[str, object]) -> bool:
    transport = _transport_entry(catalog)
    return bool(
        transport is not None
        and transport.get("supported") is True
        and transport.get("runtime_ready") is True
        and "runtime_error" in transport
        and transport.get("runtime_error") is None
    )


def safe_catalog_summary(catalog: dict[str, object]) -> str:
    protocols = catalog.get("protocols")
    transport = _transport_entry(catalog)
    summary: dict[str, object] = {
        "available": catalog.get("available") is True,
        "enabled": catalog.get("enabled") is True,
        "protocol_count": len(protocols) if isinstance(protocols, list) else 0,
    }
    if transport is not None:
        summary["transport"] = {
            "runtime_ready": transport.get("runtime_ready") is True,
            "supported": transport.get("supported") is True,
        }
    return json.dumps(summary, sort_keys=True, separators=(",", ":"))


def probe_protocols(
    url: str,
    *,
    timeout_seconds: int,
    retries: int,
    retry_delay_seconds: float = 2.0,
    fetch: Callable[[str, int], bytes] = fetch_status,
    sleep: Callable[[float], None] = time.sleep,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    require_transport_ready: bool = False,
) -> bool:
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        category = "probe_failed"
        try:
            catalog = decode_status(fetch(url, timeout_seconds))
            _transport_entry(catalog)
            if catalog.get("available") is not True:
                category = "unavailable"
                raise ProbeFailure(category)
            if require_transport_ready and not transport_is_ready(catalog):
                category = "transport_unavailable"
                raise ProbeFailure(category)
        except ProbeFailure as exc:
            category = exc.kind
            print(
                f"Monomer-MD protocols attempt {attempt}/{attempts} failed: "
                f"{category}.",
                file=stderr,
            )
            if attempt < attempts:
                sleep(retry_delay_seconds)
            continue

        print(safe_catalog_summary(catalog), file=stdout)
        return True

    print(
        f"Monomer-MD protocols remained unavailable after {attempts} attempts.",
        file=stderr,
    )
    return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=40)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay-seconds", type=float, default=2.0)
    parser.add_argument("--require-transport-ready", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not 1 <= args.timeout_seconds <= 300:
        parser.error("--timeout-seconds must be between 1 and 300")
    if not 0 <= args.retries <= 3:
        parser.error("--retries must be between 0 and 3")
    if not 0 <= args.retry_delay_seconds <= 30:
        parser.error("--retry-delay-seconds must be between 0 and 30")
    return 0 if probe_protocols(
        args.url,
        timeout_seconds=args.timeout_seconds,
        retries=args.retries,
        retry_delay_seconds=args.retry_delay_seconds,
        require_transport_ready=args.require_transport_ready,
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
