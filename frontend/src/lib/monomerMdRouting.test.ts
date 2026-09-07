import { describe, expect, it } from "vitest";
import {
  getMonomerMdJobIdFromSearch,
  getMonomerMdPath,
  hasInvalidMonomerMdJobSearch,
  isMonomerMdJobId
} from "./monomerMdRouting";

describe("monomer MD query routing", () => {
  const jobId = "0123456789abcdef0123456789abcdef";

  it("accepts exactly 32 hexadecimal characters", () => {
    expect(isMonomerMdJobId(jobId)).toBe(true);
    expect(getMonomerMdJobIdFromSearch(`?job=${jobId}`)).toBe(jobId);
    expect(getMonomerMdPath(jobId)).toBe(`/monomer-md-simulation?job=${jobId}`);
  });

  it("rejects malformed or unsafe identifiers without issuing a usable id", () => {
    expect(isMonomerMdJobId("1234")).toBe(false);
    expect(getMonomerMdJobIdFromSearch("?job=../../secret")).toBeNull();
    expect(getMonomerMdJobIdFromSearch("?job=0123456789abcdef0123456789abcdeg")).toBeNull();
    expect(hasInvalidMonomerMdJobSearch("?job=1234")).toBe(true);
    expect(hasInvalidMonomerMdJobSearch("")).toBe(false);
  });

  it("builds the module route when no job is selected", () => {
    expect(getMonomerMdPath(null)).toBe("/monomer-md-simulation");
  });
});
