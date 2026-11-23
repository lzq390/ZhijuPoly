import { describe, expect, it } from "vitest";
import { mockAssistantEvents, mockPredictions, mockReverseDesignResults, multipartPayload } from "./development-mock-api";

describe("frontend preview mock API", () => {
  it("returns only requested properties and reacts to the structure", () => {
    const ps = mockPredictions("*CC(*)c1ccccc1", ["Glass transition temperature", "Melting temperature"]);
    const pe = mockPredictions("*CC*", ["Glass transition temperature"]);
    expect(Object.keys(ps)).toEqual(["Glass transition temperature", "Melting temperature"]);
    expect(ps["Glass transition temperature"]).not.toBe(pe["Glass transition temperature"]);
    expect(mockPredictions("*CC*", ["Glass transition temperature"])).toEqual(pe);
    // Aromatic rings count toward stiffness: polystyrene lands near its familiar ~100 °C.
    expect(ps["Glass transition temperature"]).toBeGreaterThan(80);
    expect(ps["Glass transition temperature"]).toBeLessThan(120);
  });

  it("filters Tg candidates by similarity and orders them by distance to the target", () => {
    const fixture = {
      candidates: [
        { pi_id: 1, smiles: "*C*", tg: 500, similarity: 0.9, monomer_a: "N", monomer_b: "O", svg: "<svg/>" },
        { pi_id: 2, smiles: "*C*", tg: 452, similarity: 0.75, monomer_a: "N", monomer_b: "O", svg: "<svg/>" },
        { pi_id: 3, smiles: "*C*", tg: 449, similarity: 0.5, monomer_a: "N", monomer_b: "O", svg: "<svg/>" }
      ],
      monomers: [{ smiles: "N", iupac: "amine", svg: "<svg/>" }, { smiles: "O", iupac: "anhydride", svg: "<svg/>" }]
    };
    const results = mockReverseDesignResults(fixture, { target_tg: 450, similarity_threshold: 0.7, candidate_size: 10 });
    expect(results.map((item) => item.pi_id)).toEqual([2, 1]);
    expect(results[0]).toMatchObject({ rank: 1, tg_difference: 2, monomer_a_iupac: "amine" });
  });

  it("proposes confirmable operations only with page context", () => {
    const messages = [{ role: "user", content: "把目标 Tg 设为 420 并搜索" }];
    expect(mockAssistantEvents(messages, null).some(([name]) => name === "action_proposal")).toBe(false);
    const proposal = mockAssistantEvents(messages, { action_context_revision: "r1" }).find(([name]) => name === "action_proposal");
    expect(proposal?.[1]).toMatchObject({ basis_revision: "r1", operations: [{ type: "set_parameters", parameters: { target_tg: 420 } }, { type: "run_search" }] });
    expect(mockAssistantEvents(messages, { action_context_revision: "r1" }).at(-1)?.[0]).toBe("done");
  });

  it("reads the UTF-8 payload of a multipart assistant request", () => {
    const payload = JSON.stringify({ messages: [{ role: "user", content: "解释一下当前结果" }] });
    const body = Buffer.concat([
      Buffer.from(`--b1\r\nContent-Disposition: form-data; name="payload"\r\n\r\n${payload}\r\n--b1\r\nContent-Disposition: form-data; name="canvas_image"; filename="c.png"\r\n\r\n`),
      Buffer.from([0x89, 0x50, 0x4e, 0x47]), Buffer.from("\r\n--b1--\r\n")
    ]);
    const parsed = multipartPayload(body, "multipart/form-data; boundary=b1");
    expect(parsed.payload).toEqual(JSON.parse(payload));
    expect(parsed.hasUserImage).toBe(false);
  });
});
