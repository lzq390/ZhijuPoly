from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .config import WorkerSettings


OPENMM_ENV_KEYS = ("OPENMM_DIR", "OPENMM_PLUGIN_DIR", "LD_LIBRARY_PATH")
SAFE_SUBPROCESS_INHERITED_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "TZ",
        "USER",
    }
)
CORE_OPENMM_FILES = (
    Path("lib/libOpenMM.so"),
    Path("lib/plugins/libOpenMMCUDA.so"),
)
TRANSPORT_OPENMM_FILES = (
    Path("lib/libOpenMMVelocityVerlet.so"),
    Path("lib/plugins/libVelocityVerletPluginCUDA.so"),
)
REQUIRED_OPENMM_FILES = (*CORE_OPENMM_FILES, *TRANSPORT_OPENMM_FILES)


@dataclass(frozen=True)
class OpenMMEnvironmentValidation:
    paths_injectable: bool
    core_assets_error: str | None
    transport_assets_error: str | None
    transport_error: str | None


@dataclass(frozen=True)
class ByteFF2SubprocessEnvironment:
    """Immutable environment contract shared by probes and protocol runners."""

    values: Mapping[str, str]
    paths_injectable: bool
    core_assets_error: str | None
    transport_assets_error: str | None
    transport_error: str | None

    @property
    def openmm_error(self) -> str | None:
        """Compatibility alias for callers that gate the Transport protocol."""

        return self.transport_error

    def as_dict(self) -> dict[str, str]:
        return dict(self.values)


def build_byteff2_environment(
    settings: WorkerSettings,
    base_env: Mapping[str, str] | None = None,
) -> ByteFF2SubprocessEnvironment:
    if base_env is None:
        # Direct/dev launches do not necessarily pass through the production
        # literal-environment wrapper.  Start from the same small host
        # contract so CUDA/MPS, Python, loader, Torch, and package-manager
        # controls cannot drift into one runner but not another.
        env = {
            key: os.environ[key]
            for key in SAFE_SUBPROCESS_INHERITED_KEYS
            if key in os.environ
        }
    else:
        # An explicit base is a test/embedding contract and is preserved; the
        # production Worker never supplies an arbitrary base here.
        env = dict(base_env)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["BYTEFF2_ROOT"] = str(settings.byteff2_root)
    env["CUDA_VISIBLE_DEVICES"] = settings.cuda_visible_devices
    env["PYTHONPATH"] = _prepend_paths(
        env.get("PYTHONPATH", ""),
        (
            settings.byteff2_root,
            settings.byteff2_root / "submodules" / "bytemol",
        ),
    )

    validation = validate_openmm_contract(settings.byteff2_openmm_dir)
    if validation.paths_injectable and settings.byteff2_openmm_dir is not None:
        openmm_dir = settings.byteff2_openmm_dir
        library_dir = openmm_dir / "lib"
        plugin_dir = library_dir / "plugins"
        env["OPENMM_DIR"] = str(openmm_dir)
        env["OPENMM_PLUGIN_DIR"] = str(plugin_dir)
        env["LD_LIBRARY_PATH"] = _prepend_paths(
            env.get("LD_LIBRARY_PATH", ""), (library_dir, plugin_dir)
        )

    return ByteFF2SubprocessEnvironment(
        values=MappingProxyType(env),
        paths_injectable=validation.paths_injectable,
        core_assets_error=validation.core_assets_error,
        transport_assets_error=validation.transport_assets_error,
        transport_error=validation.transport_error,
    )


def validate_openmm_environment(openmm_dir: Path | None) -> str | None:
    """Return the strict Transport contract error for compatibility."""

    return validate_openmm_contract(openmm_dir).transport_error


def validate_openmm_contract(openmm_dir: Path | None) -> OpenMMEnvironmentValidation:
    if openmm_dir is None:
        error = "BYTEFF2_OPENMM_DIR is required for the Transport runtime"
        return OpenMMEnvironmentValidation(False, error, error, error)
    if not openmm_dir.is_absolute():
        error = "BYTEFF2_OPENMM_DIR must be an absolute path"
        return OpenMMEnvironmentValidation(False, error, error, error)
    if not openmm_dir.is_dir():
        error = "BYTEFF2_OPENMM_DIR does not exist or is not a directory"
        return OpenMMEnvironmentValidation(False, error, error, error)

    core_error = _missing_asset_error(openmm_dir, CORE_OPENMM_FILES, "OpenMM core")
    transport_assets_error = _missing_asset_error(
        openmm_dir, TRANSPORT_OPENMM_FILES, "Transport"
    )
    transport_error = core_error or transport_assets_error
    return OpenMMEnvironmentValidation(
        True,
        core_error,
        transport_assets_error,
        transport_error,
    )


def _missing_asset_error(
    openmm_dir: Path, required_files: tuple[Path, ...], label: str
) -> str | None:
    missing = [path for path in required_files if not (openmm_dir / path).is_file()]
    if not missing:
        return None
    return f"required {label} native library does not exist: {missing[0].name}"


def openmm_environment_values(env: Mapping[str, str]) -> dict[str, str | None]:
    return {key: env.get(key) for key in OPENMM_ENV_KEYS}


def _prepend_paths(existing: str, prefixes: tuple[Path, ...]) -> str:
    ordered = [str(path) for path in prefixes]
    ordered.extend(item for item in existing.split(os.pathsep) if item)
    result: list[str] = []
    seen: set[str] = set()
    for item in ordered:
        normalized = os.path.normpath(item)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(item)
    return os.pathsep.join(result)
