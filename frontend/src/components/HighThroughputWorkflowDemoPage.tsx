import {
  BadgeInfo,
  Bot,
  BrainCircuit,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  FileCheck2,
  FlaskConical,
  Layers3,
  Pause,
  Play,
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
  fileName: string;
  fileType: string;
  sampleCount: number;
  fieldCount: number;
  uploadedAt: string;
};

const PLAY_INTERVAL_MS = 2600;
const MAX_RENDERED_STAGE_DOTS = 2400;
const MAX_RENDERED_PROPERTY_DOTS = 2200;
const MATERIAL_MAP_WIDTH = 100;
const MATERIAL_MAP_HEIGHT = 48;
const AGENT_DISPLAY_COLORS = ["#2563eb", "#16a34a", "#7c3aed", "#f97316"] as const;
const AGENT_ACTION_LABELS = ["下一轮 3 个样本", "追加 3 个验证点", "筛选候选 Top-k", "更新局部推荐"] as const;
const TARGET_HEATMAP_COLORS: Record<HighThroughputTargetKey, { center: string; mid: string; edge: string }> = {
  tg: { center: "#1d4ed8", mid: "#93c5fd", edge: "#dbeafe" },
  cte: { center: "#15803d", mid: "#86efac", edge: "#dcfce7" },
  elongation: { center: "#6d28d9", mid: "#c4b5fd", edge: "#ede9fe" },
  modulus: { center: "#ea580c", mid: "#fdba74", edge: "#ffedd5" },
};
const TARGET_HEATMAP_ANCHORS: Record<HighThroughputTargetKey, { x: number; y: number }> = {
  tg: { x: 77, y: 21 },
  cte: { x: 22, y: 26 },
  elongation: { x: 35, y: 41 },
  modulus: { x: 81, y: 42 },
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

function getAgentForCandidate(candidateId: string) {
  return highThroughputDemoScenario.agents.find(
    (agent) =>
      agent.currentBestId === candidateId ||
      agent.topCandidateIds.includes(candidateId) ||
      agent.explorationCandidateIds.includes(candidateId),
  );
}

function buildSvgPath(points: Array<{ x: number; y: number }>) {
  return points.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`).join(" ");
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

function propertyPotential(
  candidate: HighThroughputCandidate,
  target: HighThroughputTarget,
  range: { min: number; max: number },
) {
  const span = range.max - range.min || 1;
  const value = candidate.scores[target.key];
  const normalized = (value - range.min) / span;
  return target.direction === "lower" ? 1 - normalized : normalized;
}

type ProjectedCandidate = {
  candidate: HighThroughputCandidate;
  index: number;
  point: { x: number; y: number };
};

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

function pointDistance(a: { x: number; y: number }, b: { x: number; y: number }) {
  const dx = a.x - b.x;
  const dy = a.y - b.y;
  return Math.sqrt(dx * dx + dy * dy);
}

function buildHeatmapBlobs(
  projectedCandidates: ProjectedCandidate[],
  target: HighThroughputTarget,
  range: { min: number; max: number },
) {
  const anchor = TARGET_HEATMAP_ANCHORS[target.key];
  const rankedCandidates = projectedCandidates
    .map((item) => ({
      ...item,
      potential: propertyPotential(item.candidate, target, range),
    }))
    .filter((item) => item.potential >= 0.58)
    .sort((a, b) => {
      const aAnchorScore = 1 / (1 + pointDistance(a.point, anchor) * 0.08);
      const bAnchorScore = 1 / (1 + pointDistance(b.point, anchor) * 0.08);
      return b.potential * 0.62 + bAnchorScore * 0.38 - (a.potential * 0.62 + aAnchorScore * 0.38);
    })
    .slice(0, 360);

  if (rankedCandidates.length === 0) {
    return [];
  }

  const centers: typeof rankedCandidates = [];
  for (const item of rankedCandidates) {
    if (centers.every((center) => pointDistance(center.point, item.point) > 12)) {
      centers.push(item);
    }
    if (centers.length >= 2) {
      break;
    }
  }

  const palette = TARGET_HEATMAP_COLORS[target.key];
  return centers.map((seed, blobIndex) => {
    const influenceRadius = 18 + blobIndex * 2;
    const neighbors = rankedCandidates.filter((item) => pointDistance(seed.point, item.point) <= influenceRadius);
    const weightedCenter = neighbors.reduce(
      (total, item) => {
        const distance = pointDistance(seed.point, item.point);
        const anchorDistance = pointDistance(item.point, anchor);
        const weight = (Math.pow(item.potential, 3) / (1 + distance * 0.12)) * (1 / (1 + anchorDistance * 0.04));
        return {
          x: total.x + item.point.x * weight,
          y: total.y + item.point.y * weight,
          weight: total.weight + weight,
        };
      },
      { x: 0, y: 0, weight: 0 },
    );
    const normalizedStrength =
      neighbors.reduce((total, item) => total + item.potential, 0) / Math.max(neighbors.length, 1);
    const dataCenter = {
      x: weightedCenter.weight > 0 ? weightedCenter.x / weightedCenter.weight : seed.point.x,
      y: weightedCenter.weight > 0 ? weightedCenter.y / weightedCenter.weight : seed.point.y,
    };
    const anchorWeight = blobIndex === 0 ? 0.82 : 0.68;
    const spread = 10.6 + Math.min(neighbors.length / 22, 5.4) - blobIndex * 1.4;

    return {
      blobId: `${target.key}-${blobIndex}`,
      target,
      x: anchor.x * anchorWeight + dataCenter.x * (1 - anchorWeight),
      y: anchor.y * anchorWeight + dataCenter.y * (1 - anchorWeight),
      rx: clamp(spread * 1.52, 13, 20),
      ry: clamp(spread * 0.96, 8.8, 14),
      opacity: clamp(0.5 + normalizedStrength * 0.16 - blobIndex * 0.1, 0.34, 0.62),
      ...palette,
    };
  });
}

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

function weightedAchievement(weights: WeightState) {
  const formulation = highThroughputDemoScenario.formulation;
  const totalWeight = Object.values(weights).reduce((total, value) => total + value, 0) || 1;
  const score = highThroughputDemoScenario.targets.reduce(
    (total, target) => total + formulation.achievement[target.key] * weights[target.key],
    0,
  );
  return Math.round(score / totalWeight);
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

function targetGapLabel(candidate: HighThroughputCandidate | undefined, target: HighThroughputTarget) {
  if (!candidate) {
    return "--";
  }
  const value = candidate.scores[target.key];
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
  const visibleRounds = stageIndex >= 3
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

function propertySpaceCurrentBestId(
  stageIndex: number,
  space: HighThroughputPropertySpace,
  target: HighThroughputTarget,
  iterationRoundIndex = 2,
) {
  if (stageIndex === 3) {
    const rounds = highThroughputDemoScenario.roundsByTarget[space.targetKey];
    const activeRound = rounds[clamp(iterationRoundIndex, 0, rounds.length - 1)] ?? rounds[rounds.length - 1];
    return activeRound.currentBestId;
  }
  if (stageIndex > 3) {
    return space.currentBestId;
  }
  if (stageIndex >= 2) {
    return bestCandidateId(space.priorCandidateIds, target);
  }
  return "";
}

export function HighThroughputWorkflowDemoPage(_props: HighThroughputWorkflowDemoPageProps) {
  const scenario = highThroughputDemoScenario;
  const [currentStageIndex, setCurrentStageIndex] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [weights, setWeights] = useState<WeightState>(() => buildInitialWeights());
  const [confirmedSetup, setConfirmedSetup] = useState<ConfirmedSetup>(() => buildDefaultConfirmedSetup());
  const [activeSpaceTargetKey, setActiveSpaceTargetKey] = useState<HighThroughputTargetKey>("tg");
  const [activeIterationRoundIndex, setActiveIterationRoundIndex] = useState(0);
  const [priorDataUpload, setPriorDataUpload] = useState<PriorDataUploadState | null>(null);
  const mapStageRef = useRef<HTMLDivElement | null>(null);
  const maxStageIndex = scenario.stages.length - 1;
  const computedScore = weightedAchievement(weights);

  useEffect(() => {
    if (!isPlaying) {
      return;
    }

    const timer = window.setInterval(() => {
      setCurrentStageIndex((index) => {
        if (index >= maxStageIndex) {
          window.clearInterval(timer);
          setIsPlaying(false);
          return index;
        }
        const nextIndex = index + 1;
        if (nextIndex === 3) {
          setActiveIterationRoundIndex(0);
        }
        return nextIndex;
      });
    }, PLAY_INTERVAL_MS);

    return () => window.clearInterval(timer);
  }, [isPlaying, maxStageIndex]);

  function goToStage(index: number) {
    const nextIndex = clamp(index, 0, maxStageIndex);
    setCurrentStageIndex(nextIndex);
    if (nextIndex === 3) {
      setActiveIterationRoundIndex(0);
    }
    setIsPlaying(false);
  }

  function togglePlayback() {
    if (isPlaying) {
      setIsPlaying(false);
      return;
    }
    if (currentStageIndex >= maxStageIndex) {
      setCurrentStageIndex(0);
    }
    setIsPlaying(true);
  }

  function resetDemo() {
    setCurrentStageIndex(0);
    setWeights(buildInitialWeights());
    setConfirmedSetup(buildDefaultConfirmedSetup());
    setActiveSpaceTargetKey("tg");
    setActiveIterationRoundIndex(0);
    setPriorDataUpload(null);
    setIsPlaying(false);
  }

  function confirmSetup(setup: ConfirmedSetup) {
    setConfirmedSetup(setup);
    goToStage(1);
  }

  function handlePriorDataUpload(file: File | null) {
    if (!file) {
      return;
    }

    setPriorDataUpload({
      fileName: file.name,
      fileType: file.name.split(".").pop()?.toUpperCase() || "DATA",
      sampleCount: scenario.orthogonalPrior.candidateIds.length,
      fieldCount: scenario.targets.length + 1,
      uploadedAt: new Intl.DateTimeFormat("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }).format(new Date()),
    });
  }

  return (
    <div className="high-throughput-demo">
      <main className="ht-shell">
        <section className="ht-docx-board">
          <ScenarioHeader confirmedSetup={confirmedSetup} onConfirmSetup={confirmSetup} isConfirmed={currentStageIndex > 0} />

          <FlowControlBar
            currentStageIndex={currentStageIndex}
            isPlaying={isPlaying}
            onTogglePlayback={togglePlayback}
            onPrevious={() => goToStage(currentStageIndex - 1)}
            onNext={() => goToStage(currentStageIndex + 1)}
            onReset={resetDemo}
            onSelectStage={goToStage}
          />

          <div className="ht-docx-map-stage" ref={mapStageRef}>
            <AgentOrbitPanel
              stageIndex={currentStageIndex}
              side="left"
              confirmedSetup={confirmedSetup}
              activeTargetKey={activeSpaceTargetKey}
              onSelectTarget={setActiveSpaceTargetKey}
              iterationRoundIndex={activeIterationRoundIndex}
              priorDataUpload={priorDataUpload}
              onPriorDataUpload={handlePriorDataUpload}
            />

            <div className="ht-map-column">
              {currentStageIndex <= 3 ? (
                <PropertySpaceBoard
                  stageIndex={currentStageIndex}
                  confirmedSetup={confirmedSetup}
                  activeTargetKey={activeSpaceTargetKey}
                  onActiveTargetChange={setActiveSpaceTargetKey}
                  iterationRoundIndex={activeIterationRoundIndex}
                  onIterationRoundChange={setActiveIterationRoundIndex}
                />
              ) : (
                <MaterialMap stageIndex={currentStageIndex} confirmedSetup={confirmedSetup} />
              )}
            </div>

            <AgentOrbitPanel
              stageIndex={currentStageIndex}
              side="right"
              confirmedSetup={confirmedSetup}
              activeTargetKey={activeSpaceTargetKey}
              onSelectTarget={setActiveSpaceTargetKey}
              iterationRoundIndex={activeIterationRoundIndex}
              priorDataUpload={priorDataUpload}
              onPriorDataUpload={handlePriorDataUpload}
            />

            <NarrationPanel stageIndex={currentStageIndex} confirmedSetup={confirmedSetup} />

            <AgentAttentionOverlay stageIndex={currentStageIndex} stageRef={mapStageRef} />
          </div>

          {currentStageIndex <= 3 ? (
            <ExperimentPriorPanel
              stageIndex={currentStageIndex}
              priorDataUpload={priorDataUpload}
              iterationRoundIndex={activeIterationRoundIndex}
            />
          ) : (
            <FormulationPanel
              stageIndex={currentStageIndex}
              weights={weights}
              onWeightsChange={setWeights}
              computedScore={computedScore}
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
}: {
  confirmedSetup: ConfirmedSetup;
  onConfirmSetup: (setup: ConfirmedSetup) => void;
  isConfirmed: boolean;
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
  }, [confirmedSetup]);

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
        <button type="button" className="ht-confirm-setup-button" onClick={handleConfirmSetup}>
          {isConfirmed ? <CheckCircle2 aria-hidden="true" size={17} /> : <ChevronRight aria-hidden="true" size={17} />}
          {isConfirmed ? "已确认，重新进入候选空间生成" : "确认任务设置，生成候选空间"}
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
  isPlaying,
  onTogglePlayback,
  onPrevious,
  onNext,
  onReset,
  onSelectStage,
}: {
  currentStageIndex: number;
  isPlaying: boolean;
  onTogglePlayback: () => void;
  onPrevious: () => void;
  onNext: () => void;
  onReset: () => void;
  onSelectStage: (index: number) => void;
}) {
  const stages = highThroughputDemoScenario.stages;
  const isFirst = currentStageIndex === 0;
  const isLast = currentStageIndex === stages.length - 1;

  return (
    <section className="ht-flow-control-bar" aria-label="演示播放控制">
      <div className="ht-flow-controls">
        <button type="button" className="ht-primary-control" onClick={onTogglePlayback}>
          {isPlaying ? <Pause aria-hidden="true" size={16} /> : <Play aria-hidden="true" size={16} />}
          {isPlaying ? "暂停" : "播放"}
        </button>
        <button type="button" className="ht-icon-button" onClick={onPrevious} disabled={isFirst} aria-label="上一步">
          <ChevronLeft aria-hidden="true" size={16} />
        </button>
        <button type="button" className="ht-icon-button" onClick={onNext} disabled={isLast} aria-label="下一步">
          <ChevronRight aria-hidden="true" size={16} />
        </button>
        <button type="button" className="ht-icon-button" onClick={onReset} aria-label="重置">
          <RotateCcw aria-hidden="true" size={16} />
        </button>
      </div>
      <nav className="ht-flow-steps" aria-label="S0 到 S6 演示阶段">
        {stages.map((stage, index) => (
          <button
            key={stage.id}
            type="button"
            aria-pressed={index === currentStageIndex}
            className={cn(index < currentStageIndex && "complete")}
            onClick={() => onSelectStage(index)}
          >
            <span>{stage.id}</span>
            <b>{stage.label}</b>
          </button>
        ))}
      </nav>
    </section>
  );
}

function StageStepper({
  currentStageIndex,
  isPlaying,
  onTogglePlayback,
  onPrevious,
  onNext,
  onReset,
  onSelectStage,
}: {
  currentStageIndex: number;
  isPlaying: boolean;
  onTogglePlayback: () => void;
  onPrevious: () => void;
  onNext: () => void;
  onReset: () => void;
  onSelectStage: (index: number) => void;
}) {
  const stages = highThroughputDemoScenario.stages;
  const isFirst = currentStageIndex === 0;
  const isLast = currentStageIndex === stages.length - 1;

  return (
    <aside className="ht-stepper-panel">
      <div className="ht-stepper-heading">
        <span className="ht-kicker">Guided demo</span>
        <h2>流程步骤</h2>
      </div>
      <div className="ht-play-controls">
        <button type="button" className="ht-primary-control" onClick={onTogglePlayback}>
          {isPlaying ? <Pause aria-hidden="true" size={16} /> : <Play aria-hidden="true" size={16} />}
          {isPlaying ? "暂停" : "播放"}
        </button>
        <button type="button" className="ht-icon-button" onClick={onPrevious} disabled={isFirst} aria-label="上一步">
          <ChevronLeft aria-hidden="true" size={16} />
        </button>
        <button type="button" className="ht-icon-button" onClick={onNext} disabled={isLast} aria-label="下一步">
          <ChevronRight aria-hidden="true" size={16} />
        </button>
        <button type="button" className="ht-icon-button" onClick={onReset} aria-label="重置">
          <RotateCcw aria-hidden="true" size={16} />
        </button>
      </div>
      <nav className="ht-stage-list" aria-label="高通量演示阶段">
        {stages.map((stage, index) => {
          const state = index < currentStageIndex ? "done" : index === currentStageIndex ? "active" : "pending";
          return (
            <button
              key={stage.id}
              type="button"
              className={cn("ht-stage-button", state)}
              aria-pressed={index === currentStageIndex}
              onClick={() => onSelectStage(index)}
            >
              <span className="ht-stage-index">{stage.id}</span>
              <span>
                <strong>{stage.label}</strong>
                <em>{stage.title}</em>
              </span>
              {state === "done" ? <CheckCircle2 aria-hidden="true" size={15} /> : null}
            </button>
          );
        })}
      </nav>
    </aside>
  );
}

function NarrationPanel({
  stageIndex,
  confirmedSetup,
}: {
  stageIndex: number;
  confirmedSetup: ConfirmedSetup;
}) {
  const stage = highThroughputDemoScenario.stages[stageIndex];
  const progress = Math.round((stageIndex / (highThroughputDemoScenario.stages.length - 1)) * 100);
  const body =
    stage.id === "S1"
      ? `${formatNumber(confirmedSetup.monomerACount)} 个二胺与 ${formatNumber(confirmedSetup.monomerBCount)} 个二酐形成 ${formatNumber(confirmedSetup.candidateTotal)} 个理论候选。当前阶段只展示统一候选空间点云：每个点代表一个二胺 x 二酐组合，尚未叠加性能地形、已测样本或 Agent 推荐。`
      : stage.body;

  return (
    <section className="ht-narration-panel">
      <div>
        <span className="ht-kicker">当前阶段说明</span>
        <h2>{stage.title}</h2>
        <p>{body}</p>
      </div>
      <div className="ht-stage-progress">
        <span>{stage.id}</span>
        <div className="ht-progress-track">
          <b style={{ width: `${progress}%` }} />
        </div>
        <strong>{progress}%</strong>
      </div>
      <div className="ht-callout">
        <BadgeInfo aria-hidden="true" size={16} />
        {stage.callout}
      </div>
    </section>
  );
}

function AgentAttentionOverlay({
  stageIndex,
  stageRef,
}: {
  stageIndex: number;
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
        const point = projectedCandidateMap.get(agent.currentBestId);
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
  }, [stageIndex, stageRef]);

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
  onIterationRoundChange,
}: {
  stageIndex: number;
  confirmedSetup: ConfirmedSetup;
  activeTargetKey: HighThroughputTargetKey;
  onActiveTargetChange: (targetKey: HighThroughputTargetKey) => void;
  iterationRoundIndex: number;
  onIterationRoundChange: (roundIndex: number) => void;
}) {
  const scenario = highThroughputDemoScenario;
  const activeTarget = getConfiguredTarget(activeTargetKey, confirmedSetup);
  const generatedComplete = stageIndex > 0;
  const activeRounds = scenario.roundsByTarget[activeTargetKey];
  const boardTitle =
    stageIndex === 0
      ? `${activeTarget.shortLabel} 单性质候选空间待启动`
      : stageIndex === 1
        ? `${activeTarget.shortLabel} 候选空间 + 正交实验样本`
        : stageIndex === 2
          ? `${activeTarget.shortLabel} 正交先验初始热点图`
          : `${activeTarget.shortLabel} 单性质空间迭代更新`;

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
          {stageIndex === 3 ? (
            <div className="ht-round-switcher" role="tablist" aria-label="切换 S3 迭代轮次">
              {activeRounds.map((round, index) => (
                <button
                  key={round.surfaceSnapshotId}
                  type="button"
                  role="tab"
                  aria-selected={iterationRoundIndex === index}
                  aria-pressed={iterationRoundIndex === index}
                  onClick={() => onIterationRoundChange(index)}
                >
                  {index < activeRounds.length - 1 ? `Round ${round.round}` : "Converged"}
                </button>
              ))}
            </div>
          ) : null}
        </div>
      </div>

      <div className="ht-property-space-grid single">
        <PropertySpaceCard
          key={activeTarget.key}
        stageIndex={stageIndex}
        target={activeTarget}
        variant="large"
        iterationRoundIndex={iterationRoundIndex}
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
}: {
  stageIndex: number;
  target: HighThroughputTarget;
  variant?: "compact" | "large";
  iterationRoundIndex?: number;
}) {
  const space = getPropertySpace(target.key);
  const surface = propertySurfaceForStage(stageIndex, space, iterationRoundIndex);
  const roundIds = propertyRoundIds(target.key, stageIndex, iterationRoundIndex);
  const priorIds = stageIndex >= 1 ? space.priorCandidateIds : [];
  const measuredPriorIds = stageIndex >= 2 ? space.priorCandidateIds : [];
  const currentBestId = propertySpaceCurrentBestId(stageIndex, space, target, iterationRoundIndex);
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
        ? "DOE selected"
        : stageIndex === 2
          ? "Prior DOE Surface"
          : surface?.label ?? "Round surface";

  return (
    <article className={cn("ht-property-space-card", variant)} style={{ "--target-color": target.color } as CSSProperties}>
      <div className="ht-property-space-head">
        <div>
          <span>{target.shortLabel} Space</span>
          <strong>{target.label}</strong>
        </div>
        <b>{stageLabel}</b>
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
            return (
              <circle
                key={`recommended-${point.candidateId}`}
                className="ht-recommended-sample"
                cx={projectedPoint.x}
                cy={projectedPoint.y}
                r="1.04"
              />
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
        <span>DOE <b>{space.priorCandidateIds.length}</b></span>
        <span>已测 <b>{measuredSet.size}</b></span>
        <span>推荐 <b>{recommendedSet.size}</b></span>
        <span>热点 <b>{surfaceLabel}</b></span>
      </div>
      <div className="ht-property-space-best">
        <span>当前最优</span>
        <strong>{currentBestId || "--"}</strong>
        <em>{candidateValue(currentBest, target)} / gap {targetGapLabel(currentBest, target)}</em>
      </div>
    </article>
  );
}

function ExperimentPriorPanel({
  stageIndex,
  priorDataUpload,
  iterationRoundIndex,
}: {
  stageIndex: number;
  priorDataUpload: PriorDataUploadState | null;
  iterationRoundIndex: number;
}) {
  const scenario = highThroughputDemoScenario;
  const prior = scenario.orthogonalPrior;
  const iterationActive = stageIndex >= 3;

  return (
    <section className="ht-prior-workflow-panel">
      <div className="ht-panel-header">
        <div>
          <span className="ht-kicker">DOE = Design of Experiments</span>
          <h2>正交实验先验与单性质迭代状态</h2>
        </div>
        <span className="ht-simulation-badge compact">Simulation Mode / DOE prior drives surfaces</span>
      </div>

      <div className="ht-prior-workflow-grid">
        <article className={cn("ht-prior-step-card", stageIndex >= 1 && "active")}>
          <span>S1</span>
          <strong>上传先验实验数据</strong>
          <p>DOE 是 Design of Experiments，这里指正交实验先验数据。S1 从 Agent 卡片右上角的“上传先验数据”按钮导入测量值，作为 S2 初始热点图的已测输入。</p>
          {priorDataUpload ? (
            <div className="ht-prior-upload-result">
              <strong>{priorDataUpload.fileName}</strong>
              <span>{priorDataUpload.fileType} / {priorDataUpload.sampleCount} samples / {priorDataUpload.fieldCount} fields / {priorDataUpload.uploadedAt}</span>
            </div>
          ) : (
            <b>{prior.candidateIds.length} 个正交样本已选定，等待从 Agent 卡片上传测量值</b>
          )}
        </article>
        <article className={cn("ht-prior-step-card", stageIndex >= 2 && "active")}>
          <span>S2</span>
          <strong>Prior DOE Surface</strong>
          <p>上传的四项先验测量值进入四个属性空间，形成每个单性质模型的初始热点图。</p>
          <b>{stageIndex >= 2 ? "initial surfaces ready" : priorDataUpload ? "prior data uploaded" : "waiting for prior data upload"}</b>
        </article>
        <article className={cn("ht-prior-step-card", iterationActive && "active")}>
          <span>S3</span>
          <strong>Agent rounds update surfaces</strong>
          <p>每个 Agent 只在自己的属性空间内推荐、回流和更新热点图，不做多目标配方折中。</p>
          <b>{iterationActive ? "3 rounds scripted" : "waiting for prior surface"}</b>
        </article>
      </div>

      <div className="ht-round-summary-grid">
        {scenario.targets.map((target) => {
          const rounds = scenario.roundsByTarget[target.key];
          const activeRound = rounds[clamp(iterationRoundIndex, 0, rounds.length - 1)] ?? rounds[rounds.length - 1];
          return (
            <article key={target.key} style={{ "--target-color": target.color } as CSSProperties}>
              <span>{target.shortLabel}</span>
              <strong>{iterationActive ? activeRound.currentBestId : "pending"}</strong>
              <p>{iterationActive ? activeRound.explanation : "等待正交先验输入后启动单性质 Agent。"}</p>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function MaterialMap({
  stageIndex,
  confirmedSetup,
}: {
  stageIndex: number;
  confirmedSetup: ConfirmedSetup;
}) {
  const scenario = highThroughputDemoScenario;
  const visibleCandidates = stageIndex === 0 ? [] : scenario.candidates.slice(0, MAX_RENDERED_STAGE_DOTS);
  const displayedGeneratedCount = stageIndex === 0 ? 0 : confirmedSetup.candidateTotal;
  const generatedComplete = displayedGeneratedCount === confirmedSetup.candidateTotal && stageIndex > 0;
  const showMaterialTerrain = stageIndex >= 2;
  const showOptimizationMarkers = stageIndex >= 3;
  const isIterationStage = stageIndex === 3;
  const mapTitle = showMaterialTerrain ? "四目标模拟性能地形" : "统一 a x b 候选空间";
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
  const projectedCandidates = visibleCandidates.map((candidate, index) => ({
    candidate,
    index,
    point: projectMaterialPoint(candidate, normalizedBounds),
  }));
  const targetRanges = new Map(
    scenario.targets.map((target) => {
      const values = visibleCandidates.map((candidate) => candidate.scores[target.key]);
      return [
        target.key,
        {
          min: Math.min(...values),
          max: Math.max(...values),
        },
      ];
    }),
  );
  const heatmapBlobs = showMaterialTerrain
    ? scenario.targets.flatMap((target) => {
        const range = targetRanges.get(target.key) ?? { min: 0, max: 1 };
        return buildHeatmapBlobs(projectedCandidates, target, range);
      })
    : [];
  const projectedCandidateMap = new Map(projectedCandidates.map((item) => [item.candidate.id, item.point]));
  const batchIndex = showOptimizationMarkers ? scenario.batches.length - 1 : clamp(stageIndex - 3, 0, scenario.batches.length - 1);
  const currentBatch = showOptimizationMarkers ? scenario.batches[batchIndex] : null;
  const visibleBatches = showOptimizationMarkers ? scenario.batches.slice(0, batchIndex + 1) : [];
  const testedIds = new Set(visibleBatches.flatMap((batch) => batch.testedIds));
  const currentTestedIds = new Set(isIterationStage ? currentBatch?.testedIds ?? [] : []);
  const recommendedIds = new Set(currentBatch?.recommendedIds ?? []);
  const targetForCandidate = (candidateId: string) => {
    const agent = getAgentForCandidate(candidateId);
    if (agent) {
      return getTarget(agent.targetKey);
    }

    const candidate = getCandidate(candidateId);
    if (!candidate) {
      return scenario.targets[0];
    }

    return scenario.targets.reduce(
      (best, target) => {
        const range = targetRanges.get(target.key) ?? { min: 0, max: 1 };
        const potential = propertyPotential(candidate, target, range);
        return potential > best.potential ? { target, potential } : best;
      },
      { target: scenario.targets[0], potential: Number.NEGATIVE_INFINITY },
    ).target;
  };
  return (
    <section className="ht-material-map-panel">
      <div className="ht-panel-header">
        <div>
          <span className="ht-kicker">{showMaterialTerrain ? "2D PolyBERT Material Map" : "2D PolyBERT Material Space"}</span>
          <h2>{mapTitle}</h2>
        </div>
        <div className="ht-space-status-group">
          <span className="ht-unified-space-badge">
            {formatNumber(confirmedSetup.monomerACount)} x {formatNumber(confirmedSetup.monomerBCount)} candidates
          </span>
          {showMaterialTerrain ? (
            <span className="ht-map-layer-badge">Map layer: mock property terrain</span>
          ) : (
            <span className={cn("ht-generation-status", generatedComplete ? "complete" : "pending")}>
              已生成 {formatNumber(displayedGeneratedCount)} / {formatNumber(confirmedSetup.candidateTotal)}
            </span>
          )}
        </div>
      </div>

      <div className="ht-map-canvas" data-ht-map-canvas="true">
        <svg viewBox={`0 0 ${MATERIAL_MAP_WIDTH} ${MATERIAL_MAP_HEIGHT}`} role="img" aria-label="PolyBERT candidate space point cloud">
          <defs>
            <filter id="ht-terrain-blur" x="-20%" y="-20%" width="140%" height="140%">
              <feGaussianBlur stdDeviation="1.65" />
            </filter>
            <marker id="ht-iteration-arrow" viewBox="0 0 6 6" refX="5" refY="3" markerWidth="3.2" markerHeight="3.2" orient="auto">
              <path d="M 1 1 L 5 3 L 1 5 Z" />
            </marker>
            {scenario.targets.map((target) => {
              const palette = TARGET_HEATMAP_COLORS[target.key];
              return (
                <radialGradient key={target.key} id={`ht-heatmap-gradient-${target.key}`} cx="50%" cy="50%" r="55%">
                  <stop offset="0%" stopColor={palette.center} stopOpacity="0.82" />
                  <stop offset="32%" stopColor={palette.center} stopOpacity="0.4" />
                  <stop offset="64%" stopColor={palette.mid} stopOpacity="0.24" />
                  <stop offset="84%" stopColor={palette.edge} stopOpacity="0.18" />
                  <stop offset="100%" stopColor={palette.edge} stopOpacity="0" />
                </radialGradient>
              );
            })}
          </defs>
          <rect x="0" y="0" width={MATERIAL_MAP_WIDTH} height={MATERIAL_MAP_HEIGHT} rx="3" fill="#fbfdff" />
          <g className="ht-material-grid" aria-hidden="true">
            {Array.from({ length: 7 }, (_, index) => (
              <line key={`x-${index}`} x1={(index + 1) * 12.5} y1="0" x2={(index + 1) * 12.5} y2={MATERIAL_MAP_HEIGHT} />
            ))}
            {Array.from({ length: 5 }, (_, index) => (
              <line key={`y-${index}`} x1="0" y1={(index + 1) * 8} x2={MATERIAL_MAP_WIDTH} y2={(index + 1) * 8} />
            ))}
          </g>
          {showMaterialTerrain ? (
            <g className="ht-heatmap-layers" aria-label="mock property heatmap layers">
              {heatmapBlobs.map(({ blobId, target, x, y, rx, ry, opacity }) => (
                <ellipse
                  key={blobId}
                  className="ht-heatmap-blob"
                  cx={x}
                  cy={y}
                  rx={rx}
                  ry={ry}
                  fill={`url(#ht-heatmap-gradient-${target.key})`}
                  opacity={opacity}
                />
              ))}
            </g>
          ) : null}
          {projectedCandidates.map(({ candidate, index, point }) => {
            const radius = index % 9 === 0 ? 0.42 : 0.31;
            const opacity = 0.36 + (index % 5) * 0.052;
            return (
              <circle key={candidate.id} cx={point.x} cy={point.y} r={radius} fill="#b9c5d6" opacity={opacity}>
                <title>
                  {candidate.id} / {candidate.monomerA} + {candidate.monomerB}
                </title>
              </circle>
            );
          })}
          {stageIndex === 3 ? (
            <g className="ht-iteration-loop" transform="translate(50 25)" aria-label="Agent iterative optimization loop">
              <path d="M -4.6 -1.15 A 4.75 4.75 0 0 1 4.6 -1.15" />
              <path d="M 4.6 1.15 A 4.75 4.75 0 0 1 -4.6 1.15" />
              <text className="ht-iteration-loop-title" x="0" y="0.2">Loop</text>
            </g>
          ) : null}
          {showOptimizationMarkers ? (
            <g className="ht-sample-marker-layer" aria-label="mock tested and recommended samples">
              {Array.from(testedIds).map((candidateId) => {
                const point = projectedCandidateMap.get(candidateId);
                if (!point) {
                  return null;
                }
                const target = targetForCandidate(candidateId);
                const isCurrentTested = currentTestedIds.has(candidateId);
                return (
                  <g key={`tested-${candidateId}`} style={{ "--target-color": target.color } as CSSProperties}>
                    <circle
                      className={cn("ht-tested-sample", isCurrentTested && "current")}
                      cx={point.x}
                      cy={point.y}
                      r={isCurrentTested ? "1.04" : "0.82"}
                    />
                  </g>
                );
              })}
              {Array.from(recommendedIds).map((candidateId) => {
                const point = projectedCandidateMap.get(candidateId);
                if (!point) {
                  return null;
                }
                const target = targetForCandidate(candidateId);
                return (
                  <circle
                    key={`recommended-${candidateId}`}
                    className={cn(isIterationStage ? "ht-recommended-sample" : "ht-output-sample")}
                    cx={point.x}
                    cy={point.y}
                    r={isIterationStage ? "1.12" : "1"}
                    style={{ "--target-color": target.color } as CSSProperties}
                  />
                );
              })}
            </g>
          ) : null}
          {isIterationStage ? (
            <g className="ht-sample-state-key" transform="translate(75.2 1)" aria-label="sample state legend">
              <circle className="history" cx="0" cy="0" r="0.66" />
              <text x="1.45" y="0.45">回流</text>
              <circle className="current-outline" cx="8.1" cy="0" r="1.24" />
              <circle className="current" cx="8.1" cy="0" r="0.9" />
              <text x="9.65" y="0.45">验证</text>
              <circle className="next" cx="16.2" cy="0" r="0.82" />
              <text x="17.85" y="0.45">推荐</text>
            </g>
          ) : null}
        </svg>
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
  priorDataUpload,
  onPriorDataUpload,
}: {
  stageIndex: number;
  side: "left" | "right";
  confirmedSetup: ConfirmedSetup;
  activeTargetKey?: HighThroughputTargetKey;
  onSelectTarget?: (targetKey: HighThroughputTargetKey) => void;
  iterationRoundIndex?: number;
  priorDataUpload?: PriorDataUploadState | null;
  onPriorDataUpload?: (file: File | null) => void;
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
          priorDataUpload={priorDataUpload}
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
  onPriorDataUpload?: (file: File | null) => void;
}) {
  const target = getConfiguredTarget(agent.targetKey, confirmedSetup);
  const space = getPropertySpace(agent.targetKey);
  const roundIds = propertyRoundIds(agent.targetKey, stageIndex, iterationRoundIndex);
  const rounds = highThroughputDemoScenario.roundsByTarget[agent.targetKey];
  const activeRound = stageIndex === 3
    ? rounds[clamp(iterationRoundIndex, 0, rounds.length - 1)] ?? rounds[rounds.length - 1]
    : rounds[rounds.length - 1];
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
        ? `DOE ${space.priorCandidateIds.length} / ${formatNumber(confirmedSetup.candidateTotal)}`
        : stageIndex === 2
          ? `先验最优 ${priorBestId}`
          : stageIndex < 4
            ? `${activeRound.currentBestId} / ${surface?.label ?? "Round surface"}`
            : `${space.currentBestId} + Top-k ${agent.topCandidateIds.length}`;
  const actionStatus =
    stageIndex === 0
      ? "等待确认"
      : stageIndex === 1
        ? "标记正交样本"
        : stageIndex === 2
          ? "DOE 回流→初始热点"
          : stageIndex === 3
            ? "推荐→回流→更新热点"
            : actionLabel;
  const explanationBadge =
    stageIndex === 0
      ? "待配置"
      : stageIndex === 1
        ? "DOE prior"
        : stageIndex === 2
          ? "Prior surface"
          : stageIndex === 3
            ? "Single-property"
            : target.shortLabel;
  const explanationText =
    stageIndex === 0
      ? "等待任务设置确认。"
      : stageIndex === 1
        ? "同一批正交实验样本投影到该属性空间，尚不生成热点图。"
        : stageIndex === 2
          ? `${target.shortLabel} 空间使用 DOE 测量值生成第一版热点图。`
        : stageIndex >= 3
          ? agent.recommendation
          : target.description;
  const explanationMeta =
    stageIndex === 0
      ? ["输入未确认", "未启动优化"]
      : stageIndex === 1
        ? [`DOE ${space.priorCandidateIds.length}`, "无热点图"]
        : stageIndex === 2
          ? [`已测 ${space.priorCandidateIds.length}`, surface?.label ?? "Prior DOE Surface"]
        : stageIndex === 3
          ? [`已测 ${space.priorCandidateIds.length + roundIds.testedIds.length}`, `推荐 ${roundIds.recommendedIds.length}`]
          : [directionLabel, `${space.currentBestId} from S3`];
  const canSwitchSpace = stageIndex <= 3 && Boolean(onSelectTarget);

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
    onPriorDataUpload?.(event.currentTarget.files?.[0] ?? null);
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
            className={cn("ht-agent-upload-button", priorDataUpload && "complete")}
            title={priorDataUpload ? priorDataUpload.fileName : "上传先验数据文件"}
            onClick={handleUploadClick}
          >
            <input
              type="file"
              accept=".csv,.xlsx,.xls,.json"
              onChange={handlePriorUploadChange}
            />
            {priorDataUpload ? <FileCheck2 aria-hidden="true" size={13} /> : <UploadCloud aria-hidden="true" size={13} />}
            <span>{priorDataUpload ? "已上传先验" : agent.statusByStage[stageIndex]}</span>
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
          <span>当前解释</span>
          <b>{explanationBadge}</b>
        </div>
        <p>{explanationText}</p>
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

function FormulationPanel({
  stageIndex,
  weights,
  onWeightsChange,
  computedScore,
  confirmedSetup,
}: {
  stageIndex: number;
  weights: WeightState;
  onWeightsChange: (weights: WeightState) => void;
  computedScore: number;
  confirmedSetup: ConfirmedSetup;
}) {
  const scenario = highThroughputDemoScenario;
  const formulation = scenario.formulation;
  const showFormulation = stageIndex >= 5;
  const pathPoints = showFormulation ? formulation.ratioPath : formulation.ratioPath.slice(0, 1);

  function updateWeight(targetKey: HighThroughputTargetKey, value: number) {
    onWeightsChange({ ...weights, [targetKey]: value });
  }

  return (
    <section className={cn("ht-formulation-panel", showFormulation && "active")}>
      <div className="ht-panel-header">
        <div>
          <span className="ht-kicker">Formulation Mixture Optimizer</span>
          <h2>配方混合优化演示</h2>
        </div>
        <span className="ht-simulation-badge compact">ratio step = 0.1 / mock path</span>
      </div>

      <div className="ht-formulation-grid">
        <div className="ht-component-pool">
          <SectionTitle icon={<Target aria-hidden="true" size={17} />} title="单目标候选池" />
          <div className="ht-component-list">
            {formulation.components.map((component) => {
              const target = getTarget(component.sourceTargetKey);
              const convergedCandidateId = getPropertySpace(component.sourceTargetKey).currentBestId;
              return (
                <article key={component.id} className={cn("ht-component-row", stageIndex >= 4 && "active")} style={{ "--target-color": component.color } as CSSProperties}>
                  <span>{component.id}</span>
                  <div>
                    <strong>{component.label}</strong>
                    <em>{convergedCandidateId} / {target.shortLabel} from S3 space</em>
                  </div>
                  <b>{component.description}</b>
                </article>
              );
            })}
          </div>
        </div>

        <div className="ht-simplex-panel">
          <SectionTitle icon={<Layers3 aria-hidden="true" size={17} />} title="比例空间搜索路径" />
          <svg viewBox="0 0 100 86" role="img" aria-label="mock formulation search path">
            <polygon points="50,6 8,80 92,80" fill="#f8fafc" stroke="#cbd5e1" strokeWidth="1.2" />
            <text x="50" y="4" textAnchor="middle">p1</text>
            <text x="6" y="85" textAnchor="middle">p2</text>
            <text x="94" y="85" textAnchor="middle">p4</text>
            <path d={buildSvgPath(pathPoints)} fill="none" stroke="#f97316" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.2" />
            {pathPoints.map((point, index) => (
              <circle key={point.id} cx={point.x} cy={point.y} r={index === pathPoints.length - 1 ? 3.3 : 2.1} fill={index === pathPoints.length - 1 ? "#f97316" : "#ffffff"} stroke="#f97316" strokeWidth="1.4">
                <title>{point.id} score {point.score}</title>
              </circle>
            ))}
          </svg>
          <div className="ht-ratio-bars">
            {formulation.components.map((component) => (
              <div key={component.id}>
                <span>{component.id}</span>
                <b style={{ width: `${(formulation.finalRatio[component.id] ?? 0) * 100}%`, background: component.color }} />
                <strong>{Math.round((formulation.finalRatio[component.id] ?? 0) * 100)}%</strong>
              </div>
            ))}
          </div>
        </div>

        <div className="ht-result-panel">
          <SectionTitle icon={<TestTube2 aria-hidden="true" size={17} />} title="目标达成与解释" />
          <div className="ht-score-box">
            <span>综合达成率</span>
            <strong>{showFormulation ? `${computedScore}%` : "--"}</strong>
            <em>权重调整只影响演示评分，不触发真实计算。</em>
          </div>
          <RadarChart />
          <p>{showFormulation ? formulation.rationale : "配方优化会在 S5 阶段展开，当前先展示单目标候选如何进入组分池。"}</p>
        </div>

        <div className="ht-weight-panel">
          <SectionTitle icon={<SlidersHorizontal aria-hidden="true" size={17} />} title="目标权重" />
          <div className="ht-weight-list">
            {scenario.targets.map((baseTarget) => {
              const target = getConfiguredTarget(baseTarget.key, confirmedSetup);
              return (
                <label key={target.key} style={{ "--target-color": target.color } as CSSProperties}>
                  <div className="ht-weight-label">
                    <span>
                      {target.shortLabel}
                      <em>{targetThresholdLabel(target)}</em>
                    </span>
                    <b>{weights[target.key]}</b>
                  </div>
                  <input
                    type="range"
                    min="0"
                    max="50"
                    step="5"
                    value={weights[target.key]}
                    onChange={(event) => updateWeight(target.key, Number(event.currentTarget.value))}
                  />
                </label>
              );
            })}
          </div>
        </div>
      </div>
    </section>
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

function RadarChart() {
  const scenario = highThroughputDemoScenario;
  const center = { x: 50, y: 50 };
  const radius = 34;
  const axes = scenario.targets.map((target, index) => {
    const angle = -Math.PI / 2 + (index / scenario.targets.length) * Math.PI * 2;
    const value = scenario.formulation.achievement[target.key] / 100;
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
    <svg className="ht-radar" viewBox="0 0 100 100" role="img" aria-label="target achievement radar">
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
