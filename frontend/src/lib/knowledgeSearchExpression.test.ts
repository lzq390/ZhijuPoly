import { describe, expect, it } from "vitest";
import {
  knowledgeSearchGroupsFromTerms,
  normalizeKnowledgeSearchGroups,
  parseKnowledgeSearchExpression,
  serializeKnowledgeSearchGroups
} from "./knowledgeSearchExpression";

describe("knowledge search expression", () => {
  it.each([
    ["polyimide;NMP", [{ terms: ["polyimide"] }, { terms: ["NMP"] }]],
    ["polyimide；NMP", [{ terms: ["polyimide"] }, { terms: ["NMP"] }]],
    [
      "NMP|N-methyl-2-pyrrolidone；thermal stability",
      [{ terms: ["NMP", "N-methyl-2-pyrrolidone"] }, { terms: ["thermal stability"] }]
    ],
    [
      "NMP｜N-methyl-2-pyrrolidone AND thermal stability",
      [{ terms: ["NMP", "N-methyl-2-pyrrolidone"] }, { terms: ["thermal stability"] }]
    ],
    ["epoxy or resin and coating", [{ terms: ["epoxy", "resin"] }, { terms: ["coating"] }]],
    ["thermal stability", [{ terms: ["thermal stability"] }]],
    ["Epoxy|epoxy;NMP", [{ terms: ["Epoxy"] }, { terms: ["NMP"] }]],
    ["Epoxy;epoxy;NMP", [{ terms: ["Epoxy"] }, { terms: ["NMP"] }]],
    ["A|B;b|a;C", [{ terms: ["A", "B"] }, { terms: ["C"] }]]
  ])("parses %s", (query, groups) => {
    expect(parseKnowledgeSearchExpression(query)).toEqual({ groups, error: null });
  });

  it.each([";epoxy", "epoxy；", "epoxy||resin", "epoxy OR OR resin", "epoxy;;resin"])(
    "rejects incomplete expression %s",
    (query) => {
      expect(parseKnowledgeSearchExpression(query)).toEqual({
        groups: [],
        error: "逻辑符号前后必须有完整关键词"
      });
    }
  );

  it("serializes legacy terms as required groups", () => {
    expect(serializeKnowledgeSearchGroups(knowledgeSearchGroupsFromTerms(["epoxy", "NMP"]))).toBe("epoxy；NMP");
  });

  it("deduplicates equivalent structured groups while preserving first-seen order", () => {
    expect(normalizeKnowledgeSearchGroups([
      { terms: [" Epoxy "] },
      { terms: ["epoxy"] },
      { terms: ["NMP", "solvent"] },
      { terms: ["SOLVENT", "nmp"] }
    ])).toEqual([
      { terms: ["Epoxy"] },
      { terms: ["NMP", "solvent"] }
    ]);
  });

  it("rejects more than ten raw alternatives", () => {
    expect(parseKnowledgeSearchExpression(Array.from({ length: 11 }, (_, index) => `term-${index}`).join("；"))).toEqual({
      groups: [],
      error: "最多支持 10 个关键词或同义词"
    });
  });
});
