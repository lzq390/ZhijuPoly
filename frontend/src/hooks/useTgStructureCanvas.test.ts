import { describe, expect, it } from "vitest";
import {
  shouldAdoptEditorSmiles,
  wildcardCount
} from "./useTgStructureCanvas";

describe("Tg structure canvas wildcard protection", () => {
  it("counts polymer end groups and rejects a Ketcher value that loses them", () => {
    expect(wildcardCount("*CC(*)C*")).toBe(3);
    expect(shouldAdoptEditorSmiles("*CC*", "CCC")).toBe(false);
  });

  it("accepts editor SMILES when wildcard preservation is reliable", () => {
    expect(shouldAdoptEditorSmiles("*CC*", "*C(C)*")).toBe(true);
    expect(shouldAdoptEditorSmiles("CC", "CCC")).toBe(true);
    expect(shouldAdoptEditorSmiles("CC", "")).toBe(false);
  });
});
