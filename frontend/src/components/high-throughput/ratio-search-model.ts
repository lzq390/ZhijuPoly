import { highThroughputDemoScenario, type HighThroughputTarget, type HighThroughputTargetKey } from "../../constants/highThroughputDemoScenario";

export type RatioMixCandidate = (typeof highThroughputDemoScenario.formulation.mixCandidates)[number];
export type RatioValidationValues = Record<string, Partial<Record<HighThroughputTargetKey, string>>>;
export type RatioValidationConfirmationState = Record<string, boolean>;
const formulation = highThroughputDemoScenario.formulation;
const mixById = new Map(formulation.mixCandidates.map((mix) => [mix.id, mix]));

// Keep the original fixed projection. S5 never fits the coordinates to a step.
export const RATIO_SPACE_ANCHORS: Record<string, { x: number; y: number }> = {
  p1: { x: 20, y: 12 }, p2: { x: 22, y: 36 }, p3: { x: 78, y: 13 }, p4: { x: 80, y: 37 },
};
export function projectRatioPointFromRatios(ratios: Record<string, number>) {
  return Object.entries(RATIO_SPACE_ANCHORS).reduce((point, [id, anchor]) => ({
    x: point.x + anchor.x * (ratios[id] ?? 0), y: point.y + anchor.y * (ratios[id] ?? 0),
  }), { x: 0, y: 0 });
}
export function projectRatioMixPoint(mix: RatioMixCandidate) { return projectRatioPointFromRatios(mix.ratios); }

// 286 compositions, built once rather than on every input keystroke.
export const ratioGridPoints = (() => {
  const points = [];
  for (let p1 = 0; p1 <= 10; p1++) for (let p2 = 0; p2 <= 10 - p1; p2++) for (let p3 = 0; p3 <= 10 - p1 - p2; p3++) {
    const ratios = { p1: p1 / 10, p2: p2 / 10, p3: p3 / 10, p4: (10 - p1 - p2 - p3) / 10 };
    points.push({ id: `${p1}-${p2}-${p3}`, ratios, ...projectRatioPointFromRatios(ratios) });
  }
  return points;
})();

export function ratioValidationNumber(values: RatioValidationValues, mixId: string, targetKey: HighThroughputTargetKey) {
  const raw = values[mixId]?.[targetKey];
  if (raw === undefined || raw.trim() === "") return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}
export function ratioValidationMissingCount(mixId: string, values: RatioValidationValues) {
  return highThroughputDemoScenario.targets.filter((target) => ratioValidationNumber(values, mixId, target.key) === null).length;
}
export function defaultRatioMeasurementValue(mix: RatioMixCandidate, target: HighThroughputTarget) {
  const outcome = formulation.finalExplanation.targetOutcomes.find((item) => item.targetKey === target.key);
  if (mix.id === formulation.selectedMixId && outcome) return outcome.predictedValue;
  const achievement = Math.min(1, Math.max(0, (mix.achievement[target.key] ?? 0) / 100));
  return target.target * (target.direction === "lower" ? 1.7 - achievement * 0.85 : 0.58 + achievement * 0.62);
}
export function buildInitialRatioValidationValues(): RatioValidationValues {
  return Object.fromEntries(formulation.mixCandidates.map((mix) => [mix.id, Object.fromEntries(highThroughputDemoScenario.targets.map((target) => [
    target.key, defaultRatioMeasurementValue(mix, target).toFixed(target.key === "modulus" ? 1 : 0),
  ]))]));
}
export function buildInitialRatioValidationConfirmations(): RatioValidationConfirmationState {
  return { [formulation.searchSteps[0].proposedMixId]: true };
}

/** Validation confirms a demo record; scores, terrain and decisions remain scripted.
 * A history view derives only from that step, never from the live progress index. */
export function buildRatioSearchSummary(viewStep: number, validationConfirmed: boolean) {
  const index = Math.min(formulation.searchSteps.length - 1, Math.max(0, viewStep));
  const step = formulation.searchSteps[index];
  const previousStep = formulation.searchSteps[Math.max(0, index - 1)];
  const isSeed = index === 0;
  const decisionVisible = isSeed || validationConfirmed;
  const decisionStep = decisionVisible ? step : previousStep;
  const rejectedIds = new Set(formulation.searchSteps.slice(0, index + (decisionVisible ? 1 : 0))
    .filter((item) => !item.accepted).map((item) => item.proposedMixId));
  const visibleMixes = step.mixCandidateIds.map((id) => mixById.get(id)!);
  return {
    index, step, isSeed, decisionVisible, rejectedIds, visibleMixes,
    previous: mixById.get(step.previousMixId)!, proposed: mixById.get(step.proposedMixId)!,
    current: mixById.get(decisionStep.currentMixId)!, best: mixById.get(decisionStep.currentBestId)!,
    acceptedPath: decisionStep.acceptedPathIds.map((id) => mixById.get(id)!),
    evaluatedCount: decisionStep.evaluatedCount,
  };
}
export type RatioSearchSummary = ReturnType<typeof buildRatioSearchSummary>;
