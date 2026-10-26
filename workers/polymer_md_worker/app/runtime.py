"""Pinned, private scientific runtime inventory and integrity checks."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

SOURCE_SHA = "8834b9071ae93d9af36558a3c24545dd52e5ba1a"
PATCH_VERSION = "nexpoly-md-10"
LEGACY_ADAPTER_NAMES = ("scientific_runner.py", "scientific_analysis.py", "runtime.py")
V3_ADAPTER_NAMES = (*LEGACY_ADAPTER_NAMES, "native_execution.py")
V4_ADAPTER_NAMES = (*V3_ADAPTER_NAMES, "model_execution.py")
V5_ADAPTER_NAMES = (*V4_ADAPTER_NAMES, "posteq_execution.py")
ADAPTER_NAMES = (*V5_ADAPTER_NAMES, "generated_input.py")
ROOT = Path(__file__).resolve().parents[3]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def adapter_fingerprint() -> str:
    h = hashlib.sha256()
    for name in ADAPTER_NAMES:
        path = Path(__file__).with_name(name)
        h.update(name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def _probe(path: str, adapter: str) -> dict:
    errors: list[str] = []
    try:
        manifest = json.loads(Path(path).read_text())
        unsigned = {key: value for key, value in manifest.items() if key != "runtime_fingerprint"}
        fingerprint = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if fingerprint != manifest.get("runtime_fingerprint"):
            errors.append("runtime_manifest_fingerprint_invalid")
        if manifest.get("source_sha") != SOURCE_SHA:
            errors.append("radonpy_commit_mismatch")
        if manifest.get("patch_version") != PATCH_VERSION or manifest.get("adapter_fingerprint") != adapter:
            errors.append("adapter_changed_reseal_runtime")
        for item in manifest.get("verified_files", []):
            target = Path(item["path"])
            if not target.is_file() or target.is_symlink() or sha256(target) != item["sha256"]:
                errors.append("runtime_file_integrity:" + target.name)
        if not manifest.get("verified_files"):
            errors.append("empty_runtime_inventory")
        if not manifest.get("engine_probe", {}).get("passed"):
            errors.append("engine_probe_failed")
        return {**manifest, "ready": not errors, "errors": errors}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"ready": False, "errors": ["runtime_manifest_invalid"], "error_type": type(exc).__name__}


def probe_runtime(manifest_path: str | Path | None = None) -> dict:
    path = Path(manifest_path or os.environ.get("POLYMER_MD_RUNTIME_MANIFEST", ROOT / ".runtime/polymer-md/manifest.json"))
    if not path.is_file() or path.is_symlink():
        return {"ready": False, "errors": ["runtime_manifest_missing"], "source_sha": SOURCE_SHA, "patch_version": PATCH_VERSION}
    # File metadata is not a content identity: this host can report identical
    # mtime/ctime/size for rapid same-size rewrites. Recheck all sealed bytes on
    # every readiness request; the OS page cache still amortizes disk reads.
    try:
        return _probe(str(path.resolve()), adapter_fingerprint())
    except (OSError, ValueError, KeyError, TypeError):
        return {"ready": False, "errors": ["runtime_inventory_unavailable"]}
