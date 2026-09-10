import { describe, expect, it } from "vitest";
import { highThroughputDemoScenario as scenario } from "../../constants/highThroughputDemoScenario";
import { buildFinalFormulationSummary } from "./final-formulation-model";

describe("S6 固定配方与目标结果", () => {
  it("与 S4/S5 共用组分、配比、综合分和预设结果", () => {
    const summary = buildFinalFormulationSummary(scenario.targets);
    expect(summary.mix.id).toBe("mix-5"); expect(summary.mix.score).toBe(90);
    expect(summary.mix.ratios).toEqual({ p1: .4, p2: .2, p3: .2, p4: .2 });
    expect(summary.sources.map((source) => source.candidate.id)).toEqual(scenario.formulation.components.map((component) => component.candidateId));
    expect(summary.sources.map((source) => source.ratio)).toEqual([.4, .2, .2, .2]);
    expect(summary.outcomes.map((outcome) => outcome.predictedValue)).toEqual([292, 29, 18, 3.3]);
    expect(summary.passedCount).toBe(4);
  });
  it("最终摘要不再计算或暴露 S5 验证记录对照", () => {
    const summary = buildFinalFormulationSummary(scenario.targets);
    expect(summary).not.toHaveProperty("changedRecordCount");
    expect(summary).not.toHaveProperty("missingRecordCount");
    for (const outcome of summary.outcomes) {
      expect(outcome).not.toHaveProperty("record");
      expect(outcome).not.toHaveProperty("recordDiffers");
    }
  });
  it("S0 阈值驱动方向、等值与小数比较，不改预设性质或总分", () => {
    const thresholds = { tg: 292, cte: 28.5, elongation: 18.01, modulus: 3.3 };
    const summary = buildFinalFormulationSummary(scenario.targets.map((target) => ({ ...target, target: thresholds[target.key] })));
    expect(summary.outcomes.map((outcome) => outcome.pass)).toEqual([true, false, false, true]);
    expect(summary.outcomes.map((outcome) => outcome.margin)).toEqual([0, -.5, -.01, 0]);
    expect(summary.passedCount).toBe(2); expect(summary.mix.score).toBe(90);
    expect(buildFinalFormulationSummary(scenario.targets).outcomes[3].margin).toBe(.3);
  });
  it("来源始终追踪 S3 预设输出，与当前目标列表的顺序无关", () => {
    const summary = buildFinalFormulationSummary([...scenario.targets].reverse());
    expect(summary.outcomes.map((outcome) => outcome.target.key)).toEqual(["tg", "cte", "elongation", "modulus"]);
    for (const source of summary.sources) {
      expect(source.trace.componentId).toBe(source.component.id);
      expect(source.target.key).toBe(source.component.sourceTargetKey);
      expect(source.candidate.id).toBe(source.component.candidateId);
    }
  });
});
