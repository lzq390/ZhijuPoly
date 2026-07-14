#!/usr/bin/env python3
"""Validate NexPoly's hashed Python locks and GPU runtime split."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from pathlib import Path
import re


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PYTORCH_BASE = (
    "pytorch/pytorch:2.6.0-cuda11.8-cudnn9-runtime@"
    "sha256:2428b92ebbaeceba5572b98c18c8a94e43162bead6e88588ad54471147c58a20"
)
SYSTEM_VERSIONS = {
    "torch": "2.6.0+cu118",
    "torchvision": "0.21.0+cu118",
}
SYSTEM_HASHES = {
    "torch": "3e73419aab6dbcd888a3cc6a00d1f52f5950d918d7289ea6aeae751346613edc",
    "torchvision": "5ebe0267c872ac55b387008f772052bbf1f2fdfdd8afb011d4751e124759295e",
}
BASE_RUNTIME_PACKAGES = {
    "torch",
    "torchvision",
    "triton",
    "nvidia-cublas-cu11",
    "nvidia-cuda-cupti-cu11",
    "nvidia-cuda-nvrtc-cu11",
    "nvidia-cuda-runtime-cu11",
    "nvidia-cudnn-cu11",
    "nvidia-cufft-cu11",
    "nvidia-curand-cu11",
    "nvidia-cusolver-cu11",
    "nvidia-cusparse-cu11",
    "nvidia-nccl-cu11",
    "nvidia-nvtx-cu11",
}
REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^]]+\])?)"
    r"==(?P<version>[^\s;\\]+)(?:\s*;[^\\]+)?\s*\\?$"
)
HASH = re.compile(r"--hash=sha256:([0-9a-f]{64})(?:\s*\\)?$")


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.split("[", 1)[0]).lower()


def requirement_blocks(text: str) -> list[tuple[str, str, list[str]]]:
    """Return exact requirement blocks as (normalized name, version, lines)."""

    lines = text.splitlines()
    starts: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        if not line or line[0].isspace() or line.startswith("#") or line.startswith("--"):
            continue
        match = REQUIREMENT.match(line)
        if match is None:
            raise ValueError(f"line {index + 1}: lock entry is not an exact == pin: {line}")
        starts.append((index, match))

    blocks: list[tuple[str, str, list[str]]] = []
    for position, (start, match) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        blocks.append(
            (
                normalize_name(match.group("name")),
                match.group("version"),
                lines[start:end],
            )
        )
    return blocks


def validate_lock_hashes(path: Path) -> list[str]:
    failures: list[str] = []
    text = ""
    try:
        text = path.read_text(encoding="utf-8")
        blocks = requirement_blocks(text)
    except (OSError, ValueError) as exc:
        return [f"{path}: {exc}"]
    if not blocks:
        return [f"{path}: lock contains no requirements"]
    for name, version, lines in blocks:
        hashes = [match.group(1) for line in lines if (match := HASH.search(line))]
        if not hashes:
            failures.append(f"{path}: {name}=={version} has no SHA256 hash")
        elif len(hashes) != len(set(hashes)):
            failures.append(f"{path}: {name}=={version} contains duplicate SHA256 hashes")
    unexpected_directives = [
        line for line in text.splitlines() if line.startswith("--") and line != "--only-binary :all:"
    ]
    if unexpected_directives:
        failures.append(f"{path}: lock embeds unreviewed pip options: {unexpected_directives}")
    return failures


def versions(path: Path) -> dict[str, str]:
    return {name: version for name, version, _lines in requirement_blocks(path.read_text(encoding="utf-8"))}


def input_versions(path: Path) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            raise ValueError(f"{path}:{number}: requirement-file directives are not allowed: {line}")
        match = REQUIREMENT.match(line)
        if match is None:
            raise ValueError(f"{path}:{number}: expected an exact == pin: {line}")
        parsed[normalize_name(match.group("name"))] = match.group("version")
    return parsed


def validate_system_lock(root: Path) -> list[str]:
    failures: list[str] = []
    input_path = root / "backend" / "requirements-system.in"
    lock_path = root / "backend" / "requirements-system.lock"
    try:
        declared = input_versions(input_path)
        locked_blocks = requirement_blocks(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [str(exc)]

    locked = {name: version for name, version, _lines in locked_blocks}
    if declared != SYSTEM_VERSIONS:
        failures.append(f"{input_path}: expected only {SYSTEM_VERSIONS}, got {declared}")
    if locked != SYSTEM_VERSIONS:
        failures.append(f"{lock_path}: expected only {SYSTEM_VERSIONS}, got {locked}")
    for name, _version, lines in locked_blocks:
        found = [match.group(1) for line in lines if (match := HASH.search(line))]
        expected = [SYSTEM_HASHES[name]] if name in SYSTEM_HASHES else []
        if found != expected:
            failures.append(f"{lock_path}: {name} must use the reviewed CPython 3.11 Linux wheel hash")
    return failures


def validate_backend_split(root: Path) -> list[str]:
    failures: list[str] = []
    runtime_input = root / "backend" / "requirements-runtime.in"
    app_lock = root / "backend" / "requirements.lock"
    try:
        runtime_declared = input_versions(runtime_input)
        runtime_names = set(runtime_declared)
        app_versions = versions(app_lock)
    except (OSError, ValueError) as exc:
        return [str(exc)]

    forbidden_input = runtime_names & (BASE_RUNTIME_PACKAGES | {"pytest", "pytest-asyncio"})
    if forbidden_input:
        failures.append(
            f"{runtime_input}: runtime input contains base-image or test packages: "
            f"{', '.join(sorted(forbidden_input))}"
        )

    forbidden_lock = set(app_versions) & (BASE_RUNTIME_PACKAGES | {"pytest", "pytest-asyncio"})
    if forbidden_lock:
        failures.append(
            f"{app_lock}: app lock duplicates base-image or test packages: "
            f"{', '.join(sorted(forbidden_lock))}"
        )
    for package, expected in runtime_declared.items():
        if app_versions.get(package) != expected:
            failures.append(f"{app_lock}: direct input must remain {package}=={expected}")
    for package, expected in {"scikit-learn": "1.8.0", "transformers": "4.57.6"}.items():
        if app_versions.get(package) != expected:
            failures.append(f"{app_lock}: expected {package}=={expected}")
    app_text = app_lock.read_text(encoding="utf-8")
    if "autogenerated by uv" in app_text or "uv pip compile" in app_text:
        failures.append(f"{app_lock}: app lock must be generated by pip-tools, not uv")
    if "pip-compile" not in app_text:
        failures.append(f"{app_lock}: missing pip-compile provenance header")
    if "cu121" in app_text or re.search(r"nvidia-[a-z0-9-]+-cu12", app_text):
        failures.append(f"{app_lock}: CUDA 12 packages must not be mixed into the CUDA 11.8 runtime")
    return failures


def lock_paths(root: Path) -> Iterable[Path]:
    backend = root / "backend"
    yield backend / "requirements.lock"
    yield backend / "requirements-ci.lock"
    yield backend / "requirements-system.lock"
    yield backend / "requirements-legacy.lock"
    yield from sorted((root / "workers").glob("*/requirements*.lock"))


def validate_tooling(root: Path) -> list[str]:
    failures: list[str] = []
    compile_text = (root / "scripts" / "compile_requirements.sh").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    if 'PIP_TOOLS_VERSION="7.5.0"' not in compile_text:
        failures.append("scripts/compile_requirements.sh must require pip-tools==7.5.0")
    if re.search(r"(^|\s)uv(?:\s|$)", compile_text):
        failures.append("scripts/compile_requirements.sh must not use uv")
    for required in (
        "--constraint=backend/requirements-system.in",
        "--unsafe-package=\"$package\"",
        "scripts/ci/validate_dependency_locks.py",
    ):
        if required not in compile_text:
            failures.append(f"scripts/compile_requirements.sh is missing: {required}")
    for required in (
        f"FROM {PYTORCH_BASE}",
        "COPY backend/requirements-system.lock /tmp/requirements-system.lock",
        "pip install --no-index --no-deps --require-hashes",
        "torch.version.cuda == '11.8'",
        'WEB_CONCURRENCY=1',
    ):
        if required not in dockerfile:
            failures.append(f"Dockerfile is missing immutable runtime assertion: {required}")
    if "download.pytorch.org" in dockerfile:
        failures.append("Dockerfile must not reinstall Torch from a package index")
    return failures


def validate(root: Path = REPOSITORY_ROOT) -> list[str]:
    failures: list[str] = []
    for path in lock_paths(root):
        if not path.is_file():
            failures.append(f"missing dependency lock: {path}")
            continue
        failures.extend(validate_lock_hashes(path))

    generated = (
        root / "backend" / "requirements.lock",
        root / "backend" / "requirements-ci.lock",
        root / "workers" / "monomer_md_worker" / "requirements.lock",
        root / "workers" / "monomer_md_worker" / "requirements-ci.lock",
    )
    for path in generated:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if "pip-compile" not in text or "autogenerated by uv" in text:
                failures.append(f"{path}: generated lock lacks pip-tools provenance")

    failures.extend(validate_system_lock(root))
    failures.extend(validate_backend_split(root))
    failures.extend(validate_tooling(root))
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    args = parser.parse_args()
    failures = validate(args.root.resolve())
    if failures:
        for failure in failures:
            print(f"dependency lock policy: {failure}")
        return 1
    print("validated hashed Python locks and the immutable CUDA 11.8 runtime split")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
