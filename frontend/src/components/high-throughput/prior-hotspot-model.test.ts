import { describe, expect, it } from "vitest";
import { highThroughputDemoScenario as scenario } from "../../constants/highThroughputDemoScenario";
import { buildPriorHotspotSummary, displayTargetUnit, formatPriorValue, validationNumber } from "./prior-hotspot-model";

describe("S2 先验摘要", () => {
  it("四目标各 12 个先验和 2 个推荐，推荐对应后续 R1 固定回流样本", () => {
    const summaries = buildPriorHotspotSummary(scenario.targets, {});
    expect(summaries).toHaveLength(4);
    expect(summaries.map((item) => item.recommendations.length)).toEqual([2, 2, 2, 2]);
    for (const summary of summaries) {
      expect(summary.doeCount).toBe(12);
      expect(summary.recommendations.map((candidate) => candidate.id)).toEqual(scenario.roundsByTarget[summary.target.key][0].testedIds);
      const priors = scenario.propertySpaces[summary.target.key].priorCandidateIds.map((id) => scenario.candidates.find((candidate) => candidate.id === id)!);
      const scores = priors.map((candidate) => candidate.scores[summary.target.key]);
      const expected = summary.target.direction === "lower" ? Math.min(...scores) : Math.max(...scores);
      expect(summary.best?.scores[summary.target.key]).toBe(expected);
      // Strict comparison preserves the first DOE row in a tie.
      expect(summary.best?.id).toBe(priors.find((candidate) => candidate.scores[summary.target.key] === expected)?.id);
    }
  });

  it("阈值与 S0 配置同步，CTE 按越低越好判断；编辑值不改先验最优与推荐路径", () => {
    const original = buildPriorHotspotSummary(scenario.targets, {});
    const targets = original.map((summary) => ({ ...summary.target, target: summary.best!.scores[summary.target.key] }));
    const exact = buildPriorHotspotSummary(targets, { "tg:PI-1973": "999999", "cte:PI-1121": "-999999" });
    expect(exact.every((summary) => summary.meetsTarget)).toBe(true);
    const stricter = buildPriorHotspotSummary(targets.map((target) => ({ ...target, target: target.target + (target.direction === "higher" ? 1 : -1) })), {});
    expect(stricter.every((summary) => !summary.meetsTarget)).toBe(true);
    expect(exact.map((summary) => summary.best)).toEqual(original.map((summary) => summary.best));
    expect(exact.map((summary) => summary.recommendations)).toEqual(original.map((summary) => summary.recommendations));
    expect(exact.map((summary) => summary.filledCount)).toEqual([1, 1, 0, 0]);
  });

  it.each([undefined, "", " ", "NaN", "Infinity", "-Infinity", "not a number"])("拒绝非有限或空输入 %s", (value) => {
    expect(validationNumber(value === undefined ? {} : { "tg:PI-1973": value }, "tg", "PI-1973")).toBeNull();
  });

  it("保留原有有限数输入契约和小数精度，单位仅改变显示", () => {
    expect(validationNumber({ "tg:PI-1973": "0" }, "tg", "PI-1973")).toBe(0);
    expect(validationNumber({ "tg:PI-1973": "-2.5" }, "tg", "PI-1973")).toBe(-2.5);
    expect(displayTargetUnit(scenario.targets[0])).toBe("°C");
    const modulus = scenario.targets.find((target) => target.key === "modulus")!;
    expect(formatPriorValue(modulus, 3)).toBe("3.0");
    expect(formatPriorValue(modulus, 2.91)).toBe("2.91");
    expect(formatPriorValue(scenario.targets[0], 250.25)).toBe("250.25");
    expect(formatPriorValue(scenario.targets[0], 1e-25)).toBe("1e-25");
    expect(scenario.targets[0].unit).toBe("degC");
  });
});
