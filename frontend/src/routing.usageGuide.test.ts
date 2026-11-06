import { describe, expect, it } from "vitest";
import { pathFromRoute, routeFromPath } from "./routing";

describe("usage guide routing", () => {
  it("supports direct links, trailing slashes and route round trips", () => {
    expect(routeFromPath("/help/usage")).toEqual({ module: "usageGuide", datasetKey: null });
    expect(routeFromPath("/help/usage/")).toEqual({ module: "usageGuide", datasetKey: null });
    expect(pathFromRoute({ module: "usageGuide", datasetKey: null })).toBe("/help/usage");
  });
});
