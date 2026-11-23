// @vitest-environment node
import { createRequire } from "node:module";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { createKetcherAdapter, dropStarAtomLabels, type KetcherInstance } from "./editor";
import fixture from "./__fixtures__/wildcard-stereo-export.json";

// Exercise the installed local Indigo WASM. No server, image inference, or
// provider request is involved; the captured structure is the public demo PNG.
const require = createRequire(import.meta.url);
let indigo: { MapStringString: new () => { set(key: string, value: string): void; delete(): void };
  convert(source: string, format: string, options: unknown): string;
  layout(source: string, format: string, options: unknown): string };
beforeAll(async () => { indigo = await require("indigo-ketcher")({ print: () => {}, printErr: () => {} }); });
function convert(source: string, format = "smiles", settings: Record<string, unknown> = {}) {
  const options = new indigo.MapStringString();
  try {
    for (const [key, value] of Object.entries(settings)) options.set(key, String(value));
    return indigo.convert(source, format, options);
  } finally { options.delete(); }
}
function sdkFor(molfile = fixture.native_molfile) {
  return {
    getSmiles: vi.fn(async () => fixture.defective_sdk_smiles),
    getMolfile: vi.fn(async (_format?: "v2000" | "v3000") => molfile),
    structService: { convert: vi.fn(async (data: { struct: string; input_format?: string; output_format: string }, settings?: Record<string, unknown>) => {
      expect(data.input_format).toBe("chemical/x-mdl-molfile");
      expect(data.output_format).toBe("chemical/x-daylight-smiles");
      return { struct: convert(data.struct, "smiles", settings) };
    }) }
  } satisfies KetcherInstance;
}
function layoutMolfile(source: string) {
  const options = new indigo.MapStringString();
  // Stereo needs actual 2D wedge/bond geometry, as it has in the native canvas.
  try { return indigo.layout(source, "molfile", options); }
  finally { options.delete(); }
}

describe("native Molfile SMILES export", () => {
  it("reproduces the KET lost-H defect and retains both stereochemical H through the adapter", async () => {
    expect(convert(fixture.native_ket)).toBe(fixture.defective_sdk_smiles);
    expect(convert(fixture.native_ket).match(/\[C@@?H\]/g) || []).toHaveLength(0);
    const sdk = sdkFor();
    const actual = await createKetcherAdapter(sdk).getSmiles();
    expect(actual.match(/\[C@@?H\]/g)).toHaveLength(2);
    expect(actual).toContain("*");
    // Indigo labels its star atoms; the adapter drops only those labels and keeps Indigo's SMILES.
    expect(convert(fixture.native_molfile)).toContain("star_e");
    expect(actual).not.toContain("star_e");
    expect(actual).toBe(dropStarAtomLabels(convert(fixture.native_molfile)));
    expect(sdk.getSmiles).not.toHaveBeenCalled();
    expect(sdk.getMolfile).toHaveBeenCalledWith("v2000");
  });

  it.each(["*CC*", "[13CH3][C@@H]([NH3+])C(=O)[O-]", "F/C=C/Cl", "[CH2]C"]) (
    "preserves wildcard, isotope/charge/stereo, bond stereo and radicals: %s", async source => {
      const molfile = layoutMolfile(source);
      const actual = await createKetcherAdapter(sdkFor(molfile)).getSmiles();
      // Compare with the real serializer's original input representation;
      // a separate RDKit test independently checks molecular equivalence.
      expect(actual).toBe(dropStarAtomLabels(convert(source)));
    }
  );

  it("passes editor conversion settings to the same local service", async () => {
    const sdk = { ...sdkFor(), editor: { serverSettings: { "ignore-stereochemistry-errors": true } } };
    await createKetcherAdapter(sdk).getSmiles();
    expect(sdk.structService.convert.mock.calls[0][1]).toBe(sdk.editor.serverSettings);
  });

  it("uses explicit V3000 after a V2000 capacity error", async () => {
    const molfile = convert(fixture.native_molfile, "molfile", { "molfile-saving-mode": "3000" });
    expect(molfile).toContain("V3000");
    const sdk = sdkFor(molfile);
    sdk.getMolfile.mockRejectedValueOnce(new Error("v2000 capacity"));
    const actual = await createKetcherAdapter(sdk).getSmiles();
    expect(actual.match(/\[C@@?H\]/g)).toHaveLength(2);
    expect(sdk.getMolfile.mock.calls.map(call => call[0])).toEqual(["v2000", "v3000"]);
    expect(sdk.getSmiles).not.toHaveBeenCalled();
  });

  it("returns an empty SMILES only for a valid empty native canvas", async () => {
    const sdk = sdkFor("\n  Ketcher\n\n  0  0  0  0  0  0  0  0  0  0999 V2000\nM  END\n");
    expect(await createKetcherAdapter(sdk).getSmiles()).toBe("");
    expect(sdk.structService.convert).not.toHaveBeenCalled();
  });

  it("fails closed on malformed Molfile and conversion errors without using defective SDK output", async () => {
    const sdk = sdkFor("{\"root\":{}}");
    await expect(createKetcherAdapter(sdk).getSmiles()).rejects.toThrow("Molfile");
    expect(sdk.structService.convert).not.toHaveBeenCalled();
    sdk.getMolfile.mockResolvedValue(fixture.native_molfile);
    sdk.structService.convert.mockRejectedValueOnce(new Error("converter failed"));
    await expect(createKetcherAdapter(sdk).getSmiles()).rejects.toThrow("converter failed");
    sdk.structService.convert.mockResolvedValueOnce({ struct: fixture.native_molfile });
    await expect(createKetcherAdapter(sdk).getSmiles()).rejects.toThrow("SMILES");
    expect(sdk.getSmiles).not.toHaveBeenCalled();
  });

  it("does not start conversion after its Molfile read outlives the editor", async () => {
    let current = true, finish!: (value: string) => void;
    const sdk = sdkFor();
    sdk.getMolfile.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    const pending = createKetcherAdapter(sdk, undefined, () => current).getSmiles();
    current = false; finish(fixture.native_molfile);
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(sdk.structService.convert).not.toHaveBeenCalled();
  });

  it("does not publish a conversion that completes after editor retirement", async () => {
    let current = true, finish!: (value: { struct: string }) => void;
    const sdk = sdkFor();
    sdk.structService.convert.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const pending = createKetcherAdapter(sdk, undefined, () => current).getSmiles();
    await vi.waitFor(() => expect(sdk.structService.convert).toHaveBeenCalledOnce());
    current = false; finish({ struct: "CC" });
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  });

  it("preserves the supported reaction export path", async () => {
    const sdk = { ...sdkFor(), containsReaction: () => true };
    sdk.getSmiles.mockResolvedValue("CC>>C=C");
    expect(await createKetcherAdapter(sdk).getSmiles()).toBe("CC>>C=C");
    expect(sdk.structService.convert).not.toHaveBeenCalled();
  });
});

