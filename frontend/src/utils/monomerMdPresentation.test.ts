import { describe, expect, it } from "vitest";
import type { MonomerMdSimulationResult } from "../types";
import { monomerMdDemoNotice, monomerMdServiceCanSubmit } from "./monomerMdPresentation";

const result = (steps?: number): MonomerMdSimulationResult => ({
  summary: steps == null ? {} : { n_steps: steps },
  artifacts: [],
  warnings: ["Density demo output is not equilibrated and is not a physical density estimate."],
  not_equilibrated: true,
  physical_density_estimate: false
});

describe("monomerMdServiceCanSubmit", () => {
  it("fails closed while status is unknown, loading, or failed", () => {
    expect(monomerMdServiceCanSubmit(null, false, null)).toBe(false);
    expect(monomerMdServiceCanSubmit({ can_submit: true } as never, true, null)).toBe(false);
    expect(monomerMdServiceCanSubmit({ can_submit: true } as never, false, "offline")).toBe(false);
  });

  it("requires an explicit true can_submit value", () => {
    expect(monomerMdServiceCanSubmit({ can_submit: false } as never, false, null)).toBe(false);
    expect(monomerMdServiceCanSubmit({ can_submit: true } as never, false, null)).toBe(true);
  });
});

describe("monomerMdDemoNotice", () => {
  it("uses the completed historical step count", () => {
    expect(monomerMdDemoNotice(result(300), { completed_steps: 1000, requested_steps: 1000 })).toContain("1000 步");
  });

  it("uses the new result step count", () => {
    expect(monomerMdDemoNotice(result(300), null)).toContain("300 步");
  });

  it("uses a number-free warning when the step count is unknown", () => {
    expect(monomerMdDemoNotice(result(), null)).toBe("演示结果尚未达到平衡，不能作为物理密度估计。");
  });
});
