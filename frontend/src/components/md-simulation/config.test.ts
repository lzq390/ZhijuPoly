import { describe, expect, it } from "vitest";
import type { MdDemoRunRequest } from "../../types";
import {
  MD_DEMO_PROGRESS_STEPS,
  normalizeMdDemoRequest,
  progressStepFor,
  sameMdDemoRequest,
  validateMdDemoRequest
} from "./config";

const valid: MdDemoRunRequest = {
  smiles: "*CC*",
  temperature: 300,
  pressure: 1,
  n_atom: 1000,
  n_chain: 10,
  forcefield: "GAFF2_mod"
};

describe("MD simulation form contract", () => {
  it("accepts exact backend boundaries", () => {
    expect(validateMdDemoRequest({ ...valid, temperature: Number.EPSILON, pressure: Number.EPSILON, n_atom: 100, n_chain: 1 })).toEqual({});
    expect(validateMdDemoRequest({ ...valid, temperature: 5000, pressure: 100000, n_atom: 500000, n_chain: 10000 })).toEqual({});
  });

  it("validates every request field before submission", () => {
    const errors = validateMdDemoRequest({
      smiles: " ",
      temperature: 0,
      pressure: 100001,
      n_atom: 99,
      n_chain: 1.5,
      forcefield: " "
    });
    expect(Object.keys(errors).sort()).toEqual(["forcefield", "n_atom", "n_chain", "pressure", "smiles", "temperature"]);
  });

  it("normalizes snapshots and maps the full six-stage demonstration", () => {
    const normalized = normalizeMdDemoRequest({ ...valid, smiles: "  *CC*  ", forcefield: " GAFF2_mod " });
    expect(normalized).toEqual(valid);
    expect(sameMdDemoRequest(valid, { ...valid, smiles: " *CC* " })).toBe(true);
    expect(MD_DEMO_PROGRESS_STEPS).toHaveLength(6);
    expect(progressStepFor(1).label).toBe("输入检查");
    expect(progressStepFor(100).label).toBe("结果汇总");
  });
});
