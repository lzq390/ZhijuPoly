import { describe, expect, it } from "vitest";
import { highThroughputDemoScenario as scenario } from "../../constants/highThroughputDemoScenario";
import { buildInitialRatioValidationConfirmations, buildInitialRatioValidationValues, buildRatioSearchSummary, projectRatioPointFromRatios, ratioGridPoints, ratioValidationMissingCount, ratioValidationNumber } from "./ratio-search-model";

describe("S5 固定配比与退火派生", () => {
  it("保留 286 个有效配比和固定投影，图点不随步骤重新缩放", () => {
    expect(ratioGridPoints).toHaveLength(286);
    expect(new Set(ratioGridPoints.map((point) => JSON.stringify(point.ratios))).size).toBe(286);
    for (const point of ratioGridPoints) {
      expect(Object.values(point.ratios).reduce((sum, value) => sum + value, 0)).toBeCloseTo(1);
      expect(Object.values(point.ratios).every((value) => value >= 0 && value <= 1)).toBe(true);
      expect(projectRatioPointFromRatios(point.ratios)).toEqual({ x: point.x, y: point.y });
    }
  });
  it("预填每步四项有限数值，但仅 seed 默认确认", () => {
    const defaults = buildInitialRatioValidationValues();
    for (const step of scenario.formulation.searchSteps) expect(ratioValidationMissingCount(step.proposedMixId, defaults)).toBe(0);
    expect(buildInitialRatioValidationConfirmations()).toEqual({ "mix-0": true });
    for (const outcome of scenario.formulation.finalExplanation.targetOutcomes) {
      expect(ratioValidationNumber(defaults, "mix-5", outcome.targetKey)).toBe(outcome.predictedValue);
    }
  });
  it.each(["", " ", "NaN", "Infinity", "-Infinity", "1e999", "oops"])("阻断无效验证值 %s", (value) => {
    const values = buildInitialRatioValidationValues(); values["mix-1"].tg = value;
    expect(ratioValidationNumber(values, "mix-1", "tg")).toBeNull();
    expect(ratioValidationMissingCount("mix-1", values)).toBe(1);
    expect(ratioValidationMissingCount("missing", values)).toBe(4);
  });
  it("未确认的提议不提前进入接受路径、当前解或最优", () => {
    const pending = buildRatioSearchSummary(2, false);
    expect(pending.proposed.id).toBe("mix-4");
    expect(pending.current.id).toBe("mix-1"); expect(pending.best.id).toBe("mix-1");
    expect(pending.evaluatedCount).toBe(2);
    expect(pending.acceptedPath.map((mix) => mix.id)).toEqual(["mix-0", "mix-1"]);
    expect(buildRatioSearchSummary(2, true).best.id).toBe("mix-4");
  });
  it("低温拒绝保留前解与路径，预设概率不由编辑数值重算", () => {
    const pending = buildRatioSearchSummary(3, false);
    expect(pending.rejectedIds.size).toBe(0);
    const rejected = buildRatioSearchSummary(3, true);
    expect([...rejected.rejectedIds]).toEqual(["mix-6"]);
    expect(rejected.current.id).toBe("mix-4"); expect(rejected.best.id).toBe("mix-4");
    expect(rejected.step.deltaScore).toBe(-4); expect(rejected.step.acceptanceProbability).toBe(.16);
    const values = buildInitialRatioValidationValues(); values["mix-6"].tg = "9999";
    expect(buildRatioSearchSummary(3, ratioValidationMissingCount("mix-6", values) === 0)).toEqual(rejected);
  });
  it("回看只展示所选步骤，最终 mix-5 比例不受回看与验证值改变", () => {
    expect(buildRatioSearchSummary(1, true).visibleMixes.map((mix) => mix.id)).toEqual(["mix-0", "mix-1"]);
    expect(buildRatioSearchSummary(1, true).rejectedIds.size).toBe(0);
    const final = buildRatioSearchSummary(4, true);
    expect(final.best.id).toBe("mix-5"); expect(final.best.ratios).toEqual({ p1: .4, p2: .2, p3: .2, p4: .2 });
    expect(final.best.score).toBe(90);
  });
});
