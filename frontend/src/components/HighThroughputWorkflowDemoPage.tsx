import {
  BadgeInfo,
  Bot,
  BrainCircuit,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  FlaskConical,
  Layers3,
  Pause,
  Play,
  RotateCcw,
  SlidersHorizontal,
  Target,
  TestTube2,
} from "lucide-react";
import { type CSSProperties, type ReactNode, useEffect, useState } from "react";
import {
  highThroughputDemoScenario,
  type HighThroughputCandidate,
  type HighThroughputTarget,
  type HighThroughputTargetKey,
} from "../constants/highThroughputDemoScenario";
import { cn } from "../lib/utils";
import "./HighThroughputWorkflowDemoPage.css";

type HighThroughputWorkflowDemoPageProps = {
  onBackHome: () => void;
};

type WeightState = Record<HighThroughputTargetKey, number>;

const PLAY_INTERVAL_MS = 2600;
const MAX_RENDERED_STAGE_DOTS = 2400;
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
  tg: { x: 30, y: 15 },
  cte: { x: 30, y: 33 },
  elongation: { x: 70, y: 15 },
  modulus: { x: 70, y: 33 },
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

function getTarget(targetKey: HighThroughputTargetKey) {
  return highThroughputDemoScenario.targets.find((target) => target.key === targetKey) ?? highThroughputDemoScenario.targets[0];
}

function getCandidate(candidateId: string) {
  return highThroughputDemoScenario.candidates.find((candidate) => candidate.id === candidateId);
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

function weightedAchievement(weights: WeightState) {
  const formulation = highThroughputDemoScenario.formulation;
  const totalWeight = Object.values(weights).reduce((total, value) => total + value, 0) || 1;
  const score = highThroughputDemoScenario.targets.reduce(
    (total, target) => total + formulation.achievement[target.key] * weights[target.key],
    0,
  );
  return Math.round(score / totalWeight);
}

export function HighThroughputWorkflowDemoPage(_props: HighThroughputWorkflowDemoPageProps) {
  const scenario = highThroughputDemoScenario;
  const [currentStageIndex, setCurrentStageIndex] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [weights, setWeights] = useState<WeightState>(() => buildInitialWeights());
  const currentStage = scenario.stages[currentStageIndex];
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
        return index + 1;
      });
    }, PLAY_INTERVAL_MS);

    return () => window.clearInterval(timer);
  }, [isPlaying, maxStageIndex]);

  function goToStage(index: number) {
    setCurrentStageIndex(clamp(index, 0, maxStageIndex));
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
    setIsPlaying(false);
  }

  return (
    <div className="high-throughput-demo">
      <main className="ht-shell">
        <section className="ht-docx-board">
          <ScenarioHeader onConfirmSetup={() => goToStage(1)} isConfirmed={currentStageIndex > 0} />

          <FlowControlBar
            currentStageIndex={currentStageIndex}
            isPlaying={isPlaying}
            onTogglePlayback={togglePlayback}
            onPrevious={() => goToStage(currentStageIndex - 1)}
            onNext={() => goToStage(currentStageIndex + 1)}
            onReset={resetDemo}
            onSelectStage={goToStage}
          />

          <div className="ht-docx-map-stage">
            <AgentOrbitPanel stageIndex={currentStageIndex} side="left" />

            <div className="ht-map-column">
              <MaterialMap
                stageIndex={currentStageIndex}
              />
            </div>

            <AgentOrbitPanel stageIndex={currentStageIndex} side="right" />

            <NarrationPanel stageIndex={currentStageIndex} />
          </div>

          <FormulationPanel stageIndex={currentStageIndex} weights={weights} onWeightsChange={setWeights} computedScore={computedScore} />
        </section>
      </main>
    </div>
  );
}

