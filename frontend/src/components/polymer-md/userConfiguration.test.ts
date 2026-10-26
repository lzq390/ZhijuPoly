// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GUEST_SESSION, installSession, retireSession } from "../../auth/session";
import { POLYMER_MD_NATIVE_FIELDS } from "../../services/polymerMdNativeConfiguration";
import { POLYMER_MD_DEFAULTS } from "./config";
import { expandPolymerMdProperties, POLYMER_MD_BASE_PROPERTIES, POLYMER_MD_SINGLE_POINT_PROPERTIES,
  POLYMER_MD_TG_PROPERTY, POLYMER_MD_SOLUBILITY_PROPERTIES } from "./propertyGroups";
import { defaultPolymerMdUserConfiguration, fixedPolymerMdExecutionRequest, mapPolymerMdUserConfiguration,
  polymerMdUserConfigurationSpec, POLYMER_MD_USER_CONFIGURATION_KEY, readPolymerMdUserConfiguration, savePolymerMdUserConfiguration } from "./userConfiguration";

beforeEach(() => { vi.restoreAllMocks(); sessionStorage.clear(); retireSession(); installSession(GUEST_SESSION); });
describe("RadonPy native configuration and execution mapping", () => {
  it("preserves every native method and forcefield while including shared analysis and grouped outputs", () => {
    const defaults = defaultPolymerMdUserConfiguration();
    for (const field of polymerMdUserConfigurationSpec.fields) {
      for (const option of field.options) {
        const configuration = { ...defaults, properties: ["density"],
          [field.id]: field.selection === "multiple" ? [option.value] : option.value };
        const result = mapPolymerMdUserConfiguration(configuration, { ...POLYMER_MD_DEFAULTS, tc_axis: "z" });
        if (field.id === "properties" && option.value.startsWith("qm_")) {
          expect(result.request).toBeNull();
          expect(result.unmapped.join(" ")).toContain("聚合物 MD");
          continue;
        }
        expect(result.unmapped, `${field.id}:${option.value}`).toEqual([]);
        expect(result.request?.forcefield).toBe(configuration.forcefield);
        expect(Object.keys(result.request!.native_configuration!).sort()).toEqual([...POLYMER_MD_NATIVE_FIELDS].sort());
        for (const key of POLYMER_MD_NATIVE_FIELDS) expect(result.request!.native_configuration![key]).toEqual(
          key === "properties" ? expandPolymerMdProperties(configuration.properties) : configuration[key]);
        expect(result.executedProperties).toEqual(expandPolymerMdProperties(configuration.properties));
        expect(result.request?.calculation).toBe(result.executedProperties.includes("thermal_conductivity") ? "basic_tc" : "basic");
        expect(result.request?.tc_axis).toBe("z");
      }
    }
  });
  it("includes the complete base analysis when no additional calculation is selected", () => {
    const defaults = defaultPolymerMdUserConfiguration();
    const expected = ["Cp", "Cv", "Rg", "bulk_modulus", "compressibility", "density", "isentropic_bulk_modulus",
      "isentropic_compressibility", "linear_expansion", "nematic_order_parameter", "r2", "self-diffusion",
      "static_dielectric_const", "volume_expansion"];
    expect([...POLYMER_MD_BASE_PROPERTIES].sort()).toEqual(expected);
    for (const properties of [[], ["density"], ["r2", "density"]]) {
      const configuration = { ...defaults, properties };
      const before = JSON.stringify(configuration);
      const result = mapPolymerMdUserConfiguration(configuration, POLYMER_MD_DEFAULTS);
      expect(result.unmapped).toEqual([]);
      expect(result.request?.native_configuration?.properties).toEqual(expected);
      expect(result.executedProperties).toEqual(expected);
      expect(result.request?.calculation).toBe("basic");
      expect(JSON.stringify(configuration)).toBe(before);
    }
    const mapped = mapPolymerMdUserConfiguration(defaults, POLYMER_MD_DEFAULTS);
    expect(mapped.request).toMatchObject(POLYMER_MD_DEFAULTS);
    expect(mapped.request?.native_configuration?.properties).toEqual([...(defaults.properties as string[])].sort());
  });
  it("preserves standalone QM drafts for explicit repair while keeping them out of new MD submissions", () => {
    const defaults = defaultPolymerMdUserConfiguration();
    const singlePoint = ["qm_dipole", "qm_homo", "qm_lumo", "qm_total_energy"];
    expect([...POLYMER_MD_SINGLE_POINT_PROPERTIES].sort()).toEqual(singlePoint);
    for (const key of singlePoint) {
      const configuration = { ...defaults, properties: [key] };
      const result = mapPolymerMdUserConfiguration(configuration, POLYMER_MD_DEFAULTS);
      expect(result.request).toBeNull();
      expect(result.unmapped.join(" ")).toContain("单体量化");
      expect(configuration.properties).toEqual([key]);
    }
  });
  it("keeps independent polarizability, optics and thermal choices distinct", () => {
    const defaults = defaultPolymerMdUserConfiguration();
    for (const key of ["refractive_index", "thermal_conductivity"]) {
      const result = mapPolymerMdUserConfiguration({ ...defaults, properties: [key] }, { ...POLYMER_MD_DEFAULTS, tc_axis: "y" });
      expect(result.request?.native_configuration?.properties).toEqual([...POLYMER_MD_BASE_PROPERTIES, key].sort());
      expect(result.request?.calculation).toBe(key === "thermal_conductivity" ? "basic_tc" : "basic");
      expect(result.request?.tc_axis).toBe("y");
    }
    const selected = ["thermal_diffusivity", "refractive_index"];
    const all = mapPolymerMdUserConfiguration({ ...defaults, properties: selected }, POLYMER_MD_DEFAULTS);
    expect(all.executedProperties).toEqual([...POLYMER_MD_BASE_PROPERTIES,
      "thermal_diffusivity", "refractive_index", "thermal_conductivity"].sort());
    expect(all.request?.calculation).toBe("basic_tc");
  });
  it("selects Tg without enabling RNEMD or injecting user sampling settings", () => {
    const defaults = defaultPolymerMdUserConfiguration();
    const result = mapPolymerMdUserConfiguration({ ...defaults, properties: [POLYMER_MD_TG_PROPERTY] }, POLYMER_MD_DEFAULTS);
    expect(result.unmapped).toEqual([]);
    expect(result.executedProperties).toEqual([...POLYMER_MD_BASE_PROPERTIES, "tg"].sort());
    expect(result.request?.calculation).toBe("basic");
    expect(result.request).not.toHaveProperty("posteq_configuration");
    expect(result.request?.eq_ns).toBe(POLYMER_MD_DEFAULTS.eq_ns);
  });
  it("compiles SP and CED as one rerun group and rejects a one-chain cell without changing the draft", () => {
    const defaults = defaultPolymerMdUserConfiguration();
    expect(POLYMER_MD_SOLUBILITY_PROPERTIES).toEqual(["sp_total", "sp_ced"]);
    for (const key of POLYMER_MD_SOLUBILITY_PROPERTIES) {
      const configuration = { ...defaults, properties: [key] };
      const result = mapPolymerMdUserConfiguration(configuration, { ...POLYMER_MD_DEFAULTS, n_chain: 2 });
      expect(result.executedProperties).toEqual([...POLYMER_MD_BASE_PROPERTIES, "sp_total", "sp_ced"].sort());
      expect(result.request?.calculation).toBe("basic");
      const rejected = mapPolymerMdUserConfiguration(configuration, { ...POLYMER_MD_DEFAULTS, n_chain: 1 });
      expect(rejected.request).toBeNull();
      expect(rejected.unmapped.join(" ")).toContain("至少2条");
      expect(configuration.properties).toEqual([key]);
    }
    const result = mapPolymerMdUserConfiguration({ ...defaults,
      properties: ["tg", "sp_ced", "thermal_diffusivity", "refractive_index"] }, POLYMER_MD_DEFAULTS);
    expect(result.request?.calculation).toBe("basic_tc");
    expect(result.executedProperties).toEqual([...POLYMER_MD_BASE_PROPERTIES, "tg", "sp_ced", "sp_total",
      "thermal_conductivity", "thermal_diffusivity", "refractive_index"].sort());
    expect(result.executedProperties).toHaveLength(20);
  });
  it("blocks duplicate, removed, malformed and unknown selections while preserving the draft", () => {
    const defaults = defaultPolymerMdUserConfiguration();
    for (const properties of [["density", "density"], ["temperature"], ["total_energy"], ["electronic_corrected_dielectric"],
      ["glass_transition_temperature"], ["sp_vdw"], ["sp_ele"], ["future_property"]]) {
      const configuration = { ...defaults, properties };
      savePolymerMdUserConfiguration(configuration);
      expect(readPolymerMdUserConfiguration()).toEqual(configuration);
      const result = mapPolymerMdUserConfiguration(configuration, POLYMER_MD_DEFAULTS);
      expect(result.request).toBeNull();
      expect(result.unmapped.length).toBeGreaterThan(0);
      expect(result.executedProperties).toEqual([]);
    }
    expect(mapPolymerMdUserConfiguration({ ...defaults, properties: "density" }, POLYMER_MD_DEFAULTS).request).toBeNull();
    const missingProperties = { ...defaults };
    delete missingProperties.properties;
    expect(mapPolymerMdUserConfiguration(missingProperties, POLYMER_MD_DEFAULTS).request).toBeNull();
    const invalidChoices: Record<string, string>[] = [{ opt_method: "b3lyp" }, { opt_basis: "def2-SVP" }, { forcefield: "future-forcefield" }, { old_field: "retained" }];
    for (const change of invalidChoices) {
      expect(mapPolymerMdUserConfiguration({ ...defaults, ...change }, POLYMER_MD_DEFAULTS).request).toBeNull();
    }
  });
  it("uses managed timing defaults while retaining explicit model conditions and forcefield", () => {
    const base = { ...POLYMER_MD_DEFAULTS, forcefield: "Dreiding" as const, eq_ns: .01, additional_ns: .02, max_additional: 0,
      sample_interval_ps: 10, tc_ns: .01, tc_rep: 5, seed: 37, temp: 350, tc_axis: "y" as const };
    const frozen = JSON.stringify(base);
    expect(fixedPolymerMdExecutionRequest(base)).toEqual({ ...POLYMER_MD_DEFAULTS, forcefield: "Dreiding", temp: 350, tc_axis: "y" });
    expect(JSON.stringify(base)).toBe(frozen);
  });
  it("isolates saved selections by owner, login session and API", () => {
    const a = { authenticated: true, user: { id: "a", username: "a", must_change_password: false }, session_id: "a1", csrf_token: "test", capabilities: {} };
    installSession(a);
    const configuration = { ...defaultPolymerMdUserConfiguration(), forcefield: "Dreiding" };
    savePolymerMdUserConfiguration(configuration);
    installSession({ ...a, user: { ...a.user, id: "b" } });
    expect(readPolymerMdUserConfiguration()).toEqual(defaultPolymerMdUserConfiguration());
    installSession({ ...a, session_id: "a2" });
    expect(readPolymerMdUserConfiguration()).toEqual(defaultPolymerMdUserConfiguration());
    installSession(a);
    const value = JSON.parse(sessionStorage.getItem(POLYMER_MD_USER_CONFIGURATION_KEY)!);
    value.scope.api += "/another-api";
    sessionStorage.setItem(POLYMER_MD_USER_CONFIGURATION_KEY, JSON.stringify(value));
    expect(readPolymerMdUserConfiguration()).toEqual(defaultPolymerMdUserConfiguration());
  });
  it("preserves v1 and future-version drafts until an explicit migration or reset", () => {
    const configuration = { ...defaultPolymerMdUserConfiguration(), opt_method: "b3lyp", properties: ["temperature", "density"] };
    savePolymerMdUserConfiguration(configuration);
    for (const version of [1, 99]) {
      const value = JSON.parse(sessionStorage.getItem(POLYMER_MD_USER_CONFIGURATION_KEY)!);
      value.configuration_version = version;
      sessionStorage.setItem(POLYMER_MD_USER_CONFIGURATION_KEY, JSON.stringify(value));
      const restored = readPolymerMdUserConfiguration();
      expect(restored.opt_method).toBe("b3lyp");
      expect(restored.properties).toEqual(["temperature", "density"]);
      expect(restored.__configuration_version).toBe(String(version));
      expect(mapPolymerMdUserConfiguration(restored, POLYMER_MD_DEFAULTS).unmapped.join(" ")).toContain("迁移");
    }
  });
  it("reports failed persistence instead of claiming an unsaved selection is stored", () => {
    vi.spyOn(Storage.prototype, "getItem").mockReturnValue(null);
    expect(() => savePolymerMdUserConfiguration(defaultPolymerMdUserConfiguration())).toThrow("尚未保存");
  });
});
