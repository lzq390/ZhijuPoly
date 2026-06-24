import {
  BadgeInfo,
  Bot,
  BrainCircuit,
  CheckCircle2,
  ChevronRight,
  FileCheck2,
  FlaskConical,
  Layers3,
  RotateCcw,
  SlidersHorizontal,
  Target,
  TestTube2,
  UploadCloud,
} from "lucide-react";
import { type ChangeEvent, type CSSProperties, type KeyboardEvent, type MouseEvent, type ReactNode, type RefObject, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  highThroughputDemoScenario,
  type HighThroughputCandidate,
  type HighThroughputDoeCsvFile,
  type HighThroughputPropertySpace,
  type HighThroughputSurfaceSnapshot,
  type HighThroughputTarget,
  type HighThroughputTargetKey,
} from "../constants/highThroughputDemoScenario";
import { cn } from "../lib/utils";
import "./HighThroughputWorkflowDemoPage.css";

type HighThroughputWorkflowDemoPageProps = {
  onBackHome: () => void;
};

type WeightState = Record<HighThroughputTargetKey, number>;
type RatioMixCandidate = (typeof highThroughputDemoScenario.formulation.mixCandidates)[number];

type ConfirmedSetup = {
  materialType: string;
  monomerSystem: string;
  representation: string;
  monomerACount: number;
  monomerBCount: number;
  candidateTotal: number;
  selectedTargetKeys: HighThroughputTargetKey[];
  targetValues: Record<HighThroughputTargetKey, number>;
};

type PriorDataUploadState = {
  targetKey: HighThroughputTargetKey;
  fileName: string;
  fileType: string;
  sampleCount: number;
  fieldCount: number;
  expectedFileName: string;
  propertyColumn: string;
  uploadedAt: string;
  uploadToken: string;
  isLoading: boolean;
  errorMessage?: string;
};

type PriorDataUploadsState = Partial<Record<HighThroughputTargetKey, PriorDataUploadState>>;
type RecommendationSelection = {
  targetKey: HighThroughputTargetKey;
  candidateId: string;
};
type RecommendationValidationValues = Record<string, string>;
type RecommendationValidationRequirement = {
  targetKey: HighThroughputTargetKey;
  candidateId: string;
};
type ValidationConfirmationState = Record<string, boolean>;
type RatioValidationValues = Record<string, Partial<Record<HighThroughputTargetKey, string>>>;
type RatioValidationConfirmationState = Record<string, boolean>;
type NextStepState = {
  canAdvance: boolean;
  label: string;
  hint: string;
};
type StageTransitionState = {
  message: string;
};

const DOE_PRIOR_LOAD_MS = 1000;
const STAGE_TRANSITION_MS = 900;
const VALIDATION_FEEDBACK_MS = 1400;
const RATIO_SEARCH_FEEDBACK_MS = 700;
const MAX_RENDERED_STAGE_DOTS = 2400;
const MAX_RENDERED_PROPERTY_DOTS = 2200;
const MATERIAL_MAP_WIDTH = 100;
const MATERIAL_MAP_HEIGHT = 48;
const AGENT_DISPLAY_COLORS = ["#2563eb", "#16a34a", "#7c3aed", "#f97316"] as const;
const AGENT_ACTION_LABELS = ["下一轮 3 个样本", "追加 3 个验证点", "筛选候选 Top-k", "更新局部推荐"] as const;
const RATIO_SPACE_ANCHORS: Record<string, { x: number; y: number }> = {
  p1: { x: 20, y: 12 },
  p2: { x: 22, y: 36 },
  p3: { x: 78, y: 13 },
  p4: { x: 80, y: 37 },
};
function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function formatNumber(value: number, digits = 0) {
  return new Intl.NumberFormat("en-US", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  }).format(value);
}

function targetValueDigits(target: HighThroughputTarget) {
  return target.key === "modulus" ? 1 : 0;
}

function formatTargetValue(target: HighThroughputTarget, value: number) {
  return formatNumber(value, targetValueDigits(target));
}

function targetThresholdLabel(target: HighThroughputTarget) {
  return `${target.direction === "higher" ? "≥" : "≤"} ${formatTargetValue(target, target.target)}${target.unit}`;
}

function candidateValue(candidate: HighThroughputCandidate | undefined, target: HighThroughputTarget) {
  if (!candidate) {
    return "--";
  }
  const digits = target.key === "modulus" ? 1 : 0;
  return `${formatNumber(candidate.scores[target.key], digits)} ${target.unit}`;
}

function buildInitialWeights(): WeightState {
  return Object.fromEntries(
    highThroughputDemoScenario.targets.map((target) => [target.key, target.weight]),
  ) as WeightState;
}

function buildDefaultConfirmedSetup(): ConfirmedSetup {
  const scenario = highThroughputDemoScenario;

  return {
    materialType: scenario.materialType,
    monomerSystem: "Diamine + Dianhydride",
    representation: "PolyBERT",
    monomerACount: scenario.monomerACount,
    monomerBCount: scenario.monomerBCount,
    candidateTotal: scenario.candidateTotal,
    selectedTargetKeys: scenario.targets.map((target) => target.key),
    targetValues: Object.fromEntries(
      scenario.targets.map((target) => [target.key, target.target]),
    ) as Record<HighThroughputTargetKey, number>,
  };
}

function buildTargetValueInputs(targetValues: Record<HighThroughputTargetKey, number>) {
  return Object.fromEntries(
    highThroughputDemoScenario.targets.map((target) => [
      target.key,
      formatTargetValue(target, targetValues[target.key] ?? target.target),
    ]),
  ) as Record<HighThroughputTargetKey, string>;
}

function parseCandidateCount(value: string) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(0, Math.round(parsed)) : 0;
}

function parseTargetInput(value: string, fallback: number) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function getTarget(targetKey: HighThroughputTargetKey) {
  return highThroughputDemoScenario.targets.find((target) => target.key === targetKey) ?? highThroughputDemoScenario.targets[0];
}

function getConfiguredTarget(targetKey: HighThroughputTargetKey, setup: ConfirmedSetup) {
  const target = getTarget(targetKey);
  return {
    ...target,
    target: setup.targetValues[targetKey] ?? target.target,
  };
}

function getCandidate(candidateId: string) {
  return highThroughputDemoScenario.candidates.find((candidate) => candidate.id === candidateId);
}

function recommendationValidationKey(targetKey: HighThroughputTargetKey, candidateId: string) {
  return `${targetKey}:${candidateId}`;
}

function uniqueItems<T>(items: T[]) {
  return Array.from(new Set(items));
}

function validationNumber(
  validationValues: RecommendationValidationValues,
  targetKey: HighThroughputTargetKey,
  candidateId: string,
) {
  const rawValue = validationValues[recommendationValidationKey(targetKey, candidateId)];
  if (rawValue === undefined || rawValue.trim() === "") {
    return null;
  }
  const parsedValue = Number(rawValue);
  return Number.isFinite(parsedValue) ? parsedValue : null;
}

function hasValidationValue(
  validationValues: RecommendationValidationValues,
  targetKey: HighThroughputTargetKey,
  candidateId: string,
) {
  return validationNumber(validationValues, targetKey, candidateId) !== null;
}

function ratioValidationNumber(
  validationValues: RatioValidationValues,
  mixId: string,
  targetKey: HighThroughputTargetKey,
) {
  const rawValue = validationValues[mixId]?.[targetKey];
  if (rawValue === undefined || rawValue.trim() === "") {
    return null;
  }
  const parsedValue = Number(rawValue);
  return Number.isFinite(parsedValue) ? parsedValue : null;
}

function ratioValidationMissingCount(mixId: string, validationValues: RatioValidationValues) {
  if (!mixId) {
    return highThroughputDemoScenario.targets.length;
  }
  return highThroughputDemoScenario.targets.filter((target) =>
    ratioValidationNumber(validationValues, mixId, target.key) === null,
  ).length;
}

function isTargetCsvReady(upload: PriorDataUploadState | null | undefined) {
  return Boolean(upload && !upload.isLoading && !upload.errorMessage);
}

