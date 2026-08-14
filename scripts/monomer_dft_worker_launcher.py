#!/usr/bin/env python3
"""Fail-closed launcher for the governed production Monomer-DFT Worker."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Mapping


SOURCE_ROOT = Path("/data/lzq/gith/nexpoly")
RUNTIME_ROOT = Path("/data/lzq/gith/nexpoly-runtime")
BASE_PYTHON_SHA256 = "sha256:1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118"
UV_SHA256 = "sha256:f985abdfdbef9a69f47f5a88f800eae0488bdcb0d7868f5cc1e0aa3e11a8f47e"
PIP_INVENTORY_SHA256 = "sha256:645e76321f3088ac750fb5d96eda0f21cca88d21311916874c4c8d73e2146b7b"
RELEASE_RE = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class LauncherError(RuntimeError):
    """A public-safe launcher validation failure."""


def _pinned_payload(
    path: Path, *, allowed_modes: frozenset[int], maximum_bytes: int
) -> tuple[int, bytes, os.stat_result]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) not in allowed_modes
            or before.st_nlink != 1
            or before.st_size < 1
            or before.st_size > maximum_bytes
        ):
            raise LauncherError("production monomer DFT file is unsafe")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        path_after = path.lstat()
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            len(payload) != before.st_size
            or any(getattr(before, field) != getattr(after, field) for field in fields)
            or any(
                getattr(before, field) != getattr(path_after, field)
                for field in fields
            )
        ):
            raise LauncherError("production monomer DFT file changed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, payload, before
    except BaseException:
        os.close(descriptor)
        raise


def _pinned_digest(
    path: Path,
    *,
    expected_uid: int,
    allowed_modes: frozenset[int],
    maximum_bytes: int,
) -> tuple[str, os.stat_result]:
    """Hash a stable path-bound inode without retaining its payload in RAM."""

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != expected_uid
            or stat.S_IMODE(before.st_mode) not in allowed_modes
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > maximum_bytes
        ):
            raise LauncherError("production monomer DFT file is unsafe")
        digest = hashlib.sha256()
        observed_size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            observed_size += len(chunk)
            if observed_size > maximum_bytes:
                raise LauncherError("production monomer DFT file exceeds its bound")
            digest.update(chunk)
        after = os.fstat(descriptor)
        path_after = path.lstat()
        fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if (
            observed_size != before.st_size
            or any(getattr(before, field) != getattr(after, field) for field in fields)
            or any(
                getattr(before, field) != getattr(path_after, field)
                for field in fields
            )
        ):
            raise LauncherError("production monomer DFT file changed")
        return "sha256:" + digest.hexdigest(), before
    finally:
        os.close(descriptor)


def _runtime_inventory(root: Path) -> str:
    records: list[dict[str, object]] = []
    allowed_links = {
        "venv/bin/python": "/usr/bin/python3.12",
        "venv/bin/python3": "python",
        "venv/bin/python3.12": "python",
        "venv/lib64": "lib",
    }
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative in {"READY.json", ".preparing.json"}:
            continue
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if metadata.st_uid != os.geteuid():
            raise LauncherError("production monomer DFT runtime owner differs")
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path)
            if allowed_links.get(relative) != target:
                raise LauncherError("production monomer DFT runtime link differs")
            records.append(
                {
                    "path": relative,
                    "kind": "symlink",
                    "uid": metadata.st_uid,
                    "mode": mode,
                    "target": target,
                }
            )
        elif stat.S_ISDIR(metadata.st_mode):
            if mode & 0o022:
                raise LauncherError("production monomer DFT runtime mode differs")
            records.append(
                {
                    "path": relative,
                    "kind": "directory",
                    "uid": metadata.st_uid,
                    "mode": mode,
                }
            )
        elif stat.S_ISREG(metadata.st_mode):
            if mode & 0o022 or metadata.st_nlink != 1:
                raise LauncherError("production monomer DFT runtime file differs")
            digest, stable = _pinned_digest(
                path,
                expected_uid=os.geteuid(),
                allowed_modes=frozenset({mode}),
                maximum_bytes=2 * 1024 * 1024 * 1024,
            )
            records.append(
                {
                    "path": relative,
                    "kind": "file",
                    "uid": stable.st_uid,
                    "mode": mode,
                    "size": stable.st_size,
                    "sha256": digest,
                }
            )
        else:
            raise LauncherError("production monomer DFT runtime has a special file")
    encoded = json.dumps(
        records, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _git_identity() -> tuple[str, str]:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(SOURCE_ROOT), "rev-parse", "HEAD", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
        env={
            "HOME": "/home/devuser",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
    )
    values = result.stdout.splitlines()
    if len(values) != 2:
        raise LauncherError("production source identity is unavailable")
    return values[0], values[1]


def validate(environment: Mapping[str, str]) -> int:
    release = environment.get("MONOMER_DFT_RELEASE_SHA", "")
    if RELEASE_RE.fullmatch(release) is None:
        raise LauncherError("production monomer DFT release identity is invalid")
    runtime_root = RUNTIME_ROOT / "worker-venvs/dft" / release
    manifest = runtime_root / "runtime.json"
    python = Path(environment.get("MONOMER_DFT_PYTHON", ""))
    script = SOURCE_ROOT / "workers/monomer_dft_worker/run_host_worker.sh"
    for path in (SOURCE_ROOT, runtime_root):
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or path.is_symlink()
            or path.resolve(strict=True) != path
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
        ):
            raise LauncherError("production monomer DFT path is unsafe")
    manifest_descriptor, manifest_payload, _manifest_metadata = _pinned_payload(
        manifest, allowed_modes=frozenset({0o600}), maximum_bytes=1024 * 1024
    )
    os.close(manifest_descriptor)
    script_descriptor, _script_payload, _script_metadata = _pinned_payload(
        script, allowed_modes=frozenset({0o700, 0o755}), maximum_bytes=1024 * 1024
    )
    try:
        expected_python = runtime_root / "venv/bin/python"
        if (
            python != expected_python
            or not python.is_symlink()
            or os.readlink(python) != "/usr/bin/python3.12"
            or python.resolve(strict=True) != Path("/usr/bin/python3.12")
            or not os.access(python, os.X_OK)
        ):
            raise LauncherError("production monomer DFT Python differs")
        python_target = python.resolve(strict=True)
        python_digest, pinned_python_metadata = _pinned_digest(
            python_target,
            expected_uid=0,
            allowed_modes=frozenset({0o755}),
            maximum_bytes=64 * 1024 * 1024,
        )
        if (
            not stat.S_ISREG(pinned_python_metadata.st_mode)
            or pinned_python_metadata.st_uid != 0
            or pinned_python_metadata.st_mode & 0o022
            or python_digest != BASE_PYTHON_SHA256
        ):
            raise LauncherError("production monomer DFT Python target is unsafe")
        if (
            "sha256:" + hashlib.sha256(manifest_payload).hexdigest()
            != environment.get("MONOMER_DFT_RUNTIME_CONTRACT_SHA256")
        ):
            raise LauncherError("production monomer DFT runtime contract differs")
        try:
            contract = json.loads(manifest_payload)
        except (ValueError, RecursionError) as exc:
            raise LauncherError(
                "production monomer DFT runtime contract is invalid"
            ) from exc
        source_sha, source_tree = _git_identity()
        wheelhouse_inventory = (
            contract.get("wheelhouse_inventory_sha256")
            if isinstance(contract, dict)
            else None
        )
        runtime_inventory = environment.get(
            "MONOMER_DFT_RUNTIME_INVENTORY_SHA256", ""
        )
        if (
            not isinstance(contract, dict)
            or RELEASE_RE.fullmatch(source_sha) is None
            or RELEASE_RE.fullmatch(source_tree) is None
            or source_sha != release
            or contract.get("schema_version") != 1
            or contract.get("release") != release
            or contract.get("source_tree") != source_tree
            or contract.get("python") != "3.12"
            or contract.get("uv") != "0.11.21"
            or contract.get("base_python_sha256") != BASE_PYTHON_SHA256
            or contract.get("uv_sha256") != UV_SHA256
            or contract.get("pip_inventory_sha256") != PIP_INVENTORY_SHA256
            or not isinstance(wheelhouse_inventory, str)
            or DIGEST_RE.fullmatch(wheelhouse_inventory) is None
            or DIGEST_RE.fullmatch(runtime_inventory) is None
            or environment.get("NEXPOLY_DFT_GPU_GUARD_MODE")
            not in {"enforce", "observe"}
            or environment.get("NEXPOLY_DFT_GPU_DEVICE") != "2"
            or environment.get("MONOMER_DFT_MAX_CONCURRENT_JOBS") != "1"
            or environment.get("MONOMER_DFT_MAX_QUEUED_JOBS") != "8"
        ):
            raise LauncherError("production monomer DFT runtime identity differs")
        if _runtime_inventory(runtime_root) != runtime_inventory:
            raise LauncherError("production monomer DFT runtime inventory differs")
        os.set_inheritable(script_descriptor, True)
        return script_descriptor
    except BaseException:
        os.close(script_descriptor)
        raise


def main() -> int:
    try:
        script_descriptor = validate(os.environ)
        environment = dict(os.environ)
        environment["NEXPOLY_DFT_GOVERNED_FD_LAUNCH"] = "1"
        os.execve(
            "/usr/bin/bash",
            ["/usr/bin/bash", f"/proc/self/fd/{script_descriptor}"],
            environment,
        )
    except (LauncherError, OSError, subprocess.SubprocessError) as exc:
        print(f"monomer-dft-worker-launcher: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
