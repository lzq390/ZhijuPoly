#!/usr/bin/env python3
"""Prepare and verify a stable complete API bundle; never starts a service.

Private identities stay in the selected configuration directory. The bundle
contains code, migration checksums, public specifications and an API dependency
lock, without task files, credentials, scientific prefixes or frontend builds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("backend/app", "backend/migrations/postgres", "polymer_md", "gpu_resource")
SCRIPTS = ("scripts/polymer_md_backend_dev.py", "scripts/polymer_md_api_services.py",
           "scripts/polymer_md_acceptance_registry.py")

# Non-code package data must be explicitly reviewed; never collect task records.
PUBLIC_DATA_FILES = {'backend/app/data/md_demo_fixture.json': 'ea0f3c85893a3cb1733faef59336cae277a066bca3f4e97c0f5f1a5839784e52', 'backend/app/data/md_demo_trajectory.npz': '94ff3c1816ae7c0d7b1c1f025caf1cf6c417eb78452fa6dfb73f0d87358a30f7', 'backend/migrations/postgres/0001_app_data_governance.sql': 'd5fc9f3d063f1cba476834f3530519b7970cd54f3c3711d05aba1f1cb2fd34f9', 'backend/migrations/postgres/0002_lab_identity_defaults.sql': '580ed6dc7c34970aabd662bc47765e9d02446c28aea1c4fa8fb2a99f05b1ac2f', 'backend/migrations/postgres/0003_runtime_postgres_cutover.sql': '0888ac9abd1b6b642f0addd42274b5408981a26c27f1140b7b656ff34ad73ce3', 'backend/migrations/postgres/0004_monomer_md_jobs.sql': 'b3ad64728f399f42b2bf9edb47ad035ac70f09fce6ced48e7b422ea74d5a7e8e', 'backend/migrations/postgres/0005_byteff2_formal_monomer_md.sql': 'c9ec808c50915b82a696ab482ed676c62bc75f00a9af21baf9e7f66b185bacb5', 'backend/migrations/postgres/0006_property_filter_records.sql': '57b103dc656334cf5e52bdc9512576a303ae0044ec5fb64eb7cba802021eceaa', 'backend/migrations/postgres/0007_polytao_jobs.sql': 'b15268a475e8daf8dd58be988a228a0440e59a31dbf11d5d6b52e0974c3daab5', 'backend/migrations/postgres/0008_polytao_backend_runtime.sql': 'd0d8b2187aad8657269600873d3d2630e30c7d72da2f6662e18ab22031deff90', 'backend/migrations/postgres/0009_monomer_md_job_leases.sql': 'ef1757a81976f351459e8257bd492aa6267cbf507c4ea85506fefa2d465d2db8', 'backend/migrations/postgres/0010_deployment_control.sql': 'f7fad29bcf1da1c6903a688a7312a67216bc11002ac558209ff56e25f69cf7cd', 'backend/migrations/postgres/0011_monomer_md_demo_steps.sql': '9a03f38329199aa707818c2099b9811d46366bafe0ddaeb39ae53bc20d0a68ed', 'backend/migrations/postgres/0012_drop_polytao_jobs.sql': 'c59b6f1efe9f926ad135379bd1a7141a7920730fa93c0e802646b1b913511728', 'backend/migrations/postgres/0013_monomer_dft_jobs.sql': 'ab633a6253887dad45103c288d54a0d02d4d69ce1f9a14c1271338d448f9acbc', 'backend/migrations/postgres/0014_monomer_md_task_queue_cancel.sql': '7d91b451371eaf10542440c8b947c9ac50b51e3d553cb205a76aca196eaf8df6', 'backend/migrations/postgres/0015_property_filter_performance.sql': 'e0159576c09d31de8a7da46f728d36553f67aa75adba344f93cdc302cf000732', 'backend/migrations/postgres/0016_monomer_polymerization_batch.sql': 'c79b22540864ee3d7cbfb66d63870da1a65dff22250cf85acf47b688dbd9c976', 'backend/migrations/postgres/0017_user_isolation_prepare.sql': '0eeef0058fbda71770627005fe617f5a56261cf25e236c4a159d0b059db76adb', 'backend/migrations/postgres/0018_user_isolation_cutover.sql': '0755d9d802eba5fd69225a34f61bc01828b32a63f815802d2905636809c573f2', 'backend/migrations/postgres/0019_service_auth_least_privilege.sql': '23f950f17126af7f73495938da41544585186e13dc44e060cfecf90bad207f23', 'backend/migrations/postgres/0020_structure_library.sql': '4f8ff9a2d9c1f7cf41140d3517f70dcb52b045fadac84a09d292bdf8095aa0b7', 'backend/migrations/postgres/0021_usage_metering.sql': '3e79f0fcefe0bdfff5e6c00f184e22cf17621d5a1cc46a5ddb6fb8f296680f62', 'backend/migrations/postgres/0022_usage_entitlements.sql': 'fe536a6256493931f6d773197a3e1e742b17bacc3c675e4fcbc32864e0747bb5', 'backend/migrations/postgres/0023_polymer_md_jobs.sql': 'e73e00e16b8fc3c72e3716dae523d02d264a3e2e6989671f6019aca3440a1379', 'backend/migrations/postgres/0024_literature_projection.sql': '2a5681e4b5000ed9cbf6ec88e1d8d7e6c6c5d0faf82270e802a3a9abb2060cf3', 'backend/migrations/postgres/manifest.json': 'df8a7efa0f8d59b27f1cf01b6f848a3040f0e9a4a05c0642b8e0df07d71cca47', 'polymer_md/capability_catalog.json': '91825ec09b9753ca0e4bdfa7d18fba0e6eb689dc77eed66a946f394abf05ce1d', 'polymer_md/user_configuration.json': '00d25084a40102d6afd56c4946143b57db9ae5ab02d002dedef0adc171293bfb'}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ordinary_ancestors(path):
    path = Path(os.path.abspath(path))
    for ancestor in (*reversed(path.parents), path):
        if ancestor.is_symlink():
            raise ValueError("API paths cannot contain symbolic link ancestors")
    return path


def private_release_parent(home):
    for directory in (home, home / "api-releases"):
        ordinary_ancestors(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("API release parent must be owned and not writable by other users")


def source_files(root):
    root = ordinary_ancestors(root)
    files = []
    for package in PACKAGES:
        directory = ordinary_ancestors(root / package)
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError("API package is missing or linked")
        for path in directory.rglob("*"):
            parts = path.relative_to(directory).parts
            if any(part.startswith(".") or part == "__pycache__" for part in parts) or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.is_symlink():
                raise ValueError("API source cannot contain symbolic links")
            if path.is_file():
                relative = path.relative_to(root).as_posix()
                if path.suffix != ".py" and (relative not in PUBLIC_DATA_FILES or sha256(path) != PUBLIC_DATA_FILES[relative]):
                    raise ValueError("API source contains unreviewed package data")
                files.append(path)
    files.extend(root / name for name in SCRIPTS)
    for path in files:
        ordinary_ancestors(path)
        if not path.is_file() or path.is_symlink():
            raise ValueError("API source must contain ordinary files")
    return sorted(set(files))



def public_dependency_bytes(path):
    """Keep hashed public pins and the project's binary-only safety directive."""
    data = path.read_text()
    if len(data.encode()) > 2 * 1024 * 1024:
        raise ValueError("API requires a bounded pinned public dependency lock")
    lines = [line.strip() for line in data.splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    binary_only = bool(lines and lines[0] == "--only-binary :all:")
    if binary_only:
        lines.pop(0)
    normalized = []
    for index, line in enumerate(lines):
        continued = line.endswith("\\")
        if continued:
            line = line[:-1].rstrip()
        if "\\" in line or (continued and index == len(lines) - 1):
            raise ValueError("API requires a pinned public dependency lock")
        normalized.append(line)
    joined = " ".join(normalized)
    records = []
    pattern = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9][A-Za-z0-9_.+!-]*(?:\s+--hash=sha256:[0-9a-f]{64})+)(?=\s+[A-Za-z0-9]|$)")
    cursor = 0
    while cursor < len(joined):
        while cursor < len(joined) and joined[cursor].isspace():
            cursor += 1
        match = pattern.match(joined, cursor)
        if match is None:
            raise ValueError("API requires a pinned public dependency lock")
        records.append(" ".join(match.group(1).split()))
        cursor = match.end()
    if not records:
        raise ValueError("API requires a nonempty pinned public dependency lock")
    if binary_only:
        records.insert(0, "--only-binary :all:")
    return ("\n".join(records) + "\n").encode()


