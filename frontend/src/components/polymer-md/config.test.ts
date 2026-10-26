// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import { POLYMER_MD_FORCEFIELDS } from "../../services/polymerMdNativeConfiguration";
import type { PolymerMdCapabilities, PolymerMdRequest } from "../../types/polymerMd";
import { jobIdFromLocation, POLYMER_MD_DEFAULTS, POLYMER_MD_DRAFT_KEY, readPolymerMdDraft, savePolymerMdDraft, validatePolymerMdRequest, polymerMdSamplingEstimate, stageLabel } from "./config";
beforeEach(() => { sessionStorage.clear(); window.history.replaceState(null, "", "/md-simulation"); });
describe("formal polymer request boundaries", () => {
  it("accepts the five native forcefields and respects an explicit server allowlist", () => {
    for (const forcefield of POLYMER_MD_FORCEFIELDS) expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, forcefield })).not.toHaveProperty("forcefield");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, forcefield: "unknown" } as unknown as PolymerMdRequest)).toHaveProperty("forcefield");
    const capabilities = { supported_forcefields: ["Dreiding"], limits: { max_total_atoms: 20000, max_rnemd_atoms: 60000 } } as PolymerMdCapabilities;
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, forcefield: "GAFF2" }, capabilities)).toHaveProperty("forcefield");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, forcefield: "Dreiding" }, capabilities)).not.toHaveProperty("forcefield");
  });
  it("enforces chain-level integers and replicated system budgets", () => {
    expect(validatePolymerMdRequest(POLYMER_MD_DEFAULTS)).toEqual({});
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, n_atom: 100.2 })).toHaveProperty("n_atom");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, n_atom: 5000, n_chain: 5 })).toHaveProperty("n_chain");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, calculation: "basic_tc", n_atom: 2000, n_chain: 10, tc_rep: 4 })).toHaveProperty("tc_rep");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, max_additional: 3 })).toHaveProperty("max_additional");
  });
  it("labels additional scientific stages and checks SP's molecular-count requirement", () => {
    const native = { opt_method: "HF" as const, opt_basis: "6-31G(d)", charge: "RESP" as const, charge_method: "HF" as const,
      charge_basis: "6-31G(d)", sp_method: "HF" as const, sp_basis: "6-31G(d)", polar_method: "HF" as const,
      polar_basis: "6-31G(d)", properties: ["sp_total"] };
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, n_chain: 1, native_configuration: native })).toHaveProperty("n_chain");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, n_chain: 2, native_configuration: native })).not.toHaveProperty("n_chain");
    expect(stageLabel("sp")).toBe("SP采样与能量重算");
    expect(stageLabel("sp_analyze")).toBe("SP与内聚能分析");
    expect(stageLabel("tg")).toBe("高温平衡与分段降温");
    expect(stageLabel("tg_analyze")).toBe("Tg拟合与质量评估");
  });
  it("validates explicit model configuration and defers chemical sizing for non-atom targets", () => {
    const model = { version: 1 as const, mode: "homopolymer" as const, components: [{ id: "A", smiles: "*CC*" }],
      chain_length: { mode: "dp" as const, value: 2 } };
    const request = { ...POLYMER_MD_DEFAULTS, n_atom: 5000, n_chain: 20, model_configuration: model };
    expect(validatePolymerMdRequest(request)).toEqual({});
    expect(validatePolymerMdRequest({ ...request, calculation: "basic_tc", tc_rep: 5 })).toEqual({});
    expect(validatePolymerMdRequest({ ...request, model_configuration: { ...model,
      chain_length: { mode: "atoms", value: 5000 } } })).toHaveProperty("n_chain");
    expect(validatePolymerMdRequest({ ...request, model_configuration: { ...model,
      components: [{ id: "A", smiles: "*CO*" }] } })).toHaveProperty("model_configuration");
    savePolymerMdDraft(request);
    expect(readPolymerMdDraft()).toEqual(request);
  });
  it("rejects invalid repeat units and permits a backend-generated effective seed", () => {
    for (const smiles of ["CC", "*CC*.*O*", "*CC*,*O*", "*CC*\n", "聚合物**"]) expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, smiles })).toHaveProperty("smiles");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, seed: null })).not.toHaveProperty("seed");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, seed: 0 })).toHaveProperty("seed");
  });
  it("persists a versioned private draft without touching example draft storage", () => {
    sessionStorage.setItem("nexpoly:md-simulation:draft", "demo"); savePolymerMdDraft({ ...POLYMER_MD_DEFAULTS, terminal_tail: "OH" });
    expect(POLYMER_MD_DRAFT_KEY.startsWith("nexpoly:")).toBe(true); expect(readPolymerMdDraft().terminal_tail).toBe("OH"); expect(sessionStorage.getItem("nexpoly:md-simulation:draft")).toBe("demo");
    sessionStorage.setItem(POLYMER_MD_DRAFT_KEY, JSON.stringify({ version: 1, request: { ...POLYMER_MD_DEFAULTS, terminal_tail: "unexpected" } })); expect(readPolymerMdDraft()).toEqual(POLYMER_MD_DEFAULTS);
  });
  it("accepts UUID job deep links and never requests path traversal identifiers", () => {
    window.history.replaceState(null, "", "/md-simulation?job=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"); expect(jobIdFromLocation()).toBe("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee");
    window.history.replaceState(null, "", "/md-simulation?job=../../secret"); expect(jobIdFromLocation()).toBeNull();
  });
  it("prevents formally valid ranges from producing an unusable physical analysis window", () => {
    const short = { ...POLYMER_MD_DEFAULTS, eq_ns: 1 };
    expect(polymerMdSamplingEstimate(short).equilibrium.usablePs).toBe(0);
    expect(validatePolymerMdRequest(short)).toHaveProperty("eq_ns");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, sample_interval_ps: 10 })).toHaveProperty("sample_interval_ps");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, eq_ns: 7, additional_ns: 7, sample_interval_ps: 10 })).toEqual({});
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, eq_ns: 2.5, additional_ns: 2.5 })).toEqual({});
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, eq_ns: 2.499 })).toHaveProperty("eq_ns");
  });
  it("checks every enabled additional stage independently instead of accumulating short segments", () => {
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, additional_ns: 1, max_additional: 2 })).toHaveProperty("additional_ns");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, additional_ns: .01, max_additional: 0 })).toEqual({});
    expect(polymerMdSamplingEstimate({ ...POLYMER_MD_DEFAULTS, max_additional: 0 }).additional).toBeNull();
  });
  it("uses the actual integration grid and requires cadence-aligned stage endings", () => {
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, sample_interval_ps: 3 })).toHaveProperty("eq_ns");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, eq_ns: 2.050255, sample_interval_ps: .10051 })).toHaveProperty("sample_interval_ps");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, eq_ns: 6, additional_ns: 6, sample_interval_ps: 3 })).toEqual({});
  });
  it("requires complete post-burn-in RNEMD temperature blocks without changing basic-only requests", () => {
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, calculation: "basic_tc", tc_ns: .8 })).toHaveProperty("tc_ns");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, calculation: "basic_tc", tc_ns: .8038 })).toHaveProperty("tc_ns");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, calculation: "basic_tc", tc_ns: .804 })).toEqual({});
    expect(polymerMdSamplingEstimate({ ...POLYMER_MD_DEFAULTS, calculation: "basic_tc", tc_ns: .804 }).thermal?.estimatedProfiles).toBe(20);
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, calculation: "basic_tc", tc_ns: .8041 })).toHaveProperty("tc_ns");
    expect(validatePolymerMdRequest({ ...POLYMER_MD_DEFAULTS, tc_ns: .01 })).toEqual({});
  });
  it("preserves an older range-valid draft so new sampling errors can be repaired explicitly", () => {
    savePolymerMdDraft({ ...POLYMER_MD_DEFAULTS, eq_ns: 1, sample_interval_ps: 10 });
    const restored = readPolymerMdDraft();
    expect(restored.eq_ns).toBe(1);
    expect(restored.sample_interval_ps).toBe(10);
    expect(validatePolymerMdRequest(restored)).toHaveProperty("eq_ns");
  });
});
