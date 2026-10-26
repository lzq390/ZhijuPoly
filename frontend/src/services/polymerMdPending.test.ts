// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import type { PolymerMdRequest, PolymerMdModelConfiguration } from "../types/polymerMd";
import { POLYMER_MD_DEFAULTS } from "../components/polymer-md/config";
import { defaultPolymerMdUserConfiguration, mapPolymerMdUserConfiguration } from "../components/polymer-md/userConfiguration";
import { POLYMER_MD_FORCEFIELDS } from "./polymerMdNativeConfiguration";
import { createPolymerMdPending, POLYMER_MD_PENDING_KEY, polymerMdRequestFingerprint, readPolymerMdPending, writePolymerMdPending } from "./polymerMdPending";

beforeEach(() => sessionStorage.clear());
function nativeRequest(): PolymerMdRequest {
  const request = mapPolymerMdUserConfiguration({ ...defaultPolymerMdUserConfiguration(), properties: ["density"] }, POLYMER_MD_DEFAULTS).request!;
  delete request.model_configuration;
  // Historical API subsets remain replayable even though the MD form no longer
  // exposes independent molecular-only calculations.
  return { ...request, native_configuration: { ...request.native_configuration!, properties: ["density", "qm_homo"] } };
}

describe("durable native polymer submissions", () => {
  it("stores and restores all five forcefields with exact native methods and property subsets", () => {
    for (const forcefield of POLYMER_MD_FORCEFIELDS) {
      const request = { ...nativeRequest(), forcefield };
      const value = createPolymerMdPending(request);
      expect(readPolymerMdPending()).toEqual(value);
      expect(readPolymerMdPending()?.request).toEqual(request);
    }
  });
  it("restores legacy pending requests unchanged without injecting native fields or managed defaults", () => {
    const request = { ...POLYMER_MD_DEFAULTS, eq_ns: 10, seed: 57 };
    const value = createPolymerMdPending(request);
    expect(readPolymerMdPending()).toEqual(value);
    expect(readPolymerMdPending()?.request).not.toHaveProperty("native_configuration");
    expect(readPolymerMdPending()?.request.eq_ns).toBe(10);
  });
  it("rejects unsupported, missing, empty and duplicated native choices before storage", () => {
    const request = nativeRequest(), native = request.native_configuration!;
    const changes: Record<string, unknown>[] = [
      { properties: [] }, { properties: ["density", "density"] }, { properties: ["temperature"] },
      { properties: ["electronic_corrected_dielectric"] }, { properties: ["unmapped_diffusivity"] },
      { opt_method: "b3lyp" }, { opt_basis: "def2-SVP" }, { charge: "unknown" },
      { charge_basis: null }, { sp_method: undefined }, { unknown_parameter: "value" }
    ];
    for (const change of changes) {
      expect(() => createPolymerMdPending({ ...request, native_configuration: { ...native, ...change } } as PolymerMdRequest)).toThrow("原生选项");
      expect(sessionStorage.getItem(POLYMER_MD_PENDING_KEY)).toBeNull();
    }
    for (const change of [{ native_configuration: null }, { forcefield: "unknown" }, { unexpected: true }, { temp: NaN }]) {
      expect(() => createPolymerMdPending({ ...request, ...change } as unknown as PolymerMdRequest)).toThrow("原生选项");
    }
  });
  it("requires the thermal calculation to match the selected thermal conductivity target", () => {
    const request = nativeRequest();
    expect(() => createPolymerMdPending({ ...request, calculation: "basic_tc" })).toThrow("原生选项");
    for (const property of ["thermal_conductivity", "thermal_diffusivity"]) {
      const thermal = { ...request, native_configuration: { ...request.native_configuration!, properties: [property] } };
      expect(() => createPolymerMdPending(thermal)).toThrow("原生选项");
      expect(createPolymerMdPending({ ...thermal, calculation: "basic_tc" }).request.calculation).toBe("basic_tc");
    }
  });
  it("persists Tg and SP targets without inventing thermal dependencies or accepting a one-chain SP request", () => {
    const request = nativeRequest();
    for (const properties of [["tg"], ["sp_total"], ["sp_ced"], ["tg", "sp_total", "sp_ced"]]) {
      const selected = { ...request, n_chain: 2, native_configuration: { ...request.native_configuration!, properties } };
      expect(createPolymerMdPending(selected).request).toEqual(selected);
      expect(readPolymerMdPending()?.request).toEqual(selected);
      expect(selected.calculation).toBe("basic");
    }
    const invalid = { ...request, n_chain: 1, native_configuration: { ...request.native_configuration!, properties: ["sp_total"] } };
    expect(() => createPolymerMdPending(invalid)).toThrow("原生选项");
  });
  it("validates both replay reads and pending updates without overwriting a valid record", () => {
    const pending = createPolymerMdPending(nativeRequest());
    const raw = sessionStorage.getItem(POLYMER_MD_PENDING_KEY);
    const invalid = { ...pending, request: { ...pending.request, native_configuration: { ...pending.request.native_configuration!, properties: ["removed_property"] } } };
    expect(() => writePolymerMdPending(invalid)).toThrow("原生选项");
    expect(sessionStorage.getItem(POLYMER_MD_PENDING_KEY)).toBe(raw);
    sessionStorage.setItem(POLYMER_MD_PENDING_KEY, JSON.stringify(invalid));
    expect(() => readPolymerMdPending()).toThrow("记录已损坏");
    expect(sessionStorage.getItem(POLYMER_MD_PENDING_KEY)).not.toBeNull();
  });
  it("fingerprints native field and property order canonically while preserving target differences", () => {
    const request = nativeRequest();
    const reordered = { ...request, native_configuration: Object.fromEntries(Object.entries(request.native_configuration!).reverse()) } as PolymerMdRequest;
    reordered.native_configuration!.properties = [...reordered.native_configuration!.properties].reverse();
    expect(polymerMdRequestFingerprint(reordered)).toBe(polymerMdRequestFingerprint(request));
    expect(polymerMdRequestFingerprint({ ...request, native_configuration: { ...request.native_configuration!, properties: ["density"] } })).not.toBe(polymerMdRequestFingerprint(request));
    expect(polymerMdRequestFingerprint(POLYMER_MD_DEFAULTS)).not.toContain("native_configuration");
  });
  it("persists exact model configurations and distinguishes composition or block order changes", () => {
    const model: PolymerMdModelConfiguration = { version: 1, mode: "block", components: [
      { id: "A", smiles: "*CC*" }, { id: "B", smiles: "*CCO*" }],
      blocks: [{ component_id: "A", dp: 10 }, { component_id: "B", dp: 20 }] };
    const request = { ...nativeRequest(), model_configuration: model };
    const pending = createPolymerMdPending(request);
    expect(readPolymerMdPending()?.request).toEqual(request);
    expect(readPolymerMdPending()?.key).toBe(pending.key);
    const reordered = { ...request, model_configuration: { blocks: model.blocks, components: model.components, mode: model.mode, version: model.version } };
    expect(polymerMdRequestFingerprint(reordered)).toBe(polymerMdRequestFingerprint(request));
    expect(polymerMdRequestFingerprint({ ...request, model_configuration: { ...model, blocks: [...model.blocks!].reverse() } }))
      .not.toBe(polymerMdRequestFingerprint(request));
  });
  it("rejects malformed model data without replacing a valid pending request", () => {
    const valid = createPolymerMdPending(nativeRequest());
    const saved = sessionStorage.getItem(POLYMER_MD_PENDING_KEY);
    const model: PolymerMdModelConfiguration = { version: 1, mode: "random", components: [
      { id: "A", smiles: "*CC*" }, { id: "B", smiles: "*CCO*" }], chain_length: { mode: "dp", value: 20 }, ratios: [.5, .5] };
    const bad: unknown[] = [null, { ...model, ratios: [.1, .1] }, { ...model, unknown: true },
      { ...model, components: [{ id: "A", smiles: "*CO*" }, model.components[1]] }];
    for (const value of bad) expect(() => writePolymerMdPending({ ...valid,
      request: { ...valid.request, model_configuration: value } } as typeof valid)).toThrow("原生选项");
    expect(sessionStorage.getItem(POLYMER_MD_PENDING_KEY)).toBe(saved);
  });
});
