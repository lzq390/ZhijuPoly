import { highThroughputDemoScenario as scenario, type HighThroughputCandidate, type HighThroughputTarget } from "../../constants/highThroughputDemoScenario";
import { buildIterationSummary } from "./iteration-model";
import { getDemoCandidate } from "./prior-hotspot-model";
import type { RecommendationValidationValues } from "./types";

const agentsByTarget = new Map(scenario.agents.map((agent) => [agent.targetKey, agent]));

export function candidateMeetsTarget(candidate: HighThroughputCandidate, target: HighThroughputTarget) {
  return target.direction === "lower" ? candidate.scores[target.key] <= target.target : candidate.scores[target.key] >= target.target;
}

export function buildCandidateOutputSummary(target: HighThroughputTarget, values: RecommendationValidationValues) {
  // The formulation is the single source of truth for the S4 → S5 handoff.
  // Edited feedback is evidence for comparison, never a replacement component.
  const iteration = buildIterationSummary(target, 2, values);
  const { outputComponent: component, outputCandidate: output, recordBest } = iteration;
  const agent = agentsByTarget.get(target.key)!;
  const backups = [...new Set(agent.topCandidateIds)].filter((id) => id !== output.id)
    .flatMap((id) => { const candidate = getDemoCandidate(id); return candidate ? [candidate] : []; });
  return { target, component, output, backups, recordBest,
    candidates: [output, ...backups], meetsTarget: candidateMeetsTarget(output, target),
    doeCount: iteration.priorRecords.length, returnedCount: iteration.returnedRecords.length };
}

export type CandidateOutputSummary = ReturnType<typeof buildCandidateOutputSummary>;
