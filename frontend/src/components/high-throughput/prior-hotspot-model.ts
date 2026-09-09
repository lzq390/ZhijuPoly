import {
  highThroughputDemoScenario,
  type HighThroughputCandidate,
  type HighThroughputTarget,
} from "../../constants/highThroughputDemoScenario";
import type { RecommendationValidationValues } from "./types";

const candidateById = new Map(highThroughputDemoScenario.candidates.map((candidate) => [candidate.id, candidate]));

export function getDemoCandidate(id: string) {
  return candidateById.get(id);
}

function recommendationValidationKey(targetKey: HighThroughputTarget["key"], candidateId: string) {
  return `${targetKey}:${candidateId}`;
}

export function validationNumber(
  validationValues: RecommendationValidationValues,
  targetKey: HighThroughputTarget["key"],
  candidateId: string,
) {
  const rawValue = validationValues[recommendationValidationKey(targetKey, candidateId)];
  if (rawValue === undefined || rawValue.trim() === "") {
    return null;
  }
  const parsedValue = Number(rawValue);
  return Number.isFinite(parsedValue) ? parsedValue : null;
}

export function fallbackCandidateSmiles(candidate: HighThroughputCandidate | undefined) {
  if (!candidate) {
    return {
      polymerSmiles: "--",
      monomerASmiles: "--",
      monomerBSmiles: "--",
    };
  }

  const monomerANumber = Number(candidate.monomerA.replace(/\D/g, "")) || 1;
  const monomerBNumber = Number(candidate.monomerB.replace(/\D/g, "")) || 1;
  const daSideChain = "C".repeat((monomerANumber % 10) + 1);
  const daSecondChain = "C".repeat(Math.floor(monomerANumber / 10) + 1);
  const dmSpacer = "C".repeat((monomerBNumber % 10) + 1);
  const dmMethylPattern = "C".repeat(Math.floor(monomerBNumber / 10) + 1);
  const aromaticBridge = `c1ccc(C(${daSideChain})(${daSecondChain})c2ccc(O)cc2)cc1`;
  const diamineTail = `Nc1ccc(${dmSpacer}Oc2ccc(N)c(${dmMethylPattern})c2)cc1`;

  return {
    polymerSmiles: candidate.polymerSmiles ?? `*N(C(=O)c1ccc(${aromaticBridge})cc1C(=O)*)${diamineTail}`,
    monomerASmiles: candidate.monomerASmiles ?? `O=C1OC(=O)c2ccc(${aromaticBridge})cc21`,
    monomerBSmiles: candidate.monomerBSmiles ?? diamineTail,
  };
}

export function displayTargetUnit(target: HighThroughputTarget) {
  return target.unit === "degC" ? "°C" : target.unit;
}

export function formatPriorValue(target: HighThroughputTarget, value: number) {
  return new Intl.NumberFormat("en-US", {
    minimumFractionDigits: target.key === "modulus" ? 1 : 0,
    maximumFractionDigits: target.key === "modulus" ? 1 : 0,
  }).format(value);
}

export function buildPriorHotspotSummary(targets: HighThroughputTarget[], values: RecommendationValidationValues) {
  return targets.map((target) => {
    const space = highThroughputDemoScenario.propertySpaces[target.key];
    const recommendations = highThroughputDemoScenario.roundsByTarget[target.key][0].testedIds
      .map((id) => candidateById.get(id))
      .filter((candidate): candidate is HighThroughputCandidate => Boolean(candidate));
    const best = space.priorCandidateIds.reduce<HighThroughputCandidate | undefined>((current, id) => {
      const candidate = candidateById.get(id);
      if (!candidate) return current;
      if (!current) return candidate;
      const better = target.direction === "higher"
        ? candidate.scores[target.key] > current.scores[target.key]
        : candidate.scores[target.key] < current.scores[target.key];
      return better ? candidate : current;
    }, undefined);
    const filledCount = recommendations.filter((candidate) => validationNumber(values, target.key, candidate.id) !== null).length;
    const bestValue = best?.scores[target.key];
    const meetsTarget = bestValue !== undefined && (target.direction === "higher" ? bestValue >= target.target : bestValue <= target.target);
    return { target, doeCount: space.priorCandidateIds.length, recommendations, best, filledCount, meetsTarget };
  });
}
