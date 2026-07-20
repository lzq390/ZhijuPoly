import { describe, expect, it } from "vitest";
import {
  getMonomerDftJobIdFromSearch,
  getMonomerDftPath,
  hasInvalidMonomerDftJobSearch
} from "./monomerDftRouting";

describe("monomer DFT query routing", () => {
  it("accepts a UUID job deep link and preserves its spelling", () => {
    const jobId = "7c78fd8a-e901-4fae-9873-90236b52b36a";
    expect(getMonomerDftJobIdFromSearch(`?job=${jobId}`)).toBe(jobId);
    expect(getMonomerDftPath(jobId)).toBe(`/monomer-dft?job=${jobId}`);
  });

  it("rejects missing, arbitrary and malformed identifiers", () => {
    expect(getMonomerDftJobIdFromSearch("")) .toBeNull();
    expect(getMonomerDftJobIdFromSearch("?job=../../secret")).toBeNull();
    expect(getMonomerDftJobIdFromSearch("?job=1234")).toBeNull();
    expect(hasInvalidMonomerDftJobSearch("?job=1234")).toBe(true);
    expect(hasInvalidMonomerDftJobSearch("")) .toBe(false);
  });

  it("builds the module route when no job is selected", () => {
    expect(getMonomerDftPath(null)).toBe("/monomer-dft");
  });
});
