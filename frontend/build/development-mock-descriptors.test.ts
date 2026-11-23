// @vitest-environment node
import { describe, expect, it } from "vitest";
import { approximateDescriptors, parseSmiles } from "./development-mock-descriptors";

// Reference values from the backend's RDKit calculation (app/services/structure_descriptors.py).
const REFERENCE = [
  ["*CC*", { formula: "C2H4", molecular_weight: 28.054, heavy_atom_count: 2, ring_count: 0, aromatic_ring_count: 0, rotatable_bond_count: 1, tpsa: 0, hbond_donor_count: 0 }],
  ["*CC(*)c1ccccc1 |$star_e;;;star_e;;;;;;$|", { formula: "C8H8", molecular_weight: 104.152, heavy_atom_count: 8, ring_count: 1, aromatic_ring_count: 1, rotatable_bond_count: 2, tpsa: 0, hbond_donor_count: 0 }],
  ["*CC(*)(C)C(=O)OC", { formula: "C5H8O2", molecular_weight: 100.117, heavy_atom_count: 7, ring_count: 0, aromatic_ring_count: 0, rotatable_bond_count: 2, tpsa: 26.3, hbond_donor_count: 0 }],
  ["*Oc1ccc(C(C)(C)c2ccc(OC(=O)*)cc2)cc1", { formula: "C16H14O3", molecular_weight: 254.285, heavy_atom_count: 19, ring_count: 2, aromatic_ring_count: 2, rotatable_bond_count: 4, tpsa: 35.53, hbond_donor_count: 0 }],
  ["*CC(*)C#N", { formula: "C3H3N", molecular_weight: 53.064, heavy_atom_count: 4, ring_count: 0, aromatic_ring_count: 0, rotatable_bond_count: 1, tpsa: 23.79, hbond_donor_count: 0 }],
  ["*NCCCCCC(=O)*", { formula: "C6H11NO", molecular_weight: 113.16, heavy_atom_count: 8, ring_count: 0, aromatic_ring_count: 0, rotatable_bond_count: 6, tpsa: 29.1, hbond_donor_count: 1 }],
  ["*CC(*)O", { formula: "C2H4O", molecular_weight: 44.053, heavy_atom_count: 3, ring_count: 0, aromatic_ring_count: 0, rotatable_bond_count: 1, tpsa: 20.23, hbond_donor_count: 1 }],
  ["*c1ccc(*)s1", { formula: "C4H2S", molecular_weight: 82.127, heavy_atom_count: 5, ring_count: 1, aromatic_ring_count: 1, rotatable_bond_count: 0, tpsa: 0, hbond_donor_count: 0 }],
  ["*CC(*)(F)F", { formula: "C2H2F2", molecular_weight: 64.034, heavy_atom_count: 4, ring_count: 0, aromatic_ring_count: 0, rotatable_bond_count: 1, tpsa: 0, hbond_donor_count: 0 }]
] as const;

describe("preview-mode structure descriptors", () => {
  it.each(REFERENCE)("matches RDKit for %s", (smiles, expected) => {
    const actual = approximateDescriptors(smiles);
    expect(actual.attachment_point_count).toBe(2);
    for (const [key, value] of Object.entries(expected)) {
      if (typeof value === "number" && !Number.isInteger(value)) expect(actual[key as keyof typeof actual]).toBeCloseTo(value, 1);
      else expect(actual[key as keyof typeof actual], key).toBe(value);
    }
  });

  it("gets composition right for the polyimide example", () => {
    const actual = approximateDescriptors("*c1ccc(Oc2ccc(N3C(=O)c4cc5C(=O)N(*)C(=O)c5cc4C3=O)cc2)cc1");
    expect(actual).toMatchObject({ formula: "C22H10N2O5", heavy_atom_count: 29, ring_count: 5, attachment_point_count: 2 });
    expect(actual.molecular_weight).toBeCloseTo(382.331, 1);
  });

  it("reads bracket atoms and rejects malformed SMILES", () => {
    expect(approximateDescriptors("[NH3+]CC([O-])=O").formula).toBe("C2H5NO2");
    expect(parseSmiles("c1cc[nH]c1").atoms).toHaveLength(5);
    expect(() => approximateDescriptors("C1CC")).toThrow();
    expect(() => approximateDescriptors("not-a-smiles")).toThrow();
  });
});