describe("star-atom labels", () => {
  function atomLabels(source: string) {
    return (JSON.parse(convert(source, "ket")).mol0.atoms as Array<{ label?: string; type?: string }>)
      .map((atom) => atom.label ?? atom.type);
  }

  it("drops only star_e labels and keeps every other CX field", () => {
    expect(dropStarAtomLabels("*CC* |$star_e;;;star_e$|")).toBe("*CC*");
    expect(dropStarAtomLabels("*CC* |$;;;$|")).toBe("*CC*");
    expect(dropStarAtomLabels("*CC(c1ccccc1)* |$star_e;;;;;;;;;star_e$|")).toBe("*CC(c1ccccc1)*");
    expect(dropStarAtomLabels("*C[C](*)C |^1:2,$star_e;;;star_e;$|")).toBe("*C[C](*)C |^1:2|");
    expect(dropStarAtomLabels("*CC* |$Q_e;;;star_e$|")).toBe("*CC* |$Q_e;;;$|");
    expect(dropStarAtomLabels("C[C@H](N)C(O)=O |&1:1|")).toBe("C[C@H](N)C(O)=O |&1:1|");
    expect(dropStarAtomLabels("*CC* |$_AV:star_e;;;$|")).toBe("*CC* |$_AV:star_e;;;$|");
    expect(dropStarAtomLabels("*CC*")).toBe("*CC*");
    expect(dropStarAtomLabels("")).toBe("");
  });

  it.each(["*CC*", "*CC(*)c1ccccc1", "*c1ccc(Oc2ccc(*)cc2)cc1"])(
    "loads %s back into Indigo with the same star atoms", (source) => {
      const exported = convert(layoutMolfile(source));
      expect(exported).toContain("star_e");
      expect(atomLabels(dropStarAtomLabels(exported))).toEqual(atomLabels(exported));
    }
  );

  it("keeps the labels Indigo uses for query atoms", () => {
    const query = (symbol: string) => convert([
      "", "  test", "",
      "  3  2  0  0  0  0  0  0  0  0999 V2000",
      `    0.0000    0.0000    0.0000 ${symbol.padEnd(3)} 0  0  0  0  0  0  0  0  0  0  0  0`,
      "    1.0000    0.0000    0.0000 C   0  0  0  0  0  0  0  0  0  0  0  0",
      `    2.0000    0.0000    0.0000 ${symbol.padEnd(3)} 0  0  0  0  0  0  0  0  0  0  0  0`,
      "  1  2  1  0", "  2  3  1  0", "M  END", ""
    ].join("\n"));
    for (const symbol of ["Q", "X"]) {
      const exported = query(symbol);
      expect(dropStarAtomLabels(exported)).toBe(exported);
      expect(atomLabels(dropStarAtomLabels(exported))).toEqual([symbol, "C", symbol]);
    }
  });
});
