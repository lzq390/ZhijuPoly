from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from workers.monomer_md_worker.app import runtime_probe


class _PlatformWithFailures:
    failures: list[str] = []

    @classmethod
    def getPluginLoadFailures(cls):
        return cls.failures


class _OpenMMForReadiness:
    Platform = _PlatformWithFailures


def test_main_uses_only_parent_supplied_protocols(monkeypatch, capsys):
    captured: dict[str, object] = {}

    def fake_probe(**kwargs):
        captured.update(kwargs)
        return {"runtime_ready": True, "runtime_error": None, "protocols": {}}

    monkeypatch.setattr(runtime_probe, "_probe_runtime", fake_probe)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runtime_probe.py",
            "--protocol",
            "Density",
            "--protocol",
            "Transport",
            "--protocol",
            "Density",
            "--transport-cuda-smoke",
        ],
    )

    assert runtime_probe.main() == 0
    assert captured == {
        "protocols": ("Density", "Transport"),
        "transport_cuda_smoke": True,
    }
    assert json.loads(capsys.readouterr().out)["runtime_ready"] is True


def test_probe_validates_common_cuda_and_reports_supplied_protocols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    platform_calls: list[str] = []
    model_load_calls = 0
    cuda_platform = object()

    def fake_load_model() -> None:
        nonlocal model_load_calls
        model_load_calls += 1

    monkeypatch.setattr(runtime_probe, "_load_byteff2_model", fake_load_model)

    class Platform:
        @classmethod
        def getPlatformByName(cls, name: str):
            platform_calls.append(name)
            return cuda_platform

    openmm = ModuleType("openmm")
    openmm.__path__ = []  # type: ignore[attr-defined]
    openmm.Platform = Platform  # type: ignore[attr-defined]
    unit = ModuleType("openmm.unit")
    openmm.unit = unit  # type: ignore[attr-defined]

    mdanalysis = ModuleType("MDAnalysis")
    mdanalysis.__path__ = []  # type: ignore[attr-defined]
    mda_lib = ModuleType("MDAnalysis.lib")
    mda_lib.__path__ = []  # type: ignore[attr-defined]
    mda_formats = ModuleType("MDAnalysis.lib.formats")
    mda_formats.__path__ = []  # type: ignore[attr-defined]
    libdcd = ModuleType("MDAnalysis.lib.formats.libdcd")
    libdcd.DCDFile = object  # type: ignore[attr-defined]

    byteff2 = ModuleType("byteff2")
    byteff2.__path__ = []  # type: ignore[attr-defined]
    toolkit = ModuleType("byteff2.toolkit")
    toolkit.__path__ = []  # type: ignore[attr-defined]
    protocol = ModuleType("byteff2.toolkit.protocol")
    protocol.DensityProtocol = object  # type: ignore[attr-defined]
    toolkit.protocol = protocol  # type: ignore[attr-defined]

    modules = {
        "openmm": openmm,
        "openmm.unit": unit,
        "MDAnalysis": mdanalysis,
        "MDAnalysis.lib": mda_lib,
        "MDAnalysis.lib.formats": mda_formats,
        "MDAnalysis.lib.formats.libdcd": libdcd,
        "pandas": ModuleType("pandas"),
        "byteff2": byteff2,
        "byteff2.toolkit": toolkit,
        "byteff2.toolkit.protocol": protocol,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    payload = runtime_probe._probe_runtime(
        protocols=("Density", "NotAProtocol"),
        transport_cuda_smoke=False,
    )

    assert platform_calls == ["CUDA"]
    assert model_load_calls == 1
    assert payload["runtime_ready"] is True
    assert payload["protocols"] == {
        "Density": {
            "supported": True,
            "runtime_ready": True,
            "runtime_error": None,
        },
        "NotAProtocol": {
            "supported": False,
            "runtime_ready": False,
            "runtime_error": None,
        },
    }


def test_byteff2_model_probe_loads_the_frozen_cpu_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    byteff2 = ModuleType("byteff2")
    byteff2.__path__ = []  # type: ignore[attr-defined]
    train = ModuleType("byteff2.train")
    train.__path__ = []  # type: ignore[attr-defined]
    train_utils = ModuleType("byteff2.train.utils")

    def fake_load_model(root: str) -> object:
        calls.append(("load", root))
        return object()

    train_utils.load_model = fake_load_model  # type: ignore[attr-defined]
    bytemol = ModuleType("bytemol")
    bytemol.__path__ = []  # type: ignore[attr-defined]
    bytemol_utils = ModuleType("bytemol.utils")

    def fake_get_data_file_path(relative: str, package: str) -> str:
        calls.append(("path", relative, package))
        return "/frozen/byteff2/trained_models/optimal.pt"

    bytemol_utils.get_data_file_path = fake_get_data_file_path  # type: ignore[attr-defined]
    for name, module in {
        "byteff2": byteff2,
        "byteff2.train": train,
        "byteff2.train.utils": train_utils,
        "bytemol": bytemol,
        "bytemol.utils": bytemol_utils,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    runtime_probe._load_byteff2_model()

    assert calls == [
        ("path", "trained_models/optimal.pt", "byteff2"),
        ("load", "/frozen/byteff2/trained_models"),
    ]


def test_main_classifies_unexpected_runtime_failure(monkeypatch, capsys):
    sentinel = "/private/openmm/runtime/detail"

    def fail_probe(**kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(runtime_probe, "_probe_runtime", fail_probe)
    monkeypatch.setattr(
        sys,
        "argv",
        ["runtime_probe.py", "--protocol", "Density"],
    )

    assert runtime_probe.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime_error"] == runtime_probe.RUNTIME_PROBE_FAILURE
    assert sentinel not in payload["runtime_error"]


@pytest.mark.parametrize(
    "library",
    (
        "libOpenMM.so",
        "libOpenMMCUDA.so",
        "libOpenMMVelocityVerlet.so",
        "libVelocityVerletPluginCUDA.so",
    ),
)
def test_transport_readiness_rejects_related_plugin_load_failures(
    monkeypatch: pytest.MonkeyPatch, library: str
) -> None:
    sentinel = f"/private/openmm/{library}"
    _PlatformWithFailures.failures = [
        f"Error loading {sentinel}: dependency not found"
    ]
    monkeypatch.setattr(
        runtime_probe,
        "_run_transport_cuda_smoke",
        lambda *args: (_ for _ in ()).throw(AssertionError("smoke must not run")),
    )

    error = runtime_probe._transport_runtime_error(
        _OpenMMForReadiness,
        object(),
        object(),
        transport_cuda_smoke=True,
        integrator_class=object,
    )

    assert error == runtime_probe.TRANSPORT_PLUGIN_LOAD_FAILURE
    assert sentinel not in error


def test_transport_native_link_gate_checks_three_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "openmm"
    components = (
        root / "lib/libOpenMMVelocityVerlet.so",
        root / "lib/plugins/libOpenMMCUDA.so",
        root / "lib/plugins/libVelocityVerletPluginCUDA.so",
    )
    for component in components:
        component.parent.mkdir(parents=True, exist_ok=True)
        component.touch()
    monkeypatch.setenv("OPENMM_DIR", str(root))
    calls: list[list[str]] = []

    def linked(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="all linked\n")

    monkeypatch.setattr(runtime_probe.subprocess, "run", linked)

    assert runtime_probe._transport_native_link_error() is None
    assert calls == [["/usr/bin/ldd", *(str(path) for path in components)]]


def test_transport_native_link_gate_rejects_not_found_without_leaking_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "openmm"
    for relative in (
        "lib/libOpenMMVelocityVerlet.so",
        "lib/plugins/libOpenMMCUDA.so",
        "lib/plugins/libVelocityVerletPluginCUDA.so",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setenv("OPENMM_DIR", str(root))
    sentinel = "/private/missing/libdependency.so"
    monkeypatch.setattr(
        runtime_probe.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=f"{sentinel} => not found\n",
        ),
    )

    error = runtime_probe._transport_native_link_error()

    assert error == runtime_probe.TRANSPORT_NATIVE_LINK_FAILURE
    assert sentinel not in error


def test_transport_readiness_ignores_unrelated_plugin_failure(monkeypatch):
    _PlatformWithFailures.failures = [
        "Error loading libExampleAnalysisPlugin.so: "
        "libOpenMM.so: optional dependency not found"
    ]
    smoke_calls = 0

    def fake_smoke(*args):
        nonlocal smoke_calls
        smoke_calls += 1

    monkeypatch.setattr(runtime_probe, "_run_transport_cuda_smoke", fake_smoke)

    error = runtime_probe._transport_runtime_error(
        _OpenMMForReadiness,
        object(),
        object(),
        transport_cuda_smoke=True,
        integrator_class=object,
    )

    assert error is None
    assert smoke_calls == 1


def test_transport_readiness_classifies_plugin_inspection_failure():
    class BrokenPlatform:
        @classmethod
        def getPluginLoadFailures(cls):
            raise RuntimeError("/private/plugin/detail")

    error = runtime_probe._transport_runtime_error(
        SimpleNamespace(Platform=BrokenPlatform),
        object(),
        object(),
        transport_cuda_smoke=True,
        integrator_class=object,
    )

    assert error == runtime_probe.TRANSPORT_PLUGIN_INSPECTION_FAILURE


def test_transport_readiness_ignores_unrelated_plugin_failure(monkeypatch):
    _PlatformWithFailures.failures = [
        "Error loading libExampleAnalysisPlugin.so: optional dependency not found"
    ]
    smoke_calls = 0

    def fake_smoke(*args):
        nonlocal smoke_calls
        smoke_calls += 1

    monkeypatch.setattr(runtime_probe, "_run_transport_cuda_smoke", fake_smoke)

    error = runtime_probe._transport_runtime_error(
        _OpenMMForReadiness,
        object(),
        object(),
        transport_cuda_smoke=True,
        integrator_class=object,
    )

    assert error is None
    assert smoke_calls == 1


def test_transport_readiness_requires_enabled_cuda_smoke(monkeypatch):
    _PlatformWithFailures.failures = []
    monkeypatch.setattr(
        runtime_probe,
        "_run_transport_cuda_smoke",
        lambda *args: (_ for _ in ()).throw(AssertionError("smoke must not run")),
    )

    error = runtime_probe._transport_runtime_error(
        _OpenMMForReadiness,
        object(),
        object(),
        transport_cuda_smoke=False,
        integrator_class=object,
    )

    assert error == runtime_probe.TRANSPORT_CUDA_SMOKE_DISABLED


def test_transport_readiness_classifies_cuda_smoke_failure(monkeypatch):
    _PlatformWithFailures.failures = []
    sentinel = "/private/cuda/device/detail"
    monkeypatch.setattr(
        runtime_probe,
        "_run_transport_cuda_smoke",
        lambda *args: (_ for _ in ()).throw(RuntimeError(sentinel)),
    )

    error = runtime_probe._transport_runtime_error(
        _OpenMMForReadiness,
        object(),
        object(),
        transport_cuda_smoke=True,
        integrator_class=object,
    )

    assert error == runtime_probe.TRANSPORT_CUDA_SMOKE_FAILURE
    assert sentinel not in error


def test_transport_cuda_smoke_creates_context_and_executes_one_step():
    events: list[object] = []

    class System:
        def addParticle(self, mass):
            events.append(("particle", mass))

        def addForce(self, force):
            events.append(("system_force", force))

    class CustomExternalForce:
        def __init__(self, expression):
            events.append(("force", expression))

        def addGlobalParameter(self, name, value):
            events.append(("parameter", name, value))

        def addParticle(self, index, parameters):
            events.append(("force_particle", index, parameters))

    class Context:
        def __init__(self, system, integrator, platform):
            events.append(("context", platform))

        def setPositions(self, positions):
            events.append(("positions", positions))

    class Integrator:
        def __init__(self, **kwargs):
            events.append(("integrator", kwargs))

        def step(self, steps):
            events.append(("step", steps))

    omm = SimpleNamespace(
        System=System,
        CustomExternalForce=CustomExternalForce,
        Context=Context,
        Vec3=lambda x, y, z: (x, y, z),
    )
    unit = SimpleNamespace(
        amu=1,
        nanometer=1,
        kelvin=1,
        picosecond=1,
        femtosecond=1,
    )
    cuda_platform = object()

    runtime_probe._run_transport_cuda_smoke(
        omm, unit, cuda_platform, Integrator
    )

    assert ("context", cuda_platform) in events
    assert ("step", 1) in events
