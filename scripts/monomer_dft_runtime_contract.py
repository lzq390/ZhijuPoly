#!/usr/bin/env python3
"""Immutable native AIMNet runtime identity shared by deployment controls.

The production readiness command is installed as part of a standalone control
release and cannot trust a mutable development checkout at validation time.
This module therefore carries the exact, reviewed identity derived from
``workers/monomer_dft_worker/aimnet-source.lock.json``.  Repository CI verifies
that the two representations remain byte-for-byte equivalent.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


SCHEMA_VERSION = 1


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def canonical_json_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


# ``models_sha256`` is the canonical JSON digest of the complete ordered
# ``models`` array in aimnet-source.lock.json, not merely the six checkpoint
# file hashes.  This also binds aliases, registry keys, families, URLs and the
# registry/cache audit hashes.
RUNTIME_CONTRACT: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "python_minor": "3.12",
    "uv_version": "0.11.21",
    "build_lock_sha256": (
        "sha256:"
        "21165c6077689ee6a99c333d9159cd0969a9a93b0c850bfbae774bb6c2333e76"
    ),
    "source": {
        "repository_url": "https://github.com/isayevlab/aimnetcentral.git",
        "commit": "9a6c56440349bccbb7ac0630a0622f9c584f894e",
        "tree": "fd28c0f8bf2d0e513aad24032228927140d6783c",
        "archive_inventory_sha256": (
            "sha256:"
            "abf724d01f2dabab12ee29381d53e4646f0b4a04c8f435c03f21b3d3ab19936d"
        ),
        "package_name": "aimnet",
        "package_version": "0.2.0.post1.dev41+g9a6c56440",
        "source_date_epoch": 1782945961,
    },
    "wheel": {
        "filename": "aimnet-0.2.0.post1.dev41+g9a6c56440-py3-none-any.whl",
        "sha256": (
            "sha256:"
            "9cb53c47230f3746872a34948480b1228f98258026d88b338111cf90f8d28557"
        ),
        "file_count": 47,
        "inventory_sha256": (
            "sha256:"
            "54ad7842d215f0430c9d376c6c8d550925f2ede9b880e8969dadd72b5b2471ce"
        ),
        "record_path": (
            "aimnet-0.2.0.post1.dev41+g9a6c56440.dist-info/RECORD"
        ),
        "record_sha256": (
            "sha256:"
            "54b23e6ff673423e19865c702ab174910f39259a8fcdbfd670e19303d6909d61"
        ),
    },
    "registry_sha256": (
        "sha256:"
        "000dbcb1d04e058c4b283fd39d96e24f79e09c4f9c4c16fd5dba5386cf1565c5"
    ),
    "models_sha256": (
        "sha256:"
        "82644f5e3dee45cc66bee9f442f33aecef6b16a2cc279ecd1b4a35896c52afad"
    ),
}

RUNTIME_CONTRACT_SHA256 = canonical_json_digest(RUNTIME_CONTRACT)
