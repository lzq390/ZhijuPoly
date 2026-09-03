import { describe, expect, it } from "vitest";
import {
  buildConfigFromStructuredDraft,
  estimatedFormalSteps,
  restoreManagedPaths,
  structuredDraftFromConfig,
  validateFormalConfig
} from "./config";

const densityConfig = {
  protocol: "Density",
  temperature: 298,
  natoms: 1000,
  components: { A: 1, B: 2 },
  smiles: { A: "CCO", B: "O" },
  unknown: { nested: [1, { keep: true }] },
  params_dir: "/unsafe/browser/path"
};

describe("monomer MD formal config", () => {
  it("validates common boundaries and protocol-specific rules", () => {
    expect(validateFormalConfig(densityConfig, "Density").valid).toBe(true);
    expect(validateFormalConfig({ ...densityConfig, temperature: Number.POSITIVE_INFINITY }, "Density").valid).toBe(false);
    expect(validateFormalConfig({ ...densityConfig, natoms: 10001 }, "Density").valid).toBe(false);
    expect(validateFormalConfig({ ...densityConfig, components: { A: 1 }, smiles: { B: "O" } }, "Density").valid).toBe(false);
    expect(validateFormalConfig({ ...densityConfig, components: { A: 1 }, smiles: { A: "*CC*" } }, "Density").valid).toBe(false);
    expect(validateFormalConfig({ ...densityConfig, protocol: "HVap" }, "HVap").errors).toContain("HVap 协议必须恰好包含一个组分。");
    expect(validateFormalConfig({ ...densityConfig, protocol: "Compressibility", npt_steps: 1_000_000 }, "Compressibility").valid).toBe(false);
    expect(validateFormalConfig({ ...densityConfig, protocol: "Compressibility", npt_steps: 1_000_001 }, "Compressibility").valid).toBe(true);
  });

  it("distinguishes explicit dielectric template values from implicit fallbacks", () => {
    const implicit = { ...densityConfig, protocol: "Dielectric" };
    const explicit = {
      ...implicit,
      npt_steps: 2_000_000,
      nvt_steps: 8_000_000,
      dipole_interval: 1000
    };
    expect(estimatedFormalSteps("Dielectric", implicit)).toBe(8_000_000);
    expect(estimatedFormalSteps("Dielectric", explicit)).toBe(10_000_000);
  });

  it("patches known structured paths while preserving unknown nested data and missing optional fields", () => {
    const base = { ...densityConfig, protocol: "Dielectric" };
    const draft = structuredDraftFromConfig(base);
    draft.temperature = "310";
    draft.components[0].name = "RENAMED";
    const built = buildConfigFromStructuredDraft(base, "Dielectric", draft);

    expect(built.errors).toEqual([]);
    expect(built.config).toMatchObject({
      temperature: 310,
      components: { RENAMED: 1, B: 2 },
      smiles: { RENAMED: "CCO", B: "O" },
      unknown: { nested: [1, { keep: true }] }
    });
    expect(built.config).not.toHaveProperty("npt_steps");
    expect(built.config).not.toHaveProperty("nvt_steps");
    expect(built.config).not.toHaveProperty("dipole_interval");
  });

  it("rejects duplicate component names without mutating the canonical object", () => {
    const before = JSON.stringify(densityConfig);
    const draft = structuredDraftFromConfig(densityConfig);
    draft.components[1].name = "A";
    const built = buildConfigFromStructuredDraft(densityConfig, "Density", draft);
    expect(built.config).toBeNull();
    expect(built.errors).toContain("组分名称必须唯一。");
    expect(JSON.stringify(densityConfig)).toBe(before);
  });

  it("restores every managed path immediately before submission", () => {
    expect(restoreManagedPaths(densityConfig)).toMatchObject({
      params_dir: "managed_params",
      output_dir: "managed_output",
      working_dir: "managed_working"
    });
    expect(densityConfig.params_dir).toBe("/unsafe/browser/path");
  });
});