function fallbackCandidateSmiles(candidate: HighThroughputCandidate | undefined) {
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

function getAgentForCandidate(candidateId: string) {
  return highThroughputDemoScenario.agents.find(
    (agent) =>
      agent.currentBestId === candidateId ||
      agent.topCandidateIds.includes(candidateId) ||
      agent.explorationCandidateIds.includes(candidateId),
  );
}

function projectMaterialPoint(
  point: { x: number; y: number },
  bounds: { minX: number; maxX: number; minY: number; maxY: number },
) {
  const xRange = bounds.maxX - bounds.minX || 1;
  const yRange = bounds.maxY - bounds.minY || 1;

  return {
    x: 2.8 + ((point.x - bounds.minX) / xRange) * 94.4,
    y: 3 + ((point.y - bounds.minY) / yRange) * 42,
  };
}

function buildDisplayBounds(points: Array<{ x: number; y: number }>) {
  if (points.length === 0) {
    return { minX: 0, maxX: 1, minY: 0, maxY: 1 };
  }

  return points.reduce(
    (bounds, point) => ({
      minX: Math.min(bounds.minX, point.x),
      maxX: Math.max(bounds.maxX, point.x),
      minY: Math.min(bounds.minY, point.y),
      maxY: Math.max(bounds.maxY, point.y),
    }),
    {
      minX: Number.POSITIVE_INFINITY,
      maxX: Number.NEGATIVE_INFINITY,
      minY: Number.POSITIVE_INFINITY,
      maxY: Number.NEGATIVE_INFINITY,
    },
  );
}

function projectPropertyDisplayPoint(
  point: { x: number; y: number },
  bounds: { minX: number; maxX: number; minY: number; maxY: number },
) {
  const xRange = bounds.maxX - bounds.minX || 1;
  const yRange = bounds.maxY - bounds.minY || 1;

  return {
    x: 4 + ((point.x - bounds.minX) / xRange) * 92,
    y: 3.4 + ((point.y - bounds.minY) / yRange) * 41.2,
  };
}

function projectPropertyDisplayBlob(
  blob: { x: number; y: number; rx: number; ry: number; opacity: number },
  bounds: { minX: number; maxX: number; minY: number; maxY: number },
) {
  const xRange = bounds.maxX - bounds.minX || 1;
  const yRange = bounds.maxY - bounds.minY || 1;
  const point = projectPropertyDisplayPoint(blob, bounds);

  return {
    ...blob,
    x: point.x,
    y: point.y,
    rx: clamp(blob.rx * (92 / xRange), 8, 24),
    ry: clamp(blob.ry * (41.2 / yRange), 5, 16),
  };
}

type AgentAttentionOverlayLink = {
  id: string;
  color: string;
  path: string;
};

type AgentAttentionOverlayLayout = {
  width: number;
  height: number;
  links: AgentAttentionOverlayLink[];
};

function buildProjectedCandidateMap(stageIndex: number) {
  const visibleCandidates = stageIndex === 0 ? [] : highThroughputDemoScenario.candidates.slice(0, MAX_RENDERED_STAGE_DOTS);
  const pointBounds = visibleCandidates.reduce(
    (bounds, candidate) => ({
      minX: Math.min(bounds.minX, candidate.x),
      maxX: Math.max(bounds.maxX, candidate.x),
      minY: Math.min(bounds.minY, candidate.y),
      maxY: Math.max(bounds.maxY, candidate.y),
    }),
    { minX: Number.POSITIVE_INFINITY, maxX: Number.NEGATIVE_INFINITY, minY: Number.POSITIVE_INFINITY, maxY: Number.NEGATIVE_INFINITY },
  );
  const normalizedBounds =
    visibleCandidates.length > 0
      ? pointBounds
      : { minX: 0, maxX: 1, minY: 0, maxY: 1 };

  return new Map(
    visibleCandidates.map((candidate) => [candidate.id, projectMaterialPoint(candidate, normalizedBounds)]),
  );
}

function mapPointToRenderedPosition(
  point: { x: number; y: number },
  mapRect: DOMRect,
) {
  const scale = Math.min(mapRect.width / MATERIAL_MAP_WIDTH, mapRect.height / MATERIAL_MAP_HEIGHT);
  const renderedWidth = MATERIAL_MAP_WIDTH * scale;
  const renderedHeight = MATERIAL_MAP_HEIGHT * scale;
  const offsetX = (mapRect.width - renderedWidth) / 2;
  const offsetY = (mapRect.height - renderedHeight) / 2;

  return {
    x: offsetX + point.x * scale,
    y: offsetY + point.y * scale,
  };
}

function buildSvgPath(points: Array<{ x: number; y: number }>) {
  return points
    .map((point, index) => `${index === 0 ? "M" : "L"} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`)
    .join(" ");
}

function projectRatioPointFromRatios(ratios: Record<string, number>) {
  return Object.entries(RATIO_SPACE_ANCHORS).reduce(
    (point, [componentId, anchor]) => {
      const ratio = ratios[componentId] ?? 0;
      return {
        x: point.x + anchor.x * ratio,
        y: point.y + anchor.y * ratio,
      };
    },
    { x: 0, y: 0 },
  );
}

function projectRatioMixPoint(mix: RatioMixCandidate) {
  return projectRatioPointFromRatios(mix.ratios);
}

function getSelectedRatioMix() {
  const formulation = highThroughputDemoScenario.formulation;
  return formulation.mixCandidates.find((mix) => mix.id === formulation.selectedMixId) ?? formulation.mixCandidates[formulation.mixCandidates.length - 1];
}

function defaultRatioMeasurementValue(mix: RatioMixCandidate, target: HighThroughputTarget) {
  const formulation = highThroughputDemoScenario.formulation;
  const finalOutcome = formulation.finalExplanation.targetOutcomes.find((outcome) => outcome.targetKey === target.key);
  if (mix.id === formulation.selectedMixId && finalOutcome) {
    return finalOutcome.predictedValue;
  }

  const achievement = mix.achievement[target.key] ?? 0;
  const normalizedAchievement = clamp(achievement / 100, 0, 1);
  const factor = target.direction === "lower"
    ? 1.7 - normalizedAchievement * 0.85
    : 0.58 + normalizedAchievement * 0.62;
  return target.target * factor;
}

function buildInitialRatioValidationValues(): RatioValidationValues {
  const scenario = highThroughputDemoScenario;
  return Object.fromEntries(
    scenario.formulation.mixCandidates.map((mix) => [
      mix.id,
      Object.fromEntries(
        scenario.targets.map((target) => [
          target.key,
          formatTargetValue(target, defaultRatioMeasurementValue(mix, target)),
        ]),
      ) as Partial<Record<HighThroughputTargetKey, string>>,
    ]),
  );
}

function buildInitialRatioValidationConfirmations(): RatioValidationConfirmationState {
  const seedMixId = highThroughputDemoScenario.formulation.searchSteps[0]?.proposedMixId;
  return seedMixId ? { [seedMixId]: true } : {};
}

function getPropertySpace(targetKey: HighThroughputTargetKey) {
  return highThroughputDemoScenario.propertySpaces[targetKey];
}

function bestCandidateId(candidateIds: string[], target: HighThroughputTarget) {
  return candidateIds.reduce<string | null>((bestId, candidateId) => {
    const candidate = getCandidate(candidateId);
    const bestCandidate = bestId ? getCandidate(bestId) : undefined;
    if (!candidate) {
      return bestId;
    }
    if (!bestCandidate) {
      return candidateId;
    }

    const value = candidate.scores[target.key];
    const bestValue = bestCandidate.scores[target.key];
    return target.direction === "lower"
      ? value < bestValue ? candidateId : bestId
      : value > bestValue ? candidateId : bestId;
  }, null) ?? candidateIds[0];
}

function bestCandidateIdWithValidation(
  candidateIds: string[],
  target: HighThroughputTarget,
  validationValues: RecommendationValidationValues,
) {
  return candidateIds.reduce<string | null>((bestId, candidateId) => {
    const candidate = getCandidate(candidateId);
    const bestCandidate = bestId ? getCandidate(bestId) : undefined;
    if (!candidate) {
      return bestId;
    }
    if (!bestId || !bestCandidate) {
      return candidateId;
    }

    const value = validationNumber(validationValues, target.key, candidateId) ?? candidate.scores[target.key];
    const bestValue = validationNumber(validationValues, target.key, bestId) ?? bestCandidate.scores[target.key];
    return target.direction === "lower"
      ? value < bestValue ? candidateId : bestId
      : value > bestValue ? candidateId : bestId;
  }, null) ?? candidateIds[0];
}

function targetGapLabel(candidate: HighThroughputCandidate | undefined, target: HighThroughputTarget) {
  if (!candidate) {
    return "--";
  }
  const value = candidate.scores[target.key];
  const gap = target.direction === "lower" ? target.target - value : value - target.target;
  const sign = gap > 0 ? "+" : "";
  return `${sign}${formatNumber(gap, targetValueDigits(target))}${target.unit}`;
}

function candidateValueWithValidation(
  candidate: HighThroughputCandidate | undefined,
  target: HighThroughputTarget,
  validationValues: RecommendationValidationValues,
) {
  if (!candidate) {
    return "--";
  }
  const value = validationNumber(validationValues, target.key, candidate.id) ?? candidate.scores[target.key];
  return `${formatNumber(value, targetValueDigits(target))} ${target.unit}`;
}

function targetGapLabelWithValidation(
  candidate: HighThroughputCandidate | undefined,
  target: HighThroughputTarget,
  validationValues: RecommendationValidationValues,
) {
  if (!candidate) {
    return "--";
  }
  const value = validationNumber(validationValues, target.key, candidate.id) ?? candidate.scores[target.key];
  const gap = target.direction === "lower" ? target.target - value : value - target.target;
  const sign = gap > 0 ? "+" : "";
  return `${sign}${formatNumber(gap, targetValueDigits(target))}${target.unit}`;
}

function targetOutcomePasses(value: number, target: HighThroughputTarget) {
  return target.direction === "lower" ? value <= target.target : value >= target.target;
}

function targetOutcomeMarginLabel(value: number, target: HighThroughputTarget) {
  const margin = target.direction === "lower" ? target.target - value : value - target.target;
  const formattedMargin = formatNumber(Math.abs(margin), targetValueDigits(target));
  if (margin >= 0) {
    return target.direction === "lower"
      ? `低于目标 ${formattedMargin}${target.unit}`
      : `高于目标 ${formattedMargin}${target.unit}`;
  }
  return target.direction === "lower"
    ? `高于目标 ${formattedMargin}${target.unit}`
    : `低于目标 ${formattedMargin}${target.unit}`;
}

function adjustedOutcomeAchievement(
  outcome: { targetValue: number; achievement: number },
  target: HighThroughputTarget,
) {
  const baselineTarget = outcome.targetValue || target.target || 1;
  const configuredTarget = target.target || baselineTarget;
  const adjustment = target.direction === "lower"
    ? configuredTarget / baselineTarget
    : baselineTarget / configuredTarget;

  return Math.round(clamp(outcome.achievement * adjustment, 0, 100));
}

function propertySurfaceForStage(
  stageIndex: number,
  space: HighThroughputPropertySpace,
  iterationRoundIndex = 2,
): HighThroughputSurfaceSnapshot | null {
  if (stageIndex < 2) {
    return null;
  }
  const rounds = highThroughputDemoScenario.roundsByTarget[space.targetKey];
  const activeRound = rounds[clamp(iterationRoundIndex, 0, rounds.length - 1)] ?? rounds[rounds.length - 1];
  const snapshotId = stageIndex === 2
    ? "prior"
    : stageIndex === 3
      ? activeRound.surfaceSnapshotId
      : "converged";
  return space.surfaceSnapshots.find((snapshot) => snapshot.id === snapshotId) ?? null;
}

function propertyRoundIds(targetKey: HighThroughputTargetKey, stageIndex: number, iterationRoundIndex = 2) {
  const rounds = highThroughputDemoScenario.roundsByTarget[targetKey];
  if (stageIndex === 2) {
    const firstRound = rounds[0];
    return {
      visibleRounds: [],
      testedIds: [],
      currentTestedIds: [],
      recommendedIds: firstRound?.testedIds ?? [],
    };
  }

  if (stageIndex > 3) {
    const activeRound = rounds[rounds.length - 1];
    return {
      visibleRounds: rounds,
      testedIds: rounds.flatMap((round) => round.testedIds),
      currentTestedIds: activeRound?.testedIds ?? [],
      recommendedIds: [],
    };
  }

  const visibleRounds = stageIndex === 3
    ? rounds.slice(0, clamp(iterationRoundIndex, 0, rounds.length - 1) + 1)
    : [];
  const activeRound = visibleRounds[visibleRounds.length - 1];
  return {
    visibleRounds,
    testedIds: visibleRounds.flatMap((round) => round.testedIds),
    currentTestedIds: activeRound?.testedIds ?? [],
    recommendedIds: activeRound?.recommendedIds ?? [],
  };
}

function recommendationValidationIds(targetKey: HighThroughputTargetKey, stageIndex: number, iterationRoundIndex = 2) {
  if (stageIndex === 2) {
    return propertyRoundIds(targetKey, stageIndex).recommendedIds.slice(0, 2);
  }
  if (stageIndex === 3) {
    return propertyRoundIds(targetKey, stageIndex, iterationRoundIndex).recommendedIds;
  }
  return [];
}

function getRequiredValidationIds(stageIndex: number, iterationRoundIndex = 0): RecommendationValidationRequirement[] {
  if (stageIndex !== 2 && stageIndex !== 3) {
    return [];
  }

  return highThroughputDemoScenario.targets.flatMap((target) =>
    recommendationValidationIds(target.key, stageIndex, iterationRoundIndex).map((candidateId) => ({
      targetKey: target.key,
      candidateId,
    })),
  );
}

function validationConfirmationKey(stageIndex: number, iterationRoundIndex = 0) {
  if (stageIndex === 2) {
    return "s2-validation";
  }
  if (stageIndex === 3 && iterationRoundIndex < 2) {
    return `s3-round-${iterationRoundIndex + 1}`;
  }
  return "";
}

function missingValidationCount(
  requirements: RecommendationValidationRequirement[],
  validationValues: RecommendationValidationValues,
) {
  return requirements.filter((requirement) =>
    !hasValidationValue(validationValues, requirement.targetKey, requirement.candidateId),
  ).length;
}

function buildInitialRecommendationValidationValues(): RecommendationValidationValues {
  const requirements = [
    ...getRequiredValidationIds(2),
    ...getRequiredValidationIds(3, 0),
    ...getRequiredValidationIds(3, 1),
  ];

  return Object.fromEntries(
    requirements.map((requirement) => {
      const target = getTarget(requirement.targetKey);
      const candidate = getCandidate(requirement.candidateId);
      const value = candidate ? formatTargetValue(target, candidate.scores[requirement.targetKey]) : "";
      return [recommendationValidationKey(requirement.targetKey, requirement.candidateId), value];
    }),
  );
}

function propertySpaceCurrentBestId(
  stageIndex: number,
  space: HighThroughputPropertySpace,
  target: HighThroughputTarget,
  iterationRoundIndex = 2,
  validationValues: RecommendationValidationValues = {},
) {
  if (stageIndex === 3) {
    const rounds = highThroughputDemoScenario.roundsByTarget[space.targetKey];
    const visibleRounds = rounds.slice(0, clamp(iterationRoundIndex, 0, rounds.length - 1) + 1);
    const measuredIds = uniqueItems([
      ...space.priorCandidateIds,
      ...visibleRounds.flatMap((round) => round.testedIds),
    ]);
    return bestCandidateIdWithValidation(measuredIds, target, validationValues);
  }
  if (stageIndex > 3) {
    return space.currentBestId;
  }
  if (stageIndex >= 2) {
    return bestCandidateId(space.priorCandidateIds, target);
  }
  return "";
}

function getStageCompletionState({
  currentStageIndex,
  activeIterationRoundIndex,
  activeRatioSearchStepIndex,
  priorDataUploads,
  validationValues,
  validationConfirmations,
  ratioValidationValues,
  ratioValidationConfirmations,
}: {
  currentStageIndex: number;
  activeIterationRoundIndex: number;
  activeRatioSearchStepIndex: number;
  priorDataUploads: PriorDataUploadsState;
  validationValues: RecommendationValidationValues;
  validationConfirmations: ValidationConfirmationState;
  ratioValidationValues: RatioValidationValues;
  ratioValidationConfirmations: RatioValidationConfirmationState;
}): NextStepState {
  const scenario = highThroughputDemoScenario;
  const readyCsvCount = scenario.targets.filter((target) => isTargetCsvReady(priorDataUploads[target.key])).length;
  const searchStepCount = scenario.formulation.searchSteps.length;

  if (currentStageIndex === 0) {
    return {
      canAdvance: false,
      label: "确认",
      hint: "请先确认任务设置",
    };
  }

  if (currentStageIndex === 1) {
    const remainingCsvCount = scenario.targets.length - readyCsvCount;
    return {
      canAdvance: remainingCsvCount === 0,
      label: "S2",
      hint: remainingCsvCount === 0 ? "四个 CSV 已就绪" : `请先上传 ${remainingCsvCount} 个 CSV`,
    };
  }

  if (currentStageIndex === 2) {
    const requirements = getRequiredValidationIds(2, activeIterationRoundIndex);
    const missingCount = missingValidationCount(requirements, validationValues);
    const groupKey = validationConfirmationKey(2);
    const confirmed = Boolean(validationConfirmations[groupKey]);
    return {
      canAdvance: missingCount === 0 && confirmed,
      label: "S3",
      hint: missingCount > 0
        ? `请录入 ${missingCount} 个推荐点实测值`
        : confirmed
          ? "S2 推荐点实测值已确认"
          : "请确认 S2 实测值回流",
    };
  }

  if (currentStageIndex === 3) {
    if (activeIterationRoundIndex >= 2) {
      return {
        canAdvance: true,
        label: "S4",
        hint: "单性质空间已收敛",
      };
    }

    const requirements = getRequiredValidationIds(3, activeIterationRoundIndex);
    const missingCount = missingValidationCount(requirements, validationValues);
    const groupKey = validationConfirmationKey(3, activeIterationRoundIndex);
    const confirmed = Boolean(validationConfirmations[groupKey]);
    const nextLabel = activeIterationRoundIndex === 0 ? "R2" : "收敛";
    return {
      canAdvance: missingCount === 0 && confirmed,
      label: nextLabel,
      hint: missingCount > 0
        ? `请录入 ${missingCount} 个推荐点实测值`
        : confirmed
          ? "当前轮实测值已确认"
          : "请确认本轮实测值回流",
    };
  }

  if (currentStageIndex === 4) {
    return {
      canAdvance: true,
      label: "S5",
      hint: "p1-p4 候选已输出",
    };
  }

  if (currentStageIndex === 5) {
    const lastStepIndex = Math.max(searchStepCount - 1, 0);
    const activeStep = scenario.formulation.searchSteps[clamp(activeRatioSearchStepIndex, 0, lastStepIndex)];
    const measurementRequired = activeRatioSearchStepIndex > 0;
    const proposedMixId = activeStep?.proposedMixId ?? "";
    const missingCount = measurementRequired ? ratioValidationMissingCount(proposedMixId, ratioValidationValues) : 0;
    const confirmed = !measurementRequired || Boolean(ratioValidationConfirmations[proposedMixId]);
    return {
      canAdvance: missingCount === 0 && confirmed,
      label: activeRatioSearchStepIndex < lastStepIndex
        ? `T${activeRatioSearchStepIndex + 1}`
        : "S6",
      hint: missingCount > 0
        ? `请录入 ${proposedMixId} 的 ${missingCount} 个实测值`
        : confirmed
          ? activeRatioSearchStepIndex < lastStepIndex ? "实测值已回流，继续比例搜索" : "S5 最终配方已锁定"
          : `请确认 ${proposedMixId} 实测值回流`,
    };
  }

  return {
    canAdvance: false,
    label: "完成",
    hint: "最终结果已生成",
  };
}

export function HighThroughputWorkflowDemoPage(_props: HighThroughputWorkflowDemoPageProps) {
  const scenario = highThroughputDemoScenario;
  const [currentStageIndex, setCurrentStageIndex] = useState(0);
  const [weights, setWeights] = useState<WeightState>(() => buildInitialWeights());
  const [confirmedSetup, setConfirmedSetup] = useState<ConfirmedSetup>(() => buildDefaultConfirmedSetup());
  const [setupResetToken, setSetupResetToken] = useState(0);
  const [activeSpaceTargetKey, setActiveSpaceTargetKey] = useState<HighThroughputTargetKey>("tg");
  const [activeIterationRoundIndex, setActiveIterationRoundIndex] = useState(0);
  const [activeRatioSearchStepIndex, setActiveRatioSearchStepIndex] = useState(0);
  const [priorDataUploads, setPriorDataUploads] = useState<PriorDataUploadsState>({});
  const [selectedRecommendation, setSelectedRecommendation] = useState<RecommendationSelection | null>(null);
  const [recommendationValidationValues, setRecommendationValidationValues] = useState<RecommendationValidationValues>(() => buildInitialRecommendationValidationValues());
  const [validationConfirmations, setValidationConfirmations] = useState<ValidationConfirmationState>({});
  const [ratioValidationValues, setRatioValidationValues] = useState<RatioValidationValues>(() => buildInitialRatioValidationValues());
  const [ratioValidationConfirmations, setRatioValidationConfirmations] = useState<RatioValidationConfirmationState>(() => buildInitialRatioValidationConfirmations());
  const [stageTransition, setStageTransition] = useState<StageTransitionState | null>(null);
  const priorUploadTimersRef = useRef<Partial<Record<HighThroughputTargetKey, number>>>({});
  const stageTransitionTimerRef = useRef<number | null>(null);
  const mapStageRef = useRef<HTMLDivElement | null>(null);
  const nextStepState = getStageCompletionState({
    currentStageIndex,
    activeIterationRoundIndex,
    activeRatioSearchStepIndex,
    priorDataUploads,
    validationValues: recommendationValidationValues,
    validationConfirmations,
    ratioValidationValues,
    ratioValidationConfirmations,
  });
  const activeValidationRequirements = getRequiredValidationIds(currentStageIndex, activeIterationRoundIndex);
  const activeValidationMissingCount = missingValidationCount(activeValidationRequirements, recommendationValidationValues);
  const activeValidationGroupKey = validationConfirmationKey(currentStageIndex, activeIterationRoundIndex);
  const activeValidationConfirmed = activeValidationGroupKey ? Boolean(validationConfirmations[activeValidationGroupKey]) : false;
  const ratioSearchSteps = scenario.formulation.searchSteps;
  const activeRatioSearchStep = ratioSearchSteps[clamp(activeRatioSearchStepIndex, 0, Math.max(ratioSearchSteps.length - 1, 0))];
  const activeRatioMixId = activeRatioSearchStep?.proposedMixId ?? "";
  const activeRatioMeasurementRequired = currentStageIndex === 5 && activeRatioSearchStepIndex > 0;
  const activeRatioValidationMissingCount = activeRatioMeasurementRequired
    ? ratioValidationMissingCount(activeRatioMixId, ratioValidationValues)
    : 0;
  const activeRatioValidationConfirmed = !activeRatioMeasurementRequired || Boolean(ratioValidationConfirmations[activeRatioMixId]);
  const displayedNextStepState = stageTransition
    ? { canAdvance: false, label: "处理中", hint: stageTransition.message }
    : nextStepState;

  useEffect(() => () => {
    Object.values(priorUploadTimersRef.current).forEach((timerId) => {
      if (timerId !== undefined) {
        window.clearTimeout(timerId);
      }
    });
    if (stageTransitionTimerRef.current !== null) {
      window.clearTimeout(stageTransitionTimerRef.current);
    }
  }, []);

  useEffect(() => {
    if (currentStageIndex !== 2 && currentStageIndex !== 3) {
      return;
    }

    const recommendedIds = recommendationValidationIds(activeSpaceTargetKey, currentStageIndex, activeIterationRoundIndex);
    setSelectedRecommendation((selection) => {
      if (
        selection &&
        selection.targetKey === activeSpaceTargetKey &&
        recommendedIds.includes(selection.candidateId)
      ) {
        return selection;
      }

      const candidateId = recommendedIds[0];
      return candidateId ? { targetKey: activeSpaceTargetKey, candidateId } : null;
    });
  }, [activeIterationRoundIndex, activeSpaceTargetKey, currentStageIndex]);

  function clearPriorUploadTimer(targetKey: HighThroughputTargetKey) {
    const timerId = priorUploadTimersRef.current[targetKey];
    if (timerId !== undefined) {
      window.clearTimeout(timerId);
      delete priorUploadTimersRef.current[targetKey];
    }
  }

  function clearAllPriorUploadTimers() {
    scenario.targets.forEach((target) => clearPriorUploadTimer(target.key));
  }

  function enterStage(index: number) {
    const nextIndex = clamp(index, 0, scenario.stages.length - 1);
    setCurrentStageIndex(nextIndex);
    if (nextIndex === 3) {
      setActiveIterationRoundIndex(0);
    } else if (nextIndex === 4) {
      setActiveIterationRoundIndex(2);
    } else if (nextIndex === 5) {
      setActiveRatioSearchStepIndex(0);
    } else if (nextIndex === 6) {
      setActiveRatioSearchStepIndex(scenario.formulation.searchSteps.length - 1);
    }
  }

  function runStageTransition(message: string, duration: number, onComplete: () => void) {
    if (stageTransitionTimerRef.current !== null) {
      window.clearTimeout(stageTransitionTimerRef.current);
    }
    setStageTransition({ message });
    stageTransitionTimerRef.current = window.setTimeout(() => {
      onComplete();
      setStageTransition(null);
      stageTransitionTimerRef.current = null;
    }, duration);
  }

  function handleNextStep() {
    if (!nextStepState.canAdvance || stageTransition) {
      return;
    }

    if (currentStageIndex === 1) {
      runStageTransition("正在导入 DOE 先验并生成初始热点图...", STAGE_TRANSITION_MS, () => enterStage(2));
      return;
    }

    if (currentStageIndex === 2) {
      runStageTransition("实验结果回流中，正在生成第一轮单性质模型...", VALIDATION_FEEDBACK_MS, () => enterStage(3));
      return;
    }

    if (currentStageIndex === 3) {
      if (activeIterationRoundIndex < 2) {
        const message = activeIterationRoundIndex === 0
          ? "回流本轮实测值，正在更新单性质模型与热点图..."
          : "验证最终推荐点，正在确认当前最优稳定...";
        runStageTransition(message, VALIDATION_FEEDBACK_MS, () =>
          setActiveIterationRoundIndex((roundIndex) => clamp(roundIndex + 1, 0, 2)),
        );
        return;
      }
      runStageTransition("汇总四个收敛候选，正在生成 p1-p4 输出...", STAGE_TRANSITION_MS, () => enterStage(4));
      return;
    }

    if (currentStageIndex === 4) {
      runStageTransition("汇总 p1-p4 组分池，正在进入比例搜索...", STAGE_TRANSITION_MS, () => enterStage(5));
      return;
    }

    if (currentStageIndex === 5) {
      const lastStepIndex = scenario.formulation.searchSteps.length - 1;
      if (activeRatioSearchStepIndex < lastStepIndex) {
        runStageTransition("评估邻域比例，正在更新模拟退火路径...", RATIO_SEARCH_FEEDBACK_MS, () =>
          setActiveRatioSearchStepIndex((stepIndex) => clamp(stepIndex + 1, 0, lastStepIndex)),
        );
        return;
      }
      runStageTransition("锁定最终配方，正在生成解释结果...", STAGE_TRANSITION_MS, () => enterStage(6));
      return;
    }

    runStageTransition("正在推进实验流程...", STAGE_TRANSITION_MS, () => enterStage(currentStageIndex + 1));
  }

  function resetValidationValues(requirements: RecommendationValidationRequirement[]) {
    const defaultValues = buildInitialRecommendationValidationValues();
    setRecommendationValidationValues((values) => {
      const nextValues = { ...values };
      requirements.forEach((requirement) => {
        const key = recommendationValidationKey(requirement.targetKey, requirement.candidateId);
        nextValues[key] = defaultValues[key] ?? "";
      });
      return nextValues;
    });
  }

  function clearValidationConfirmation(stageIndex: number, iterationRoundIndex = 0) {
    const groupKey = validationConfirmationKey(stageIndex, iterationRoundIndex);
    if (!groupKey) {
      return;
    }
    setValidationConfirmations((confirmations) => ({
      ...confirmations,
      [groupKey]: false,
    }));
  }

  function confirmCurrentValidationGroup() {
    if (!activeValidationGroupKey || activeValidationMissingCount > 0) {
      return;
    }
    setValidationConfirmations((confirmations) => ({
      ...confirmations,
      [activeValidationGroupKey]: true,
    }));
  }

  function resetRatioValidationState() {
    setRatioValidationValues(buildInitialRatioValidationValues());
    setRatioValidationConfirmations(buildInitialRatioValidationConfirmations());
  }

  function handleRatioValidationValueChange(targetKey: HighThroughputTargetKey, value: string) {
    if (!activeRatioMixId) {
      return;
    }
    setRatioValidationValues((values) => ({
      ...values,
      [activeRatioMixId]: {
        ...values[activeRatioMixId],
        [targetKey]: value,
      },
    }));
    setRatioValidationConfirmations((confirmations) => ({
      ...confirmations,
      [activeRatioMixId]: false,
    }));
  }

  function confirmCurrentRatioValidation() {
    if (!activeRatioMeasurementRequired || !activeRatioMixId || activeRatioValidationMissingCount > 0) {
      return;
    }
    setRatioValidationConfirmations((confirmations) => ({
      ...confirmations,
      [activeRatioMixId]: true,
    }));
  }

  function resetCurrentStageActions() {
    if (currentStageIndex === 0) {
      setConfirmedSetup(buildDefaultConfirmedSetup());
      setSetupResetToken((token) => token + 1);
      setActiveSpaceTargetKey("tg");
      setValidationConfirmations({});
      resetRatioValidationState();
      return;
    }

    if (currentStageIndex === 1) {
      clearAllPriorUploadTimers();
      setPriorDataUploads({});
      setSelectedRecommendation(null);
      setValidationConfirmations({});
      return;
    }

    if (currentStageIndex === 2) {
      resetValidationValues(getRequiredValidationIds(2));
      clearValidationConfirmation(2);
      setSelectedRecommendation(null);
      return;
    }

    if (currentStageIndex === 3) {
      resetValidationValues(getRequiredValidationIds(3, activeIterationRoundIndex));
      clearValidationConfirmation(3, activeIterationRoundIndex);
      setSelectedRecommendation(null);
      return;
    }

    if (currentStageIndex === 5) {
      setActiveRatioSearchStepIndex(0);
      resetRatioValidationState();
    }
  }

  function confirmSetup(setup: ConfirmedSetup) {
    setConfirmedSetup(setup);
    enterStage(1);
  }

  function handlePriorDataUpload(targetKey: HighThroughputTargetKey, file: File | null) {
    if (!file) {
      return;
    }

    clearPriorUploadTimer(targetKey);
    const csvFile = scenario.doeCsvFiles[targetKey];
    const uploadedAt = new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date());

    if (file.name !== csvFile.fileName) {
      setPriorDataUploads((uploads) => ({
        ...uploads,
        [targetKey]: {
          targetKey,
          fileName: file.name,
          fileType: file.name.split(".").pop()?.toUpperCase() || "UNKNOWN",
          sampleCount: 0,
          fieldCount: 0,
          expectedFileName: csvFile.fileName,
          propertyColumn: csvFile.propertyColumn,
          uploadedAt,
          uploadToken: `${targetKey}-invalid-${Date.now()}`,
          isLoading: false,
          errorMessage: "文件格式错误",
        },
      }));
      return;
    }

    const uploadToken = `${targetKey}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    setPriorDataUploads((uploads) => ({
      ...uploads,
      [targetKey]: {
        targetKey,
        fileName: file.name,
        fileType: file.name.split(".").pop()?.toUpperCase() || "CSV",
        sampleCount: csvFile.rows.length,
        fieldCount: 12,
        expectedFileName: csvFile.fileName,
        propertyColumn: csvFile.propertyColumn,
        uploadedAt,
        uploadToken,
        isLoading: true,
      },
    }));

    priorUploadTimersRef.current[targetKey] = window.setTimeout(() => {
      setPriorDataUploads((uploads) => {
        const upload = uploads[targetKey];
        if (!upload || upload.uploadToken !== uploadToken) {
          return uploads;
        }

        return {
          ...uploads,
          [targetKey]: {
            ...upload,
            isLoading: false,
          },
        };
      });
      delete priorUploadTimersRef.current[targetKey];
    }, DOE_PRIOR_LOAD_MS);
  }

  function handleRecommendationValidationValueChange(targetKey: HighThroughputTargetKey, candidateId: string, value: string) {
    setRecommendationValidationValues((values) => ({
      ...values,
      [recommendationValidationKey(targetKey, candidateId)]: value,
    }));
    clearValidationConfirmation(currentStageIndex, activeIterationRoundIndex);
  }

  return (
    <div className="high-throughput-demo">
      <main className="ht-shell">
        <section className="ht-docx-board">
          <ScenarioHeader
            confirmedSetup={confirmedSetup}
            onConfirmSetup={confirmSetup}
            isConfirmed={currentStageIndex > 0}
            resetToken={setupResetToken}
          />

          <FlowControlBar
            currentStageIndex={currentStageIndex}
            nextStepState={displayedNextStepState}
            onNextStep={handleNextStep}
            onResetStage={resetCurrentStageActions}
            canResetStage={!stageTransition && [0, 1, 2, 3, 5].includes(currentStageIndex)}
          />

          <div className="ht-docx-map-stage" ref={mapStageRef}>
            <AgentOrbitPanel
              stageIndex={currentStageIndex}
              side="left"
              confirmedSetup={confirmedSetup}
              activeTargetKey={activeSpaceTargetKey}
              onSelectTarget={setActiveSpaceTargetKey}
              iterationRoundIndex={activeIterationRoundIndex}
              priorDataUploads={priorDataUploads}
              onPriorDataUpload={handlePriorDataUpload}
            />

            <div className="ht-map-column">
              {currentStageIndex <= 4 ? (
                <PropertySpaceBoard
                  stageIndex={currentStageIndex}
                  confirmedSetup={confirmedSetup}
                  activeTargetKey={activeSpaceTargetKey}
                  onActiveTargetChange={setActiveSpaceTargetKey}
                  iterationRoundIndex={activeIterationRoundIndex}
                  priorDataUploads={priorDataUploads}
                  validationValues={recommendationValidationValues}
                  selectedRecommendation={selectedRecommendation}
                  onSelectRecommendation={setSelectedRecommendation}
                />
              ) : (
                <RatioAnnealingMap
                  stageIndex={currentStageIndex}
                  activeStepIndex={activeRatioSearchStepIndex}
                  confirmedSetup={confirmedSetup}
                  activeStepValidationConfirmed={activeRatioValidationConfirmed}
                />
              )}
            </div>

            <AgentOrbitPanel
              stageIndex={currentStageIndex}
              side="right"
              confirmedSetup={confirmedSetup}
              activeTargetKey={activeSpaceTargetKey}
              onSelectTarget={setActiveSpaceTargetKey}
              iterationRoundIndex={activeIterationRoundIndex}
              priorDataUploads={priorDataUploads}
              onPriorDataUpload={handlePriorDataUpload}
            />

            {currentStageIndex <= 4 ? (
              <AgentAttentionOverlay
                stageIndex={currentStageIndex}
                iterationRoundIndex={activeIterationRoundIndex}
                stageRef={mapStageRef}
              />
            ) : null}
            {stageTransition ? (
              <div className="ht-stage-transition-overlay" role="status" aria-live="polite">
                <span>实验流程处理中</span>
                <strong>{stageTransition.message}</strong>
              </div>
            ) : null}
          </div>

          {currentStageIndex <= 3 ? (
            <ExperimentPriorPanel
              stageIndex={currentStageIndex}
              activeTargetKey={activeSpaceTargetKey}
              priorDataUploads={priorDataUploads}
              iterationRoundIndex={activeIterationRoundIndex}
              selectedRecommendation={selectedRecommendation}
              onSelectRecommendation={setSelectedRecommendation}
              validationValues={recommendationValidationValues}
              onValidationValueChange={handleRecommendationValidationValueChange}
              validationConfirmed={activeValidationConfirmed}
              validationMissingCount={activeValidationMissingCount}
              validationRequiredCount={activeValidationRequirements.length}
              onConfirmValidationGroup={confirmCurrentValidationGroup}
            />
          ) : currentStageIndex === 4 ? (
            <CandidateOutputPanel activeTargetKey={activeSpaceTargetKey} confirmedSetup={confirmedSetup} />
          ) : currentStageIndex === 5 ? (
            <RatioSearchPanel
              activeStepIndex={activeRatioSearchStepIndex}
              confirmedSetup={confirmedSetup}
              ratioValidationValues={ratioValidationValues}
              validationConfirmed={activeRatioValidationConfirmed}
              validationMissingCount={activeRatioValidationMissingCount}
              onValidationValueChange={handleRatioValidationValueChange}
              onConfirmValidation={confirmCurrentRatioValidation}
            />
          ) : (
            <FinalFormulationPanel
              weights={weights}
              confirmedSetup={confirmedSetup}
            />
          )}
        </section>
      </main>
    </div>
  );
}

function ScenarioHeader({
  confirmedSetup,
  onConfirmSetup,
  isConfirmed,
  resetToken,
}: {
  confirmedSetup: ConfirmedSetup;
  onConfirmSetup: (setup: ConfirmedSetup) => void;
  isConfirmed: boolean;
  resetToken: number;
}) {
  const scenario = highThroughputDemoScenario;
  const [materialType, setMaterialType] = useState(confirmedSetup.materialType);
  const [monomerSystem, setMonomerSystem] = useState(confirmedSetup.monomerSystem);
  const [representation, setRepresentation] = useState(confirmedSetup.representation);
  const [candidateA, setCandidateA] = useState(String(confirmedSetup.monomerACount));
  const [candidateB, setCandidateB] = useState(String(confirmedSetup.monomerBCount));
  const [selectedTargetKeys, setSelectedTargetKeys] = useState<HighThroughputTargetKey[]>(confirmedSetup.selectedTargetKeys);
  const [focusedTargetKey, setFocusedTargetKey] = useState<HighThroughputTargetKey>("tg");
  const [targetValues, setTargetValues] = useState<Record<HighThroughputTargetKey, string>>(
    () => buildTargetValueInputs(confirmedSetup.targetValues),
  );
  const selectedTargets = scenario.targets.filter((target) => selectedTargetKeys.includes(target.key));
  const candidateTotalPreview = parseCandidateCount(candidateA) * parseCandidateCount(candidateB);

  useEffect(() => {
    setMaterialType(confirmedSetup.materialType);
    setMonomerSystem(confirmedSetup.monomerSystem);
    setRepresentation(confirmedSetup.representation);
    setCandidateA(String(confirmedSetup.monomerACount));
    setCandidateB(String(confirmedSetup.monomerBCount));
    setSelectedTargetKeys(confirmedSetup.selectedTargetKeys);
    setTargetValues(buildTargetValueInputs(confirmedSetup.targetValues));
  }, [confirmedSetup, resetToken]);

  function handleTargetClick(targetKey: HighThroughputTargetKey) {
    setFocusedTargetKey(targetKey);
    setSelectedTargetKeys((current) => {
      if (current.includes(targetKey)) {
        return current.filter((key) => key !== targetKey);
      }
      return [...current, targetKey];
    });
  }

  function handleConfirmSetup() {
    const monomerACount = parseCandidateCount(candidateA);
    const monomerBCount = parseCandidateCount(candidateB);
    const nextTargetValues = Object.fromEntries(
      scenario.targets.map((target) => [
        target.key,
        parseTargetInput(targetValues[target.key], target.target),
      ]),
    ) as Record<HighThroughputTargetKey, number>;
    const nextSetup: ConfirmedSetup = {
      materialType,
      monomerSystem,
      representation,
      monomerACount,
      monomerBCount,
      candidateTotal: monomerACount * monomerBCount,
      selectedTargetKeys,
      targetValues: nextTargetValues,
    };

    setCandidateA(String(monomerACount));
    setCandidateB(String(monomerBCount));
    setTargetValues(buildTargetValueInputs(nextTargetValues));
    onConfirmSetup(nextSetup);
  }

  return (
    <section className="ht-scenario-panel" aria-label="材料与目标设置">
      <div className="ht-scenario-title">
        <FlaskConical aria-hidden="true" size={22} />
        <div>
          <span className="ht-kicker">Material & Target Setup</span>
          <h2>材料体系与目标设置</h2>
        </div>
      </div>

      <div className="ht-setup-grid">
        <div className="ht-setup-section">
          <SetupControl icon={<Layers3 aria-hidden="true" size={20} />} label="Material type">
            <select value={materialType} onChange={(event) => setMaterialType(event.currentTarget.value)}>
              <option value="Polyimide">Polyimide</option>
            </select>
          </SetupControl>

          <SetupControl icon={<FlaskConical aria-hidden="true" size={20} />} label="Monomer system">
            <select value={monomerSystem} onChange={(event) => setMonomerSystem(event.currentTarget.value)}>
              <option value="Diamine + Dianhydride">Diamine + Dianhydride</option>
            </select>
          </SetupControl>
        </div>

        <div className="ht-setup-section middle">
          <SetupControl icon={<Layers3 aria-hidden="true" size={20} />} label="Candidate space">
            <div className="ht-candidate-space-inputs">
              <input
                aria-label="Candidate monomer A count"
                inputMode="numeric"
                min="0"
                type="number"
                value={candidateA}
                onChange={(event) => setCandidateA(event.currentTarget.value)}
              />
              <span aria-hidden="true">x</span>
              <input
                aria-label="Candidate monomer B count"
                inputMode="numeric"
                min="0"
                type="number"
                value={candidateB}
                onChange={(event) => setCandidateB(event.currentTarget.value)}
              />
            </div>
          </SetupControl>

          <SetupControl icon={<BrainCircuit aria-hidden="true" size={20} />} label="Representation">
            <select value={representation} onChange={(event) => setRepresentation(event.currentTarget.value)}>
              <option value="PolyBERT">PolyBERT</option>
            </select>
          </SetupControl>
        </div>

        <div className="ht-setup-section targets">
          <div className="ht-setup-row">
            <span className="ht-setup-icon">
              <Target aria-hidden="true" size={20} />
            </span>
            <div className="ht-setup-field">
              <span className="ht-setup-label">Target properties</span>
              <div className="ht-property-buttons">
                {scenario.targets.map((target) => (
                  <button
                    key={target.key}
                    type="button"
                    aria-pressed={selectedTargetKeys.includes(target.key)}
                    onClick={() => handleTargetClick(target.key)}
                    style={{ "--target-color": target.color } as CSSProperties}
                  >
                    <span>{target.shortLabel}</span>
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="ht-setup-row">
            <span className="ht-setup-icon">
              <BadgeInfo aria-hidden="true" size={20} />
            </span>
            <div className="ht-setup-field">
              <span className="ht-setup-label">Target values</span>
              <div className="ht-target-value-inputs">
                {selectedTargets.length > 0 ? selectedTargets.map((target) => (
                  <label key={target.key} style={{ "--target-color": target.color } as CSSProperties}>
                    <span>{target.shortLabel}</span>
                    <input
                      aria-label={`${target.shortLabel} target value`}
                      inputMode="decimal"
                      step={target.key === "modulus" ? "0.1" : "1"}
                      type="number"
                      value={targetValues[target.key]}
                      onFocus={() => setFocusedTargetKey(target.key)}
                      onChange={(event) =>
                        setTargetValues((current) => ({
                          ...current,
                          [target.key]: event.currentTarget.value,
                        }))
                      }
                    />
                  </label>
                )) : <span className="ht-target-empty">请选择目标性质</span>}
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="ht-setup-actions">
        <div className="ht-setup-summary">
          <span>Candidate space preview</span>
          <strong>
            {candidateA || "0"} x {candidateB || "0"} = {formatNumber(candidateTotalPreview)}
          </strong>
          <em>{selectedTargets.length} target properties selected</em>
        </div>
        <button type="button" className="ht-confirm-setup-button" onClick={handleConfirmSetup} disabled={isConfirmed}>
          {isConfirmed ? <CheckCircle2 aria-hidden="true" size={17} /> : <ChevronRight aria-hidden="true" size={17} />}
          {isConfirmed ? "任务设置已确认" : "确认任务设置，生成候选空间"}
        </button>
      </div>
    </section>
  );
}

function SetupControl({ icon, label, children }: { icon: ReactNode; label: string; children: ReactNode }) {
  return (
    <div className="ht-setup-row">
      <span className="ht-setup-icon">{icon}</span>
      <span className="ht-setup-field">
        <span className="ht-setup-label">{label}</span>
        {children}
      </span>
    </div>
  );
}

function MetricBlock({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <div className="ht-metric-block">
      <span>{label}</span>
      <strong>{value}</strong>
      <em>{detail}</em>
    </div>
  );
}

function FlowControlBar({
  currentStageIndex,
  nextStepState,
  onNextStep,
  onResetStage,
  canResetStage,
}: {
  currentStageIndex: number;
  nextStepState: NextStepState;
  onNextStep: () => void;
  onResetStage: () => void;
  canResetStage: boolean;
}) {
  const stages = highThroughputDemoScenario.stages;

  return (
    <section className="ht-flow-control-bar" aria-label="演示播放控制">
      <div className="ht-flow-next">
        <div className="ht-flow-next-actions">
          <button
            type="button"
            className="ht-primary-control ht-next-step-control"
            onClick={onNextStep}
            disabled={!nextStepState.canAdvance}
          >
            {nextStepState.label}
            <ChevronRight aria-hidden="true" size={16} />
          </button>
          <button
            type="button"
            className="ht-icon-button ht-stage-reset-button"
            onClick={onResetStage}
            aria-label="重置当前阶段"
            title={canResetStage ? "重置当前阶段" : "当前阶段无可重置动作"}
            disabled={!canResetStage}
          >
            <RotateCcw aria-hidden="true" size={15} />
          </button>
        </div>
        <span>{nextStepState.hint}</span>
      </div>
      <nav className="ht-flow-steps" aria-label="S0 到 S6 演示阶段">
        {stages.map((stage, index) => {
          const state = index < currentStageIndex
            ? "complete"
            : index === currentStageIndex
              ? "active"
              : "locked";
          return (
            <div
              key={stage.id}
              aria-current={index === currentStageIndex ? "step" : undefined}
              className={cn("ht-flow-step", state)}
            >
              <span>{stage.id}</span>
              <b>{stage.label}</b>
            </div>
          );
        })}
      </nav>
    </section>
  );
}

function AgentAttentionOverlay({
  stageIndex,
  iterationRoundIndex,
  stageRef,
}: {
  stageIndex: number;
  iterationRoundIndex: number;
  stageRef: RefObject<HTMLDivElement | null>;
}) {
  const [layout, setLayout] = useState<AgentAttentionOverlayLayout>({ width: 0, height: 0, links: [] });

  useLayoutEffect(() => {
    if (stageIndex !== 3) {
      setLayout({ width: 0, height: 0, links: [] });
      return;
    }

    const stageElement = stageRef.current;
    if (!stageElement) {
      return;
    }

    const mapElement = stageElement.querySelector<HTMLElement>("[data-ht-map-canvas='true']");
    if (!mapElement) {
      return;
    }

    let animationFrame = 0;
    const scenario = highThroughputDemoScenario;
    const projectedCandidateMap = buildProjectedCandidateMap(stageIndex);

    const updateLayout = () => {
      const stage = stageRef.current;
      const map = stage?.querySelector<HTMLElement>("[data-ht-map-canvas='true']");
      if (!stage || !map) {
        setLayout({ width: 0, height: 0, links: [] });
        return;
      }

      const stageRect = stage.getBoundingClientRect();
      const mapRect = map.getBoundingClientRect();
      if (stageRect.width <= 0 || stageRect.height <= 0 || mapRect.width <= 0 || mapRect.height <= 0) {
        setLayout({ width: 0, height: 0, links: [] });
        return;
      }

      const mapCenterX = mapRect.left + mapRect.width / 2;
      const links = scenario.agents.flatMap((agent) => {
        const card = stage.querySelector<HTMLElement>(`[data-agent-id="${agent.id}"]`);
        const rounds = scenario.roundsByTarget[agent.targetKey];
        const activeRound = rounds[clamp(iterationRoundIndex, 0, rounds.length - 1)] ?? rounds[rounds.length - 1];
        const point = projectedCandidateMap.get(activeRound.currentBestId);
        if (!card || !point) {
          return [];
        }

        const cardRect = card.getBoundingClientRect();
        const cardCenterX = cardRect.left + cardRect.width / 2;
        const isLeftSide = cardCenterX < mapCenterX;
        const sourceX = (isLeftSide ? cardRect.right : cardRect.left) - stageRect.left;
        const sourceY = cardRect.top - stageRect.top + cardRect.height * 0.46 + 6;
        const renderedPoint = mapPointToRenderedPosition(point, mapRect);
        const targetX = mapRect.left - stageRect.left + renderedPoint.x;
        const targetY = mapRect.top - stageRect.top + renderedPoint.y;
        const handleOffset = clamp(Math.abs(targetX - sourceX) * 0.36, 32, 110);
        const path = [
          `M ${sourceX.toFixed(2)} ${sourceY.toFixed(2)}`,
          `C ${(sourceX + (isLeftSide ? handleOffset : -handleOffset)).toFixed(2)} ${sourceY.toFixed(2)}`,
          `${(targetX + (isLeftSide ? -handleOffset : handleOffset)).toFixed(2)} ${targetY.toFixed(2)}`,
          `${targetX.toFixed(2)} ${targetY.toFixed(2)}`,
        ].join(" ");
        const target = getTarget(agent.targetKey);

        return [
          {
            id: agent.id,
            color: target.color,
            path,
          },
        ];
      });

      setLayout({ width: stageRect.width, height: stageRect.height, links });
    };

    const scheduleLayout = () => {
      window.cancelAnimationFrame(animationFrame);
      animationFrame = window.requestAnimationFrame(updateLayout);
    };

    scheduleLayout();
    window.addEventListener("resize", scheduleLayout);

    const resizeObserver = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(scheduleLayout);
    resizeObserver?.observe(stageElement);
    resizeObserver?.observe(mapElement);
    scenario.agents.forEach((agent) => {
      const card = stageElement.querySelector<HTMLElement>(`[data-agent-id="${agent.id}"]`);
      if (card) {
        resizeObserver?.observe(card);
      }
    });

    return () => {
      window.cancelAnimationFrame(animationFrame);
      window.removeEventListener("resize", scheduleLayout);
      resizeObserver?.disconnect();
    };
  }, [iterationRoundIndex, stageIndex, stageRef]);

  if (stageIndex !== 3 || layout.width <= 0 || layout.height <= 0 || layout.links.length === 0) {
    return null;
  }

  return (
    <svg
      className="ht-agent-attention-overlay"
      viewBox={`0 0 ${layout.width} ${layout.height}`}
      aria-hidden="true"
      preserveAspectRatio="none"
    >
      {layout.links.map((link) => (
        <g key={link.id} style={{ "--target-color": link.color } as CSSProperties}>
          <path className="ht-agent-attention-overlay-line" d={link.path} />
        </g>
      ))}
    </svg>
  );
}

function PropertySpaceBoard({
  stageIndex,
  confirmedSetup,
  activeTargetKey,
  onActiveTargetChange,
  iterationRoundIndex,
  priorDataUploads,
  validationValues,
  selectedRecommendation,
  onSelectRecommendation,
}: {
  stageIndex: number;
  confirmedSetup: ConfirmedSetup;
  activeTargetKey: HighThroughputTargetKey;
  onActiveTargetChange: (targetKey: HighThroughputTargetKey) => void;
  iterationRoundIndex: number;
  priorDataUploads: PriorDataUploadsState;
  validationValues: RecommendationValidationValues;
  selectedRecommendation: RecommendationSelection | null;
  onSelectRecommendation: (selection: RecommendationSelection) => void;
}) {
  const scenario = highThroughputDemoScenario;
  const activeTarget = getConfiguredTarget(activeTargetKey, confirmedSetup);
  const generatedComplete = stageIndex > 0;
  const boardTitle =
    stageIndex === 0
      ? `${activeTarget.shortLabel} 单性质候选空间待启动`
      : stageIndex === 1
        ? `${activeTarget.shortLabel} 候选空间 + 正交实验样本`
        : stageIndex === 2
          ? `${activeTarget.shortLabel} 正交先验初始热点图`
          : stageIndex === 3
            ? `${activeTarget.shortLabel} 单性质空间迭代更新`
            : `${activeTarget.shortLabel} S3 收敛候选输出`;

  return (
    <section className="ht-property-space-board-panel">
      <div className="ht-panel-header">
        <div>
          <span className="ht-kicker">Single-property Candidate Space</span>
          <h2>{boardTitle}</h2>
        </div>
        <div className="ht-space-status-group">
          <div className="ht-property-space-switcher" role="tablist" aria-label="切换单性质候选空间">
            {scenario.targets.map((target) => (
              <button
                key={target.key}
                type="button"
                role="tab"
                aria-selected={target.key === activeTargetKey}
                aria-pressed={target.key === activeTargetKey}
                onClick={() => onActiveTargetChange(target.key)}
                style={{ "--target-color": target.color } as CSSProperties}
              >
                {target.shortLabel}
              </button>
            ))}
          </div>
          <span className={cn("ht-candidate-space-status", generatedComplete ? "complete" : "pending")}>
            {stageIndex === 0
              ? `${formatNumber(confirmedSetup.monomerACount)} x ${formatNumber(confirmedSetup.monomerBCount)} candidates pending`
              : `${formatNumber(confirmedSetup.monomerACount)} x ${formatNumber(confirmedSetup.monomerBCount)} = ${formatNumber(confirmedSetup.candidateTotal)} candidates ready`}
          </span>
        </div>
      </div>

      <div className="ht-property-space-grid single">
        <PropertySpaceCard
          key={activeTarget.key}
          stageIndex={stageIndex}
          target={activeTarget}
          variant="large"
          iterationRoundIndex={iterationRoundIndex}
          priorDataUpload={priorDataUploads[activeTarget.key] ?? null}
          validationValues={validationValues}
          selectedRecommendation={selectedRecommendation}
          onSelectRecommendation={onSelectRecommendation}
        />
      </div>
    </section>
  );
}

function PropertySpaceCard({
  stageIndex,
  target,
  variant = "compact",
  iterationRoundIndex = 2,
  priorDataUpload = null,
  validationValues = {},
  selectedRecommendation = null,
  onSelectRecommendation,
}: {
  stageIndex: number;
  target: HighThroughputTarget;
  variant?: "compact" | "large";
  iterationRoundIndex?: number;
  priorDataUpload?: PriorDataUploadState | null;
  validationValues?: RecommendationValidationValues;
  selectedRecommendation?: RecommendationSelection | null;
  onSelectRecommendation?: (selection: RecommendationSelection) => void;
}) {
  const space = getPropertySpace(target.key);
  const activeRounds = highThroughputDemoScenario.roundsByTarget[target.key];
  const activeRoundIndex = clamp(iterationRoundIndex, 0, activeRounds.length - 1);
  const surface = propertySurfaceForStage(stageIndex, space, iterationRoundIndex);
  const roundIds = propertyRoundIds(target.key, stageIndex, iterationRoundIndex);
  const isPriorLoading = Boolean(priorDataUpload?.isLoading);
  const isPriorError = Boolean(priorDataUpload?.errorMessage);
  const isPriorReady = Boolean(priorDataUpload && !priorDataUpload.isLoading && !priorDataUpload.errorMessage);
  const showPriorDoe = stageIndex >= 2 || (stageIndex === 1 && isPriorReady);
  const priorIds = showPriorDoe ? space.priorCandidateIds : [];
  const measuredPriorIds = stageIndex >= 2 ? space.priorCandidateIds : [];
  const currentBestId = propertySpaceCurrentBestId(stageIndex, space, target, iterationRoundIndex, validationValues);
  const currentBest = currentBestId ? getCandidate(currentBestId) : undefined;
  const specialIds = new Set([
    ...priorIds,
    ...measuredPriorIds,
    ...roundIds.testedIds,
    ...roundIds.recommendedIds,
    currentBestId,
  ].filter(Boolean));
  const renderedPoints = stageIndex >= 1
    ? space.candidatePoints.filter((point, index) => index < MAX_RENDERED_PROPERTY_DOTS || specialIds.has(point.candidateId))
    : [];
  const displayBounds = buildDisplayBounds(renderedPoints.length > 0 ? renderedPoints : space.candidatePoints);
  const projectedRenderedPoints = renderedPoints.map((point) => ({
    ...point,
    ...projectPropertyDisplayPoint(point, displayBounds),
  }));
  const projectedSurfaceBlobs = surface?.blobs.map((blob) => projectPropertyDisplayBlob(blob, displayBounds)) ?? [];
  const projectSpacePoint = (point: { x: number; y: number }) => projectPropertyDisplayPoint(point, displayBounds);
  const measuredSet = new Set([...measuredPriorIds, ...roundIds.testedIds]);
  const currentTestedSet = new Set(stageIndex === 2 ? measuredPriorIds : roundIds.currentTestedIds);
  const priorSet = new Set(priorIds);
  const recommendedSet = new Set(roundIds.recommendedIds);
  const surfaceLabel = surface?.label ?? "No surface yet";
  const stageLabel =
    stageIndex === 0
      ? "待生成"
      : stageIndex === 1
        ? isPriorLoading
          ? "Loading DOE"
          : isPriorError
          ? "CSV error"
          : isPriorReady
          ? "DOE uploaded"
          : "Waiting CSV"
      : stageIndex === 2
          ? "Prior DOE Surface"
          : surface?.label ?? "Round surface";
  const canInspectRecommendation =
    (stageIndex === 2 || stageIndex === 3) &&
    recommendedSet.size > 0 &&
    Boolean(onSelectRecommendation);
  const selectRecommendation = (candidateId: string) => {
    if (canInspectRecommendation) {
      onSelectRecommendation?.({ targetKey: target.key, candidateId });
    }
  };
  const handleRecommendationKeyDown = (event: KeyboardEvent<SVGGElement>, candidateId: string) => {
    if (!canInspectRecommendation) {
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      selectRecommendation(candidateId);
    }
  };

  return (
    <article className={cn("ht-property-space-card", variant)} style={{ "--target-color": target.color } as CSSProperties}>
      <div className="ht-property-space-head">
        <div>
          <span>{target.shortLabel} Space</span>
          <strong>{target.label}</strong>
        </div>
        <b className={cn(stageIndex === 3 && "ht-property-round-label")} data-tooltip={stageLabel} tabIndex={stageIndex === 3 ? 0 : undefined}>
          <span>{stageLabel}</span>
        </b>
      </div>

      <svg viewBox={`0 0 ${MATERIAL_MAP_WIDTH} ${MATERIAL_MAP_HEIGHT}`} role="img" aria-label={`${target.shortLabel} single-property optimization space`}>
        <defs>
          <radialGradient id={`ht-property-gradient-${target.key}-${surface?.id ?? "none"}`} cx="50%" cy="50%" r="55%">
            <stop offset="0%" stopColor={target.color} stopOpacity="0.82" />
            <stop offset="46%" stopColor={target.color} stopOpacity="0.32" />
            <stop offset="100%" stopColor={target.color} stopOpacity="0" />
          </radialGradient>
        </defs>
        <rect x="0" y="0" width={MATERIAL_MAP_WIDTH} height={MATERIAL_MAP_HEIGHT} rx="3" fill="#fbfdff" />
        <g className="ht-material-grid" aria-hidden="true">
          {Array.from({ length: 5 }, (_, index) => (
            <line key={`x-${index}`} x1={(index + 1) * 16} y1="0" x2={(index + 1) * 16} y2={MATERIAL_MAP_HEIGHT} />
          ))}
          {Array.from({ length: 3 }, (_, index) => (
            <line key={`y-${index}`} x1="0" y1={(index + 1) * 12} x2={MATERIAL_MAP_WIDTH} y2={(index + 1) * 12} />
          ))}
        </g>
        {surface ? (
          <g className="ht-property-heatmap-layer">
            {projectedSurfaceBlobs.map((blob, index) => (
              <ellipse
                key={`${surface.id}-${index}`}
                cx={blob.x}
                cy={blob.y}
                rx={blob.rx}
                ry={blob.ry}
                fill={`url(#ht-property-gradient-${target.key}-${surface.id})`}
                opacity={blob.opacity}
              />
            ))}
          </g>
        ) : null}
        {projectedRenderedPoints.map((point, index) => (
          <circle
            key={point.candidateId}
            cx={point.x}
            cy={point.y}
            r={index % 11 === 0 ? "0.38" : "0.28"}
            fill="#b9c5d6"
            opacity="0.34"
          >
            <title>{point.candidateId}</title>
          </circle>
        ))}
        {space.candidatePoints
          .filter((point) => priorSet.has(point.candidateId))
          .map((point) => {
            const projectedPoint = projectSpacePoint(point);
            return (
              <rect
                key={`prior-${point.candidateId}`}
                className={cn("ht-doe-sample", measuredSet.has(point.candidateId) && "measured")}
                x={projectedPoint.x - 0.82}
                y={projectedPoint.y - 0.82}
                width="1.64"
                height="1.64"
                rx="0.2"
              >
                <title>{point.candidateId} DOE prior</title>
              </rect>
            );
          })}
        {space.candidatePoints
          .filter((point) => measuredSet.has(point.candidateId) && !priorSet.has(point.candidateId))
          .map((point) => {
            const projectedPoint = projectSpacePoint(point);
            return (
              <circle
                key={`tested-${point.candidateId}`}
                className={cn("ht-tested-sample", currentTestedSet.has(point.candidateId) && "current")}
                cx={projectedPoint.x}
                cy={projectedPoint.y}
                r={currentTestedSet.has(point.candidateId) ? "1.02" : "0.78"}
              />
            );
          })}
        {space.candidatePoints
          .filter((point) => recommendedSet.has(point.candidateId))
          .map((point) => {
            const projectedPoint = projectSpacePoint(point);
            const isSelected = selectedRecommendation?.targetKey === target.key && selectedRecommendation.candidateId === point.candidateId;
            return (
              <g
                key={`recommended-${point.candidateId}`}
                className={cn("ht-recommended-sample-node", canInspectRecommendation && "selectable", isSelected && "selected")}
                role={canInspectRecommendation ? "button" : undefined}
                tabIndex={canInspectRecommendation ? 0 : undefined}
                aria-label={canInspectRecommendation ? `查看 ${point.candidateId} SMILES 并录入 ${target.shortLabel} 实测值` : undefined}
                onClick={() => selectRecommendation(point.candidateId)}
                onKeyDown={(event) => handleRecommendationKeyDown(event, point.candidateId)}
              >
                <circle
                  className="ht-recommended-sample-hit-area"
                  cx={projectedPoint.x}
                  cy={projectedPoint.y}
                  r="2.65"
                />
                <circle
                  className="ht-recommended-sample"
                  cx={projectedPoint.x}
                  cy={projectedPoint.y}
                  r="1.04"
                />
                {isSelected ? (
                  <circle
                    className="ht-recommended-sample-selected"
                    cx={projectedPoint.x}
                    cy={projectedPoint.y}
                    r="1.88"
                  />
                ) : null}
                <title>{point.candidateId} 推荐验证点</title>
              </g>
            );
          })}
        {currentBestId ? (
          space.candidatePoints
            .filter((point) => point.candidateId === currentBestId)
            .map((point) => {
              const projectedPoint = projectSpacePoint(point);
              return (
                <g key={`best-${point.candidateId}`} className="ht-current-best-marker" transform={`translate(${projectedPoint.x} ${projectedPoint.y})`}>
                  <circle r="1.65" />
                  <path d="M 0 -2.2 L 0.56 -0.64 L 2.15 -0.64 L 0.86 0.28 L 1.34 1.86 L 0 0.9 L -1.34 1.86 L -0.86 0.28 L -2.15 -0.64 L -0.56 -0.64 Z" />
                </g>
              );
            })
        ) : null}
      </svg>

      <div className="ht-property-space-meta">
        <span>DOE <b>{priorSet.size}</b></span>
        <span>已测 <b>{measuredSet.size}</b></span>
        <span>推荐 <b>{recommendedSet.size}</b></span>
        <span>热点 <b>{surfaceLabel}</b></span>
      </div>
      <div className="ht-property-space-best">
        <span>当前最优</span>
        <strong>{currentBestId || "--"}</strong>
        <em>{candidateValueWithValidation(currentBest, target, validationValues)} / gap {targetGapLabelWithValidation(currentBest, target, validationValues)}</em>
      </div>
    </article>
  );
}

