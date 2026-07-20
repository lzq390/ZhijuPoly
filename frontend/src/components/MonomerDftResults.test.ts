import { describe, expect, it } from "vitest";
import type { MonomerDftResult, MonomerDftTrajectoryArtifact } from "../types";
import { atomsToXyz } from "./MoleculeCoordinates3D";
import { buildFrames, isImaginaryMonomerDftFrequency } from "./MonomerDftResults";

const result = {
  schema_version: 1,
  calculation_type: "optimization",
  engine: "aimnet2",
  model: "aimnet2",
  input: {
    input_type: "smiles",
    canonical_smiles: "CO",
    net_charge: 0,
    input_formal_charge: 0,
    multiplicity: 1,
    electron_count: 18
  },
  atoms: { count: 2, atomic_numbers: [6, 8], symbols: ["C", "O"] },
  geometry: {
    initial_coordinates_angstrom: [[0, 0, 0], [1, 0, 0]],
    final_coordinates_angstrom: [[0, 0, 0], [1.2, 0, 0]],
    units: "angstrom"
  },
  rdkit: {
    seed: 1,
    force_field: "MMFF94",
    optimization_performed: true,
    optimization_status: 0,
    optimization_state: "converged"
  },
  properties: { energy: { value_eV: -100 } },
  optimization: {
    converged: true,
    steps: 2,
    fmax_threshold_eV_per_A: 0.01,
    max_steps: 50,
    trajectory_artifact_id: "optimization_trajectory",
    trace: [
      { step: 0, energy_eV: -99, fmax_eV_per_A: 0.4 },
      { step: 1, energy_eV: -99.5, fmax_eV_per_A: 0.1 },
      { step: 2, energy_eV: -100, fmax_eV_per_A: 0.005 }
    ]
  },
  scientific_status: {
    calculation_completed: true,
    geometry_status: "converged",
    is_stationary: true,
    minimum_assessment: "unassessed",
    fmax_eV_per_A: 0.005
  },
  warnings: [],
  timings: {},
  provenance: {}
} satisfies MonomerDftResult;

const trajectory = {
  units: { energy: "eV", fmax: "eV/angstrom", coordinates: "angstrom" },
  frames: [
    { step: 0, energy_eV: -99, fmax_eV_per_A: 0.4, coordinates_angstrom: [[0, 0, 0], [1, 0, 0]] },
    { step: 1, energy_eV: -99.5, fmax_eV_per_A: 0.1, coordinates_angstrom: [[0, 0, 0], [1.1, 0, 0]] },
    { step: 2, energy_eV: -100, fmax_eV_per_A: 0.005, coordinates_angstrom: [[0, 0, 0], [1.2 + 5e-9, 0, 0]] }
  ]
} satisfies MonomerDftTrajectoryArtifact;

describe("monomer DFT coordinate frames", () => {
  it("keeps explicit initial/final frames and removes duplicate trajectory endpoints", () => {
    const frames = buildFrames(result, trajectory);

    expect(frames.map((frame) => [frame.id, frame.kind, frame.step])).toEqual([
      ["initial", "initial", undefined],
      ["step-1", "trajectory", 1],
      ["final", "final", undefined]
    ]);
    expect(result.optimization.trace.map((point) => point.step)).toEqual([0, 1, 2]);
    expect(atomsToXyz(frames[1].atoms, frames[1].label).split("\n")).toEqual([
      "2",
      "优化第 1 步",
      "C 0.0000000000 0.0000000000 0.0000000000",
      "O 1.1000000000 0.0000000000 0.0000000000"
    ]);
  });

  it("carries V2 isotope and atomic-mass metadata into displayed atoms", () => {
    const v2Result = {
      ...result,
      schema_version: 2,
      provenance: {
        rdkit_optimization_performed: true,
        rdkit_optimization_status: 0,
        rdkit_version: "2026.03.3",
        mass_source: "rdkit_periodic_table_explicit_isotopes",
        execution_path: "primary",
        gpu_uuid: "GPU-test",
        gpu_budget_mib: 4096,
        broker_instance_id: "broker-test",
        lease_id: "lease-test",
        fencing_token: 1
      },
      atoms: {
        ...result.atoms,
        isotope_mass_numbers: [13, 0],
        atomic_masses_u: [13.00335483507, 15.999]
      }
    } satisfies MonomerDftResult;

    const frames = buildFrames(v2Result, null);
    expect(frames.at(-1)?.atoms.map((atom) => [atom.element, atom.isotope_mass_number, atom.atomic_mass_u])).toEqual([
      ["C", 13, 13.00335483507],
      ["O", 0, 15.999]
    ]);
  });
});

describe("monomer DFT frequency classification", () => {
  it("uses the scientific imaginary-frequency threshold instead of zero", () => {
    expect(isImaginaryMonomerDftFrequency(-5, -10)).toBe(false);
    expect(isImaginaryMonomerDftFrequency(-11, -10)).toBe(true);
    expect(isImaginaryMonomerDftFrequency(-10, -10)).toBe(false);
  });
});
