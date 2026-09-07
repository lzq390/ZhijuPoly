import { describe, expect, it } from "vitest";
import type { MonomerMdSimulationResult } from "../../types";
import {
  adaptMonomerMdMetrics,
  adaptMonomerMdVisualization,
  clampMonomerMdProgress,
  normalizeMonomerMdSeries,
  safeMonomerMdArtifacts
} from "./presentation";

function result(overrides: Partial<MonomerMdSimulationResult> = {}): MonomerMdSimulationResult {
  return { summary: {}, artifacts: [], ...overrides };
}

describe("monomer MD result presentation", () => {
  it("treats backend progress as 0–100 without fractional amplification", () => {
    expect(clampMonomerMdProgress(1)).toBe(1);
    expect(clampMonomerMdProgress(100)).toBe(100);
    expect(clampMonomerMdProgress(120)).toBe(100);
  });

  it("combines values and standard deviations while treating units as metadata", () => {
    const adapted = adaptMonomerMdMetrics(result({
      metrics: { density: 1.21, density_std: 0.02, density_unit: "g/mL", units: { density: "ignored" } }
    }), "Density");
    expect(adapted.metrics).toEqual([{ key: "density", label: "密度", value: 1.21, standardDeviation: 0.02, unit: "g/mL" }]);
    expect(adapted.raw).not.toHaveProperty("density_unit");
    expect(adapted.raw).not.toHaveProperty("units");
  });

  it.each([
    ["HVap", { hvap: 8.2, hvap_std: 0.4, hvap_unit: "kcal/mol" }, "hvap", 0.4, "kcal/mol"],
    ["Dielectric", { dielectric: 12.5, dielectric_unit: "1", volume: 42, volume_unit: "nm3" }, "dielectric", null, "1"],
    ["Compressibility", { compressibility: 0.81, compressibility_std: 0.03, compressibility_unit: "GPa^-1" }, "compressibility", 0.03, "GPa^-1"],
    ["Transport", { viscosity: 2.4, viscosity_std: 0.2, viscosity_unit: "mPa s" }, "viscosity", 0.2, "mPa s"]
  ] as const)("adapts %s protocol scalar metrics", (protocol, metrics, key, standardDeviation, unit) => {
    const adapted = adaptMonomerMdMetrics(result({ metrics }), protocol);
    expect(adapted.metrics.find((metric) => metric.key === key)).toMatchObject({
      standardDeviation,
      unit
    });
  });

  it("pairs transport arrays and preserves mismatched contracts as raw data", () => {
    const paired = adaptMonomerMdMetrics(result({ metrics: {
      components: ["Li", "PF6"],
      Dself_inf: [1.1, 2.2],
      diffusivity_unit: "cm2/s"
    } }), "Transport");
    expect(paired.transportRows).toHaveLength(2);
    expect(paired.transportRows[0]).toMatchObject({ component: "Li", diffusivity: 1.1, unit: "cm2/s" });

    const mismatch = adaptMonomerMdMetrics(result({ metrics: {
      components: ["Li"],
      Dself_inf: [1.1, 2.2]
    } }), "Transport");
    expect(mismatch.contractWarnings).toHaveLength(1);
    expect(mismatch.raw).toMatchObject({ components: ["Li"], Dself_inf: [1.1, 2.2] });
  });

  it("normalizes object and array series and handles empty data", () => {
    expect(normalizeMonomerMdSeries({ points: [{ time_ps: 2, density: 1.1 }] }, ["density"])).toEqual([{ x: 2, y: 1.1 }]);
    expect(normalizeMonomerMdSeries([{ step: 4, value: 9 }], ["density"])).toEqual([{ x: 4, y: 9 }]);
    expect(normalizeMonomerMdSeries(undefined, ["density"])).toEqual([]);
  });

  it("adapts v1 stages, honors a usable default, and falls back from an empty default", () => {
    const adapted = adaptMonomerMdVisualization(result({ visualization: {
      schema_version: 1,
      status: "partial",
      default_stage_id: "nvt",
      warnings: ["nvt:TRAJECTORY_DCD_MISSING"],
      stages: [
        { stage_id: "npt", label: "NPT", density_series: { points: [{ time_ps: 1, value: 1.1 }] }, warnings: [] },
        { stage_id: "nvt", label: "NVT", warnings: ["TRAJECTORY_DCD_MISSING"] }
      ]
    } }));

    expect(adapted.schemaVersion).toBe(1);
    expect(adapted.defaultStageId).toBe("npt");
    expect(adapted.stages.map((stage) => stage.stage_id)).toEqual(["npt", "nvt"]);
    expect(adapted.warnings).toEqual(["nvt:TRAJECTORY_DCD_MISSING"]);
  });

  it("adapts v2 energy decomposition series", () => {
    const adapted = adaptMonomerMdVisualization(result({ visualization: {
      schema_version: 2,
      status: "complete",
      default_stage_id: "npt",
      warnings: [],
      stages: [{
        stage_id: "npt",
        label: "NPT",
        potential_energy_series: { points: [{ time_ps: 1, value: -30 }] },
        kinetic_energy_series: { points: [{ time_ps: 1, value: 10 }] },
        energy_series: { points: [{ time_ps: 1, value: -20 }] },
        warnings: []
      }]
    } }));

    expect(adapted.schemaVersion).toBe(2);
    expect(adapted.defaultStageId).toBe("npt");
    expect(adapted.stages[0].potential_energy_series).toBeTruthy();
    expect(adapted.stages[0].kinetic_energy_series).toBeTruthy();
  });

  it("treats a lazily loadable trajectory as usable stage data", () => {
    const adapted = adaptMonomerMdVisualization(result({ visualization: {
      schema_version: 3,
      status: "complete",
      default_stage_id: "nvt",
      warnings: [],
      stages: [
        { stage_id: "npt", label: "NPT", density_series: { points: [{ value: 1.1 }] }, warnings: [] },
        { stage_id: "nvt", label: "NVT", trajectory_timeline_available: true, warnings: [] }
      ]
    } }));

    expect(adapted.defaultStageId).toBe("nvt");
  });

  it("adapts legacy flat demo results into one stage", () => {
    const adapted = adaptMonomerMdVisualization(result({
      density_series: { points: [{ step: 10, value: 0.9 }] },
      trajectory_preview: null
    }));

    expect(adapted.schemaVersion).toBe(0);
    expect(adapted.defaultStageId).toBe("npt");
    expect(adapted.stages).toHaveLength(1);
    expect(adapted.stages[0].density_series).toBeTruthy();
  });

  it("never exposes artifact paths as actions and only keeps real URLs", () => {
    const artifacts = safeMonomerMdArtifacts(result({ artifacts: {
      density: { path: "/srv/private/jobs/a/density.json", size_bytes: 1024 },
      report: { name: "report.json", url: "/api/files/report", kind: "JSON" },
      namedPath: { name: "/srv/private/jobs/a/trajectory.dcd", kind: "DCD" }
    } }), null);
    expect(artifacts[0]).toMatchObject({ name: "density.json", url: null });
    expect(JSON.stringify(artifacts)).not.toContain("/srv/private");
    expect(artifacts[1]).toMatchObject({ name: "report.json", url: "/api/files/report" });
    expect(artifacts[2]).toMatchObject({ name: "trajectory.dcd", url: null });
  });
});
