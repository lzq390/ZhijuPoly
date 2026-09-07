import { describe, expect, it } from "vitest";
import { MONOMER_MD_SESSION_VERSION, parseMonomerMdSession } from "./session";

const density = {
  protocol: "Density",
  temperature: 298,
  natoms: 1000,
  components: { A: 1 },
  smiles: { A: "CCO" }
};

describe("monomer MD session draft", () => {
  it("restores only versioned, legal protocol configs", () => {
    const session = parseMonomerMdSession(JSON.stringify({
      version: MONOMER_MD_SESSION_VERSION,
      runMode: "formal",
      demoSmiles: "CCO",
      selectedProtocol: "Density",
      configs: { Density: density, HVap: { protocol: "HVap" } },
      templateFingerprints: { Density: "fingerprint" }
    }));
    expect(session.runMode).toBe("formal");
    expect(session.configs.Density).toEqual(density);
    expect(session.configs.HVap).toBeUndefined();
    expect(session.templateFingerprints.Density).toBe("fingerprint");
  });

  it("drops drafts from an unknown schema version", () => {
    expect(parseMonomerMdSession(JSON.stringify({ version: 1, demoSmiles: "CCO" })).demoSmiles).toBe("");
    expect(parseMonomerMdSession("not-json").selectedProtocol).toBe("Density");
  });
});