def freeze_api(root, home, dependency_lock):
    root = ordinary_ancestors(root)
    home = ordinary_ancestors(home)
    ordinary_ancestors(home / "api-releases")
    dependency_lock = ordinary_ancestors(dependency_lock)
    if not dependency_lock.is_file() or dependency_lock.is_symlink():
        raise ValueError("API dependency lock must be an ordinary file")
    if home.resolve().is_relative_to(root.resolve()):
        raise ValueError("API release home must be outside source")
    dependency_bytes = public_dependency_bytes(dependency_lock)
    inventory = [{"path": str(path.relative_to(root)), "size_bytes": path.stat().st_size,
                  "sha256": sha256(path)} for path in source_files(root)]
    inventory.append({"path": "backend-api-requirements.lock", "size_bytes": len(dependency_bytes),
                      "sha256": hashlib.sha256(dependency_bytes).hexdigest()})
    inventory.sort(key=lambda item: item["path"])
    digest = hashlib.sha256(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    private_release_parent(home)
    release = home / "api-releases" / digest
    if release.exists():
        verify_api(release)
        return release
    staging = home / "api-releases" / (".prepare-" + uuid4().hex)
    staging.mkdir(mode=0o700, parents=True)
    try:
        for item in inventory:
            target = staging / item["path"]
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = dependency_lock if item["path"] == "backend-api-requirements.lock" else root / item["path"]
            if item["path"] == "backend-api-requirements.lock":
                target.write_bytes(dependency_bytes)
            else:
                shutil.copyfile(source, target)
            target.chmod(0o400)
        manifest = {"version": 1, "purpose": "complete_private_polymer_md_api", "source_digest": digest,
                    "dependency_lock_sha256": hashlib.sha256(dependency_bytes).hexdigest(), "files": inventory}
        (staging / "api-release.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (staging / "api-release.json").chmod(0o400)
        verify_api(staging, expected_digest=digest)
        staging.rename(release)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return release


def verify_api(release, *, expected_digest=None):
    release = ordinary_ancestors(release)
    if not release.is_dir() or release.is_symlink():
        raise ValueError("API bundle must be an ordinary directory")
    index = release / "api-release.json"
    if not index.is_file() or index.is_symlink():
        raise ValueError("API bundle inventory is missing")
    manifest = json.loads(index.read_text())
    inventory = manifest["files"]
    digest = hashlib.sha256(json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if manifest.get("version") != 1 or manifest.get("purpose") != "complete_private_polymer_md_api" or manifest.get("source_digest") != digest:
        raise ValueError("API bundle inventory identity is invalid")
    if digest != (release.name if expected_digest is None else expected_digest):
        raise ValueError("API bundle location differs from its source identity")
    actual = set()
    for path in release.rglob("*"):
        if path.is_symlink():
            raise ValueError("API bundle must not contain symbolic links")
        if path.is_file() and path != index:
            actual.add(path.relative_to(release).as_posix())
    expected = {item["path"] for item in inventory}
    if actual != expected or len(expected) != len(inventory):
        raise ValueError("API bundle has missing or unexpected files")
    for item in inventory:
        relative = Path(item["path"])
        path = release / relative
        if relative.is_absolute() or ".." in relative.parts or sha256(path) != item["sha256"] or path.stat().st_size != item["size_bytes"]:
            raise ValueError("API bundle bytes differ from their inventory")
    for item in inventory:
        path = release / item["path"]
        if path.suffix != ".py" and item["path"] != "backend-api-requirements.lock":
            if PUBLIC_DATA_FILES.get(item["path"]) != item["sha256"]:
                raise ValueError("API bundle contains unreviewed package data")
    lock = release / "backend-api-requirements.lock"
    if sha256(lock) != manifest["dependency_lock_sha256"] or public_dependency_bytes(lock) != lock.read_bytes():
        raise ValueError("API dependency lock differs from its bundle")
    return manifest


def quote(value):
    value = str(value)
    if any(character in value for character in ("\n", "\r", "\0")):
        raise ValueError("API service path contains a control character")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def api_unit(release, python, config, assets):
    verify_api(release)
    for path in (release, python, config, assets):
        if not path.is_absolute():
            raise ValueError("API service paths must be absolute")
    command = (python, release / "scripts/polymer_md_backend_dev.py", "serve-full", "--runtime", config, "--asset-root", assets)
    preflight = (python, release / "scripts/polymer_md_api_services.py", "verify", "--release", release)
    return "\n".join([
        "[Unit]", "Description=NexPoly complete private polymer MD development API", "After=default.target",
        "StartLimitIntervalSec=60", "StartLimitBurst=3", "", "[Service]", "Type=simple",
        "WorkingDirectory=" + str(release).replace("%", "%%"), "UMask=0077",
        "Environment=" + quote("PYTHONPATH=" + str(release / "backend") + ":" + str(release)),
        "Environment=PYTHONDONTWRITEBYTECODE=1", "Environment=POLYMER_MD_DEV_FRONTEND_ORIGIN=http://127.0.0.1:39872",
        "Environment=OMP_NUM_THREADS=1", "Environment=OPENBLAS_NUM_THREADS=1", "Environment=MKL_NUM_THREADS=1",
        "Environment=CUDA_VISIBLE_DEVICES=", "Environment=NVIDIA_VISIBLE_DEVICES=none",
        "ExecStartPre=" + " ".join(quote(value) for value in preflight),
        "ExecStart=" + " ".join(quote(value) for value in command),
        "Restart=on-failure", "RestartSec=5", "KillMode=control-group", "TimeoutStopSec=120",
        "", "[Install]", "WantedBy=default.target", "",
    ])


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "verify", "render-unit"))
    parser.add_argument("--home", type=Path, default=Path.home() / ".local/share/nexpoly/polymer-md-dev")
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--dependency-lock", type=Path)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.action == "freeze":
        if args.dependency_lock is None:
            parser.error("freeze requires --dependency-lock")
        release = freeze_api(args.source_root.absolute(), args.home.absolute(), args.dependency_lock.absolute())
        print(json.dumps({"release": str(release), "source_digest": release.name, "started": False}))
    elif args.action == "verify":
        if args.release is None:
            parser.error("verify requires --release")
        print(json.dumps({"verified": True, "source_digest": verify_api(args.release)["source_digest"]}))
    else:
        if any(value is None for value in (args.release, args.python, args.config, args.assets, args.output)):
            parser.error("render-unit requires release, python, config, assets and output")
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("render-unit must not overwrite a prior unit")
        unit = api_unit(args.release, args.python, args.config, args.assets)
        output = ordinary_ancestors(args.output)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(unit)
        print(json.dumps({"unit_file": str(args.output), "installed": False, "started": False}))
    return 0


def main():
    previous_umask = os.umask(0o077)
    try:
        return _main()
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
