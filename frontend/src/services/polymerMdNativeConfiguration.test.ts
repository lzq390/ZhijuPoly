import { describe, expect, it } from "vitest";
import type { PolymerMdModelConfiguration } from "../types/polymerMd";
import { POLYMER_MD_DEFAULTS } from "../components/polymer-md/config";
import { estimatePolymerMdRequestedAtoms, validPolymerMdModelConfiguration, validPolymerMdModelRequest,
  validPolymerMdNativeConfiguration, validPolymerMdPostEquilibriumRequest } from "./polymerMdNativeConfiguration";

const components = [{ id: "A", smiles: "*CC*" }, { id: "B", smiles: "*CCO*" }];
function model(mode: PolymerMdModelConfiguration["mode"]): PolymerMdModelConfiguration {
  const common = { version: 1 as const, mode, components };
  if (mode === "block") return { ...common, blocks: [{ component_id: "A", dp: 10 }, { component_id: "B", dp: 20 }, { component_id: "A", dp: 10 }] };
  if (mode === "blend") return { ...common, components: components.map(component => ({ ...component, n_chain: 2,
    chain_length: { mode: "dp", value: 20 } })) };
  return { ...common, components: mode === "homopolymer" ? components.slice(0, 1) : components,
    chain_length: { mode: "atoms", value: 1000 }, ...(mode === "random" ? { ratios: [.4, .6] } : {}) };
}

describe("exact polymer model configuration validation", () => {
  it("accepts five explicit model structures and all supported chain length measures", () => {
    for (const mode of ["homopolymer", "alternating", "random", "block", "blend"] as const) {
      expect(validPolymerMdModelConfiguration(model(mode)), mode).toBe(true);
      expect(validPolymerMdModelRequest({ ...POLYMER_MD_DEFAULTS, n_chain: 4, model_configuration: model(mode) }), mode).toBe(true);
    }
    for (const chain_length of [{ mode: "atoms", value: 100 }, { mode: "atoms", value: 5000 }, { mode: "dp", value: 1 },
      { mode: "dp", value: 5000 }, { mode: "molecular_weight", value: 1000.5 }])
      expect(validPolymerMdModelConfiguration({ ...model("homopolymer"), chain_length })).toBe(true);
  });
  it("rejects unknown, mismatched, duplicate or ambiguous model information", () => {
    const random = model("random");
    for (const value of [null, {}, { ...random, version: 2 }, { ...random, mode: "crosslinked" }, { ...random, unknown: true },
      { ...random, components: [components[0], components[0]] }, { ...random, components: [{ id: "../A", smiles: "*CC*" }, components[1]] },
      { ...random, components: [{ id: "A", smiles: "CC" }, components[1]] }, { ...random, ratios: [.2, .2] },
      { ...random, ratios: [0, 1] }, { ...random, ratios: [NaN, .5] }, { ...random, ratios: [true, false] },
      { ...random, chain_length: { mode: "dp", value: 1.5 } }, { ...random, chain_length: { mode: "atoms", value: 99 } },
      { ...random, chain_length: { mode: "molecular_weight", value: Infinity } },
      { ...model("alternating"), ratios: [.5, .5] }, { ...model("block"), chain_length: { mode: "dp", value: 1 } },
      { ...model("blend"), components }, { ...model("block"), blocks: [{ component_id: "A", dp: 10 }, { component_id: "C", dp: 1 }] }])
      expect(validPolymerMdModelConfiguration(value), JSON.stringify(value)).toBe(false);
    expect(validPolymerMdModelRequest({ ...POLYMER_MD_DEFAULTS, smiles: "*CO*", model_configuration: random })).toBe(false);
    expect(validPolymerMdModelRequest({ ...POLYMER_MD_DEFAULTS, n_chain: 3, model_configuration: model("blend") })).toBe(false);
    expect(validPolymerMdModelRequest(POLYMER_MD_DEFAULTS)).toBe(true);
    const quantum = { opt_method: "HF" as const, opt_basis: "6-31G(d)", charge: "RESP" as const, charge_method: "HF" as const,
      charge_basis: "6-31G(d)", sp_method: "HF" as const, sp_basis: "6-31G(d)", polar_method: "HF" as const,
      polar_basis: "6-31G(d)", properties: ["qm_homo"] };
    expect(validPolymerMdModelRequest({ ...POLYMER_MD_DEFAULTS, native_configuration: quantum })).toBe(true);
    expect(validPolymerMdModelRequest({ ...POLYMER_MD_DEFAULTS, native_configuration: quantum,
      model_configuration: model("homopolymer") })).toBe(false);
  });
  it("does not use legacy atom placeholders to estimate DP, molecular weight or block targets", () => {
    const legacy = { ...POLYMER_MD_DEFAULTS, n_atom: 5000, n_chain: 20 };
    expect(estimatePolymerMdRequestedAtoms(legacy)).toBe(100000);
    expect(estimatePolymerMdRequestedAtoms({ ...legacy, model_configuration: model("homopolymer") })).toBe(20000);
    expect(estimatePolymerMdRequestedAtoms({ ...legacy, model_configuration: { ...model("homopolymer"),
      chain_length: { mode: "dp", value: 2 } } })).toBeNull();
    expect(estimatePolymerMdRequestedAtoms({ ...legacy, model_configuration: model("block") })).toBeNull();
    const blend = model("blend");
    blend.components = blend.components.map(component => ({ ...component, chain_length: { mode: "atoms", value: 1000 } }));
    expect(estimatePolymerMdRequestedAtoms({ ...legacy, n_chain: 4, model_configuration: blend })).toBe(4000);
  });
  it("validates the supported post-equilibrium target keys and SP chain-count condition", () => {
    const quantum = { opt_method: "HF" as const, opt_basis: "6-31G(d)", charge: "RESP" as const, charge_method: "HF" as const,
      charge_basis: "6-31G(d)", sp_method: "HF" as const, sp_basis: "6-31G(d)", polar_method: "HF" as const,
      polar_basis: "6-31G(d)", properties: ["tg"] };
    expect(validPolymerMdNativeConfiguration(quantum)).toBe(true);
    expect(validPolymerMdPostEquilibriumRequest({ ...POLYMER_MD_DEFAULTS, n_chain: 1, native_configuration: quantum })).toBe(true);
    for (const key of ["sp_total", "sp_ced"]) {
      const native_configuration = { ...quantum, properties: [key] };
      expect(validPolymerMdNativeConfiguration(native_configuration)).toBe(true);
      expect(validPolymerMdPostEquilibriumRequest({ ...POLYMER_MD_DEFAULTS, n_chain: 1, native_configuration })).toBe(false);
      expect(validPolymerMdPostEquilibriumRequest({ ...POLYMER_MD_DEFAULTS, n_chain: 2, native_configuration })).toBe(true);
    }
    expect(validPolymerMdNativeConfiguration({ ...quantum, properties: ["sp_vdw"] })).toBe(false);
  });
});
