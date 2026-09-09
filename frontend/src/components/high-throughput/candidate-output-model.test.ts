import { describe, expect, it } from "vitest";
import { highThroughputDemoScenario as scenario } from "../../constants/highThroughputDemoScenario";
import { buildCandidateOutputSummary, candidateMeetsTarget } from "./candidate-output-model";

describe("S4 固定候选输出", () => {
  it("四份输出直接读取配方组分真源，备选保持原 Agent 顺序且排除固定输出", () => {
    for (const target of scenario.targets) {
      const summary = buildCandidateOutputSummary(target, {});
      const component = scenario.formulation.components.find((item) => item.sourceTargetKey === target.key)!;
      expect(summary.component).toBe(component);
      expect(summary.output.id).toBe(component.candidateId);
      expect(summary.backups.map((candidate) => candidate.id)).toEqual(scenario.agents.find((agent) => agent.targetKey === target.key)!.topCandidateIds.filter((id) => id !== component.candidateId));
      expect(new Set(summary.candidates.map((candidate) => candidate.id)).size).toBe(3);
      expect(summary.doeCount).toBe(12);
      expect(summary.returnedCount).toBe(5);
    }
  });

  it("极端回流值仅改变证据对照，四份固定输出和备选均不改变", () => {
    const values = { "tg:PI-2326": "999.25", "cte:PI-085": "-100.25", "modulus:PI-734": "900" };
    for (const target of scenario.targets) {
      const initial = buildCandidateOutputSummary(target, {});
      const edited = buildCandidateOutputSummary(target, values);
      expect(edited.output).toBe(initial.output);
      expect(edited.backups).toEqual(initial.backups);
      expect(edited.meetsTarget).toBe(initial.meetsTarget);
    }
    expect(buildCandidateOutputSummary(scenario.targets[0], values).recordBest?.value).toBe(999.25);
    expect(buildCandidateOutputSummary(scenario.targets[1], values).recordBest?.value).toBe(-100.25);
    expect(buildCandidateOutputSummary(scenario.targets[2], values).recordBest?.value).not.toBe(900);
  });

  it("阈值比较方向和等号正确，不对场景小数阈值提前舍入", () => {
    for (const target of scenario.targets) {
      const candidate = buildCandidateOutputSummary(target, {}).output;
      const value = candidate.scores[target.key];
      expect(candidateMeetsTarget(candidate, { ...target, target: value })).toBe(true);
      expect(candidateMeetsTarget(candidate, { ...target, target: value + (target.direction === "lower" ? -0.01 : 0.01) })).toBe(false);
    }
  });
});
