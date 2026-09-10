import {
  BadgeInfo,
  Bot,
  BrainCircuit,
  Check,
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
import { type ChangeEvent, type CSSProperties, type KeyboardEvent, type MouseEvent, type ReactNode, type RefObject, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
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
import { WorkbenchSelect } from "./structure-workbench/WorkbenchSelect";
import { PriorImportWorkspace } from "./high-throughput/PriorImportWorkspace";
import { PriorHotspotWorkspace } from "./high-throughput/PriorHotspotWorkspace";
import { SinglePropertyIterationWorkspace } from "./high-throughput/SinglePropertyIterationWorkspace";
import { CandidateOutputWorkspace } from "./high-throughput/CandidateOutputWorkspace";
import { RatioSearchWorkspace } from "./high-throughput/RatioSearchWorkspace";
import { FinalFormulationWorkspace } from "./high-throughput/FinalFormulationWorkspace";
import { buildInitialRatioValidationValues, buildInitialRatioValidationConfirmations, ratioValidationMissingCount,
  type RatioValidationValues, type RatioValidationConfirmationState } from "./high-throughput/ratio-search-model";
import { formatIterationValue, type IterationTargetSummary } from "./high-throughput/iteration-model";
import { fallbackCandidateSmiles, validationNumber } from "./high-throughput/prior-hotspot-model";
import type { PriorDataUploadState, PriorDataUploadsState, RecommendationSelection, RecommendationValidationValues } from "./high-throughput/types";
import "../styles/structure-workbench.css";
import "./HighThroughputWorkflowDemoPage.css";

type HighThroughputWorkflowDemoPageProps = {
  onBackHome: () => void;
};


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

type RecommendationValidationRequirement = {
  targetKey: HighThroughputTargetKey;
  candidateId: string;
};
type ValidationConfirmationState = Record<string, boolean>;
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
    highThroughputDemoScenario.targets.map((target) => {
      const value = targetValues[target.key] ?? target.target;
      // Number inputs need ungrouped, lossless values. Display rounding here
      // would silently change confirmed thresholds when returning to S0.
      return [target.key, target.key === "modulus" && Number.isInteger(value) ? value.toFixed(1) : String(value)];
    }),
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


function hasValidationValue(
  validationValues: RecommendationValidationValues,
  targetKey: HighThroughputTargetKey,
  candidateId: string,
) {
  return validationNumber(validationValues, targetKey, candidateId) !== null;
}

function isTargetCsvReady(upload: PriorDataUploadState | null | undefined) {
  return Boolean(upload && !upload.isLoading && !upload.errorMessage);
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
        ? `请填写 ${proposedMixId} 的 ${missingCount} 项演示验证值`
        : confirmed
          ? activeRatioSearchStepIndex < lastStepIndex ? "本步已确认，可继续预设搜索" : "S5 预设最终配方已确认"
          : `请确认 ${proposedMixId} 演示验证值`,
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
  const [confirmedSetup, setConfirmedSetup] = useState<ConfirmedSetup>(() => buildDefaultConfirmedSetup());
  const [setupResetToken, setSetupResetToken] = useState(0);
  const [activeSpaceTargetKey, setActiveSpaceTargetKey] = useState<HighThroughputTargetKey>("tg");
  const [activeIterationRoundIndex, setActiveIterationRoundIndex] = useState(0);
  const [viewIterationRoundIndex, setViewIterationRoundIndex] = useState(0);
  const [activeRatioSearchStepIndex, setActiveRatioSearchStepIndex] = useState(0);
  const [viewRatioSearchStepIndex, setViewRatioSearchStepIndex] = useState(0);
  const [priorDataUploads, setPriorDataUploads] = useState<PriorDataUploadsState>({});
  const [selectedRecommendation, setSelectedRecommendation] = useState<RecommendationSelection | null>(null);
  const [recommendationValidationValues, setRecommendationValidationValues] = useState<RecommendationValidationValues>(() => buildInitialRecommendationValidationValues());
  const [validationConfirmations, setValidationConfirmations] = useState<ValidationConfirmationState>({});
  const [ratioValidationValues, setRatioValidationValues] = useState<RatioValidationValues>(() => buildInitialRatioValidationValues());
  const [ratioValidationConfirmations, setRatioValidationConfirmations] = useState<RatioValidationConfirmationState>(() => buildInitialRatioValidationConfirmations());
  const [stageTransition, setStageTransition] = useState<StageTransitionState | null>(null);
  const priorUploadTimersRef = useRef<Partial<Record<HighThroughputTargetKey, number>>>({});
  const stageTransitionTimerRef = useRef<number | null>(null);
  const previousStageRef = useRef(currentStageIndex);
  const scrollRegionRef = useRef<HTMLElement | null>(null);
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
  const isWorkbenchStage = currentStageIndex <= 6;
  const isIterationReview = currentStageIndex === 3 && viewIterationRoundIndex < activeIterationRoundIndex;
  const isRatioReview = currentStageIndex === 5 && viewRatioSearchStepIndex < activeRatioSearchStepIndex;

  useEffect(() => {
    if (previousStageRef.current !== currentStageIndex) {
      if (scrollRegionRef.current) scrollRegionRef.current.scrollTop = 0;
      if (isWorkbenchStage) document.getElementById("ht-workbench-surface-title")?.focus({ preventScroll: true });
    }
    previousStageRef.current = currentStageIndex;
  }, [currentStageIndex, isWorkbenchStage]);

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

    const recommendedIds = recommendationValidationIds(activeSpaceTargetKey, currentStageIndex, viewIterationRoundIndex);
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
  }, [viewIterationRoundIndex, activeSpaceTargetKey, currentStageIndex]);

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
      setViewIterationRoundIndex(activeIterationRoundIndex);
    } else if (nextIndex === 4) {
      setActiveIterationRoundIndex(2);
    } else if (nextIndex === 5) {
      setViewRatioSearchStepIndex(activeRatioSearchStepIndex);
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
    if (!nextStepState.canAdvance || stageTransition || isIterationReview || isRatioReview) {
      return;
    }

    if (currentStageIndex === 1) {
      runStageTransition("正在导入 DOE 先验并生成初始热点图...", STAGE_TRANSITION_MS, () => enterStage(2));
      return;
    }

    if (currentStageIndex === 2) {
      runStageTransition("正在演示验证回流，准备进入 S3 单性质迭代…", VALIDATION_FEEDBACK_MS, () => enterStage(3));
      return;
    }

    if (currentStageIndex === 3) {
      if (activeIterationRoundIndex < 2) {
        const message = activeIterationRoundIndex === 0
          ? "正在演示验证回流，切换到 R2 预设热点…"
          : "正在演示验证回流，汇总收敛记录与预设输出…";
        runStageTransition(message, VALIDATION_FEEDBACK_MS, () => {
          setActiveIterationRoundIndex(activeIterationRoundIndex + 1);
          setViewIterationRoundIndex(activeIterationRoundIndex + 1);
        });
        return;
      }
      runStageTransition("汇总四个收敛候选，正在生成 p1-p4 输出...", STAGE_TRANSITION_MS, () => enterStage(4));
      return;
    }

    if (currentStageIndex === 4) {
      runStageTransition("沿用预设 p1–p4 组分池，正在进入配比搜索…", STAGE_TRANSITION_MS, () => enterStage(5));
      return;
    }

    if (currentStageIndex === 5) {
      const lastStepIndex = scenario.formulation.searchSteps.length - 1;
      if (activeRatioSearchStepIndex < lastStepIndex) {
        runStageTransition("正在演示验证回流，切换到下一步预设比例…", RATIO_SEARCH_FEEDBACK_MS, () => {
          setActiveRatioSearchStepIndex(activeRatioSearchStepIndex + 1);
          setViewRatioSearchStepIndex(activeRatioSearchStepIndex + 1);
        });
        return;
      }
      runStageTransition("正在演示验证回流，准备展示预设最终配方…", STAGE_TRANSITION_MS, () => enterStage(6));
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
    if (!activeValidationGroupKey || activeValidationMissingCount > 0 || stageTransition || isIterationReview) {
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
    if (!activeRatioMeasurementRequired || !activeRatioMixId || stageTransition || isRatioReview || ratioValidationValues[activeRatioMixId]?.[targetKey] === value) {
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
    if (!activeRatioMeasurementRequired || !activeRatioMixId || activeRatioValidationMissingCount > 0 || stageTransition || isRatioReview) {
      return;
    }
    setRatioValidationConfirmations((confirmations) => ({
      ...confirmations,
      [activeRatioMixId]: true,
    }));
  }

  function resetCurrentStageActions() {
    if (stageTransition || isIterationReview || isRatioReview) return;
    if (currentStageIndex === 0) {
      setConfirmedSetup(buildDefaultConfirmedSetup());
      setSetupResetToken((token) => token + 1);
      setActiveSpaceTargetKey("tg");
      setValidationConfirmations({});
      invalidateIterationProgress();
      resetRatioValidationState();
      return;
    }

    if (currentStageIndex === 1) {
      clearAllPriorUploadTimers();
      setPriorDataUploads({});
      setSelectedRecommendation(null);
      setValidationConfirmations({});
      invalidateIterationProgress();
      return;
    }

    if (currentStageIndex === 2) {
      resetValidationValues(getRequiredValidationIds(2));
      clearValidationConfirmation(2);
      invalidateIterationProgress();
      setSelectedRecommendation({ targetKey: activeSpaceTargetKey, candidateId: recommendationValidationIds(activeSpaceTargetKey, 2)[0] });
      return;
    }

    if (currentStageIndex === 3) {
      if (activeIterationRoundIndex === 2) {
        if (!window.confirm("重新演示 S3？将恢复两轮共 12 项默认验证值并清除 S3 进度，保留 S0–S2 数据。")) return;
        resetValidationValues([...getRequiredValidationIds(3, 0), ...getRequiredValidationIds(3, 1)]);
        invalidateIterationProgress();
      } else {
        resetValidationValues(getRequiredValidationIds(3, activeIterationRoundIndex));
        clearValidationConfirmation(3, activeIterationRoundIndex);
        invalidateRatioProgress();
      }
      setSelectedRecommendation({ targetKey: activeSpaceTargetKey,
        candidateId: recommendationValidationIds(activeSpaceTargetKey, 3, activeIterationRoundIndex === 2 ? 0 : activeIterationRoundIndex)[0] });
      return;
    }

    if (currentStageIndex === 5) {
      if (!window.confirm("重新演示 S5？将恢复各步默认验证值并清除 S5 确认与进度，保留 S0–S4 数据。")) return;
      invalidateRatioProgress();
      resetRatioValidationState();
    }
  }

  function invalidateRatioProgress() {
    setActiveRatioSearchStepIndex(0);
    setViewRatioSearchStepIndex(0);
    setRatioValidationConfirmations(buildInitialRatioValidationConfirmations());
  }

  function restartDemo() {
    if (stageTransition || !window.confirm("重新开始整个演示？将清除 S0–S6 的场景设置、上传数据、验证记录、确认与进度，恢复默认场景并返回 S0。")) return;
    clearAllPriorUploadTimers();
    setConfirmedSetup(buildDefaultConfirmedSetup());
    setSetupResetToken((token) => token + 1);
    setPriorDataUploads({});
    setSelectedRecommendation(null);
    setActiveSpaceTargetKey("tg");
    setRecommendationValidationValues(buildInitialRecommendationValidationValues());
    setValidationConfirmations({});
    setActiveIterationRoundIndex(0);
    setViewIterationRoundIndex(0);
    setActiveRatioSearchStepIndex(0);
    setViewRatioSearchStepIndex(0);
    resetRatioValidationState();
    enterStage(0);
  }

  function invalidateIterationProgress() {
    setActiveIterationRoundIndex(0);
    setViewIterationRoundIndex(0);
    setValidationConfirmations((confirmations) => ({ ...confirmations, "s3-round-1": false, "s3-round-2": false }));
    invalidateRatioProgress();
  }

  function confirmSetup(setup: ConfirmedSetup) {
    // Going back alone preserves confirmation; a genuinely changed scene requires a new review.
    const targetsChanged = setup.selectedTargetKeys.length !== confirmedSetup.selectedTargetKeys.length ||
      setup.selectedTargetKeys.some((key) => !confirmedSetup.selectedTargetKeys.includes(key));
    if (targetsChanged || setup.materialType !== confirmedSetup.materialType ||
      setup.monomerSystem !== confirmedSetup.monomerSystem || setup.representation !== confirmedSetup.representation ||
      setup.monomerACount !== confirmedSetup.monomerACount || setup.monomerBCount !== confirmedSetup.monomerBCount ||
      scenario.targets.some((target) => setup.targetValues[target.key] !== confirmedSetup.targetValues[target.key])) {
      clearValidationConfirmation(2);
      invalidateIterationProgress();
    }
    setConfirmedSetup(setup);
    enterStage(1);
  }

  function handlePriorDataUpload(targetKey: HighThroughputTargetKey, file: File | null) {
    if (file) beginPriorDataImport(targetKey, file.name);
  }

  function handlePriorSampleUpload(targetKey: HighThroughputTargetKey) {
    if (currentStageIndex !== 1 || stageTransition) return;
    beginPriorDataImport(targetKey, scenario.doeCsvFiles[targetKey].fileName);
  }

  // Both entry points select built-in demo rows by filename, sharing the same
  // loading timer and stale-result guard. No file contents or service are used.
  function beginPriorDataImport(targetKey: HighThroughputTargetKey, fileName: string) {
    if (currentStageIndex !== 1 || stageTransition) return;
    clearValidationConfirmation(2);
    invalidateIterationProgress();
    clearPriorUploadTimer(targetKey);
    const csvFile = scenario.doeCsvFiles[targetKey];
    const uploadedAt = new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date());

    if (fileName !== csvFile.fileName) {
      setPriorDataUploads((uploads) => ({
        ...uploads,
        [targetKey]: {
          targetKey,
          fileName,
          fileType: fileName.split(".").pop()?.toUpperCase() || "UNKNOWN",
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
        fileName,
        fileType: fileName.split(".").pop()?.toUpperCase() || "CSV",
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
    if (stageTransition || isIterationReview) return;
    const key = recommendationValidationKey(targetKey, candidateId);
    if (recommendationValidationValues[key] === value || !activeValidationRequirements.some((item) => item.targetKey === targetKey && item.candidateId === candidateId)) return;
    setRecommendationValidationValues((values) => ({
      ...values,
      [recommendationValidationKey(targetKey, candidateId)]: value,
    }));
    clearValidationConfirmation(currentStageIndex, activeIterationRoundIndex);
    if (currentStageIndex === 2) invalidateIterationProgress();
    if (currentStageIndex === 3) invalidateRatioProgress();
  }

  return (
    <div className={cn("high-throughput-demo", isWorkbenchStage && "ht-workbench-page", currentStageIndex === 0 && "ht-s0-page", currentStageIndex === 1 && "ht-s1-page", currentStageIndex === 2 && "ht-s2-page", currentStageIndex === 3 && "ht-s3-page", currentStageIndex === 4 && "ht-s4-page", currentStageIndex === 5 && "ht-s5-page", currentStageIndex === 6 && "ht-s6-page")}>
      {isWorkbenchStage ? <h1 className="ht-workbench-title">高通量优化演示</h1> : null}
      {isWorkbenchStage ? (
        <ScenarioModuleToolbar
          canReset={!stageTransition}
          onReset={resetCurrentStageActions}
          showReset={!isIterationReview && !isRatioReview && currentStageIndex !== 4 && currentStageIndex !== 6}
          resetText={currentStageIndex === 5 ? "重演 S5" : currentStageIndex === 3 ? activeIterationRoundIndex === 2 ? "重演 S3" : "重置本批" : "重置"}
          resetLabel={currentStageIndex === 5 ? "重演 S5" : currentStageIndex === 0 ? "恢复默认场景参数" : currentStageIndex === 1 ? "重置 S1 上传数据" : currentStageIndex === 2 ? "重置 S2 验证值" : activeIterationRoundIndex === 2 ? "重演 S3" : "重置本批验证值"}
        />
      ) : null}
      <main ref={scrollRegionRef} className="ht-shell ht-scroll-region">
        <section
          className={cn("ht-docx-board", isWorkbenchStage && "ht-workbench-board np-sw-accented-surface", currentStageIndex === 0 && "ht-s0-board", (currentStageIndex >= 1 && currentStageIndex <= 6) && "ht-s1-board", currentStageIndex === 2 && "ht-s2-board", currentStageIndex === 3 && "ht-s3-board", currentStageIndex === 4 && "ht-s4-board", currentStageIndex === 5 && "ht-s5-board", currentStageIndex === 6 && "ht-s6-board")}
          aria-labelledby={isWorkbenchStage ? "ht-workbench-surface-title" : undefined}
        >
          {isWorkbenchStage ? <ScenarioSurfaceHeader stageIndex={currentStageIndex} /> : null}

          <FlowControlBar
            currentStageIndex={currentStageIndex}
            nextStepState={displayedNextStepState}
            onNextStep={handleNextStep}
            onResetStage={resetCurrentStageActions}
            canResetStage={!stageTransition && [0, 1, 2, 3, 5].includes(currentStageIndex)}
            navigationOnly={isWorkbenchStage}
          />

          {currentStageIndex === 0 ? (
            <ScenarioHeader
              confirmedSetup={confirmedSetup}
              onConfirmSetup={confirmSetup}
              resetToken={setupResetToken}
            />
          ) : currentStageIndex === 1 ? (
            <PriorImportWorkspace
              targets={scenario.targets.map((target) => getConfiguredTarget(target.key, confirmedSetup))}
              candidateTotal={confirmedSetup.candidateTotal}
              materialType={confirmedSetup.materialType}
              representation={confirmedSetup.representation}
              activeTargetKey={activeSpaceTargetKey}
              onSelectTarget={setActiveSpaceTargetKey}
              uploads={priorDataUploads}
              onUpload={handlePriorDataUpload}
              onUploadSample={handlePriorSampleUpload}
              onBack={() => enterStage(0)}
              onNext={handleNextStep}
              canAdvance={displayedNextStepState.canAdvance}
              transitionMessage={stageTransition?.message ?? null}
              candidateMap={(
                <PropertySpaceCard
                  stageIndex={1}
                  target={getConfiguredTarget(activeSpaceTargetKey, confirmedSetup)}
                  variant="large"
                  priorDataUpload={priorDataUploads[activeSpaceTargetKey] ?? null}
                  showSummary={false}
                />
              )}
            />
          ) : currentStageIndex === 2 ? (
            <PriorHotspotWorkspace
              targets={scenario.targets.map((target) => getConfiguredTarget(target.key, confirmedSetup))}
              candidateTotal={confirmedSetup.candidateTotal}
              materialType={confirmedSetup.materialType}
              representation={confirmedSetup.representation}
              uploads={priorDataUploads}
              activeTargetKey={activeSpaceTargetKey}
              onSelectTarget={setActiveSpaceTargetKey}
              selectedRecommendation={selectedRecommendation}
              onSelectRecommendation={setSelectedRecommendation}
              validationValues={recommendationValidationValues}
              onValidationValueChange={handleRecommendationValidationValueChange}
              validationConfirmed={activeValidationConfirmed}
              onConfirm={confirmCurrentValidationGroup}
              onBack={() => enterStage(1)}
              onNext={handleNextStep}
              canAdvance={displayedNextStepState.canAdvance}
              transitionMessage={stageTransition?.message ?? null}
              renderCandidateMap={(onSelect) => (
                <PropertySpaceCard stageIndex={2} target={getConfiguredTarget(activeSpaceTargetKey, confirmedSetup)}
                  variant="large" showSummary={false} validationValues={recommendationValidationValues}
                  selectedRecommendation={selectedRecommendation} onSelectRecommendation={onSelect}
                  interactionDisabled={Boolean(stageTransition)} />
              )}
            />
          ) : currentStageIndex === 3 ? (
            <SinglePropertyIterationWorkspace
              targets={scenario.targets.map((target) => getConfiguredTarget(target.key, confirmedSetup))}
              candidateTotal={confirmedSetup.candidateTotal} materialType={confirmedSetup.materialType} representation={confirmedSetup.representation}
              activeTargetKey={activeSpaceTargetKey} onSelectTarget={setActiveSpaceTargetKey}
              progressRound={activeIterationRoundIndex} viewRound={viewIterationRoundIndex}
              onViewRound={(round) => { if (!stageTransition && round >= 0 && round <= activeIterationRoundIndex) setViewIterationRoundIndex(round); }}
              selectedRecommendation={selectedRecommendation} onSelectRecommendation={setSelectedRecommendation}
              validationValues={recommendationValidationValues} onValidationValueChange={handleRecommendationValidationValueChange}
              validationConfirmed={Boolean(validationConfirmations[validationConfirmationKey(3, viewIterationRoundIndex) ?? ""])}
              onConfirm={confirmCurrentValidationGroup} onBack={() => { if (!stageTransition) enterStage(2); }} onNext={handleNextStep}
              canAdvance={displayedNextStepState.canAdvance} transitionMessage={stageTransition?.message ?? null}
              renderCandidateMap={(summary, onSelect) => (
                <PropertySpaceCard stageIndex={3} target={summary.target} variant="large" showSummary={false}
                  iterationRoundIndex={viewIterationRoundIndex} iterationSummary={summary} reviewMode={isIterationReview}
                  validationValues={recommendationValidationValues} selectedRecommendation={selectedRecommendation}
                  onSelectRecommendation={onSelect} interactionDisabled={Boolean(stageTransition)} />
              )}
            />
          ) : currentStageIndex === 4 ? (
            <CandidateOutputWorkspace
              targets={scenario.targets.map((target) => getConfiguredTarget(target.key, confirmedSetup))}
              candidateTotal={confirmedSetup.candidateTotal} materialType={confirmedSetup.materialType} representation={confirmedSetup.representation}
              activeTargetKey={activeSpaceTargetKey} onSelectTarget={setActiveSpaceTargetKey} validationValues={recommendationValidationValues}
              onBack={() => { if (!stageTransition) enterStage(3); }} onNext={handleNextStep}
              canAdvance={displayedNextStepState.canAdvance} transitionMessage={stageTransition?.message ?? null}
              renderCandidateMap={(summary, selectedId, onSelect) => (
                <PropertySpaceCard stageIndex={4} target={summary.target} variant="large" showSummary={false}
                  interactionDisabled={Boolean(stageTransition)} outputPreview={{ outputId: summary.output.id,
                    candidateIds: summary.candidates.map((candidate) => candidate.id), selectedId, onSelect }} />
              )}
            />
          ) : currentStageIndex === 5 ? (
            <RatioSearchWorkspace
              targets={scenario.targets.map((target) => getConfiguredTarget(target.key, confirmedSetup))}
              progressStep={activeRatioSearchStepIndex} viewStep={viewRatioSearchStepIndex}
              onViewStep={(step) => { if (!stageTransition && step >= 0 && step <= activeRatioSearchStepIndex) setViewRatioSearchStepIndex(step); }}
              validationValues={ratioValidationValues} confirmations={ratioValidationConfirmations}
              onValidationValueChange={handleRatioValidationValueChange} onConfirm={confirmCurrentRatioValidation}
              onBack={() => { if (!stageTransition && !isRatioReview) enterStage(4); }} onNext={handleNextStep}
              canAdvance={displayedNextStepState.canAdvance} transitionMessage={stageTransition?.message ?? null}
            />
          ) : (
            <FinalFormulationWorkspace
              targets={scenario.targets.map((target) => getConfiguredTarget(target.key, confirmedSetup))}
              onBack={() => { if (!stageTransition) enterStage(5); }}
              onRestart={restartDemo}
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
  resetToken,
}: {
  confirmedSetup: ConfirmedSetup;
  onConfirmSetup: (setup: ConfirmedSetup) => void;
  resetToken: number;
}) {
  const scenario = highThroughputDemoScenario;
  const [materialType, setMaterialType] = useState(confirmedSetup.materialType);
  const [monomerSystem, setMonomerSystem] = useState(confirmedSetup.monomerSystem);
  const [representation, setRepresentation] = useState(confirmedSetup.representation);
  const [candidateA, setCandidateA] = useState(String(confirmedSetup.monomerACount));
  const [candidateB, setCandidateB] = useState(String(confirmedSetup.monomerBCount));
  const [selectedTargetKeys, setSelectedTargetKeys] = useState<HighThroughputTargetKey[]>(confirmedSetup.selectedTargetKeys);
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
      <div className="ht-workbench-demo-note">
        <BadgeInfo aria-hidden="true" size={16} />
        <span><strong>交互说明：</strong>保留任务设置输入用于演示配置过程；候选数据、推荐批次与后续搜索路径仍采用预设场景。</span>
      </div>

      <div className="ht-setup-grid">
        <div className="ht-setup-section">
          <SetupSectionHeading
            index="01"
            title="材料体系"
            description="定义聚合物类别与单体组合方式"
          />
          <SetupControl icon={<Layers3 aria-hidden="true" size={20} />} label="材料类型" htmlFor="ht-material-type">
            <WorkbenchSelect
              id="ht-material-type"
              ariaLabel="材料类型"
              value={materialType}
              options={[{ value: "Polyimide", label: "Polyimide", description: "聚酰亚胺（PI）薄膜" }]}
              onChange={setMaterialType}
            />
          </SetupControl>

          <SetupControl icon={<FlaskConical aria-hidden="true" size={20} />} label="单体体系" htmlFor="ht-monomer-system">
            <WorkbenchSelect
              id="ht-monomer-system"
              ariaLabel="单体体系"
              value={monomerSystem}
              options={[{ value: "Diamine + Dianhydride", label: "Diamine + Dianhydride", description: "二胺与二酐组合" }]}
              onChange={setMonomerSystem}
            />
          </SetupControl>
        </div>

        <div className="ht-setup-section middle">
          <SetupSectionHeading
            index="02"
            title="候选空间"
            description="设置组合规模与二维空间表征"
          />
          <SetupControl icon={<Layers3 aria-hidden="true" size={20} />} label="候选空间">
            <div className="ht-candidate-space-inputs">
              <input
                aria-label="单体 A 候选数量"
                inputMode="numeric"
                min="0"
                type="number"
                value={candidateA}
                onChange={(event) => setCandidateA(event.currentTarget.value)}
              />
              <span aria-hidden="true">×</span>
              <input
                aria-label="单体 B 候选数量"
                inputMode="numeric"
                min="0"
                type="number"
                value={candidateB}
                onChange={(event) => setCandidateB(event.currentTarget.value)}
              />
            </div>
          </SetupControl>

          <SetupControl icon={<BrainCircuit aria-hidden="true" size={20} />} label="空间表征" htmlFor="ht-representation">
            <WorkbenchSelect
              id="ht-representation"
              ariaLabel="空间表征"
              value={representation}
              options={[{ value: "PolyBERT", label: "PolyBERT", description: "聚合物表征的二维投影" }]}
              onChange={setRepresentation}
            />
          </SetupControl>
        </div>

        <div className="ht-setup-section targets">
          <SetupSectionHeading
            index="03"
            title="优化目标"
            description="选择目标性质并调整演示阈值"
          />
          <div className="ht-s0-target-controls">
            <SetupControl icon={<Target aria-hidden="true" size={20} />} label="目标性质">
              <div className="ht-property-buttons">
                {scenario.targets.map((target) => (
                  <button
                    key={target.key}
                    type="button"
                    aria-pressed={selectedTargetKeys.includes(target.key)}
                    title={target.label}
                    onClick={() => handleTargetClick(target.key)}
                    style={{ "--target-color": target.color } as CSSProperties}
                  >
                    <Check aria-hidden="true" className="ht-target-check" />
                    <span>{target.shortLabel}</span>
                  </button>
                ))}
              </div>
            </SetupControl>

            <SetupControl icon={<BadgeInfo aria-hidden="true" size={20} />} label="目标阈值">
              <div className="ht-target-value-inputs">
                {selectedTargets.length > 0 ? selectedTargets.map((target) => (
                  <label key={target.key} style={{ "--target-color": target.color } as CSSProperties}>
                    <span>{target.shortLabel}</span>
                    <span
                      id={`ht-target-direction-${target.key}`}
                      className="ht-target-direction"
                    >
                      <span aria-hidden="true">{target.direction === "higher" ? "≥" : "≤"}</span>
                      <span className="sr-only">{target.direction === "higher" ? "不低于" : "不超过"}</span>
                    </span>
                    <input
                      aria-label={`${target.shortLabel} 目标值`}
                      aria-describedby={`ht-target-direction-${target.key} ht-target-unit-${target.key}`}
                      inputMode="decimal"
                      step={target.key === "modulus" ? "0.1" : "1"}
                      type="number"
                      value={targetValues[target.key]}
                      onChange={(event) => {
                        const value = event.currentTarget.value;
                        setTargetValues((current) => ({
                          ...current,
                          [target.key]: value,
                        }));
                      }}
                    />
                    <span id={`ht-target-unit-${target.key}`} className="ht-target-unit">
                      {target.unit === "degC" ? "°C" : target.unit}
                    </span>
                  </label>
                )) : <span className="ht-target-empty">请选择目标性质</span>}
              </div>
            </SetupControl>
          </div>
        </div>
      </div>

      <div className="ht-setup-actions">
        <div className="ht-setup-summary">
          <span>候选空间预览</span>
          <strong>
            {candidateA || "0"} × {candidateB || "0"} = {formatNumber(candidateTotalPreview)}
          </strong>
          <em>已选择 {selectedTargets.length} 个目标性质</em>
        </div>
        <button type="button" className="ht-confirm-setup-button" onClick={handleConfirmSetup}>
          确认场景设置，进入 S1
          <ChevronRight aria-hidden="true" size={17} />
        </button>
      </div>
    </section>
  );
}

function ScenarioModuleToolbar({
  canReset,
  onReset,
  resetLabel,
  resetText = "重置",
  showReset = true,
}: {
  canReset: boolean;
  onReset: () => void;
  resetLabel: string;
  resetText?: string;
  showReset?: boolean;
}) {
  return (
    <div className="ht-workbench-toolbar" aria-label="高通量优化演示状态">
      <div className="ht-workbench-actions">
        <span role="status">
          <i aria-hidden="true" />
          <strong>固定演示</strong>
        </span>
        {showReset ? <button
          type="button"
          onClick={onReset}
          aria-label={resetLabel}
          disabled={!canReset}
        >
          <RotateCcw aria-hidden="true" />
          {resetText}
        </button> : null}
      </div>
    </div>
  );
}

function ScenarioSurfaceHeader({ stageIndex }: { stageIndex: number }) {
  const isPrior = stageIndex === 1;
  const isHotspot = stageIndex === 2;
  const isIteration = stageIndex === 3;
  const isOutput = stageIndex === 4;
  const isRatioSearch = stageIndex === 5;
  const isFinal = stageIndex === 6;
  return (
    <header className="ht-workbench-header">
      <div className="ht-workbench-heading">
        <span className="ht-workbench-mark">
          {isFinal ? <CheckCircle2 aria-hidden="true" /> : isRatioSearch ? <SlidersHorizontal aria-hidden="true" /> : isOutput ? <FileCheck2 aria-hidden="true" /> : isHotspot || isIteration ? <BrainCircuit aria-hidden="true" /> : isPrior ? <TestTube2 aria-hidden="true" /> : <FlaskConical aria-hidden="true" />}
        </span>
        <div>
          <h2 id="ht-workbench-surface-title" tabIndex={-1}>{isFinal ? "最终配方与结果解释" : isRatioSearch ? "多目标配比搜索" : isOutput ? "单性质候选输出" : isIteration ? "单性质迭代与验证回流" : isHotspot ? "先验热点与推荐验证" : isPrior ? "正交实验与先验导入" : "材料体系与目标设置"}</h2>
          <p>{isFinal ? "查看锁定配方、四目标模拟结果与组分来源，为下一轮真实实验验证提供候选。" : isRatioSearch ? "固定 p1–p4 组分，逐步查看模拟退火决策与配方验证回流。" : isOutput ? "查看四个固定输出组分的性质、结构与备选，衔接多目标配比搜索。" : isIteration ? "逐轮验证推荐点，对照回流记录与预设路径，查看单性质收敛结果。" : isHotspot ? "查看四个性质的初始热点，编辑并确认本批推荐点的演示验证值。" : isPrior ? "为四个性质 Agent 导入 DOE 样例，查看候选分布与先验数据。" : "设置材料体系、候选空间与优化目标。"}</p>
        </div>
      </div>
    </header>
  );
}

function SetupControl({ icon, label, htmlFor, children }: { icon: ReactNode; label: string; htmlFor?: string; children: ReactNode }) {
  const labelId = useId();
  return (
    <div className="ht-setup-row" role="group" aria-labelledby={labelId}>
      <div className="ht-setup-label">
        <span className="ht-setup-icon">{icon}</span>
        {htmlFor ? <label id={labelId} htmlFor={htmlFor}>{label}</label> : <span id={labelId}>{label}</span>}
      </div>
      <div className="ht-setup-field">
        {children}
      </div>
    </div>
  );
}

function SetupSectionHeading({
  index,
  title,
  description,
}: {
  index: string;
  title: string;
  description: string;
}) {
  return (
    <header className="ht-s0-section-heading">
      <span>{index}</span>
      <div>
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
    </header>
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
  navigationOnly = false,
}: {
  currentStageIndex: number;
  nextStepState: NextStepState;
  onNextStep: () => void;
  onResetStage: () => void;
  canResetStage: boolean;
  navigationOnly?: boolean;
}) {
  const stages = highThroughputDemoScenario.stages;

  return (
    <section className="ht-flow-control-bar" aria-label="演示播放控制">
      {currentStageIndex !== 0 && !navigationOnly ? (
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
      ) : null}
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
  showSummary = true,
  interactionDisabled = false,
  iterationSummary,
  reviewMode = false,
  outputPreview,
}: {
  stageIndex: number;
  target: HighThroughputTarget;
  variant?: "compact" | "large";
  iterationRoundIndex?: number;
  priorDataUpload?: PriorDataUploadState | null;
  validationValues?: RecommendationValidationValues;
  selectedRecommendation?: RecommendationSelection | null;
  onSelectRecommendation?: (selection: RecommendationSelection) => void;
  showSummary?: boolean;
  interactionDisabled?: boolean;
  iterationSummary?: IterationTargetSummary;
  reviewMode?: boolean;
  outputPreview?: { outputId: string; candidateIds: string[]; selectedId: string; onSelect: (id: string) => void };
}) {
  const plotGridId = useId();
  const isWorkbenchPlot = !showSummary && variant === "large" && stageIndex >= 1 && stageIndex <= 4;
  const isOutputPlot = stageIndex === 4 && Boolean(outputPreview);
  const space = getPropertySpace(target.key);
  const activeRounds = highThroughputDemoScenario.roundsByTarget[target.key];
  const activeRoundIndex = clamp(iterationRoundIndex, 0, activeRounds.length - 1);
  const surface = iterationSummary ? iterationSummary.surface : propertySurfaceForStage(stageIndex, space, iterationRoundIndex);
  const roundIds = iterationSummary ? {
    testedIds: iterationSummary.returnedRecords.map((record) => record.candidate.id),
    currentTestedIds: iterationSummary.round.testedIds,
    recommendedIds: iterationSummary.recommendations.map((candidate) => candidate.id),
  } : propertyRoundIds(target.key, stageIndex, iterationRoundIndex);
  const isPriorLoading = Boolean(priorDataUpload?.isLoading);
  const isPriorError = Boolean(priorDataUpload?.errorMessage);
  const isPriorReady = Boolean(priorDataUpload && !priorDataUpload.isLoading && !priorDataUpload.errorMessage);
  const showPriorDoe = stageIndex >= 2 || (stageIndex === 1 && isPriorReady);
  const priorIds = showPriorDoe ? space.priorCandidateIds : [];
  const measuredPriorIds = stageIndex >= 2 ? space.priorCandidateIds : [];
  const currentBestId = isOutputPlot ? outputPreview!.outputId : iterationSummary ? iterationSummary.recordBest?.candidate.id ?? "" : propertySpaceCurrentBestId(stageIndex, space, target, iterationRoundIndex, validationValues);
  const currentBest = currentBestId ? getCandidate(currentBestId) : undefined;
  const specialIds = new Set([
    ...priorIds,
    ...measuredPriorIds,
    ...roundIds.testedIds,
    ...roundIds.recommendedIds,
    ...(outputPreview?.candidateIds ?? []),
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
    !interactionDisabled &&
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
  const canInspectOutput = isOutputPlot && !interactionDisabled;
  const selectOutput = (candidateId: string) => { if (canInspectOutput) outputPreview?.onSelect(candidateId); };
  const handleOutputKeyDown = (event: KeyboardEvent<SVGGElement>, candidateId: string) => {
    if (canInspectOutput && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); selectOutput(candidateId); }
  };

  return (
    <article className={cn("ht-property-space-card", variant)} style={{ "--target-color": target.color } as CSSProperties}>
      {showSummary ? (
        <div className="ht-property-space-head">
          <div>
            <span>{target.shortLabel} Space</span>
            <strong>{target.label}</strong>
          </div>
          <b className={cn(stageIndex === 3 && "ht-property-round-label")} data-tooltip={stageLabel} tabIndex={stageIndex === 3 ? 0 : undefined}>
            <span>{stageLabel}</span>
          </b>
        </div>
      ) : null}

      <svg viewBox={`0 0 ${MATERIAL_MAP_WIDTH} ${MATERIAL_MAP_HEIGHT}`} role={stageIndex === 2 || iterationSummary || isOutputPlot ? "group" : "img"}
        data-surface-snapshot={iterationSummary ? surface?.id : undefined}
        aria-label={isOutputPlot ? `${target.shortLabel} 固定输出与备选位置图` : iterationSummary ? `${target.shortLabel} ${iterationRoundIndex === 2 ? "收敛" : `R${iterationRoundIndex + 1}`} 回流记录与预设热点` : stageIndex === 2 ? `${target.shortLabel} 先验热点与推荐点` : `${target.shortLabel} single-property optimization space`}>
        <defs>
          <radialGradient id={`ht-property-gradient-${target.key}-${surface?.id ?? "none"}`} cx="50%" cy="50%" r="55%">
            <stop offset="0%" stopColor={target.color} stopOpacity="0.82" />
            <stop offset="46%" stopColor={target.color} stopOpacity="0.32" />
            <stop offset="100%" stopColor={target.color} stopOpacity="0" />
          </radialGradient>
          {isWorkbenchPlot ? (
            <pattern id={plotGridId} width="4" height="4" patternUnits="userSpaceOnUse">
              <path className="ht-property-grid-detail-line" d="M 4 0 H 0 V 4" fill="none" />
            </pattern>
          ) : null}
        </defs>
        <rect className="ht-property-space-backdrop" x="0" y="0" width={MATERIAL_MAP_WIDTH} height={MATERIAL_MAP_HEIGHT} rx="3" fill="#fbfdff" />
        {isWorkbenchPlot ? (
          <rect className="ht-property-grid-detail" width={MATERIAL_MAP_WIDTH} height={MATERIAL_MAP_HEIGHT}
            rx="3" fill={`url(#${plotGridId})`} aria-hidden="true" pointerEvents="none" />
        ) : null}
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
            className="ht-property-candidate-point"
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
          .filter((point) => !isOutputPlot && priorSet.has(point.candidateId))
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
                <title>{point.candidateId} · DOE 先验</title>
              </rect>
            );
          })}
        {space.candidatePoints
          .filter((point) => !isOutputPlot && measuredSet.has(point.candidateId) && !priorSet.has(point.candidateId))
          .map((point) => {
            const projectedPoint = projectSpacePoint(point);
            const returnedRecord = iterationSummary?.returnedRecords.find((record) => record.candidate.id === point.candidateId);
            return (
              <circle
                key={`tested-${point.candidateId}`}
                className={cn("ht-tested-sample", currentTestedSet.has(point.candidateId) && "current")}
                cx={projectedPoint.x}
                cy={projectedPoint.y}
                r={currentTestedSet.has(point.candidateId) ? "1.02" : "0.78"}
              >
                <title>{point.candidateId} · {returnedRecord ? `${returnedRecord.source} · 演示验证值 ${formatIterationValue(target, returnedRecord.value)} ${target.unit === "degC" ? "°C" : target.unit}` : `已回流 · ${candidateValueWithValidation(getCandidate(point.candidateId), target, validationValues)}`}</title>
              </circle>
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
                aria-label={canInspectRecommendation ? reviewMode ? `查看 ${point.candidateId} 推荐点及结构详情` : `查看 ${point.candidateId} 推荐点并编辑 ${target.shortLabel} 演示验证值` : undefined}
                aria-pressed={canInspectRecommendation ? isSelected : undefined}
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
                <title>{point.candidateId} · {iterationSummary ? `${iterationRoundIndex === 0 ? "R1" : "R2"} 预设推荐 · ${reviewMode ? "已确认，只读回看" : "本轮待回流"}` : "推荐验证点"}</title>
              </g>
            );
          })}
        {isOutputPlot ? space.candidatePoints.filter((point) => point.candidateId !== currentBestId && outputPreview!.candidateIds.includes(point.candidateId)).map((point) => {
          const projected = projectSpacePoint(point);
          const selected = outputPreview!.selectedId === point.candidateId;
          return <g key={`output-backup-${point.candidateId}`} className="ht-s4-map-point" transform={`translate(${projected.x} ${projected.y})`}
            data-candidate-id={point.candidateId} role="button" tabIndex={canInspectOutput ? 0 : -1} aria-disabled={!canInspectOutput}
            aria-label={`在位置图查看 ${point.candidateId} 备选`} aria-pressed={selected}
            onClick={() => selectOutput(point.candidateId)} onKeyDown={(event) => handleOutputKeyDown(event, point.candidateId)}>
            <circle className="ht-s4-map-hit" r="4" /><rect className="ht-s4-map-backup" x="-1.15" y="-1.15" width="2.3" height="2.3" rx="0.15" />
            {selected ? <circle className="ht-s4-map-selection" r="2.7" /> : null}
            <title>{point.candidateId} · Top-k 备选 · 仅供比较，不替换输出</title>
          </g>;
        }) : null}
        {currentBestId ? (
          space.candidatePoints
            .filter((point) => point.candidateId === currentBestId)
            .map((point) => {
              const projectedPoint = projectSpacePoint(point);
              return (
                <g key={`best-${point.candidateId}`} className={cn("ht-current-best-marker", isOutputPlot && "ht-s4-map-point")} data-candidate-id={point.candidateId} transform={`translate(${projectedPoint.x} ${projectedPoint.y})`}
                  role={isOutputPlot ? "button" : undefined} tabIndex={isOutputPlot ? canInspectOutput ? 0 : -1 : undefined}
                  aria-disabled={isOutputPlot ? !canInspectOutput : undefined} aria-pressed={isOutputPlot ? outputPreview!.selectedId === point.candidateId : undefined}
                  aria-label={isOutputPlot ? `在位置图查看 ${point.candidateId} 固定输出` : undefined}
                  onClick={isOutputPlot ? () => selectOutput(point.candidateId) : undefined}
                  onKeyDown={isOutputPlot ? (event) => handleOutputKeyDown(event, point.candidateId) : undefined}>
                  {isOutputPlot ? <><title>{point.candidateId} · S4 固定输出 · 不随备选选择改变</title><circle className="ht-s4-map-hit" r="4" /></> : null}
                  {iterationSummary?.recordBest ? <title>{point.candidateId} · 回流记录最优 · {iterationSummary.recordBest.source} · {formatIterationValue(target, iterationSummary.recordBest.value)} {target.unit === "degC" ? "°C" : target.unit}</title> : null}
                  <circle r="1.65" />
                  <path d="M 0 -2.2 L 0.56 -0.64 L 2.15 -0.64 L 0.86 0.28 L 1.34 1.86 L 0 0.9 L -1.34 1.86 L -0.86 0.28 L -2.15 -0.64 L -0.56 -0.64 Z" />
                  {isOutputPlot && outputPreview!.selectedId === point.candidateId ? <circle className="ht-s4-map-selection" r="2.7" /> : null}
                </g>
              );
            })
        ) : null}
      </svg>

      {showSummary ? (
        <>
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
        </>
      ) : null}
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
