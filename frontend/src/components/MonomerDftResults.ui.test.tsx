/* @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MonomerDftJobResponse, MonomerDftResult } from "../types";

vi.mock("./MoleculeCoordinates3D", () => ({
  MoleculeCoordinates3D: () => <div aria-label="mock explicit coordinates" />
}));

import { MonomerDftResults } from "./MonomerDftResults";

const result: MonomerDftResult = {
  schema_version: 1,
  calculation_type: "single_point",
  engine: "aimnet2",
  model: "aimnet2",
  input: {
    input_type: "smiles",
    canonical_smiles: "CCO",
    net_charge: 0,
    input_formal_charge: 0,
    multiplicity: 1,
    electron_count: 26
  },
  atoms: { count: 2, atomic_numbers: [6, 8], symbols: ["C", "O"] },
  geometry: {
    initial_coordinates_angstrom: [[0, 0, 0], [1, 0, 0]],
    final_coordinates_angstrom: [[0, 0, 0], [1, 0, 0]],
    units: "angstrom"
  },
  rdkit: {
    seed: 1,
    force_field: "MMFF94",
    optimization_status: 0,
    optimization_performed: true,
    optimization_state: "converged"
  },
  properties: {
    energy: { value_eV: -12.3 },
    charges: { values_e: [-0.1, 0.1], sum_e: 0, conservation_error_e: 0, conserved: true },
    forces: { values_eV_per_A: [[0, 0, 0], [0, 0, 0]], fmax_eV_per_A: 0 }
  },
  optimization: null,
  scientific_status: {
    calculation_completed: true,
    geometry_status: "not_optimized",
    is_stationary: false,
    minimum_assessment: "unassessed",
    fmax_eV_per_A: 0
  },
  warnings: [],
  timings: { model_compute_ms: 42 },
  provenance: {}
};

function resultWithExplicitIsotope(includeFrequencies: boolean): MonomerDftResult {
  return {
    ...result,
    schema_version: 2,
    atoms: {
      ...result.atoms,
      isotope_mass_numbers: [13, 0],
      atomic_masses_u: [13.003355, 15.999]
    },
    rdkit: {
      seed: 1,
      force_field: "MMFF94",
      optimization_status: 0,
      optimization_performed: true,
      optimization_state: "converged"
    },
    properties: {
      ...result.properties,
      ...(includeFrequencies ? {
        frequencies: {
          artifact_id: "frequencies.json",
          values_cm_1: [120, 850],
          mode_count: 2,
          removed_rigid_modes: 6,
          expected_rigid_modes: 6,
          linear_molecule: false,
          imaginary_threshold_cm_1: -20,
          imaginary_mode_count: 0,
          imaginary_values_cm_1: [],
          near_zero_mode_count: 0
        }
      } : {})
    },
    provenance: {
      rdkit_optimization_performed: true,
      rdkit_optimization_status: 0,
      rdkit_version: "2026.03",
      mass_source: "explicit_isotope",
      execution_path: "primary",
      gpu_uuid: "gpu-1",
      gpu_budget_mib: 1024,
      broker_instance_id: "broker-1",
      lease_id: "lease-1",
      fencing_token: 1
    }
  };
}

function job(overrides: Partial<MonomerDftJobResponse> = {}): MonomerDftJobResponse {
  return {
    job_id: "11111111-1111-4111-8111-111111111111",
    calculation_type: "single_point",
    status: "completed",
    request: {
      calculation_type: "single_point",
      input: { smiles: "CCO", net_charge: 0, multiplicity: 1, psmiles_mode: null },
      model: "aimnet2",
      conformer: { seed: 1, max_iterations: 500 },
      single_point: { properties: ["energy", "forces", "charges"] }
    },
    request_sha256: "hash",
    attempt: 1,
    queue_position: null,
    stage: "completed",
    progress_percent: 100,
    scientific_status: "completed",
    warnings: [],
    result,
    timings: { total_ms: 50 },
    provenance: {},
    error: null,
    artifacts: [],
    artifacts_state: "none",
    artifacts_deleted: false,
    cancel_requested: false,
    created_at: "2026-09-03T00:00:00Z",
    updated_at: "2026-09-03T00:00:01Z",
    finished_at: "2026-09-03T00:00:01Z",
    idempotent_replay: false,
    ...overrides
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("MonomerDftResults analysis tabs", () => {
  it("exposes three keyboard-operable result tabs without a files-and-records panel", () => {
    render(<MonomerDftResults job={job()} />);

    const tablist = screen.getByRole("tablist", { name: "DFT 结果分析" });
    expect(within(tablist).getAllByRole("tab")).toHaveLength(3);
    expect(screen.getByLabelText("mock explicit coordinates")).toBeTruthy();
    expect(screen.queryByRole("tab", { name: /文件与记录/ })).toBeNull();

    fireEvent.keyDown(tablist, { key: "End" });
    expect(screen.getByRole("tab", { name: /原子数据/ }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByLabelText("原子数据表，可横向滚动")).toBeTruthy();

    fireEvent.keyDown(tablist, { key: "ArrowLeft" });
    expect(screen.getByRole("tab", { name: /曲线与频率/ }).getAttribute("aria-selected")).toBe("true");
  });

  it("shows a failed terminal state instead of a waiting placeholder when no result exists", () => {
    render(<MonomerDftResults job={job({
      status: "failed",
      result: null,
      stage: "failed",
      progress_percent: 30,
      error: { code: "worker_failed", message: "GPU worker failed", retryable: true }
    })} />);

    expect(screen.getByText("计算失败，未生成结果")).toBeTruthy();
    expect(screen.getByText("计算服务异常，本次任务未能完成，请稍后重试。")).toBeTruthy();
    expect(screen.queryByText("正在等待计算结果")).toBeNull();
  });

  it("renders coded calculation warnings in Chinese without exposing backend text", () => {
    const backendWarning =
      "Explicit net_charge overrides SMILES charge inference and matches the encoded formal charge.";
    render(<MonomerDftResults job={job({
      warnings: [backendWarning],
      result: {
        ...result,
        warnings: [{ code: "net_charge_override", message: backendWarning }]
      }
    })} />);

    expect(screen.getByText("已使用手动设置的总电荷，该值与结构中标注的形式电荷一致。")).toBeTruthy();
    expect(screen.queryByText(backendWarning)).toBeNull();
  });

  it("does not claim isotope masses were used for a frequency analysis that was not requested", () => {
    render(<MonomerDftResults job={job({ result: resultWithExplicitIsotope(false) })} />);

    fireEvent.click(screen.getByRole("tab", { name: /曲线与频率/ }));
    expect(screen.queryByText("频率分析已使用指定的同位素质量")).toBeNull();
    expect(screen.getByText("没有曲线或频率数据")).toBeTruthy();
  });

  it("explains isotope mass usage when frequency results are present", () => {
    render(<MonomerDftResults job={job({ result: resultWithExplicitIsotope(true) })} />);

    fireEvent.click(screen.getByRole("tab", { name: /曲线与频率/ }));
    expect(screen.getByText("频率分析已使用指定的同位素质量")).toBeTruthy();
  });

});
