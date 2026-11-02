"""Observe and revalidate the explicitly sealed candidate DFT build tools.

The science source lock still requires uv 0.11.21. Only that already audited
binary may be reused from a frozen production release; Python and pip must be
the current root-owned system installation, whose identity is measured here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess

PYTHON = Path('/usr/bin/python3.12')
PIP_ROOT = Path('/usr/lib/python3/dist-packages/pip')
RUNTIME_ROOT = Path('/data/lzq/gith/nexpoly-runtime')
UV_SHA256 = 'sha256:f985abdfdbef9a69f47f5a88f800eae0488bdcb0d7868f5cc1e0aa3e11a8f47e'
FIELDS = frozenset({'schema_version', 'python', 'python_sha256', 'pip_root',
    'pip_inventory_sha256', 'uv', 'uv_sha256', 'uv_uid', 'uv_mode', 'uv_version'})


class ToolchainError(RuntimeError):
    pass


def observe(uv: Path) -> dict:
    from scripts.monomer_dft_worker_launcher import _pinned_digest
    from scripts.pull_deploy_controller import directory_inventory_digest

    expected_parent = RUNTIME_ROOT / 'worker-venvs/dft'
    if (uv.parent.name != 'tools' or uv.name != 'uv'
            or uv.parent.parent.parent != expected_parent
            or re.fullmatch(r'[0-9a-f]{40}', uv.parent.parent.name) is None
            or uv.resolve(strict=True) != uv):
        raise ToolchainError('Candidate uv must be the audited frozen release tool')
    for path in (uv.parent.parent, uv.parent):
        metadata = path.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700):
            raise ToolchainError('Frozen candidate uv directory is unsafe')
    uv_digest, _ = _pinned_digest(uv, expected_uid=os.geteuid(),
        allowed_modes=frozenset({0o700}), maximum_bytes=256 * 1024 * 1024)
    if uv_digest != UV_SHA256:
        raise ToolchainError('Candidate uv differs from the audited scientific toolchain')
    clean_env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'}
    version = subprocess.run([str(uv), '--version'], check=True, capture_output=True,
        text=True, env=clean_env).stdout.strip()
    if not version.startswith('uv 0.11.21 '):
        raise ToolchainError('Candidate uv version differs from the science lock')
    if PYTHON.resolve(strict=True) != PYTHON or PIP_ROOT.resolve(strict=True) != PIP_ROOT:
        raise ToolchainError('Candidate system toolchain path is not canonical')
    python_digest, _ = _pinned_digest(PYTHON, expected_uid=0,
        allowed_modes=frozenset({0o755}), maximum_bytes=64 * 1024 * 1024)
    minor = subprocess.run([str(PYTHON), '-I', '-c',
        "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        check=True, capture_output=True, text=True, env=clean_env).stdout.strip()
    if minor != '3.12':
        raise ToolchainError('Candidate system Python must be 3.12')
    for path in (PIP_ROOT, *PIP_ROOT.rglob('*')):
        metadata = path.lstat()
        if (metadata.st_uid != 0 or metadata.st_mode & 0o022
                or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode))
                or (path.is_symlink() and not path.resolve(strict=True).is_relative_to(PIP_ROOT))):
            raise ToolchainError('Candidate system pip tree is unsafe')
    return {'schema_version': 1, 'python': str(PYTHON), 'python_sha256': python_digest,
        'pip_root': str(PIP_ROOT), 'pip_inventory_sha256': directory_inventory_digest(PIP_ROOT),
        'uv': str(uv), 'uv_sha256': UV_SHA256, 'uv_uid': os.geteuid(),
        'uv_mode': 0o700, 'uv_version': '0.11.21'}


def load(path: Path, expected_digest: str) -> dict:
    from scripts.production_worker_cohort import sealed_payload, _unique_object
    try:
        document = json.loads(sealed_payload(path, expected_digest), object_pairs_hook=_unique_object)
        if (type(document) is not dict or set(document) != FIELDS
                or type(document['schema_version']) is not int or document['schema_version'] != 1
                or document != observe(Path(document['uv']))):
            raise ToolchainError('Candidate DFT tools differ from their observed seal')
        return document
    except (KeyError, ValueError, TypeError) as exc:
        raise ToolchainError('Candidate DFT toolchain record is invalid') from exc
