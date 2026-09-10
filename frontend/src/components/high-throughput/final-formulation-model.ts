import { highThroughputDemoScenario as scenario, type HighThroughputTarget } from "../../constants/highThroughputDemoScenario";
import { getDemoCandidate } from "./prior-hotspot-model";

const formulation = scenario.formulation;
const selectedMix = formulation.mixCandidates.find((mix) => mix.id === formulation.selectedMixId)!;

export function buildFinalFormulationSummary(targets: HighThroughputTarget[]) {
  const outcomes = formulation.finalExplanation.targetOutcomes.map((outcome) => {
    const target = targets.find((item) => item.key === outcome.targetKey)!;
    // Remove subtraction noise (3.3 - 3), retaining the sign and tiny differences.
    const margin = Number((target.direction === "lower" ? target.target - outcome.predictedValue : outcome.predictedValue - target.target).toPrecision(12));
    return { ...outcome, target, margin, pass: margin >= 0 };
  });
  // Components/ratios use the same source as S4 and S5, never editable records.
  const sources = formulation.components.map((component) => ({
    component, ratio: selectedMix.ratios[component.id], candidate: getDemoCandidate(component.candidateId)!,
    target: targets.find((target) => target.key === component.sourceTargetKey)!,
    trace: formulation.finalExplanation.sourceTrace.find((trace) => trace.componentId === component.id)!,
  }));
  return { mix: selectedMix, outcomes, sources, passedCount: outcomes.filter((outcome) => outcome.pass).length };
}
