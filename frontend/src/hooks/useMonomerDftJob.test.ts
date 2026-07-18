import { describe, expect, it } from "vitest";
import type { MonomerDftCapabilitiesResponse } from "../types";
import {
  extractElementsFromSmiles,
  isRetryableMonomerDftPollError,
  isMonomerDftTerminal,
  validateMonomerDftRequest
} from "./useMonomerDftJob";
import { MonomerDftApiError } from "../services/api";

const capabilities: MonomerDftCapabilitiesResponse = {
  enabled: true,
  available: true,
  schema_ready: true,
  calculation_types: ["single_point", "optimization"],
  properties: ["energy", "charges", "forces", "hessian", "frequencies"],
  default_model: "aimnet2",
  models: [{
    id: "aimnet2",
    label: "AIMNet2",
    available: true,
    supported_calculation_types: ["single_point", "optimization"],
    supported_properties: ["energy", "charges", "forces", "hessian", "frequencies"],
    supported_elements: ["H", "C", "N", "O"],
    supports_spin: false,
    charge_min: -5,
    charge_max: 5
  }],
  defaults: {
    conformer: { seed: 1, max_iterations: 500 },
    single_point: { properties: ["energy", "charges", "forces"] },
    optimization: { fmax_eV_per_A: 0.01, max_steps: 50, post_optimization_properties: [] }
  },
  limits: {
    max_optimization_steps: 50,
    min_optimization_steps: 10,
    max_concurrent_jobs: 1,
    max_queued_jobs: 8,
    max_active_jobs: 9
  }
};

function validate(overrides: Partial<Parameters<typeof validateMonomerDftRequest>[0]> = {}) {
  return validateMonomerDftRequest({
    smiles: "CCO",
    netCharge: null,
    multiplicity: 1,
    psmilesMode: null,
    calculationType: "single_point",
    modelId: "aimnet2",
    properties: ["energy", "charges", "forces"],
    ...overrides
  }, capabilities);
}

describe("monomer DFT validation", () => {
  it("recognizes aromatic and two-letter elements", () => {
    expect(extractElementsFromSmiles("c1cc(Cl)ncc1Br")).toEqual(["C", "Cl", "N", "Br"]);
  });

  it("requires an explicit conversion mode for PSMILES only", () => {
    expect(validate({ smiles: "*CC*" }).some((issue) => issue.message.includes("PSMILES"))).toBe(true);
    expect(validate({ smiles: "*CC*", psmilesMode: "cap" })).toEqual([]);
    expect(validate({ psmilesMode: "cap" }).some((issue) => issue.message.includes("普通单体"))).toBe(true);
  });

  it("uses capability metadata for elements and multiplicity", () => {
    expect(validate({ smiles: "C[SiH3]" }).some((issue) => issue.message.includes("Si"))).toBe(true);
    expect(validate({ multiplicity: 3 }).some((issue) => issue.message.includes("开放壳层"))).toBe(true);
  });

  it("allows requesting frequencies without explicitly selecting Hessian", () => {
    expect(validate({ properties: ["energy", "frequencies"] })).toEqual([]);
  });

  it("enforces approved multiplicity and optimization bounds in JavaScript", () => {
    expect(validate({ netCharge: 6 }).some((issue) => issue.field === "charge")).toBe(true);
    expect(validate({ netCharge: -6 }).some((issue) => issue.field === "charge")).toBe(true);
    expect(validate({ multiplicity: 8 }).some((issue) => issue.field === "multiplicity")).toBe(true);
    expect(validate({ calculationType: "optimization", fmax: 0, maxSteps: 9 }).filter((issue) => issue.field === "optimization")).toHaveLength(2);
    expect(validate({ calculationType: "optimization", fmax: 0.01, maxSteps: 50 })).toEqual([]);
  });

  it("uses only completed, failed and cancelled as terminal states", () => {
    expect(isMonomerDftTerminal("completed")).toBe(true);
    expect(isMonomerDftTerminal("failed")).toBe(true);
    expect(isMonomerDftTerminal("cancelled")).toBe(true);
    expect(isMonomerDftTerminal("cancel_requested")).toBe(false);
    expect(isMonomerDftTerminal("queued")).toBe(false);
  });

  it("retries explicitly retryable API errors but stops explicit non-retryable 4xx errors", () => {
    expect(isRetryableMonomerDftPollError(new MonomerDftApiError({
      message: "retry later",
      status: 409,
      retryable: true
    }))).toBe(true);
    expect(isRetryableMonomerDftPollError(new MonomerDftApiError({
      message: "not found",
      status: 404,
      retryable: false
    }))).toBe(false);
  });
});
