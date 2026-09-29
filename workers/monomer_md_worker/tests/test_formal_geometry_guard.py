"""The managed entrypoint rejects geometry before any integration starts."""
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from workers.monomer_md_worker.app import byteff2_formal_entrypoint as entrypoint


@pytest.mark.parametrize("protocol", ["Density", "HVap", "Dielectric", "Compressibility", "Transport"])
@pytest.mark.parametrize("edge,valid", [(1.42, False), (2.67, True)])
def test_guard_precedes_context_and_preserves_protocol(monkeypatch, tmp_path, protocol, edge, valid):
    events = []
    class Quantity:
        def __init__(self, value):
            self.value = value
        def value_in_unit(self, unit):
            return self.value
    force = SimpleNamespace(usesPeriodicBoundaryConditions=lambda: True,
                            getCutoffDistance=lambda: Quantity(1.0))
    system = SimpleNamespace(usesPeriodicBoundaryConditions=lambda: True,
        getForces=lambda: [force], getDefaultPeriodicBoxVectors=lambda: [
            Quantity([edge, 0, 0]), Quantity([0, edge, 0]), Quantity([0, 0, edge])])
    module = ModuleType("byteff2.toolkit.protocol")
    original = lambda: ("topology", system)
    module.generate_openmm_system = original
    class Protocol:
        def __init__(self, config):
            assert config == {"protocol": protocol, "unchanged": 123}
        def run_protocol(self):
            assert module.generate_openmm_system() == ("topology", system)
            events.append("context-and-complete-steps")
        def post_process(self):
            events.append("post-process")
    setattr(module, protocol + "Protocol", Protocol)
    package, toolkit = ModuleType("byteff2"), ModuleType("byteff2.toolkit")
    package.toolkit, toolkit.protocol = toolkit, module
    for name, value in {"byteff2": package, "byteff2.toolkit": toolkit,
                        "byteff2.toolkit.protocol": module,
                        "openmm": SimpleNamespace(unit=SimpleNamespace(nanometer="nm"))}.items():
        monkeypatch.setitem(sys.modules, name, value)
    monkeypatch.chdir(tmp_path)
    if valid:
        entrypoint.run({"protocol": protocol, "unchanged": 123})
        assert events == ["context-and-complete-steps", "post-process"]
        assert len(json.loads((tmp_path / "formal_geometry.json").read_text())["checks"]) == 1
    else:
        with pytest.raises(entrypoint.FormalSystemSizeError, match="1.42"):
            entrypoint.run({"protocol": protocol, "unchanged": 123})
        assert events == []
        assert json.loads((tmp_path / "formal_failure.json").read_text())["error_category"] == "invalid_periodic_box"
    assert module.generate_openmm_system is original
