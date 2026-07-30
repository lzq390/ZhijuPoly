import { describe, expect, it } from "vitest";
import type { MonomerDftModelCapability } from "../types";
import { selectableMonomerDftModels } from "./MonomerDftPage";

function model(
  id: MonomerDftModelCapability["id"],
  deprecated = false
): MonomerDftModelCapability {
  return {
    id,
    label: id,
    available: true,
    deprecated,
    description: id === "aimnet2-2025" ? "AIMNet2 model trained against B97-3c data." : null,
    deprecation_message: null,
    supported_elements: ["H", "C"],
    supported_calculation_types: ["single_point", "optimization"],
    supports_spin: false,
    supported_properties: ["energy", "forces", "charges"]
  };
}

describe("selectableMonomerDftModels", () => {
  it("hides only the deprecated model from new-job selection", () => {
    const models = [
      model("aimnet2-b973c", true),
      model("aimnet2-2025"),
      model("aimnet2-pd")
    ];

    expect(selectableMonomerDftModels(models).map((item) => item.id)).toEqual([
      "aimnet2-2025",
      "aimnet2-pd"
    ]);
    expect(models.map((item) => item.id)).toContain("aimnet2-b973c");
  });
});
