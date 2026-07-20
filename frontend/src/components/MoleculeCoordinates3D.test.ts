import { describe, expect, it } from "vitest";
import { atomsToXyz } from "./MoleculeCoordinates3D";

describe("atomsToXyz", () => {
  it("serializes the exact worker coordinates instead of reconstructing from SMILES", () => {
    const xyz = atomsToXyz([
      { index: 1, atomic_number: 8, element: "O", position_angstrom: [0.12345678901, -1, 2] },
      { index: 2, atomic_number: 1, element: "H", position_angstrom: [1, 0, 0] }
    ], "explicit");
    expect(xyz.split("\n")).toEqual([
      "2",
      "explicit",
      "O 0.1234567890 -1.0000000000 2.0000000000",
      "H 1.0000000000 0.0000000000 0.0000000000"
    ]);
  });

  it("covers every nontrivial element in the advertised AIMNet2 domains", () => {
    const xyz = atomsToXyz([
      { index: 1, atomic_number: 33, element: "", position_angstrom: [0, 0, 0] },
      { index: 2, atomic_number: 34, element: "", position_angstrom: [1, 0, 0] },
      { index: 3, atomic_number: 46, element: "", position_angstrom: [2, 0, 0] }
    ]);

    expect(xyz.split("\n").slice(2).map((line) => line.split(" ")[0])).toEqual([
      "As",
      "Se",
      "Pd"
    ]);
  });
});
