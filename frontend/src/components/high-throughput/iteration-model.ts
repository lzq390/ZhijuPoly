import { highThroughputDemoScenario as scenario, type HighThroughputTarget } from "../../constants/highThroughputDemoScenario";
import { formatPriorValue, getDemoCandidate, validationNumber } from "./prior-hotspot-model";
import type { RecommendationValidationValues } from "./types";

export const ITERATION_ROUNDS = [
  { label: "R1", title: "首轮回流", description: "S2 结果已回流 · 验证新推荐 8 项" },
  { label: "R2", title: "边界验证", description: "R1 结果已回流 · 验证最终 4 项" },
  { label: "收敛", title: "收敛对照", description: "最终批次已回流 · 查看预设输出" },
] as const;

const RETURN_SOURCES = ["S2 验证回流", "R1 验证回流", "R2 验证回流"] as const;

// Editable records may have more precision than the preset integer/0.1 values.
// Never display a rounded threshold or record that disagrees with the comparison.
export function formatIterationValue(target: HighThroughputTarget, value: number) {
  return formatPriorValue(target, value);
}

export function buildIterationSummary(target: HighThroughputTarget, viewRound: number, values: RecommendationValidationValues) {
  const rounds = scenario.roundsByTarget[target.key];
  const roundIndex = Math.max(0, Math.min(2, viewRound));
  const round = rounds[roundIndex];
  const space = scenario.propertySpaces[target.key];
  const priorRecords = space.priorCandidateIds.flatMap((id) => {
    const candidate = getDemoCandidate(id);
    return candidate ? [{ candidate, value: candidate.scores[target.key], source: "DOE 先验" }] : [];
  });
  const returnedRecords = rounds.slice(0, roundIndex + 1).flatMap((item, index) => item.testedIds.flatMap((id) => {
    const candidate = getDemoCandidate(id);
    return candidate ? [{ candidate, value: validationNumber(values, target.key, id) ?? candidate.scores[target.key], source: RETURN_SOURCES[index] }] : [];
  }));
  // Only the viewed round's returned evidence participates. Current/future
  // recommendation drafts remain outside this comparison until progression.
  const seen = new Set<string>();
  const evidence = [...priorRecords, ...returnedRecords].filter(({ candidate }) => {
    if (seen.has(candidate.id)) return false;
    seen.add(candidate.id);
    return true;
  });
  const recordBest = evidence.reduce<(typeof evidence)[number] | undefined>((best, record) => {
    if (!best) return record;
    const better = target.direction === "lower" ? record.value < best.value : record.value > best.value;
    return better ? record : best;
  }, undefined);
  const recommendations = round.recommendedIds.flatMap((id) => {
    const candidate = getDemoCandidate(id);
    return candidate ? [candidate] : [];
  });
  const filledCount = recommendations.filter((candidate) => validationNumber(values, target.key, candidate.id) !== null).length;
  const scriptCandidate = getDemoCandidate(round.currentBestId);
  const outputComponent = scenario.formulation.components.find((item) => item.sourceTargetKey === target.key)!;
  const outputCandidate = getDemoCandidate(outputComponent.candidateId)!;
  const meetsTarget = recordBest !== undefined && (target.direction === "lower" ? recordBest.value <= target.target : recordBest.value >= target.target);
  return {
    target, roundIndex, round, priorRecords, returnedRecords, evidence, recommendations, filledCount,
    recordBest, meetsTarget, scriptCandidate, outputCandidate, outputComponent,
    surface: space.surfaceSnapshots.find((snapshot) => snapshot.id === round.surfaceSnapshotId) ?? null,
  };
}

export type IterationTargetSummary = ReturnType<typeof buildIterationSummary>;
