"""Run the pinned ByteFF2 protocol with an integration-level geometry guard."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

if __package__:
    from .byteff2_system_geometry import FormalSystemSizeError, validate_periodic_box
else:
    from byteff2_system_geometry import FormalSystemSizeError, validate_periodic_box


def run(config: dict) -> None:
    from openmm import unit
    import byteff2.toolkit.protocol as protocols

    name = config["protocol"]
    if name not in {"Density", "Transport", "HVap", "Dielectric", "Compressibility"}:
        raise ValueError("Unsupported formal protocol")
    original = protocols.generate_openmm_system
    checks = []

    def guarded_system(*args, **kwargs):
        topology, system = original(*args, **kwargs)
        if system.usesPeriodicBoundaryConditions():
            cutoffs = [float(force.getCutoffDistance().value_in_unit(unit.nanometer))
                       for force in system.getForces()
                       if force.usesPeriodicBoundaryConditions() and hasattr(force, "getCutoffDistance")]
            if cutoffs:
                vectors = [[float(x) for x in v.value_in_unit(unit.nanometer)]
                           for v in system.getDefaultPeriodicBoxVectors()]
                checks.append(validate_periodic_box(vectors, max(cutoffs)))
        return topology, system

    protocols.generate_openmm_system = guarded_system
    try:
        protocol = getattr(protocols, name + "Protocol")(config)
        protocol.run_protocol()
        protocol.post_process()
        Path("formal_geometry.json").write_text(json.dumps({"protocol": name, "checks": checks}) + "\n")
    except FormalSystemSizeError as exc:
        Path("formal_failure.json").write_text(json.dumps({
            "error_category": "invalid_periodic_box", "stage": "system_construction",
            "protocol": name, "message": str(exc),
        }) + "\n")
        raise
    finally:
        protocols.generate_openmm_system = original


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    run(json.loads(args.config.read_text()))
