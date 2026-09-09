import { describe, expect, it } from "vitest";
import { highThroughputDemoScenario as scenario } from "../../constants/highThroughputDemoScenario";
import { buildIterationSummary, formatIterationValue } from "./iteration-model";

describe("S3 轮次证据派生", () => {
  it.each([0, 1, 2])("轮次 %i 只包含对应快照的累计证据与推荐", (round) => {
    const summaries = scenario.targets.map((target) => buildIterationSummary(target, round, {}));
    expect(summaries.reduce((sum, item) => sum + item.recommendations.length, 0)).toBe([8, 4, 0][round]);
    for (const summary of summaries) {
      expect(summary.priorRecords).toHaveLength(12);
      expect(summary.returnedRecords).toHaveLength([2, 4, 5][round]);
      expect(summary.evidence).toHaveLength([14, 16, 17][round]);
      expect(summary.round).toBe(scenario.roundsByTarget[summary.target.key][round]);
      expect(summary.surface?.id).toBe(summary.round.surfaceSnapshotId);
      expect(summary.recommendations.map((item) => item.id)).toEqual(summary.round.recommendedIds);
    }
  });

  it.each(scenario.targets)("$shortLabel 待验证极值不提前参与，推进后按方向生效", (target) => {
    const rounds = scenario.roundsByTarget[target.key];
    const r1Id = rounds[0].recommendedIds[0];
    const r2Id = rounds[1].recommendedIds[0];
    const extreme = target.direction === "lower" ? -10000 : 10000;
    const values = { [`${target.key}:${r1Id}`]: String(extreme), [`${target.key}:${r2Id}`]: String(extreme * 2) };
    const baseline = buildIterationSummary(target, 0, {});
    const first = buildIterationSummary(target, 0, values);
    const second = buildIterationSummary(target, 1, values);
    const final = buildIterationSummary(target, 2, values);
    expect(first.recordBest).toEqual(baseline.recordBest);
    expect(second.recordBest).toMatchObject({ candidate: { id: r1Id }, value: extreme, source: "R1 验证回流" });
    expect(final.recordBest).toMatchObject({ candidate: { id: r2Id }, value: extreme * 2, source: "R2 验证回流" });
    expect(buildIterationSummary(target, 0, values).recordBest).toEqual(baseline.recordBest);
    for (const [round, summary] of [first, second, final].entries()) {
      const original = buildIterationSummary(target, round, {});
      expect(summary.surface).toBe(original.surface);
      expect(summary.scriptCandidate).toBe(original.scriptCandidate);
      expect(summary.outputCandidate).toBe(original.outputCandidate);
      expect(summary.recommendations).toEqual(original.recommendations);
    }
  });

  it("并列按 DOE、S2、R1、R2 的证据顺序稳定选择", () => {
    for (const target of scenario.targets) {
      const baseline = buildIterationSummary(target, 0, {});
      const doeBestValue = target.direction === "lower" ? Math.min(...baseline.priorRecords.map((item) => item.value)) : Math.max(...baseline.priorRecords.map((item) => item.value));
      const values = Object.fromEntries(baseline.returnedRecords.map((item) => [`${target.key}:${item.candidate.id}`, String(doeBestValue)]));
      const result = buildIterationSummary(target, 0, values);
      expect(result.recordBest).toEqual(baseline.priorRecords.find((item) => item.value === doeBestValue));
      const extreme = target.direction === "lower" ? -10000 : 10000;
      for (const item of baseline.returnedRecords) values[`${target.key}:${item.candidate.id}`] = String(extreme);
      expect(buildIterationSummary(target, 0, values).recordBest?.candidate.id).toBe(baseline.returnedRecords[0].candidate.id);
    }
  });

  it("跨目标同 ID 的 PI-734 验证值隔离，阈值消费配置且不修改场景", () => {
    const values = { "elongation:PI-734": "8000", "modulus:PI-734": "9000" };
    const elongation = buildIterationSummary(scenario.targets.find((item) => item.key === "elongation")!, 2, values);
    const modulus = buildIterationSummary(scenario.targets.find((item) => item.key === "modulus")!, 0, values);
    expect(elongation.recordBest?.value).toBe(8000);
    expect(modulus.recordBest?.value).toBe(9000);
    expect(elongation.recordBest?.source).toBe("R2 验证回流");
    expect(modulus.recordBest?.source).toBe("S2 验证回流");
    expect(buildIterationSummary({ ...elongation.target, target: 8001 }, 2, values).meetsTarget).toBe(false);
    const cte = scenario.targets.find((item) => item.key === "cte")!;
    const result = buildIterationSummary(cte, 2, {});
    expect(buildIterationSummary({ ...cte, target: result.recordBest!.value }, 2, {}).meetsTarget).toBe(true);
    expect(buildIterationSummary({ ...cte, target: result.recordBest!.value - 1 }, 2, {}).meetsTarget).toBe(false);
  });

  it("当前批有效计数拒绝非有限数与空值", () => {
    const target = scenario.targets[0];
    const ids = scenario.roundsByTarget.tg[0].recommendedIds;
    for (const value of ["", " ", "NaN", "Infinity", "1e999"]) {
      expect(buildIterationSummary(target, 0, { [`tg:${ids[0]}`]: value, [`tg:${ids[1]}`]: "1" }).filledCount).toBe(1);
    }
  });

  it("记录和阈值展示保留用户小数精度，不将临界未达标值四舍五入", () => {
    const tg = scenario.targets[0];
    const modulus = scenario.targets.find((item) => item.key === "modulus")!;
    expect(formatIterationValue(tg, 299.99)).toBe("299.99");
    expect(formatIterationValue(modulus, 3.01)).toBe("3.01");
    expect(formatIterationValue(modulus, 3)).toBe("3.0");
    expect(formatIterationValue(tg, 1e-25)).toBe("1e-25");
  });
});