function ExperimentPriorPanel({
  stageIndex,
  activeTargetKey,
  priorDataUploads,
  iterationRoundIndex,
  selectedRecommendation,
  onSelectRecommendation,
  validationValues,
  onValidationValueChange,
  validationConfirmed,
  validationMissingCount,
  validationRequiredCount,
  onConfirmValidationGroup,
}: {
  stageIndex: number;
  activeTargetKey: HighThroughputTargetKey;
  priorDataUploads: PriorDataUploadsState;
  iterationRoundIndex: number;
  selectedRecommendation: RecommendationSelection | null;
  onSelectRecommendation: (selection: RecommendationSelection) => void;
  validationValues: RecommendationValidationValues;
  onValidationValueChange: (targetKey: HighThroughputTargetKey, candidateId: string, value: string) => void;
  validationConfirmed: boolean;
  validationMissingCount: number;
  validationRequiredCount: number;
  onConfirmValidationGroup: () => void;
}) {
  const scenario = highThroughputDemoScenario;
  const activeTarget = getTarget(activeTargetKey);
  const activeCsvFile = scenario.doeCsvFiles[activeTargetKey];
  const activeUpload = priorDataUploads[activeTargetKey] ?? null;
  const activeUploadLoading = Boolean(activeUpload?.isLoading);
  const activeUploadError = activeUpload?.errorMessage ?? "";
  const activeUploadReady = Boolean(activeUpload && !activeUpload.isLoading && !activeUpload.errorMessage);
  const uploadedCount = Object.values(priorDataUploads).filter((upload) => upload && !upload.isLoading && !upload.errorMessage).length;
  const iterationActive = stageIndex >= 3;
  const activeRecommendationIds = recommendationValidationIds(activeTargetKey, stageIndex, iterationRoundIndex);

  return (
    <section className="ht-prior-workflow-panel">
      <div className="ht-panel-header">
        <div>
          <span className="ht-kicker">DOE = Design of Experiments</span>
          <h2>正交实验先验与单性质迭代状态</h2>
        </div>
      </div>

      <div className="ht-prior-workflow-grid">
        <article className={cn("ht-prior-step-card", stageIndex >= 1 && "active")}>
          <span>S1</span>
          <strong>上传对应性质 CSV</strong>
          {activeUpload ? (
            <div className={cn("ht-prior-upload-result", activeUploadError && "error")}>
              {activeUploadError ? (
                <strong>{activeUploadError}</strong>
              ) : (
                <>
                  <strong>{activeUpload.fileName}</strong>
                  <span>
                    {activeUploadLoading ? "loading DOE samples" : "ready"} / {activeUpload.fileType} / {activeUpload.sampleCount} samples / {activeUpload.fieldCount} fields / {activeUpload.propertyColumn} / {activeUpload.uploadedAt}
                  </span>
                </>
              )}
            </div>
          ) : (
            <b>等待上传 {activeCsvFile.fileName}</b>
          )}
        </article>
        <article className={cn("ht-prior-step-card", stageIndex >= 2 && "active")}>
          <span>S2</span>
          <strong>Prior DOE Surface</strong>
          <b>{stageIndex >= 2 ? "initial surfaces ready" : `${uploadedCount}/4 CSV ready`}</b>
        </article>
        <article className={cn("ht-prior-step-card", iterationActive && "active")}>
          <span>S3</span>
          <strong>Agent rounds update surfaces</strong>
          <b>{iterationActive ? "3 rounds scripted" : "waiting for prior surface"}</b>
        </article>
      </div>

      {stageIndex === 1 ? (
        <DoeCsvPreviewPanel
          target={activeTarget}
          csvFile={activeCsvFile}
          upload={activeUpload}
          isLoading={activeUploadLoading}
          isReady={activeUploadReady}
          errorMessage={activeUploadError}
          selectedCount={activeUploadReady ? activeCsvFile.rows.length : 0}
        />
      ) : null}

      {(stageIndex === 2 || stageIndex === 3) && activeRecommendationIds.length > 0 ? (
        <RecommendationValidationPanel
          stageIndex={stageIndex}
          iterationRoundIndex={iterationRoundIndex}
          target={activeTarget}
          recommendedIds={activeRecommendationIds}
          selectedCandidateId={selectedRecommendation?.targetKey === activeTargetKey ? selectedRecommendation.candidateId : ""}
          validationValues={validationValues}
          onSelectCandidate={(candidateId) => onSelectRecommendation({ targetKey: activeTargetKey, candidateId })}
          onValidationValueChange={(candidateId, value) => onValidationValueChange(activeTargetKey, candidateId, value)}
          validationConfirmed={validationConfirmed}
          validationMissingCount={validationMissingCount}
          validationRequiredCount={validationRequiredCount}
          onConfirmValidationGroup={onConfirmValidationGroup}
        />
      ) : null}

      <div className="ht-round-summary-grid">
        {scenario.targets.map((target) => {
          const rounds = scenario.roundsByTarget[target.key];
          const activeRound = rounds[clamp(iterationRoundIndex, 0, rounds.length - 1)] ?? rounds[rounds.length - 1];
          return (
            <article key={target.key} style={{ "--target-color": target.color } as CSSProperties}>
              <span>{target.shortLabel}</span>
              <strong>{iterationActive ? activeRound.currentBestId : "pending"}</strong>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function RecommendationValidationPanel({
  stageIndex,
  iterationRoundIndex,
  target,
  recommendedIds,
  selectedCandidateId,
  validationValues,
  onSelectCandidate,
  onValidationValueChange,
  validationConfirmed,
  validationMissingCount,
  validationRequiredCount,
  onConfirmValidationGroup,
}: {
  stageIndex: number;
  iterationRoundIndex: number;
  target: HighThroughputTarget;
  recommendedIds: string[];
  selectedCandidateId: string;
  validationValues: RecommendationValidationValues;
  onSelectCandidate: (candidateId: string) => void;
  onValidationValueChange: (candidateId: string, value: string) => void;
  validationConfirmed: boolean;
  validationMissingCount: number;
  validationRequiredCount: number;
  onConfirmValidationGroup: () => void;
}) {
  const activeCandidateId = recommendedIds.includes(selectedCandidateId)
    ? selectedCandidateId
    : recommendedIds[0] ?? "";
  const candidate = activeCandidateId ? getCandidate(activeCandidateId) : undefined;
  const smiles = fallbackCandidateSmiles(candidate);
  const validationValue = activeCandidateId ? validationValues[recommendationValidationKey(target.key, activeCandidateId)] ?? "" : "";
  const panelLabel = stageIndex === 2 ? "S2 推荐验证" : `S3 Round ${iterationRoundIndex + 1} 推荐验证`;
  const savedMessage = stageIndex === 2
    ? "验证值已记录，进入 S3 后作为回流样本更新热点图。"
    : "验证值已记录，切换到下一轮后作为回流样本更新热点图。";
  const pendingMessage = stageIndex === 2
    ? "输入实验验证值后，本演示会保留该点的回流记录状态。"
    : "输入该轮推荐点的实测值后，本演示会保留为下一轮回流记录。";
  const confirmationStatus = validationMissingCount > 0
    ? `待补 ${validationMissingCount} / ${validationRequiredCount}`
    : validationConfirmed
      ? "已确认回流"
      : "待确认回流";
  const confirmationHint = validationConfirmed
    ? "下一步会进入实验回流与模型更新。"
    : validationMissingCount > 0
      ? "所有推荐点都有实测值后才能确认。"
      : "确认后才允许进入下一步，模拟实验结果完成回流。";

  if (recommendedIds.length === 0) {
    return null;
  }

  return (
    <section className="ht-s2-validation-panel" style={{ "--target-color": target.color } as CSSProperties}>
      <div className="ht-s2-validation-head">
        <div>
          <span className="ht-kicker">{panelLabel}</span>
          <h3>点击推荐点查看 SMILES，并录入实际验证值</h3>
        </div>
        <b>{target.shortLabel} / {targetThresholdLabel(target)}</b>
      </div>

      <div className="ht-s2-validation-grid">
        <div className="ht-s2-recommendation-list" aria-label={`${target.shortLabel} 推荐验证点`}>
          {recommendedIds.map((candidateId, index) => {
            const recommendedCandidate = getCandidate(candidateId);
            const savedValue = validationValues[recommendationValidationKey(target.key, candidateId)] ?? "";
            return (
              <button
                key={candidateId}
                type="button"
                className={cn("ht-s2-recommendation-card", candidateId === activeCandidateId && "active", savedValue && "saved")}
                onClick={() => onSelectCandidate(candidateId)}
                aria-pressed={candidateId === activeCandidateId}
              >
                <span>推荐 {index + 1}</span>
                <strong>{candidateId}</strong>
                <em>预测 {candidateValue(recommendedCandidate, target)}</em>
                {savedValue ? <i>已录入 {savedValue}{target.unit}</i> : <i>待验证</i>}
              </button>
            );
          })}
        </div>

        <article className="ht-s2-smiles-card">
          <div className="ht-s2-candidate-summary">
            <span>{activeCandidateId || "--"}</span>
            <strong>{candidate?.monomerA ?? "--"} + {candidate?.monomerB ?? "--"}</strong>
            <em>{candidate?.cluster ?? "--"} / 预测 {candidateValue(candidate, target)}</em>
          </div>
          <div className="ht-s2-smiles-grid">
            <label>
              <span>polymer_smiles</span>
              <code>{smiles.polymerSmiles}</code>
            </label>
            <label>
              <span>monomer_a_smiles</span>
              <code>{smiles.monomerASmiles}</code>
            </label>
            <label>
              <span>monomer_b_smiles</span>
              <code>{smiles.monomerBSmiles}</code>
            </label>
          </div>
        </article>

        <article className="ht-s2-measurement-card">
          <label htmlFor={`s2-validation-${target.key}-${activeCandidateId}`}>
            <span>实际验证 {target.shortLabel}</span>
            <div>
              <input
                id={`s2-validation-${target.key}-${activeCandidateId}`}
                type="number"
                inputMode="decimal"
                step={target.key === "modulus" ? "0.1" : "1"}
                placeholder={candidate ? formatTargetValue(target, candidate.scores[target.key]) : ""}
                value={validationValue}
                onChange={(event) => onValidationValueChange(activeCandidateId, event.currentTarget.value)}
                disabled={!activeCandidateId}
              />
              <b>{target.unit}</b>
            </div>
          </label>
          <p>{validationValue ? savedMessage : pendingMessage}</p>
          <div className={cn("ht-validation-confirm-box", validationConfirmed && "confirmed")}>
            <span>本轮闭环</span>
            <strong>{confirmationStatus}</strong>
            <button
              type="button"
              onClick={onConfirmValidationGroup}
              disabled={validationMissingCount > 0 || validationConfirmed}
            >
              {validationConfirmed ? "已确认" : "确认本轮实测值"}
            </button>
            <em>{confirmationHint}</em>
          </div>
        </article>
      </div>
    </section>
  );
}

function DoeCsvPreviewPanel({
  target,
  csvFile,
  upload,
  isLoading,
  isReady,
  errorMessage,
  selectedCount,
}: {
  target: HighThroughputTarget;
  csvFile: HighThroughputDoeCsvFile;
  upload: PriorDataUploadState | null;
  isLoading: boolean;
  isReady: boolean;
  errorMessage: string;
  selectedCount: number;
}) {
  return (
    <section className={cn("ht-doe-csv-preview-panel", isLoading && "loading", isReady && "uploaded", errorMessage && "error")} style={{ "--target-color": target.color } as CSSProperties}>
      <div className="ht-doe-csv-preview-head">
        <div>
          <span className="ht-kicker">{target.shortLabel} DOE CSV Preview</span>
          <h3>{csvFile.fileName}</h3>
        </div>
        <a href={csvFile.href} download={csvFile.fileName}>
          下载示例 CSV
        </a>
      </div>

      {errorMessage ? (
        <div className="ht-doe-csv-status-row error">
          <b>{errorMessage}</b>
        </div>
      ) : (
        <div className="ht-doe-csv-status-row">
          <span>{isLoading ? "loading samples" : isReady ? "uploaded" : "waiting upload"}</span>
          <b>{upload ? upload.fileName : csvFile.displayName}</b>
          <em>{selectedCount} DOE rows / {csvFile.propertyColumn}</em>
        </div>
      )}

      <div className="ht-doe-csv-table-wrap">
        <table className="ht-doe-csv-table">
          <thead>
            <tr>
              <th>doe_run</th>
              <th>candidate_id</th>
              <th>pi_source_id</th>
              <th>monomer_a</th>
              <th>monomer_b</th>
              <th>polybert_x</th>
              <th>polybert_y</th>
              <th>polymer_smiles</th>
              <th>monomer_a_smiles</th>
              <th>monomer_b_smiles</th>
              <th>{csvFile.propertyColumn}</th>
            </tr>
          </thead>
          <tbody>
            {isReady ? (
              csvFile.rows.map((row) => (
                <tr key={row.candidateId}>
                  <td>{row.doeRun}</td>
                  <td><strong>{row.candidateId}</strong></td>
                  <td>{row.sourcePiId ?? "--"}</td>
                  <td>{row.monomerA}</td>
                  <td>{row.monomerB}</td>
                  <td>{row.polybertX.toFixed(2)}</td>
                  <td>{row.polybertY.toFixed(2)}</td>
                  <td>{row.polymerSmiles}</td>
                  <td>{row.monomerASmiles}</td>
                  <td>{row.monomerBSmiles}</td>
                  <td>{row.propertyValue}</td>
                </tr>
              ))
            ) : (
              <tr>
                <td className="ht-doe-csv-placeholder" colSpan={11}>
                  {errorMessage || (isLoading ? "正在加载 DOE 样本点..." : `上传 ${csvFile.fileName} 后显示 DOE 表格数据`)}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function RatioAnnealingMap({
  stageIndex,
  activeStepIndex,
  confirmedSetup,
  activeStepValidationConfirmed = true,
}: {
  stageIndex: number;
  activeStepIndex: number;
  confirmedSetup: ConfirmedSetup;
  activeStepValidationConfirmed?: boolean;
}) {
  const scenario = highThroughputDemoScenario;
  const formulation = scenario.formulation;
  const finalExplanation = formulation.finalExplanation;
  const isFinalStage = stageIndex >= 6;
  const steps = formulation.searchSteps;
  const activeIndex = isFinalStage ? steps.length - 1 : clamp(activeStepIndex, 0, steps.length - 1);
  const activeStep = steps[activeIndex];
  const mixById = new Map(formulation.mixCandidates.map((mix) => [mix.id, mix]));
  const decisionVisible = isFinalStage || activeIndex === 0 || activeStepValidationConfirmed;
  const visibleMixes = activeStep.mixCandidateIds
    .map((mixId) => mixById.get(mixId))
    .filter((mix): mix is RatioMixCandidate => Boolean(mix));
  const pathMixIds = decisionVisible
    ? activeStep.acceptedPathIds
    : steps[Math.max(activeIndex - 1, 0)]?.acceptedPathIds ?? activeStep.acceptedPathIds;
  const acceptedPathMixes = pathMixIds
    .map((mixId) => mixById.get(mixId))
    .filter((mix): mix is RatioMixCandidate => Boolean(mix));
  const previousStep = steps[Math.max(activeIndex - 1, 0)] ?? activeStep;
  const rawCurrentMix = mixById.get(activeStep.currentMixId) ?? visibleMixes[0] ?? formulation.mixCandidates[0];
  const previousMix = mixById.get(activeStep.previousMixId) ?? rawCurrentMix;
  const proposedMix = mixById.get(activeStep.proposedMixId) ?? rawCurrentMix;
  const rawBestMix = mixById.get(activeStep.currentBestId) ?? rawCurrentMix;
  const previousBestMix = mixById.get(previousStep.currentBestId) ?? rawCurrentMix;
  const currentMix = decisionVisible ? rawCurrentMix : previousMix;
  const currentBestMix = decisionVisible ? rawBestMix : previousBestMix;
  const selectedMix = mixById.get(formulation.selectedMixId) ?? currentBestMix;
  const focusMix = isFinalStage ? selectedMix : currentBestMix;
  const focusPoint = projectRatioMixPoint(focusMix);
  const currentPoint = projectRatioMixPoint(currentMix);
  const previousPoint = projectRatioMixPoint(previousMix);
  const proposedPoint = projectRatioMixPoint(proposedMix);
  const selectedPoint = projectRatioMixPoint(selectedMix);
  const visibleMixIdSet = new Set(visibleMixes.map((mix) => mix.id));
  const pathPoints = acceptedPathMixes.map(projectRatioMixPoint);
  const selectedRatioLabel = formulation.components
    .map((component) => ratioPercent(selectedMix.ratios[component.id] ?? 0))
    .join(" / ");
  const finalPassCount = finalExplanation.targetOutcomes.filter((outcome) =>
    targetOutcomePasses(outcome.predictedValue, getConfiguredTarget(outcome.targetKey, confirmedSetup)),
  ).length;
  const finalTargetStatus = `${finalPassCount}/${finalExplanation.targetOutcomes.length} 达标`;
  const rejectedMixIdSet = new Set(
    steps
      .slice(0, decisionVisible ? activeIndex + 1 : activeIndex)
      .filter((step) => !step.accepted)
      .map((step) => step.proposedMixId),
  );
  const ratioGridPoints = [];

  for (let p1 = 0; p1 <= 10; p1 += 1) {
    for (let p2 = 0; p2 <= 10 - p1; p2 += 1) {
      for (let p3 = 0; p3 <= 10 - p1 - p2; p3 += 1) {
        const p4 = 10 - p1 - p2 - p3;
        ratioGridPoints.push({
          id: `${p1}-${p2}-${p3}-${p4}`,
          point: projectRatioPointFromRatios({
            p1: p1 / 10,
            p2: p2 / 10,
            p3: p3 / 10,
            p4: p4 / 10,
          }),
        });
      }
    }
  }

  return (
    <section className={cn("ht-material-map-panel ht-ratio-annealing-map-panel", isFinalStage && "final")}>
      <div className="ht-panel-header">
        <div>
          <span className="ht-kicker">{isFinalStage ? "最终配方结果" : "四组分比例性能地形"}</span>
          <h2>{isFinalStage ? "最终推荐配方位置" : "模拟退火比例性能地形"}</h2>
        </div>
        <div className="ht-space-status-group">
          <span className="ht-unified-space-badge">
            比例步长 {formulation.ratioGrid.step.toFixed(1)} / {formulation.ratioGrid.candidateCount} 个候选
          </span>
          <span className="ht-generated-space-badge">
            {isFinalStage ? `最终配方 ${selectedMix.id}` : "模拟退火路径"}
          </span>
        </div>
      </div>

      <div className="ht-map-canvas ht-ratio-annealing-canvas" aria-label={isFinalStage ? "S6 最终推荐配方位置图" : "S5 模拟退火比例搜索图"}>
        <svg viewBox="0 0 100 48" role="img">
          <title>{isFinalStage ? "S6 最终推荐配方位置" : "S5 模拟退火比例性能地形"}</title>
          <defs>
            <filter id="ht-ratio-terrain-blur" x="-25%" y="-25%" width="150%" height="150%">
              <feGaussianBlur stdDeviation="2.9" />
            </filter>
            <radialGradient id="ht-ratio-performance-gradient" cx="50%" cy="50%" r="58%">
              <stop offset="0%" stopColor="#0891b2" stopOpacity="0.58" />
              <stop offset="48%" stopColor="#67e8f9" stopOpacity="0.3" />
              <stop offset="100%" stopColor="#ecfeff" stopOpacity="0" />
            </radialGradient>
            <marker id="ht-ratio-path-arrow" markerHeight="5" markerWidth="5" orient="auto" refX="4.2" refY="2.5">
              <path d="M0,0 L5,2.5 L0,5 z" />
            </marker>
          </defs>

          <rect className="ht-ratio-map-bg" x="0.6" y="0.8" width="98.8" height="46.4" rx="2.3" />
          <g className="ht-material-grid" aria-hidden="true">
            {[18, 34, 50, 66, 82].map((x) => <line key={`v-${x}`} x1={x} y1="5" x2={x} y2="43" />)}
            {[10, 19, 28, 37].map((y) => <line key={`h-${y}`} x1="8" y1={y} x2="92" y2={y} />)}
          </g>

          <g className="ht-ratio-grid-points" aria-label="0.1 比例网格候选">
            {ratioGridPoints.map(({ id, point }) => (
              <circle key={id} cx={point.x} cy={point.y} r="0.34" />
            ))}
          </g>

          <g className="ht-ratio-performance-layer" aria-label={isFinalStage ? "最终配方达成区域" : "模拟目标地形"}>
            <ellipse cx={focusPoint.x} cy={focusPoint.y} rx="24" ry="13.5" />
            <ellipse cx={focusPoint.x + 2.2} cy={focusPoint.y - 0.8} rx="13.2" ry="7.2" />
          </g>

          <g className="ht-ratio-anchor-layer" aria-label="p1-p4 比例锚点">
            {formulation.components.map((component) => {
              const anchor = RATIO_SPACE_ANCHORS[component.id];
              if (!anchor) {
                return null;
              }

              return (
                <g key={component.id} className="ht-ratio-anchor" style={{ "--component-color": component.color } as CSSProperties}>
                  <rect x={anchor.x - 4.35} y={anchor.y - 2.25} width="8.7" height="4.5" rx="1.1" />
                  <text x={anchor.x} y={anchor.y + 0.58} textAnchor="middle">{component.id}</text>
                  <title>{`${component.id} 组分锚点，不是退火候选点`}</title>
                </g>
              );
            })}
          </g>

          {pathPoints.length > 1 ? (
            <path
              className={cn("ht-ratio-annealing-path", isFinalStage && "final")}
              d={buildSvgPath(pathPoints)}
              markerEnd={isFinalStage ? undefined : "url(#ht-ratio-path-arrow)"}
            />
          ) : null}

          {!isFinalStage && previousMix.id !== proposedMix.id ? (
            <path
              className={cn(
                "ht-ratio-proposal-line",
                decisionVisible ? activeStep.accepted ? "accepted" : "rejected" : "pending",
              )}
              d={buildSvgPath([previousPoint, proposedPoint])}
            />
          ) : null}

          <g className="ht-ratio-mix-layer" aria-label={isFinalStage ? "最终配方候选" : "退火搜索候选"}>
            {visibleMixes.map((mix) => {
              const point = projectRatioMixPoint(mix);
              const isVisible = visibleMixIdSet.has(mix.id);
              const isCurrent = mix.id === currentMix.id;
              const isBest = mix.id === currentBestMix.id;
              const isProposed = !isFinalStage && mix.id === proposedMix.id;
              const isRejected = rejectedMixIdSet.has(mix.id);
              const isSelected = isFinalStage && mix.id === selectedMix.id;
              const mixTitle = isFinalStage
                ? `${mix.id} / p1 ${ratioPercent(mix.ratios.p1 ?? 0)}, p2 ${ratioPercent(mix.ratios.p2 ?? 0)}, p3 ${ratioPercent(mix.ratios.p3 ?? 0)}, p4 ${ratioPercent(mix.ratios.p4 ?? 0)}`
                : `${mix.id} 综合 ${mix.score} / p1 ${ratioPercent(mix.ratios.p1 ?? 0)}, p2 ${ratioPercent(mix.ratios.p2 ?? 0)}, p3 ${ratioPercent(mix.ratios.p3 ?? 0)}, p4 ${ratioPercent(mix.ratios.p4 ?? 0)}`;

              return (
                <g
                  key={mix.id}
                  className={cn(
                    "ht-ratio-mix-node",
                    isVisible && "visible",
                    isProposed && "proposed",
                    isRejected && "rejected",
                    isCurrent && "current",
                    isBest && "best",
                    isSelected && "selected",
                  )}
                >
                  <circle cx={point.x} cy={point.y} r={isSelected ? 1.95 : isRejected ? 1.45 : isCurrent || isBest ? 1.65 : 1.05} />
                  {isCurrent || isBest || isSelected || isProposed || isRejected ? (
                    <text x={point.x + 2.1} y={point.y - 1.8}>{mix.id}</text>
                  ) : null}
                  <title>{mixTitle}</title>
                </g>
              );
            })}
          </g>

          {isFinalStage ? (
            <g className="ht-ratio-selected-star" transform={`translate(${selectedPoint.x} ${selectedPoint.y})`} aria-label="最终推荐配方">
              <path d="M0 -3.3 L0.76 -1 L3.14 -1 L1.2 0.42 L1.92 2.74 L0 1.35 L-1.92 2.74 L-1.2 0.42 L-3.14 -1 L-0.76 -1 Z" />
            </g>
          ) : (
            <g className="ht-ratio-current-ring" transform={`translate(${currentPoint.x} ${currentPoint.y})`} aria-label="当前退火配方">
              <circle r="3.2" />
            </g>
          )}
        </svg>

        {isFinalStage ? (
          <div className="ht-ratio-map-summary final">
            <span><b>最终配方</b>{selectedMix.id}</span>
            <span><b>目标状态</b>{finalTargetStatus}</span>
            <span><b>配方比例</b>{selectedRatioLabel}</span>
            <span><b>下一步</b>{finalExplanation.nextStep}</span>
          </div>
        ) : (
          <div className="ht-ratio-map-summary">
            <span><b>温度</b>{activeStep.coolingLabel}</span>
            <span><b>扰动</b>{previousMix.id} {"->"} {proposedMix.id}</span>
            <span><b>决策</b>{decisionVisible ? activeStep.decisionLabel : "等待实测回流"}</span>
            <span><b>当前最优</b>{currentBestMix.id} / {currentBestMix.score}</span>
          </div>
        )}
      </div>
    </section>
  );
}

function AgentOrbitPanel({
  stageIndex,
  side,
  confirmedSetup,
  activeTargetKey,
  onSelectTarget,
  iterationRoundIndex = 2,
  priorDataUploads,
  onPriorDataUpload,
}: {
  stageIndex: number;
  side: "left" | "right";
  confirmedSetup: ConfirmedSetup;
  activeTargetKey?: HighThroughputTargetKey;
  onSelectTarget?: (targetKey: HighThroughputTargetKey) => void;
  iterationRoundIndex?: number;
  priorDataUploads?: PriorDataUploadsState;
  onPriorDataUpload?: (targetKey: HighThroughputTargetKey, file: File | null) => void;
}) {
  const agents = highThroughputDemoScenario.agents;
  const visibleAgents = side === "left" ? agents.slice(0, 2) : agents.slice(2);

  return (
    <aside className={cn("ht-agent-orbit", side)}>
      {visibleAgents.map((agent, index) => (
        <AgentOptimizationCard
          key={agent.id}
          agent={agent}
          stageIndex={stageIndex}
          displayIndex={side === "left" ? index + 1 : index + 3}
          confirmedSetup={confirmedSetup}
          isActive={agent.targetKey === activeTargetKey}
          onSelectTarget={onSelectTarget}
          iterationRoundIndex={iterationRoundIndex}
          priorDataUpload={priorDataUploads?.[agent.targetKey] ?? null}
          onPriorDataUpload={onPriorDataUpload}
        />
      ))}
    </aside>
  );
}

function AgentOptimizationCard({
  agent,
  stageIndex,
  displayIndex,
  confirmedSetup,
  isActive = false,
  onSelectTarget,
  iterationRoundIndex = 2,
  priorDataUpload,
  onPriorDataUpload,
}: {
  agent: (typeof highThroughputDemoScenario.agents)[number];
  stageIndex: number;
  displayIndex: number;
  confirmedSetup: ConfirmedSetup;
  isActive?: boolean;
  onSelectTarget?: (targetKey: HighThroughputTargetKey) => void;
  iterationRoundIndex?: number;
  priorDataUpload?: PriorDataUploadState | null;
  onPriorDataUpload?: (targetKey: HighThroughputTargetKey, file: File | null) => void;
}) {
  const target = getConfiguredTarget(agent.targetKey, confirmedSetup);
  const space = getPropertySpace(agent.targetKey);
  const csvFile = highThroughputDemoScenario.doeCsvFiles[agent.targetKey];
  const outputComponent = highThroughputDemoScenario.formulation.components.find((component) => component.sourceTargetKey === agent.targetKey);
  const outputLabel = outputComponent?.id ?? target.shortLabel;
  const isPriorLoading = Boolean(priorDataUpload?.isLoading);
  const isPriorError = Boolean(priorDataUpload?.errorMessage);
  const isPriorReady = Boolean(priorDataUpload && !priorDataUpload.isLoading && !priorDataUpload.errorMessage);
  const roundIds = propertyRoundIds(agent.targetKey, stageIndex, iterationRoundIndex);
  const rounds = highThroughputDemoScenario.roundsByTarget[agent.targetKey];
  const activeRound = stageIndex === 3
    ? rounds[clamp(iterationRoundIndex, 0, rounds.length - 1)] ?? rounds[rounds.length - 1]
    : rounds[rounds.length - 1];
  const isConvergedRound = stageIndex === 3 && iterationRoundIndex >= 2;
  const priorBestId = bestCandidateId(space.priorCandidateIds, target);
  const surface = propertySurfaceForStage(stageIndex, space, iterationRoundIndex);
  const stageBestId =
    stageIndex === 3
      ? activeRound.currentBestId
      : stageIndex > 3
      ? space.currentBestId
      : stageIndex >= 2
        ? priorBestId
        : "";
  const candidate = stageBestId ? getCandidate(stageBestId) : undefined;
  const progress = agent.progressByStage[stageIndex] ?? 0;
  const displayColor = AGENT_DISPLAY_COLORS[Math.min(displayIndex - 1, AGENT_DISPLAY_COLORS.length - 1)];
  const actionLabel = AGENT_ACTION_LABELS[Math.min(displayIndex - 1, AGENT_ACTION_LABELS.length - 1)];
  const agentTitle = `${target.shortLabel} Agent`;
  const directionLabel = `${target.direction === "higher" ? "最大化" : "最小化"} ${target.shortLabel}`;
  const thresholdLabel = targetThresholdLabel(target);
  const candidateStatus =
    stageIndex === 0
      ? "等待任务设置"
      : stageIndex === 1
        ? `DOE ${isPriorReady ? space.priorCandidateIds.length : 0} / ${formatNumber(confirmedSetup.candidateTotal)}`
        : stageIndex === 2
          ? `先验最优 ${priorBestId}`
          : stageIndex < 4
            ? `${activeRound.currentBestId} / ${surface?.label ?? "Round surface"}`
            : `${outputLabel} = ${space.currentBestId}`;
  const actionStatus =
    stageIndex === 0
      ? "等待确认"
      : stageIndex === 1
        ? isPriorLoading
          ? "加载 DOE 点..."
          : isPriorReady
            ? "CSV 已上传"
            : `等待 ${target.shortLabel} CSV`
      : stageIndex === 2
        ? "先验建模→推荐 Round 1"
          : stageIndex === 3
            ? isConvergedRound
              ? "最终回流→最优稳定"
              : "推荐→回流→更新热点"
            : stageIndex === 4
              ? `锁定 ${outputLabel} 候选`
              : stageIndex === 5
                ? `${outputLabel} 进入配方池`
                : stageIndex === 6
                  ? `${outputLabel} 参与解释`
                  : actionLabel;
  const explanationBadge =
    stageIndex === 0
      ? "待配置"
      : stageIndex === 1
        ? isPriorLoading ? "加载 DOE" : "DOE 先验"
        : stageIndex === 2
          ? "先验热点"
          : stageIndex === 3
            ? "单性质迭代"
            : stageIndex === 4
            ? "候选输出"
            : stageIndex === 5
              ? "配方输入"
              : "来源解释";
  const stageStatusText =
    stageIndex === 0
      ? "等待任务设置确认"
      : stageIndex === 1
        ? isPriorLoading
          ? "正在加载 DOE 样本点"
          : isPriorReady
            ? `${target.shortLabel} CSV 已上传`
            : `等待上传 ${target.shortLabel} CSV`
        : stageIndex === 2
          ? "DOE 先验热点已生成"
          : stageIndex === 3
            ? isConvergedRound
              ? "最终实测回流完成"
              : `第 ${activeRound.round} 轮回流更新`
            : stageIndex === 4
              ? `${outputLabel} 候选已输出`
              : stageIndex === 5
                ? `${outputLabel} 进入比例搜索`
                : `${outputLabel} 参与最终解释`;
  const explanationMeta =
    stageIndex === 0
      ? ["输入未确认", "未启动优化"]
      : stageIndex === 1
        ? [`DOE ${isPriorReady ? space.priorCandidateIds.length : 0}`, isPriorLoading ? "loading samples" : csvFile.propertyColumn]
    : stageIndex === 2
        ? [`已测 ${space.priorCandidateIds.length}`, `推荐 ${roundIds.recommendedIds.length}`]
        : stageIndex === 3
          ? isConvergedRound
            ? [`已测 ${space.priorCandidateIds.length + roundIds.testedIds.length}`, "当前最优稳定"]
            : [`已测 ${space.priorCandidateIds.length + roundIds.testedIds.length}`, `推荐 ${roundIds.recommendedIds.length}`]
          : stageIndex === 4
            ? [`${outputLabel} 来自 S3`, `备选 ${agent.topCandidateIds.length}`]
            : stageIndex === 5
              ? ["比例搜索输入", `${outputLabel} 锁定`]
              : ["来源追踪", `${space.currentBestId} 来自 S3`];
  const canSwitchSpace = stageIndex <= 4 && Boolean(onSelectTarget);

  function handleSelectSpace() {
    if (canSwitchSpace) {
      onSelectTarget?.(agent.targetKey);
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (!canSwitchSpace) {
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelectTarget?.(agent.targetKey);
    }
  }

  function handleUploadClick(event: MouseEvent<HTMLLabelElement>) {
    event.stopPropagation();
  }

  function handlePriorUploadChange(event: ChangeEvent<HTMLInputElement>) {
    event.stopPropagation();
    onPriorDataUpload?.(agent.targetKey, event.currentTarget.files?.[0] ?? null);
    event.currentTarget.value = "";
  }

  return (
    <article
      className={cn("ht-agent-card", stageIndex === 3 && "iterating", canSwitchSpace && "selectable", isActive && "active")}
      data-agent-id={agent.id}
      role={canSwitchSpace ? "button" : undefined}
      tabIndex={canSwitchSpace ? 0 : undefined}
      aria-pressed={canSwitchSpace ? isActive : undefined}
      onClick={handleSelectSpace}
      onKeyDown={handleKeyDown}
      style={{ "--target-color": displayColor } as CSSProperties}
    >
      <div className="ht-agent-identity">
        <span>AGENT-{String(displayIndex).padStart(2, "0")}</span>
        <i aria-hidden="true" />
        <em>autonomous node</em>
      </div>
      <div className="ht-agent-card-head">
        <span className="ht-agent-avatar">
          <Bot aria-hidden="true" size={19} strokeWidth={2.4} />
        </span>
        <div>
          <h3>{agentTitle}</h3>
          <span>single-objective optimizer</span>
        </div>
        {stageIndex === 1 && onPriorDataUpload ? (
          <label
            className={cn("ht-agent-upload-button", isPriorLoading && "loading", isPriorReady && "complete", isPriorError && "error")}
            title={priorDataUpload?.errorMessage || (priorDataUpload ? priorDataUpload.fileName : `上传 ${csvFile.fileName}`)}
            onClick={handleUploadClick}
          >
            <input
              type="file"
              accept=".csv"
              onChange={handlePriorUploadChange}
            />
            {isPriorReady ? <FileCheck2 aria-hidden="true" size={13} /> : <UploadCloud aria-hidden="true" size={13} />}
            <span>{isPriorLoading ? "加载 DOE 点..." : isPriorError ? "文件错误" : isPriorReady ? `已上传 ${target.shortLabel} CSV` : `上传 ${target.shortLabel} CSV`}</span>
          </label>
        ) : (
          <strong>{agent.statusByStage[stageIndex]}</strong>
        )}
      </div>
      <div className="ht-agent-progress" aria-label={`${agentTitle} progress ${progress}%`}>
        <b style={{ width: `${progress}%` }} />
      </div>
      <div className="ht-agent-meaning" aria-label={`${agentTitle} property interpretation`}>
        <div className="ht-agent-explanation-head">
          <span>当前状态</span>
          <b>{explanationBadge}</b>
        </div>
        <p className="ht-agent-status-line" title={stageStatusText}>{stageStatusText}</p>
        <div className="ht-agent-explanation-meta">
          {explanationMeta.map((item) => (
            <span key={item}>{item}</span>
          ))}
        </div>
      </div>
      <div className="ht-agent-info-grid">
        <span>目标: <b>{thresholdLabel}</b></span>
        <span>当前: <b>{candidateValue(candidate, target)}</b></span>
        <span>候选: <b>{candidateStatus}</b></span>
        <span>动作: <b>{actionStatus}</b></span>
      </div>
    </article>
  );
}

function AgentPanel({
  stageIndex,
  confirmedSetup,
}: {
  stageIndex: number;
  confirmedSetup: ConfirmedSetup;
}) {
  const scenario = highThroughputDemoScenario;

  return (
    <aside className="ht-agent-panel">
      <div className="ht-panel-header">
        <div>
          <span className="ht-kicker">Single-objective agents</span>
          <h2>并行单目标优化</h2>
        </div>
        <BrainCircuit aria-hidden="true" size={20} />
      </div>
      <div className="ht-agent-list">
        {scenario.agents.map((agent) => {
          const index = scenario.agents.indexOf(agent);
          return (
            <AgentOptimizationCard
              key={agent.id}
              agent={agent}
              stageIndex={stageIndex}
              displayIndex={index + 1}
              confirmedSetup={confirmedSetup}
            />
          );
        })}
      </div>
    </aside>
  );
}

function CandidateOutputPanel({
  activeTargetKey,
  confirmedSetup,
}: {
  activeTargetKey: HighThroughputTargetKey;
  confirmedSetup: ConfirmedSetup;
}) {
  const scenario = highThroughputDemoScenario;
  const target = getConfiguredTarget(activeTargetKey, confirmedSetup);
  const space = getPropertySpace(activeTargetKey);
  const component = scenario.formulation.components.find((item) => item.sourceTargetKey === activeTargetKey);
  const agent = scenario.agents.find((item) => item.targetKey === activeTargetKey);
  const outputCandidate = getCandidate(space.currentBestId);
  const backupIds = (agent?.topCandidateIds ?? []).filter((candidateId) => candidateId !== space.currentBestId);
  const outputLabel = component?.id ?? "p?";
  const outputDescription = component?.description ?? "单性质收敛候选";

  return (
    <section className="ht-candidate-output-panel" style={{ "--target-color": target.color } as CSSProperties}>
      <div className="ht-panel-header">
        <div>
          <span className="ht-kicker">S4 Single-property Candidate Output</span>
          <h2>{target.shortLabel}{" -> "}{outputLabel} 候选输出</h2>
        </div>
        <span className="ht-simulation-badge compact">来自 S3 收敛空间</span>
      </div>

      <div className="ht-candidate-output-grid">
        <article className="ht-candidate-output-focus">
          <span>{outputLabel}</span>
          <div>
            <strong>{space.currentBestId}</strong>
            <em>{outputDescription}</em>
          </div>
          <dl>
            <div>
              <dt>来源空间</dt>
              <dd>{target.shortLabel} / {target.label}</dd>
            </div>
            <div>
              <dt>目标</dt>
              <dd>{targetThresholdLabel(target)}</dd>
            </div>
            <div>
              <dt>当前值</dt>
              <dd>{candidateValue(outputCandidate, target)}</dd>
            </div>
            <div>
              <dt>目标差距</dt>
              <dd>{targetGapLabel(outputCandidate, target)}</dd>
            </div>
          </dl>
        </article>

        <article className="ht-candidate-output-backups">
          <div className="ht-candidate-output-backup-label">
            <span><Target aria-hidden="true" size={16} /></span>
            <strong>Top-k 备选</strong>
          </div>
          <div className="ht-candidate-output-backup-list">
            {backupIds.map((candidateId) => {
              const candidate = getCandidate(candidateId);
              return (
                <span key={candidateId}>
                  <b>{candidateId}</b>
                  <em>{candidateValue(candidate, target)} / {targetGapLabel(candidate, target)}</em>
                </span>
              );
            })}
          </div>
        </article>

      </div>
    </section>
  );
}

function RatioSearchPanel({
  activeStepIndex,
  confirmedSetup,
  ratioValidationValues,
  validationConfirmed,
  validationMissingCount,
  onValidationValueChange,
  onConfirmValidation,
}: {
  activeStepIndex: number;
  confirmedSetup: ConfirmedSetup;
  ratioValidationValues: RatioValidationValues;
  validationConfirmed: boolean;
  validationMissingCount: number;
  onValidationValueChange: (targetKey: HighThroughputTargetKey, value: string) => void;
  onConfirmValidation: () => void;
}) {
  const scenario = highThroughputDemoScenario;
  const formulation = scenario.formulation;
  const steps = formulation.searchSteps;
  const activeIndex = clamp(activeStepIndex, 0, steps.length - 1);
  const activeStep = steps[activeIndex];
  const mixById = new Map(formulation.mixCandidates.map((mix) => [mix.id, mix]));
  const visibleMixes = activeStep.mixCandidateIds
    .map((mixId) => mixById.get(mixId))
    .filter((mix): mix is NonNullable<typeof mix> => Boolean(mix));
  const currentMix = mixById.get(activeStep.currentMixId) ?? visibleMixes[0] ?? formulation.mixCandidates[0];
  const currentBestMix = mixById.get(activeStep.currentBestId) ?? currentMix;
  const previousMix = mixById.get(activeStep.previousMixId) ?? currentMix;
  const proposedMix = mixById.get(activeStep.proposedMixId) ?? currentMix;
  const selectedMix = mixById.get(formulation.selectedMixId) ?? currentBestMix;
  const measurementRequired = activeIndex > 0;
  const decisionVisible = !measurementRequired || validationConfirmed;
  const displayCurrentMix = decisionVisible ? currentMix : previousMix;
  const previousBestMix = mixById.get(steps[Math.max(activeIndex - 1, 0)]?.currentBestId) ?? currentBestMix;
  const displayBestMix = decisionVisible ? currentBestMix : previousBestMix;
  const displayAchievementMix = decisionVisible ? currentMix : proposedMix;
  const displayedEvaluatedCount = decisionVisible
    ? activeStep.evaluatedCount
    : steps[Math.max(activeIndex - 1, 0)]?.evaluatedCount ?? activeStep.evaluatedCount;
  const deltaLabel = `${activeStep.deltaScore > 0 ? "+" : ""}${activeStep.deltaScore}`;
  const acceptanceLabel = `${Math.round(activeStep.acceptanceProbability * 100)}%`;
  const validationStatus = !measurementRequired
    ? "初始解已知"
    : validationMissingCount > 0
      ? `待补 ${validationMissingCount} 项`
      : validationConfirmed
        ? "实测已回流"
        : "待确认回流";

  return (
    <section className="ht-ratio-search-panel">
      <div className="ht-panel-header">
        <div>
          <span className="ht-kicker">S5 Formulation Ratio Search</span>
          <h2>四组分模拟退火比例搜索</h2>
        </div>
        <span className="ht-simulation-badge compact">p1-p4 from S4 output</span>
      </div>

      <div className="ht-ratio-search-grid">
        <div className="ht-component-pool">
          <SectionTitle icon={<Target aria-hidden="true" size={17} />} title="S4 输入组分池" />
          <div className="ht-component-list">
            {formulation.components.map((component) => {
              const target = getTarget(component.sourceTargetKey);
              const convergedCandidateId = getPropertySpace(component.sourceTargetKey).currentBestId;
              return (
                <article key={component.id} className="ht-component-row active locked" style={{ "--target-color": component.color } as CSSProperties}>
                  <span>{component.id}</span>
                  <div>
                    <strong>{component.label}</strong>
                    <em>{convergedCandidateId} / {target.shortLabel} from S4 output</em>
                  </div>
                  <b>{component.description}</b>
                </article>
              );
            })}
          </div>
        </div>

        <div className="ht-mix-candidate-panel">
          <div className="ht-ratio-step-control readonly" aria-label="S5 ratio search step">
            <div>
              <span>{activeStep.label}</span>
              <strong>{activeStep.title}</strong>
            </div>
          </div>
          <div className="ht-annealing-step-timeline" aria-label="Simulated annealing event timeline">
            {steps.map((step, index) => {
              const stepDecisionVisible = index < activeIndex || (index === activeIndex && decisionVisible);
              return (
                <span
                  key={step.id}
                  aria-current={index === activeIndex ? "step" : undefined}
                  className={cn(
                    stepDecisionVisible ? step.accepted ? "accepted" : "rejected" : "pending",
                    index === activeIndex && "active",
                  )}
                >
                  <span>{step.label}</span>
                  <b>{step.coolingLabel}</b>
                </span>
              );
            })}
          </div>

          <article className={cn("ht-annealing-decision-card", decisionVisible ? activeStep.accepted ? "accepted" : "rejected" : "pending")}>
            <div className="ht-annealing-move-grid">
              <div className="ht-annealing-mix-card">
                <span>当前解</span>
                <strong>{previousMix.id}</strong>
                <RatioStackedBar mix={previousMix} components={formulation.components} />
                <em>综合 {previousMix.score}</em>
              </div>
              <div className="ht-annealing-mix-card proposed">
                <span>邻域扰动</span>
                <strong>{proposedMix.id}</strong>
                <RatioStackedBar mix={proposedMix} components={formulation.components} />
                <em>{decisionVisible ? `综合 ${proposedMix.score}` : "待实测"}</em>
              </div>
              <div className="ht-annealing-mix-card">
                <span>决策后当前解</span>
                <strong>{displayCurrentMix.id}</strong>
                <RatioStackedBar mix={displayCurrentMix} components={formulation.components} />
                <em>{decisionVisible ? activeStep.accepted ? "accepted" : "kept previous" : "待决策"}</em>
              </div>
            </div>

            <div className="ht-annealing-metrics">
              <span><b>温度 T</b>{activeStep.temperature.toFixed(2)}</span>
              <span><b>Δscore</b>{decisionVisible ? deltaLabel : "--"}</span>
              <span><b>接受概率</b>{decisionVisible ? acceptanceLabel : "--"}</span>
              <span><b>结果</b>{decisionVisible ? activeStep.decisionLabel : "等待实测"}</span>
            </div>

          </article>
        </div>

        <div className="ht-mix-preview-panel ht-annealing-status-panel">
          <SectionTitle icon={<TestTube2 aria-hidden="true" size={17} />} title="退火状态回流" />
          <div className="ht-ratio-status-compact">
            <div className={cn("ht-current-mix-card", decisionVisible ? activeStep.accepted ? "accepted" : "rejected" : "pending")}>
              <span>{decisionVisible ? activeStep.actionLabel : <>等待配方<br />实测</>}</span>
              <strong>{decisionVisible ? activeStep.accepted ? currentMix.id : `${proposedMix.id} rejected` : proposedMix.id}</strong>
              <RatioStackedBar mix={decisionVisible ? activeStep.accepted ? currentMix : proposedMix : proposedMix} components={formulation.components} showLabels orientation="vertical" />
            </div>
            <div className={cn("ht-ratio-measurement-box", validationConfirmed && "confirmed")}>
              <div className="ht-ratio-measurement-head">
                <span>配方实测回流</span>
                <strong>{proposedMix.id}</strong>
                <b>{validationStatus}</b>
              </div>
              {measurementRequired ? (
                <>
                  <div className="ht-ratio-measurement-grid">
                    {scenario.targets.map((target) => {
                      const configuredTarget = getConfiguredTarget(target.key, confirmedSetup);
                      return (
                        <label key={target.key} style={{ "--target-color": target.color } as CSSProperties}>
                          <span>{target.shortLabel}</span>
                          <div>
                            <input
                              type="number"
                              step={target.key === "modulus" ? "0.1" : "1"}
                              value={ratioValidationValues[proposedMix.id]?.[target.key] ?? ""}
                              onChange={(event) => onValidationValueChange(target.key, event.target.value)}
                              aria-label={`${proposedMix.id} ${target.shortLabel} 实测值`}
                            />
                            <em>{configuredTarget.unit}</em>
                          </div>
                        </label>
                      );
                    })}
                  </div>
                  <button
                    type="button"
                    onClick={onConfirmValidation}
                    disabled={validationMissingCount > 0 || validationConfirmed}
                  >
                    {validationConfirmed ? "已确认" : "确认本步实测值"}
                  </button>
                </>
              ) : (
                <p>初始化 mix-0 作为已知起点，下一步开始对邻域配方做实测回流。</p>
              )}
            </div>
          </div>
          <div className="ht-ratio-status-bottom">
            <div className="ht-mix-score-grid">
              <span>已评估 <b>{displayedEvaluatedCount}/{formulation.ratioGrid.candidateCount}</b></span>
              <span>当前解 <b>{displayCurrentMix.id}</b></span>
              <span>当前最优 <b>{displayBestMix.id}</b></span>
            </div>
            <div className="ht-achievement-preview" aria-label={`${displayAchievementMix.id} property achievement`}>
              {scenario.targets.map((target) => {
                const value = displayAchievementMix.achievement[target.key];
                return (
                  <div key={target.key} style={{ "--target-color": target.color } as CSSProperties}>
                    <span>{target.shortLabel}</span>
                    <b>{value}%</b>
                    <i><em style={{ width: `${value}%` }} /></i>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function FinalFormulationPanel({
  weights,
  confirmedSetup,
}: {
  weights: WeightState;
  confirmedSetup: ConfirmedSetup;
}) {
  const scenario = highThroughputDemoScenario;
  const formulation = scenario.formulation;
  const finalExplanation = formulation.finalExplanation;
  const selectedMix = getSelectedRatioMix();
  const targetOutcomes = finalExplanation.targetOutcomes.map((outcome) => {
    const target = getConfiguredTarget(outcome.targetKey, confirmedSetup);
    return {
      ...outcome,
      target,
      achievement: adjustedOutcomeAchievement(outcome, target),
      pass: targetOutcomePasses(outcome.predictedValue, target),
    };
  });
  const displayedAchievement = Object.fromEntries(
    targetOutcomes.map((outcome) => [outcome.targetKey, outcome.achievement]),
  ) as Record<HighThroughputTargetKey, number>;
  const passedOutcomeCount = targetOutcomes.filter((outcome) => outcome.pass).length;

  return (
    <section className="ht-final-formulation-panel ht-formulation-panel active">
      <div className="ht-panel-header">
        <div>
          <span className="ht-kicker">S6 最终解释</span>
          <h2>最终推荐配方解释</h2>
        </div>
        <span className="ht-simulation-badge compact">来自 S5 锁定结果 {selectedMix.id}</span>
      </div>

      <div className="ht-formulation-grid">
        <div className="ht-selected-mix-panel ht-final-ratio-panel">
          <SectionTitle icon={<Layers3 aria-hidden="true" size={17} />} title="最终配方比例" />
          <article className="ht-selected-mix-card">
            <span>S6 最终配方</span>
            <strong>{selectedMix.id}</strong>
            <RatioStackedBar mix={selectedMix} components={formulation.components} showLabels />
          </article>
          <div className="ht-ratio-bars">
            {formulation.components.map((component) => (
              <div key={component.id}>
                <span>{component.id}</span>
                <b style={{ width: `${(selectedMix.ratios[component.id] ?? 0) * 100}%`, background: component.color }} />
                <strong>{ratioPercent(selectedMix.ratios[component.id] ?? 0)}</strong>
              </div>
            ))}
          </div>

          <SectionTitle icon={<Target aria-hidden="true" size={17} />} title="来源追踪" />
          <div className="ht-source-trace-list">
            {finalExplanation.sourceTrace.map((trace) => {
              const component = formulation.components.find((item) => item.id === trace.componentId);
              const target = getTarget(trace.targetKey);
              return (
                <article key={trace.componentId} className="ht-source-trace-row" style={{ "--target-color": component?.color ?? target.color } as CSSProperties}>
                  <span>{trace.componentId}</span>
                  <div>
                    <strong>{trace.componentId} ← {trace.agentLabel} ← {trace.candidateId}</strong>
                    <em>{target.shortLabel} / {trace.sourceStage} / {ratioPercent(trace.ratio)}</em>
                  </div>
                  <b>{component?.description ?? target.shortLabel}</b>
                </article>
              );
            })}
          </div>
        </div>

        <div className="ht-result-panel ht-final-outcome-panel">
          <SectionTitle icon={<TestTube2 aria-hidden="true" size={17} />} title="目标达成" />
          <div className="ht-score-box">
            <span>目标状态</span>
            <strong>{`${passedOutcomeCount}/${targetOutcomes.length} 达标`}</strong>
          </div>
          <div className="ht-target-outcome-grid">
            {targetOutcomes.map((outcome) => {
              const target = outcome.target;
              return (
                <article key={outcome.targetKey} style={{ "--target-color": target.color } as CSSProperties}>
                  <div>
                    <span>{target.shortLabel}</span>
                    <b>{outcome.pass ? "达标" : "待优化"}</b>
	                  </div>
	                  <strong>{formatTargetValue(target, outcome.predictedValue)} {target.unit}</strong>
	                  <em>目标 {targetThresholdLabel(target)} / {targetOutcomeMarginLabel(outcome.predictedValue, target)}</em>
	                </article>
	              );
	            })}
          </div>
          <div className="ht-radar-card">
            <span>综合评分雷达</span>
            <RadarChart achievement={displayedAchievement} />
          </div>
        </div>

        <div className="ht-final-summary-panel">
          <SectionTitle icon={<CheckCircle2 aria-hidden="true" size={17} />} title="推荐结论" />
          <article className="ht-final-summary-card">
            <strong>{finalExplanation.summary}</strong>
            <span>{finalExplanation.nextStep}</span>
            <p>推荐配方作为下一轮真实实验验证候选。</p>
          </article>

          <SectionTitle icon={<SlidersHorizontal aria-hidden="true" size={17} />} title="解释权重 / 目标约束" />
          <div className="ht-constraint-list">
            {scenario.targets.map((baseTarget) => {
              const target = getConfiguredTarget(baseTarget.key, confirmedSetup);
              return (
                <article key={target.key} style={{ "--target-color": target.color } as CSSProperties}>
                  <span>{target.shortLabel}</span>
                  <b>{weights[target.key]}</b>
                  <em>{targetThresholdLabel(target)}</em>
                </article>
              );
            })}
          </div>
        </div>
      </div>
    </section>
  );
}

function ratioPercent(value: number) {
  return `${Math.round(value * 100)}%`;
}

function RatioStackedBar({
  mix,
  components,
  showLabels = false,
  orientation = "horizontal",
}: {
  mix: (typeof highThroughputDemoScenario.formulation.mixCandidates)[number];
  components: typeof highThroughputDemoScenario.formulation.components;
  showLabels?: boolean;
  orientation?: "horizontal" | "vertical";
}) {
  return (
    <div className={cn("ht-stacked-ratio-bar", orientation === "vertical" && "vertical")} aria-label={`${mix.id} 组分比例`}>
      {components.map((component) => {
        const ratio = mix.ratios[component.id] ?? 0;
        return (
          <span
            key={component.id}
            style={{
              "--component-color": component.color,
              width: orientation === "vertical" ? undefined : ratioPercent(ratio),
              height: orientation === "vertical" ? ratioPercent(ratio) : undefined,
            } as CSSProperties}
            title={`${component.id} ${ratioPercent(ratio)}`}
          >
            {showLabels && ratio > 0 ? <b>{component.id} {ratioPercent(ratio)}</b> : null}
          </span>
        );
      })}
    </div>
  );
}

function SectionTitle({ icon, title }: { icon: ReactNode; title: string }) {
  return (
    <div className="ht-section-title">
      {icon}
      <span>{title}</span>
    </div>
  );
}

function RadarChart({
  achievement = highThroughputDemoScenario.formulation.achievement,
}: {
  achievement?: Record<HighThroughputTargetKey, number>;
}) {
  const scenario = highThroughputDemoScenario;
  const center = { x: 50, y: 50 };
  const radius = 34;
  const axes = scenario.targets.map((target, index) => {
    const angle = -Math.PI / 2 + (index / scenario.targets.length) * Math.PI * 2;
    const value = achievement[target.key] / 100;
    return {
      target,
      outer: {
        x: center.x + Math.cos(angle) * radius,
        y: center.y + Math.sin(angle) * radius,
      },
      value: {
        x: center.x + Math.cos(angle) * radius * value,
        y: center.y + Math.sin(angle) * radius * value,
      },
    };
  });
  const polygonPoints = axes.map((axis) => `${axis.value.x},${axis.value.y}`).join(" ");

  return (
    <svg className="ht-radar" viewBox="0 0 100 100" role="img" aria-label="综合评分雷达图">
      <title>综合评分雷达图</title>
      {[0.35, 0.7, 1].map((scale) => (
        <polygon
          key={scale}
          points={axes.map((axis) => `${center.x + (axis.outer.x - center.x) * scale},${center.y + (axis.outer.y - center.y) * scale}`).join(" ")}
          fill="none"
          stroke="#dbe4ee"
          strokeWidth="0.8"
        />
      ))}
      {axes.map((axis) => (
        <g key={axis.target.key}>
          <line x1={center.x} y1={center.y} x2={axis.outer.x} y2={axis.outer.y} stroke="#cbd5e1" strokeWidth="0.8" />
          <text x={axis.outer.x} y={axis.outer.y} textAnchor="middle" dominantBaseline="middle">
            {axis.target.shortLabel}
          </text>
        </g>
      ))}
      <polygon points={polygonPoints} fill="#0f766e" opacity="0.2" stroke="#0f766e" strokeWidth="1.5" />
    </svg>
  );
}
