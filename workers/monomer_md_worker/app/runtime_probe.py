from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
from typing import Any


TRANSPORT_PLUGIN_FAILURE_MARKERS = (
    "velocityverlet",
    "libopenmm.so",
    "libopenmmcuda",
    "libopenmmvelocityverlet",
    "libvelocityverletplugincuda",
)
RUNTIME_PROBE_FAILURE = "runtime import and CUDA initialization failed"
TRANSPORT_INTEGRATOR_IMPORT_FAILURE = "Transport VVIntegrator import failed"
TRANSPORT_PLUGIN_LOAD_FAILURE = "Transport OpenMM plugin loading failed"
TRANSPORT_PLUGIN_INSPECTION_FAILURE = (
    "Transport OpenMM plugin load inspection failed"
)
TRANSPORT_NATIVE_LINK_FAILURE = "Transport native library linkage failed"
TRANSPORT_CUDA_SMOKE_DISABLED = "Transport CUDA kernel smoke probe is disabled"
TRANSPORT_CUDA_SMOKE_FAILURE = "Transport CUDA kernel smoke probe failed"
SAFE_TRANSPORT_RUNTIME_ERRORS = frozenset(
    {
        TRANSPORT_INTEGRATOR_IMPORT_FAILURE,
        TRANSPORT_PLUGIN_LOAD_FAILURE,
        TRANSPORT_PLUGIN_INSPECTION_FAILURE,
        TRANSPORT_NATIVE_LINK_FAILURE,
        TRANSPORT_CUDA_SMOKE_DISABLED,
        TRANSPORT_CUDA_SMOKE_FAILURE,
    }
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe the ByteFF2/OpenMM runtime once.")
    parser.add_argument("--transport-cuda-smoke", action="store_true")
    parser.add_argument("--protocol", action="append", dest="protocols", required=True)
    args = parser.parse_args()
    try:
        payload = _probe_runtime(
            protocols=tuple(dict.fromkeys(args.protocols)),
            transport_cuda_smoke=args.transport_cuda_smoke,
        )
    except Exception:
        print(
            json.dumps(
                {"runtime_ready": False, "runtime_error": RUNTIME_PROBE_FAILURE}
            )
        )
        return 1
    print(json.dumps(payload))
    return 0


def _probe_runtime(
    *, protocols: tuple[str, ...], transport_cuda_smoke: bool
) -> dict[str, Any]:
    import MDAnalysis  # noqa: F401
    import openmm as omm
    import openmm.unit as unit
    import pandas  # noqa: F401
    from MDAnalysis.lib.formats.libdcd import DCDFile  # noqa: F401
    from byteff2.toolkit import protocol as byteff2_protocol

    _load_byteff2_model()
    cuda_platform = omm.Platform.getPlatformByName("CUDA")
    if not hasattr(byteff2_protocol, "DensityProtocol"):
        raise RuntimeError("DensityProtocol is not available")
    protocol_statuses: dict[str, dict[str, Any]] = {}
    for name in protocols:
        supported = hasattr(byteff2_protocol, f"{name}Protocol")
        protocol_statuses[name] = {
            "supported": supported,
            "runtime_ready": supported,
            "runtime_error": None,
        }

    transport = protocol_statuses.get("Transport")
    if transport is not None and transport["supported"]:
        transport_error = _transport_runtime_error(
            omm,
            unit,
            cuda_platform,
            transport_cuda_smoke=transport_cuda_smoke,
            native_link_error=_transport_native_link_error(),
        )
        if transport_error is not None:
            transport["runtime_ready"] = False
            transport["runtime_error"] = transport_error
    return {
        "runtime_ready": True,
        "runtime_error": None,
        "protocols": protocol_statuses,
    }


def _load_byteff2_model() -> None:
    """CPU-load the frozen force-field model as part of runtime readiness."""

    import os

    from byteff2.train.utils import load_model
    from bytemol.utils import get_data_file_path

    checkpoint = get_data_file_path("trained_models/optimal.pt", "byteff2")
    model = load_model(os.path.dirname(checkpoint))
    del model


def _transport_runtime_error(
    omm,
    unit,
    cuda_platform,
    *,
    transport_cuda_smoke: bool,
    integrator_class=None,
    native_link_error: str | None = None,
) -> str | None:
    if native_link_error is not None:
        return native_link_error
    if integrator_class is None:
        try:
            from velocityverletplugin import VVIntegrator
        except Exception:
            return TRANSPORT_INTEGRATOR_IMPORT_FAILURE
        integrator_class = VVIntegrator

    try:
        plugin_failures = [
            str(failure)
            for failure in omm.Platform.getPluginLoadFailures()
            if _is_transport_plugin_failure(str(failure))
        ]
    except Exception:
        return TRANSPORT_PLUGIN_INSPECTION_FAILURE
    if plugin_failures:
        return TRANSPORT_PLUGIN_LOAD_FAILURE
    if not transport_cuda_smoke:
        return TRANSPORT_CUDA_SMOKE_DISABLED
    try:
        _run_transport_cuda_smoke(omm, unit, cuda_platform, integrator_class)
    except Exception:
        return TRANSPORT_CUDA_SMOKE_FAILURE
    return None


def _transport_native_link_error() -> str | None:
    """Run the release-acceptance ldd gate for three native components."""

    raw_root = os.environ.get("OPENMM_DIR", "")
    root = Path(raw_root)
    if not raw_root or not root.is_absolute() or not root.is_dir():
        return TRANSPORT_NATIVE_LINK_FAILURE
    components = (
        root / "lib/libOpenMMVelocityVerlet.so",
        root / "lib/plugins/libOpenMMCUDA.so",
        root / "lib/plugins/libVelocityVerletPluginCUDA.so",
    )
    if any(not component.is_file() for component in components):
        return TRANSPORT_NATIVE_LINK_FAILURE
    try:
        completed = subprocess.run(
            ["/usr/bin/ldd", *(str(component) for component in components)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return TRANSPORT_NATIVE_LINK_FAILURE
    if completed.returncode != 0 or "not found" in completed.stdout.casefold():
        return TRANSPORT_NATIVE_LINK_FAILURE
    return None


def _is_transport_plugin_failure(failure: str) -> bool:
    # OpenMM reports ``Error loading <subject>: <dependency detail>``.  Only
    # classify the failed plugin/library itself; an unrelated plugin may name
    # libOpenMM.so merely as a dependency in the detail after the first colon.
    subject = failure.casefold().split(":", 1)[0]
    return any(marker in subject for marker in TRANSPORT_PLUGIN_FAILURE_MARKERS)


def _run_transport_cuda_smoke(omm, unit, cuda_platform, integrator_class) -> None:
    system = omm.System()
    system.addParticle(39.948 * unit.amu)
    force = omm.CustomExternalForce("0.5*k*(x*x+y*y+z*z)")
    force.addGlobalParameter("k", 1.0)
    force.addParticle(0, [])
    system.addForce(force)
    integrator = integrator_class(
        temperature=298.0 * unit.kelvin,
        frequency=1.0 / unit.picosecond,
        drudeTemperature=298.0 * unit.kelvin,
        drudeFrequency=100.0 / unit.picosecond,
        stepSize=1.0 * unit.femtosecond,
        numNHChains=3,
        loopsPerStep=1,
    )
    context = omm.Context(system, integrator, cuda_platform)
    try:
        context.setPositions([omm.Vec3(0.01, 0.0, 0.0)] * unit.nanometer)
        integrator.step(1)
    finally:
        del context


if __name__ == "__main__":
    raise SystemExit(main())
