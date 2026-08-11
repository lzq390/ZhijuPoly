"""Exact frozen-B and current-F migration fixtures for bridge tests.

The bridge is intentionally asymmetric: B ends at 0012 while F is the unique
B-plus-0013/0014/0015 extension. Reading B from its pinned Git object prevents a
current F checkout from being mistaken for the historical bridge target.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
B_SHA = "82a69ddb42bcd5c4666b5bf038d02414bccc6dde"
B_TREE = "44e4b4c398b7b84abdeb40bc02b885569aba4d8b"
MANIFEST_PATH = "backend/migrations/postgres/manifest.json"
MIGRATION_DIRECTORY = "backend/migrations/postgres"
B_MANIFEST_SHA256 = (
    "sha256:3f149c17e596c9dfe7c88245894c36e3e2d22ab67cf38375c84f2b1d7d7224fa"
)
F_MANIFEST_SHA256 = (
    "sha256:0c1ccfe4bc4515b4558e33b3c06524c6d79451a51b0bc1d2e1e14ec4a50ad26b"
)
FINAL_MIGRATION_RECORDS = [
    {
        "version": "0013_monomer_dft_jobs",
        "kind": "expand",
        "epoch": 2,
        "checksum": (
            "ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc"
        ),
        "requires_contracts": [
            {
                "version": "0012_drop_polytao_jobs",
                "checksum": (
                    "c59b6f1efe9f926ad135379bd1a7141a7920730fa93c0e802646b1b913511728"
                ),
            }
        ],
    },
    {
        "version": "0014_monomer_md_task_queue_cancel",
        "kind": "expand",
        "epoch": 2,
        "checksum": (
            "7d91b451371eaf10542440c8b947c9ac50b51e3d553cb205a76aca196eaf8df6"
        ),
        "requires_contracts": [
            {
                "version": "0012_drop_polytao_jobs",
                "checksum": (
                    "c59b6f1efe9f926ad135379bd1a7141a7920730fa93c0e802646b1b913511728"
                ),
            }
        ],
    },
    {
        "version": "0015_property_filter_performance",
        "kind": "expand",
        "epoch": 2,
        "checksum": (
            "e0159576c09d31de8a7da46f728d36553f67aa75adba344f93cdc302cf000732"
        ),
        "requires_contracts": [
            {
                "version": "0012_drop_polytao_jobs",
                "checksum": (
                    "c59b6f1efe9f926ad135379bd1a7141a7920730fa93c0e802646b1b913511728"
                ),
            }
        ],
    },
]


def _git(*arguments: str) -> bytes:
    try:
        return subprocess.run(
            ["/usr/bin/git", "-C", str(REPOSITORY_ROOT), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "HOME": "/nonexistent",
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
            },
        ).stdout
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            "exact frozen B Git object is unavailable; bridge tests require "
            f"complete history: {detail}"
        ) from exc


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _manifest_records(
    payload: bytes,
    *,
    label: str,
) -> list[dict[str, Any]]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} migration manifest is invalid JSON") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "migrations"}
        or document.get("schema_version") != 2
        or not isinstance(document.get("migrations"), list)
    ):
        raise RuntimeError(f"{label} migration manifest has an invalid shape")
    return document["migrations"]


if (
    _git("rev-parse", "--verify", f"{B_SHA}^{{tree}}")
    .decode("ascii")
    .strip()
    != B_TREE
):
    raise RuntimeError("frozen B tree differs from its reviewed identity")

B_MANIFEST_PAYLOAD = _git("show", f"{B_SHA}:{MANIFEST_PATH}")
if _sha256(B_MANIFEST_PAYLOAD) != B_MANIFEST_SHA256:
    raise RuntimeError("frozen B migration manifest differs from its reviewed digest")
B_MANIFEST_RECORDS = _manifest_records(B_MANIFEST_PAYLOAD, label="frozen B")
B_MIGRATION_PATHS = tuple(
    _git("ls-tree", "-r", "--name-only", B_SHA, MIGRATION_DIRECTORY)
    .decode("utf-8")
    .splitlines()
)
_expected_b_migration_paths = tuple(
    [
        f"{MIGRATION_DIRECTORY}/{record['version']}.sql"
        for record in B_MANIFEST_RECORDS
    ]
    + [MANIFEST_PATH]
)
if B_MIGRATION_PATHS != _expected_b_migration_paths:
    raise RuntimeError("frozen B migration SQL file set differs from its manifest")
B_MIGRATION_FILES = {
    Path(path).name: _git("show", f"{B_SHA}:{path}") for path in B_MIGRATION_PATHS
}

F_MANIFEST_PAYLOAD = (REPOSITORY_ROOT / MANIFEST_PATH).read_bytes()
if _sha256(F_MANIFEST_PAYLOAD) != F_MANIFEST_SHA256:
    raise RuntimeError("current F migration manifest differs from its reviewed digest")
F_MANIFEST_RECORDS = _manifest_records(F_MANIFEST_PAYLOAD, label="current F")
if F_MANIFEST_RECORDS != [*B_MANIFEST_RECORDS, *FINAL_MIGRATION_RECORDS]:
    raise RuntimeError(
        "current F migration manifest is not the unique frozen-B plus "
        "0013/0014/0015 extension"
    )


def materialize_b_migration_directory(destination: Path) -> None:
    """Write the exact B Git-object migration directory into a test sandbox."""

    destination.mkdir(parents=True)
    for name, payload in B_MIGRATION_FILES.items():
        (destination / name).write_bytes(payload)