function ScenarioHeader({
  onConfirmSetup,
  isConfirmed,
}: {
  onConfirmSetup: () => void;
  isConfirmed: boolean;
}) {
  const scenario = highThroughputDemoScenario;
  const [materialType, setMaterialType] = useState(scenario.materialType);
  const [monomerSystem, setMonomerSystem] = useState("Diamine + Dianhydride");
  const [representation, setRepresentation] = useState("PolyBERT");
  const [candidateA, setCandidateA] = useState(String(scenario.monomerACount));
  const [candidateB, setCandidateB] = useState(String(scenario.monomerBCount));
  const [selectedTargetKeys, setSelectedTargetKeys] = useState<HighThroughputTargetKey[]>(
    scenario.targets.map((target) => target.key),
  );
  const [focusedTargetKey, setFocusedTargetKey] = useState<HighThroughputTargetKey>("tg");
  const [targetValues, setTargetValues] = useState<Record<HighThroughputTargetKey, string>>({
    tg: "250",
    cte: "35",
    elongation: "15",
    modulus: "3.0",
  });
  const selectedTargets = scenario.targets.filter((target) => selectedTargetKeys.includes(target.key));
  const candidateTotalPreview = Math.max(0, Math.round(Number(candidateA) || 0)) * Math.max(0, Math.round(Number(candidateB) || 0));

  function handleTargetClick(targetKey: HighThroughputTargetKey) {
    setFocusedTargetKey(targetKey);
    setSelectedTargetKeys((current) => {
      if (current.includes(targetKey)) {
        return current.filter((key) => key !== targetKey);
      }
      return [...current, targetKey];
    });
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
        <button type="button" className="ht-confirm-setup-button" onClick={onConfirmSetup}>
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

function NarrationPanel({ stageIndex }: { stageIndex: number }) {
  const stage = highThroughputDemoScenario.stages[stageIndex];
  const progress = Math.round((stageIndex / (highThroughputDemoScenario.stages.length - 1)) * 100);

  return (
    <section className="ht-narration-panel">
      <div>
        <span className="ht-kicker">当前阶段说明</span>
        <h2>{stage.title}</h2>
        <p>{stage.body}</p>
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

function MaterialMap({
  stageIndex,
}: {
  stageIndex: number;
}) {
  const scenario = highThroughputDemoScenario;
  const visibleCandidates = stageIndex === 0 ? [] : scenario.candidates.slice(0, MAX_RENDERED_STAGE_DOTS);
  const generatedCount = visibleCandidates.length;
  const generatedComplete = generatedCount === scenario.candidateTotal;
  const showMaterialTerrain = stageIndex >= 2;
  const showOptimizationMarkers = stageIndex >= 3;
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
  const batchIndex = clamp(stageIndex - 3, 0, scenario.batches.length - 1);
  const visibleBatches = showOptimizationMarkers ? scenario.batches.slice(0, batchIndex + 1) : [];
  const testedIds = new Set(visibleBatches.flatMap((batch) => batch.testedIds));
  const recommendedIds = new Set(showOptimizationMarkers ? scenario.batches[batchIndex]?.recommendedIds ?? [] : []);

  return (
    <section className="ht-material-map-panel">
      <div className="ht-panel-header">
        <div>
          <span className="ht-kicker">{showMaterialTerrain ? "2D PolyBERT Material Map" : "2D PolyBERT Material Space"}</span>
          <h2>{mapTitle}</h2>
        </div>
        <div className="ht-space-status-group">
          <span className="ht-unified-space-badge">40 x 60 candidates</span>
          {showMaterialTerrain ? (
            <span className="ht-map-layer-badge">Map layer: mock property terrain</span>
          ) : (
            <span className={cn("ht-generation-status", generatedComplete ? "complete" : "pending")}>
              已生成 {formatNumber(generatedCount)} / {formatNumber(scenario.candidateTotal)}
            </span>
          )}
        </div>
      </div>

      <div className="ht-map-canvas">
        <svg viewBox={`0 0 ${MATERIAL_MAP_WIDTH} ${MATERIAL_MAP_HEIGHT}`} role="img" aria-label="PolyBERT candidate space point cloud">
          <defs>
            <filter id="ht-terrain-blur" x="-20%" y="-20%" width="140%" height="140%">
              <feGaussianBlur stdDeviation="1.65" />
            </filter>
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
          {showOptimizationMarkers ? (
            <g className="ht-sample-marker-layer" aria-label="mock tested and recommended samples">
              {Array.from(testedIds).map((candidateId) => {
                const point = projectedCandidateMap.get(candidateId);
                if (!point) {
                  return null;
                }
                return <circle key={`tested-${candidateId}`} className="ht-tested-sample" cx={point.x} cy={point.y} r="0.88" />;
              })}
              {Array.from(recommendedIds).map((candidateId) => {
                const point = projectedCandidateMap.get(candidateId);
                if (!point) {
                  return null;
                }
                return <circle key={`recommended-${candidateId}`} className="ht-recommended-sample" cx={point.x} cy={point.y} r="1.1" />;
              })}
            </g>
          ) : null}
        </svg>
      </div>
    </section>
  );
}

function AgentOrbitPanel({ stageIndex, side }: { stageIndex: number; side: "left" | "right" }) {
  const agents = highThroughputDemoScenario.agents;
  const visibleAgents = side === "left" ? agents.slice(0, 2) : agents.slice(2);

  return (
    <aside className={cn("ht-agent-orbit", side)}>
      {visibleAgents.map((agent, index) => (
        <AgentOptimizationCard key={agent.id} agent={agent} stageIndex={stageIndex} displayIndex={side === "left" ? index + 1 : index + 3} />
      ))}
    </aside>
  );
}

function AgentOptimizationCard({
  agent,
  stageIndex,
  displayIndex,
}: {
  agent: (typeof highThroughputDemoScenario.agents)[number];
  stageIndex: number;
  displayIndex: number;
}) {
  const target = getTarget(agent.targetKey);
  const candidate = getCandidate(agent.currentBestId);
  const progress = agent.progressByStage[stageIndex] ?? 0;
  const displayColor = AGENT_DISPLAY_COLORS[Math.min(displayIndex - 1, AGENT_DISPLAY_COLORS.length - 1)];
  const actionLabel = AGENT_ACTION_LABELS[Math.min(displayIndex - 1, AGENT_ACTION_LABELS.length - 1)];
  const agentTitle = `${target.shortLabel} Agent`;
  const directionLabel = `${target.direction === "higher" ? "最大化" : "最小化"} ${target.shortLabel}`;
  const thresholdLabel = `${target.direction === "higher" ? "≥" : "≤"} ${target.target}${target.unit}`;
  const candidateStatus =
    stageIndex === 0
      ? "等待任务设置"
      : stageIndex === 1
        ? "已接收 2,400 候选"
        : stageIndex === 2
          ? "定位目标热点"
          : stageIndex < 4
            ? "候选更新中"
            : agent.topCandidateIds.join(" / ");
  const actionStatus = stageIndex === 0 ? "等待确认" : stageIndex === 1 ? "等待材料地图" : stageIndex === 2 ? "读取材料地图" : actionLabel;
  const explanationBadge = stageIndex === 0 ? "待配置" : stageIndex === 1 ? "候选空间" : target.shortLabel;
  const explanationText =
    stageIndex === 0
      ? "等待任务设置确认。"
      : stageIndex === 1
        ? "候选空间已生成，等待地形层。"
        : target.description;
  const explanationMeta =
    stageIndex === 0
      ? ["输入未确认", "未启动优化"]
      : stageIndex === 1
        ? ["候选空间已生成", "等待地形层"]
        : [directionLabel, `目标 ${thresholdLabel}`];

  return (
    <article className="ht-agent-card" style={{ "--target-color": displayColor } as CSSProperties}>
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
        <strong>{agent.statusByStage[stageIndex]}</strong>
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

function AgentPanel({ stageIndex }: { stageIndex: number }) {
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
          return <AgentOptimizationCard key={agent.id} agent={agent} stageIndex={stageIndex} displayIndex={index + 1} />;
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
}: {
  stageIndex: number;
  weights: WeightState;
  onWeightsChange: (weights: WeightState) => void;
  computedScore: number;
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
              return (
                <article key={component.id} className={cn("ht-component-row", stageIndex >= 4 && "active")} style={{ "--target-color": component.color } as CSSProperties}>
                  <span>{component.id}</span>
                  <div>
                    <strong>{component.label}</strong>
                    <em>{component.candidateId} / {target.shortLabel}</em>
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
            {scenario.targets.map((target) => (
              <label key={target.key} style={{ "--target-color": target.color } as CSSProperties}>
                <span>
                  {target.shortLabel}
                  <b>{weights[target.key]}</b>
                </span>
                <input
                  type="range"
                  min="0"
                  max="50"
                  step="5"
                  value={weights[target.key]}
                  onChange={(event) => updateWeight(target.key, Number(event.currentTarget.value))}
                />
              </label>
            ))}
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
